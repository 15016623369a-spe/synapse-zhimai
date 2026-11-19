"""聊天：SSE 流式回答（LLM 优先，失败 / 未配置时降级本地检索模式）。

v2.0 增强：
- 引用文档识别：问题提到知识库中的文档（标题/文件名/arXiv ID）时，走文档级全文回答，
  不再误判 gap、不盲目联网；
- 学术 gap：调用 arXiv / OpenAlex / 学术白名单 Bing 工具检索，候选打分过滤后回传前端；
- SSE 事件：meta（含 reference）→ sources → [tool →] [gap] → [web_sources] → delta → done；
- MCP 工具（V2）：命中工具意图时先走 MCP 只读工具循环（tool 事件），
  工具结果作为外部上下文进入最终回答，``done`` 增加 ``mcp_results``；
  工具成功返回结果后不再发送 ``gap`` 事件（``done.gap`` 为 null），
  也不再触发联网补充搜索；工具失败时才回退到常规 gap / RAG 流程。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import db
from ..schemas import ChatIn
from ..services import (
    agent,
    context as context_service,
    doc_ref,
    extend as extend_service,
    llm,
    mcp as mcp_service,
    retrieval,
    search as search_service,
)
from ..utils import collapse_whitespace, make_snippet, new_id, now_iso, truncate

logger = logging.getLogger("synapse.chat")

router = APIRouter()

DELTA_CHUNK_CHARS = 24
DELTA_DELAY_SECONDS = 0.012
MAX_MESSAGE_CHARS = 4000
#: 传给 LLM 的历史条数 / assistant 内容截断（少而准，避免上下文膨胀）
HISTORY_LIMIT = 4
HISTORY_ASSISTANT_MAX_CHARS = 700
#: 追问继承文档时 doc-scoped 检索的 chunk 数（不注入整篇正文）
DOC_SCOPED_TOP_K = 8
#: 回答实际使用的片段上限 / 相关性下限（相对 Top-1）
ANSWER_SOURCE_LIMIT = 4
RELEVANCE_FLOOR = 0.40
RELEVANCE_RATIO = 0.45
#: 模型没标引用时的兜底展示条数
UNVERIFIED_CITATION_LIMIT = 3

_CITATION_MARK = re.compile(r"\[(\d{1,2})\]")


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _conversation_title(message: str) -> str:
    title = collapse_whitespace(message)[:24].strip()
    return title or "新对话"


def _ensure_conversation(conn, conversation_id: str | None, message: str) -> str:
    now = now_iso()
    if conversation_id:
        row = db.fetch_one(
            conn, "SELECT id FROM conversations WHERE id = ?", (conversation_id,)
        )
        if row is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
        )
        return conversation_id
    new_conversation_id = new_id("conv")
    conn.execute(
        "INSERT INTO conversations(id, title, created_at, updated_at) VALUES(?, ?, ?, ?)",
        (new_conversation_id, _conversation_title(message), now, now),
    )
    return new_conversation_id


def _recent_history(conn, conversation_id: str) -> list[dict[str, str]]:
    rows = db.fetch_all(
        conn,
        "SELECT role, content FROM messages WHERE conversation_id = ? "
        "ORDER BY created_at ASC, rowid ASC",
        (conversation_id,),
    )
    history: list[dict[str, str]] = []
    for row in rows:
        role = str(row["role"])
        if role not in {"user", "assistant"}:
            continue
        content = str(row["content"])
        if role == "assistant":
            content = truncate(content, HISTORY_ASSISTANT_MAX_CHARS, suffix="…")
        history.append({"role": role, "content": content})
    return history[-HISTORY_LIMIT:]


def _insert_message(
    conn,
    conversation_id: str,
    role: str,
    content: str,
    citations: list[dict[str, object]] | None = None,
) -> str:
    message_id = new_id("m")
    now = now_iso()
    conn.execute(
        "INSERT INTO messages(id, conversation_id, role, content, citations, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (
            message_id,
            conversation_id,
            role,
            content,
            json.dumps(citations or [], ensure_ascii=False),
            now,
        ),
    )
    conn.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
    )
    return message_id


def _save_assistant(
    conversation_id: str, content: str, citations: list[dict[str, object]]
) -> str | None:
    try:
        with db.get_db() as conn:
            return _insert_message(conn, conversation_id, "assistant", content, citations)
    except Exception:  # 客户端断开时尽量保存，但不能影响流结束
        logger.exception("保存助手消息失败：%s", conversation_id)
        return None


def _conversation_title_of(conversation_id: str) -> str:
    with db.get_db() as conn:
        row = db.fetch_one(
            conn, "SELECT title FROM conversations WHERE id = ?", (conversation_id,)
        )
    return str(row["title"]) if row is not None else "新对话"


def _search(message: str, top_k: int, doc_ids: list[str] | None) -> list[dict[str, object]]:
    with db.get_db() as conn:
        return retrieval.search_chunks(conn, message, top_k, doc_ids)


def _search_doc_scoped(
    query: str, doc_id: str, top_k: int = DOC_SCOPED_TOP_K
) -> list[dict[str, object]]:
    """只看单篇文档的 top_k 片段（追问继承文档时用，避免注入整篇正文）。"""
    with db.get_db() as conn:
        return retrieval.search_chunks(
            conn, query, top_k, [doc_id], max_per_doc=top_k
        )


def _load_reference_doc(
    message: str, reference_doc_id: str | None
) -> dict[str, object] | None:
    """识别问题里引用的知识库文档（显式 reference_doc_id 优先）。"""
    with db.get_db() as conn:
        if reference_doc_id:
            row = db.fetch_one(
                conn,
                "SELECT id, title, filename, source_type, content, size "
                "FROM documents WHERE id = ?",
                (reference_doc_id,),
            )
            if row is not None:
                ids = doc_ref.extract_ids(f"{row['title']} {row['filename'] or ''}")
                return {
                    "doc_id": str(row["id"]),
                    "title": str(row["title"]),
                    "filename": row["filename"],
                    "source_type": str(row["source_type"]),
                    "content": str(row["content"] or ""),
                    "size": int(row["size"] or 0),
                    "chunk_count": 0,
                    "arxiv_id": ids["arxiv_ids"][0] if ids["arxiv_ids"] else None,
                    "doi": ids["dois"][0] if ids["dois"] else None,
                }
        return doc_ref.detect_referenced_document(conn, message)


def _document_context_result(
    reference_doc: dict[str, object], context_text: str
) -> dict[str, object]:
    doc_id = str(reference_doc["doc_id"])
    with db.get_db() as conn:
        row = db.fetch_one(
            conn,
            "SELECT id FROM chunks WHERE doc_id = ? ORDER BY idx LIMIT 1",
            (doc_id,),
        )
        if row is None:
            row = db.fetch_one(
                conn, "SELECT id FROM chunks WHERE doc_id = ? LIMIT 1", (doc_id,)
            )
    chunk_id = str(row["id"]) if row is not None else f"c_{doc_id}"
    return {
        "chunk_id": chunk_id,
        "doc_id": doc_id,
        "doc_title": str(reference_doc.get("title") or ""),
        "text": context_text,
        "snippet": make_snippet(context_text),
        # 整篇文档没有 chunk 级相似度，不要伪造 0.99；前端按 kind=document 展示。
        "score": None,
        "relevance": "document",
        "kind": "document",
        "source_type": str(reference_doc.get("source_type") or "pdf"),
    }


def _filter_answer_sources(
    results: list[dict[str, object]],
    limit: int = ANSWER_SOURCE_LIMIT,
) -> list[dict[str, object]]:
    """只保留真正能支撑回答的片段。

    ``score`` 是归一化融合分（Top-1≈0.98），所以用「相对 Top-1 比例 + 绝对下限」
    双重门槛，而不是拿它当概率。文档级全文引用不参与过滤。
    """
    if not results:
        return []
    numeric = [
        float(result["score"])
        for result in results
        if result.get("score") is not None
    ]
    top = max(numeric) if numeric else 0.0
    if top <= 0:
        return results[:limit]
    floor = max(RELEVANCE_FLOOR, top * RELEVANCE_RATIO)
    kept = [
        result
        for result in results
        if result.get("kind") == "document"
        or result.get("score") is None
        or float(result["score"]) >= floor
    ]
    return (kept or results[:1])[:limit]


def _prune_citations(
    answer: str, citations: list[dict[str, object]]
) -> tuple[list[dict[str, object]], list[int]]:
    """按回答里实际出现的 ``[n]`` 裁剪引用，并返回越界编号。

    - 文档级全文引用（kind=document）不参与：整篇内容本身就是来源；
    - 模型一个引用都没标时，保留前 3 条并打 ``unverified`` 标记，避免来源面板空掉；
    - 返回的 ``invalid`` 是回答里引用了不存在的编号（幻觉引用），用于日志/诊断。
    """
    if not citations:
        return [], []
    if any(citation.get("kind") == "document" for citation in citations):
        return citations, []

    used = {int(match) for match in _CITATION_MARK.findall(answer)}
    invalid = sorted(index for index in used if index < 1 or index > len(citations))
    cited = [citation for citation in citations if int(citation["index"]) in used]
    if cited:
        return cited, invalid
    fallback = [
        {**citation, "unverified": True}
        for citation in citations[:UNVERIFIED_CITATION_LIMIT]
    ]
    return fallback, invalid


async def _stream_text(text: str) -> AsyncIterator[str]:
    """把 extractive 文本切成小块，模拟打字机效果。"""
    for start in range(0, len(text), DELTA_CHUNK_CHARS):
        yield text[start : start + DELTA_CHUNK_CHARS]
        await asyncio.sleep(DELTA_DELAY_SECONDS)


@router.post("/chat")
async def chat(payload: ChatIn, request: Request) -> StreamingResponse:
    message = (payload.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")
    message = message[:MAX_MESSAGE_CHARS]
    doc_ids = [str(doc_id) for doc_id in payload.doc_ids] if payload.doc_ids else None

    with db.get_db() as conn:
        conversation_id = _ensure_conversation(conn, payload.conversation_id, message)
        history = _recent_history(conn, conversation_id)
        # 多轮上下文：在写入当前 user 消息前解析追问（继承上文引用文档 / 检索文本增强）
        contextual = context_service.resolve_conversation_context(
            conn, conversation_id, message
        )
        user_message_id = _insert_message(conn, conversation_id, "user", message)

    # ---- 引用文档识别（显式引用优先，其次追问继承上文文档）----
    reference_doc = _load_reference_doc(message, payload.reference_doc_id)
    inherited_reference = False
    context_used = False
    context_summary: str | None = None
    search_text = message
    if reference_doc is None and contextual is not None:
        inherited_doc = contextual.get("reference_doc")
        context_used = True
        context_summary = str(contextual.get("context_summary") or "") or None
        if inherited_doc is not None:
            reference_doc = inherited_doc  # type: ignore[assignment]
            inherited_reference = True
            logger.info(
                "多轮追问继承上文引用文档：%s", reference_doc.get("title")
            )
        else:
            search_text = str(contextual.get("search_text") or message)
            logger.info("多轮追问使用上下文检索文本：%s", search_text[:80])
    reference: dict[str, object] | None = None
    summary_intent = False
    suppress_gap = False
    doc_context_text = ""
    if reference_doc is not None:
        reference = {
            "doc_id": str(reference_doc["doc_id"]),
            "title": str(reference_doc.get("title") or ""),
            "filename": reference_doc.get("filename"),
            "source_type": str(reference_doc.get("source_type") or ""),
        }
        doc_id = str(reference_doc["doc_id"])
        if inherited_reference:
            # 追问继承文档：只有明确「总结/全文/核心内容」才用文档级大上下文
            summary_intent = any(
                keyword in message.lower() for keyword in doc_ref.SUMMARY_KEYWORDS
            )
        else:
            summary_intent = doc_ref.is_summary_intent(message, reference_doc)
        if inherited_reference and not summary_intent:
            # 紧凑追问：只做 doc-scoped 检索（top_k≈8），不注入整篇正文
            suppress_gap = (
                int(reference_doc.get("size") or 0)
                >= doc_ref.REFERENCE_CONTENT_MIN_CHARS
            )
            topic_terms = [
                str(term) for term in (contextual or {}).get("topic_terms") or []
            ]
            retrieval_query = " ".join([*topic_terms, message]).strip()
            results = _search_doc_scoped(
                retrieval_query or message, doc_id, DOC_SCOPED_TOP_K
            )
            if not results:
                results = _search_doc_scoped(message, doc_id, DOC_SCOPED_TOP_K)
        else:
            content = str(reference_doc.get("content") or "")
            if not content:
                with db.get_db() as conn:
                    loaded = context_service.load_document(conn, doc_id)
                content = str((loaded or {}).get("content") or "")
            suppress_gap = len(content) >= doc_ref.REFERENCE_CONTENT_MIN_CHARS
            if summary_intent:
                doc_context_text = doc_ref.build_document_context_text(content)
                results = [_document_context_result(reference_doc, doc_context_text)]
            else:
                results = _search(message, 12, [doc_id])
                if not results:
                    doc_context_text = doc_ref.build_document_context_text(content)
                    results = [_document_context_result(reference_doc, doc_context_text)]
    else:
        results = _search(search_text, payload.top_k, doc_ids)
    results = _filter_answer_sources(results)
    llm_config = llm.get_config()
    mode = "llm" if llm.is_configured(llm_config) else "extractive"
    gap = (
        None
        if suppress_gap
        else agent.detect_gap(search_text, results, reference_doc=reference_doc)
    )
    if gap and float(gap.get("coverage") or 0) <= 0.01:
        # 覆盖率为 0（通常是问题实体在语料里完全未登录）：不要把无关片段
        # 硬凑成「找到 X 段相关内容」，直接走「没找到」+ 缺口卡。
        results = []
    citations = retrieval.to_citations(results)

    def _extractive_answer(include_gap_hint: bool = True) -> str:
        if reference_doc is not None and summary_intent:
            return doc_ref.build_local_document_summary(
                str(reference_doc.get("title") or ""),
                str(reference_doc.get("content") or ""),
            )
        answer = llm.build_extractive_answer(message, results)
        if gap and include_gap_hint:
            answer += (
                "\n\n（当前知识库资料有限，可联网检索补充资料；加入前由你确认。）"
            )
        return answer

    def _extra_system() -> str | None:
        if reference_doc is None:
            return None
        if inherited_reference and not summary_intent:
            card = contextual or {}
            topic_terms = "、".join(str(term) for term in card.get("topic_terms") or [])
            previous_intent = str(card.get("previous_intent") or "")
            intent_note = f"（上文意图：{previous_intent}）" if previous_intent else ""
            return (
                f"上文：用户此前在讨论知识库文档《{reference_doc.get('title')}》{intent_note}。"
                f"本次只提供该文档的相关片段（主题词：{topic_terms}），请直接回答当前追问；"
                "不要声称知识库资料不足，不要联网，也不要重复粘贴大段原文。"
            )
        if summary_intent:
            return (
                f"用户明确引用了知识库文档《{reference_doc.get('title')}》"
                "（可能是 arXiv 论文）。请直接基于【资料】中的该文档内容做全面总结，"
                "覆盖背景、方法、实验、结论与贡献；不要声称知识库资料不足，不要联网。"
            )
        return (
            f"用户问题与知识库文档《{reference_doc.get('title')}》相关，"
            "请优先基于该文档内容回答。"
        )

    async def event_stream() -> AsyncIterator[str]:
        parts: list[str] = []
        saved = False
        final_mode = mode
        disconnected = False
        web_candidates: list[dict[str, object]] = []
        web_queries: list[str] = []
        web_provider = search_service.provider_name()
        web_intent = "general"
        mcp_results: list[dict[str, object]] = []
        mcp_used = False
        # 上下文/提示词实际长度诊断（不进入 prompt，供验收与前端调试）
        diagnostics: dict[str, object] = {
            "context_used": context_used,
            "history_messages": len(history),
            "history_chars": sum(
                len(str(item.get("content") or "")) for item in history
            ),
            "context_chars": int((contextual or {}).get("context_chars") or 0),
            "token_budget": int((contextual or {}).get("token_budget") or 0),
            "topic_terms": list((contextual or {}).get("topic_terms") or []),
            "doc_context_chars": len(doc_context_text),
            "retrieval_chunks": len(results),
            "prompt_chars": 0,
        }

        try:
            yield _sse(
                "meta",
                {
                    "conversation_id": conversation_id,
                    "user_message_id": user_message_id,
                    "mode": mode,
                    "reference": reference,
                    "context_used": context_used,
                    "context_summary": context_summary,
                },
            )
            yield _sse("sources", {"citations": citations})

            # ---- MCP 只读工具阶段（命中意图 + 已启用工具才触发）----
            # 放在 gap 事件之前：MCP 工具成功返回结果时不再提示「知识库资料有限」。
            if mode == "llm" and not suppress_gap and mcp_service.detect_tool_intent(message):
                mcp_outcome: dict[str, object] = {}
                async for mcp_event in agent.gather_mcp_sources(
                    message, llm_config, reference_doc=reference_doc
                ):
                    if str(mcp_event.get("event")) == "result":
                        mcp_outcome = dict(mcp_event.get("data") or {})
                    else:
                        yield _sse(
                            str(mcp_event["event"]), mcp_event["data"]  # type: ignore[arg-type]
                        )
                mcp_results = list(mcp_outcome.get("results") or [])
                mcp_used = any(bool(item.get("ok")) for item in mcp_results)

            if gap and not mcp_used:
                yield _sse("gap", gap)

            if (
                mode == "llm"
                and gap
                and search_service.is_enabled()
                and not mcp_used
            ):
                tool_result: dict[str, object] = {}
                async for tool_event in agent.gather_web_sources(
                    search_text, llm_config, reference_doc=reference_doc
                ):
                    if str(tool_event.get("event")) == "result":
                        tool_result = tool_event.get("data") or {}  # type: ignore[assignment]
                    else:
                        yield _sse(str(tool_event["event"]), tool_event["data"])  # type: ignore[arg-type]
                raw_candidates = list(tool_result.get("candidates") or [])
                web_queries = [str(query) for query in (tool_result.get("queries") or [])]
                web_provider = str(tool_result.get("provider") or web_provider)
                if raw_candidates:
                    with db.get_db() as conn:
                        web_candidates, web_intent = extend_service.prepare_tool_candidates(
                            conn, search_text, reference_doc, raw_candidates
                        )
                if web_candidates:
                    yield _sse(
                        "web_sources",
                        {
                            "provider": web_provider,
                            "intent": web_intent,
                            "queries": web_queries,
                            "candidates": web_candidates,
                        },
                    )

            if mode == "llm":
                try:
                    messages = llm.build_messages(
                        message,
                        results,
                        history,
                        web_sources=web_candidates or None,
                        extra_system=_extra_system(),
                        source_truncate_chars=(
                            doc_ref.DOCUMENT_CONTEXT_MAX_CHARS
                            if summary_intent and reference_doc is not None
                            else llm.SOURCE_TRUNCATE_CHARS
                        ),
                        mcp_context=(
                            agent.build_mcp_context(mcp_results) if mcp_used else None
                        ),
                    )
                    diagnostics["prompt_chars"] = sum(
                        len(str(item.get("content") or "")) for item in messages
                    )
                    async for delta in llm.stream_chat(llm_config, messages):
                        parts.append(delta)
                        yield _sse("delta", {"text": delta})
                        if await request.is_disconnected():
                            disconnected = True
                            break
                    if not parts:
                        raise llm.LLMError("大模型未返回内容")
                except llm.LLMError as exc:
                    logger.warning("LLM 调用失败，切换本地检索模式：%s", exc)
                    final_mode = "extractive"
                    yield _sse(
                        "notice",
                        {"message": "大模型连接失败，已切换本地检索模式", "mode": "extractive"},
                    )
                    if parts:
                        separator = "\n\n"
                        parts.append(separator)
                        yield _sse("delta", {"text": separator})
                    if not disconnected:
                        fallback = _extractive_answer(include_gap_hint=not mcp_used)
                        async for piece in _stream_text(fallback):
                            parts.append(piece)
                            yield _sse("delta", {"text": piece})
                            if await request.is_disconnected():
                                disconnected = True
                                break
            else:
                fallback = _extractive_answer(include_gap_hint=not mcp_used)
                async for piece in _stream_text(fallback):
                    parts.append(piece)
                    yield _sse("delta", {"text": piece})
                    if await request.is_disconnected():
                        disconnected = True
                        break

            answer = "".join(parts)
            final_citations, invalid_citations = _prune_citations(answer, citations)
            if invalid_citations:
                logger.warning(
                    "回答引用了不存在的来源编号：%s（可用 %s 条）",
                    invalid_citations,
                    len(citations),
                )
            diagnostics["invalid_citations"] = invalid_citations
            diagnostics["cited_sources"] = len(final_citations)
            assistant_message_id = _save_assistant(
                conversation_id, answer, final_citations
            )
            saved = True
            title = _conversation_title_of(conversation_id)
            yield _sse(
                "done",
                {
                    "message_id": assistant_message_id,
                    "conversation_id": conversation_id,
                    "citations": final_citations,
                    "mode": final_mode,
                    "title": title,
                    "gap": None if mcp_used else gap,
                    "web_sources": web_candidates,
                    "reference": reference,
                    "context_used": context_used,
                    "context_summary": context_summary,
                    "context_diagnostics": diagnostics,
                    "mcp_results": mcp_results,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("聊天流生成失败：%s", conversation_id)
            yield _sse("error", {"message": "生成回答时出现错误，请稍后重试"})
        finally:
            if not saved:
                answer = "".join(parts)
                if answer.strip():
                    fallback_citations, _ = _prune_citations(answer, citations)
                    _save_assistant(conversation_id, answer, fallback_citations)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
