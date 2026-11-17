"""Agent 联网工具调用：知识缺口检测 + ``search_web`` 工具循环。

- 仅在「知识缺口 + 已配置 LLM」时进入工具阶段；引用文档内容足够时 chat 不会进入本模块；
- 学术意图（arXiv ID / DOI / 论文）优先走 **arXiv API / OpenAlex** 确定性检索，
  并给引用文档上下文；普通意图走 LLM tool calling（最多 2 轮 / 2 次搜索）；
- LLM 返回的检索词必须通过 ``search.validate_query``，不合格时回退确定性规划；
- 工具阶段任何失败都降级为普通回答（不抛异常、不 500）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from ..utils import truncate
from . import corpus, doc_ref, embedding, llm, mcp, search
from . import extend as extend_service

logger = logging.getLogger("synapse.agent")

GAP_COVERAGE_THRESHOLD = 0.34
GAP_COVERAGE_STRICT_THRESHOLD = 0.85
GAP_RARE_TERM_IDF = 2.2
GAP_WEAK_RESULT_SCORE = 0.6
MAX_SEARCH_CALLS = 2
MAX_TOOL_ROUNDS = 2
TOOL_MAX_RESULTS = 5
TOOL_CANDIDATE_LIMIT = 8

#: MCP 工具阶段：最多 2 轮 / 3 次工具调用
MCP_MAX_ROUNDS = 2
MCP_MAX_CALLS = 3
MCP_CALL_MAX_TOKENS = 500
#: 传给最终回答的单个工具结果截断
MCP_CONTEXT_RESULT_CHARS = 4000


def _corpus_stats():  # 保持为可 monkeypatch 的测试注入点
    return corpus.corpus_stats()


def _term_idf(term: str, df, corpus_size: int) -> float:
    return corpus.idf(term, df, corpus_size)


SEARCH_TOOL: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": (
            "当用户个人知识库中的资料不足以回答问题时，联网搜索外部参考资料。"
            "参数 query 为精确检索词（优先专有名词、英文术语、arXiv ID）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "精确检索词，如「RAG 评估指标」或 arXiv ID「2410.06294」",
                }
            },
            "required": ["query"],
        },
    },
}

TOOL_SYSTEM_PROMPT = (
    "你是「Synapse 知脉」的联网检索助手。当前用户个人知识库资料不足，"
    "你必须调用 search_web 工具联网搜索外部资料，而不是直接判断无法回答；"
    "请针对用户问题的核心实体/术语调用 1–2 次，每次只传一个精确检索词，最多调用 2 次。"
    "如果用户问题涉及论文/arXiv/DOI，优先用 arXiv ID 或英文标题短语作为检索词。"
)


def detect_gap(
    question: str,
    results: list[dict[str, object]],
    reference_doc: dict[str, object] | None = None,
) -> dict[str, object] | None:
    """检测知识缺口；有缺口返回 ``{needs_more, reason, suggested_queries}``。"""
    question = str(question or "").strip()
    if not question:
        return None
    suggested = search.plan_queries(
        question, None, {"reference_doc": reference_doc or {}}
    )
    if not results:
        return {
            "needs_more": True,
            "reason": "知识库中未找到相关内容",
            "suggested_queries": suggested,
        }

    question_terms = set(embedding.tokenize_terms(question))
    coverage = 0.0
    rare_unmatched: list[str] = []
    if question_terms:
        top_text = " ".join(str(result.get("text") or "") for result in results[:3])
        top_terms = set(embedding.tokenize_terms(top_text))
        df, corpus_size = _corpus_stats()
        if corpus_size > 0:
            total_idf = sum(_term_idf(term, df, corpus_size) for term in question_terms) or 1.0
            matched_idf = sum(
                _term_idf(term, df, corpus_size)
                for term in question_terms
                if term in top_terms
            )
            coverage = matched_idf / total_idf
            rare_unmatched = [
                term
                for term in question_terms
                if term not in top_terms and df.get(term, 0) == 0
            ]
        else:
            coverage = len(question_terms & top_terms) / len(question_terms)

    weak_single = len(results) <= 1 and float(results[0].get("score") or 0) < GAP_WEAK_RESULT_SCORE
    if (
        coverage < GAP_COVERAGE_THRESHOLD
        or (coverage < GAP_COVERAGE_STRICT_THRESHOLD and rare_unmatched)
        or weak_single
    ):
        return {
            "needs_more": True,
            "reason": "知识库中相关段落较少",
            "suggested_queries": suggested,
        }
    return None


def _dedupe_candidates(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    unique: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in candidates:
        try:
            key = extend_service.normalize_url(item.get("url"))
        except extend_service.ExtendError:
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append({**item, "url": key})
        if len(unique) >= TOOL_CANDIDATE_LIMIT:
            break
    return unique


def _tool_message(reference_doc: dict[str, object] | None, academic: dict[str, object]) -> str:
    lines = [TOOL_SYSTEM_PROMPT]
    if reference_doc:
        lines.append(
            f"用户引用的知识库文档：《{reference_doc.get('title', '')}》"
            f"（文件名：{reference_doc.get('filename') or '未知'}）"
        )
    if academic.get("arxiv_ids"):
        lines.append(f"已知 arXiv ID：{'、'.join(academic['arxiv_ids'][:2])}")
    if academic.get("dois"):
        lines.append(f"已知 DOI：{'、'.join(academic['dois'][:2])}")
    return "\n".join(lines)


async def _academic_searches(
    question: str,
    academic: dict[str, object],
    reference_doc: dict[str, object] | None,
) -> list[tuple[str, list[dict[str, object]]]]:
    """确定性学术检索：arXiv ID → OpenAlex DOI → arXiv 标题搜索 / Bing 学术白名单。"""
    executed: list[tuple[str, list[dict[str, object]]]] = []
    arxiv_ids = [str(item) for item in academic.get("arxiv_ids") or []][:2]
    dois = [str(item) for item in academic.get("dois") or []][:2]
    title_terms = [str(item) for item in academic.get("title_terms") or []]

    for arxiv_id in arxiv_ids:
        if len(executed) >= MAX_SEARCH_CALLS:
            break
        try:
            results = search.arxiv_by_id(arxiv_id, max_results=TOOL_MAX_RESULTS)
        except search.SearchUnavailable as exc:
            logger.info("arXiv 检索不可用：%s", exc)
            results = []
        executed.append((arxiv_id, [item.to_dict() for item in results]))

    for doi in dois:
        if len(executed) >= MAX_SEARCH_CALLS:
            break
        try:
            results = search.openalex_by_doi(doi)
        except search.SearchUnavailable as exc:
            logger.info("OpenAlex 检索不可用：%s", exc)
            results = []
        executed.append((doi, [item.to_dict() for item in results]))

    if not executed and title_terms:
        query = " ".join(title_terms[:6])
        try:
            results = search.arxiv_search(query, max_results=TOOL_MAX_RESULTS)
        except search.SearchUnavailable as exc:
            logger.info("arXiv 标题搜索不可用：%s", exc)
            results = []
        executed.append((query, [item.to_dict() for item in results]))

    # 学术来源没有结果时，用 Bing + 学术域名白名单兜底
    if not any(items for _query, items in executed):
        for query in search.deterministic_queries(question, {"reference_doc": reference_doc or {}})[:1]:
            try:
                results = search.search_web(query, max_results=TOOL_MAX_RESULTS)
            except search.SearchUnavailable as exc:
                logger.info("学术兜底搜索不可用：%s", exc)
                results = []
            filtered = [
                item
                for item in results
                if search.authority_category(item.url) == "academic"
            ]
            executed.append((query, [item.to_dict() for item in filtered]))
            break
    return executed


async def gather_web_sources(
    question: str,
    llm_config: dict[str, object],
    reference_doc: dict[str, object] | None = None,
) -> AsyncIterator[dict[str, object]]:
    """工具调用阶段：产出 ``tool`` 事件，最后产出 ``result`` 事件。

    ``result.data`` = ``{provider, intent, queries, candidates, error}``；
    任何失败都只记录在 ``error`` 中，不抛出。
    """
    academic = search.build_academic_context(question, reference_doc)
    intent = "academic" if academic["intent"] else "general"
    messages: list[dict[str, object]] = [
        {"role": "system", "content": _tool_message(reference_doc, academic)},
        {"role": "user", "content": f"用户问题：{question}"},
    ]
    queries: list[str] = []
    collected: list[dict[str, object]] = []
    executed = 0
    error: str | None = None
    provider = search.provider_name()
    context = {"reference_doc": reference_doc or {}}
    fallback_queries = search.deterministic_queries(question, context)

    try:
        if intent == "academic":
            searches = await _academic_searches(question, academic, reference_doc)
            for query, results in searches:
                executed += 1
                queries.append(query)
                yield {
                    "event": "tool",
                    "data": {"phase": "start", "name": "search_web", "query": query},
                }
                yield {
                    "event": "tool",
                    "data": {"phase": "end", "name": "search_web", "count": len(results)},
                }
                collected.extend(results)
        else:
            for _round in range(MAX_TOOL_ROUNDS):
                payload = await llm.chat_completion_async(
                    llm_config,
                    messages,  # type: ignore[arg-type]
                    tools=[SEARCH_TOOL],
                    tool_choice="auto",
                    max_tokens=300,
                    temperature=0.2,
                    timeout=45.0,
                )
                calls = llm.extract_tool_calls(payload)
                if not calls:
                    break
                messages.append(
                    {
                        "role": "assistant",
                        "content": llm.extract_message_text(payload),
                        "tool_calls": calls,
                    }
                )
                for call in calls:
                    if executed >= MAX_SEARCH_CALLS:
                        break
                    function = call.get("function") or {}
                    if str(function.get("name") or "") != "search_web":
                        continue
                    try:
                        arguments = json.loads(str(function.get("arguments") or "{}"))
                    except json.JSONDecodeError:
                        arguments = {}
                    query = str(arguments.get("query") or "").strip()
                    if not search.validate_query(query):
                        query = next(
                            (
                                candidate
                                for candidate in fallback_queries
                                if candidate not in queries
                            ),
                            "",
                        )
                    if not query:
                        continue
                    executed += 1
                    queries.append(query)
                    yield {
                        "event": "tool",
                        "data": {"phase": "start", "name": "search_web", "query": query},
                    }
                    try:
                        results = search.search_web(query, max_results=TOOL_MAX_RESULTS)
                    except search.SearchUnavailable as exc:
                        logger.info("工具搜索不可用：%s", exc)
                        results = []
                    yield {
                        "event": "tool",
                        "data": {"phase": "end", "name": "search_web", "count": len(results)},
                    }
                    tool_payload = json.dumps(
                        [result.to_dict() for result in results], ensure_ascii=False
                    )[:4000]
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(call.get("id") or ""),
                            "content": tool_payload,
                        }
                    )
                    collected.extend(result.to_dict() for result in results)
                if executed >= MAX_SEARCH_CALLS:
                    break
    except llm.LLMError as exc:
        error = str(exc)
        logger.warning("工具调用阶段失败，降级为普通回答：%s", exc)
    except Exception as exc:  # 工具阶段任何异常都不能影响正常回答
        error = "联网搜索阶段出现异常，已降级为普通回答"
        logger.exception("工具调用阶段异常：%s", exc)

    deduped = _dedupe_candidates(collected)
    candidate_sources = {
        str(item.get("source") or "") for item in deduped if item.get("source")
    }
    if candidate_sources == {"arxiv"}:
        provider = "arxiv"
    elif candidate_sources and candidate_sources <= {"arxiv", "openalex"}:
        provider = "academic"

    yield {
        "event": "result",
        "data": {
            "provider": provider,
            "intent": intent,
            "queries": queries,
            "candidates": deduped,
            "error": error,
            "academic_context": {
                "arxiv_ids": academic["arxiv_ids"],
                "dois": academic["dois"],
            },
        },
    }


# ---------------------------------------------------------------------------
# MCP 只读工具阶段（V2）
# ---------------------------------------------------------------------------

MCP_SYSTEM_PROMPT = (
    "你是「Synapse 知脉」的外部工具调用助手。当前用户启用了 MCP 只读工具"
    "（文件系统 / 知识库数据库 / GitHub 等）。规则：\n"
    "1) 需要外部信息时调用工具获取，不要凭记忆编造；\n"
    "2) 工具参数必须来自用户问题的实际内容（路径 / 仓库名 / SQL 表名等），不要臆造；\n"
    "3) 最多调用 3 次工具，一次能解决的问题不要重复调用；\n"
    "4) 用户要求列出 / 查找文件时，优先调用支持递归搜索的工具（如 search_files），"
    "不要只看一层目录列表就断定没有文件；\n"
    "5) 工具返回后，基于工具结果用中文简洁回答，并说明数据来自外部工具。"
)


def mcp_tool_schema(tool: dict[str, object]) -> dict[str, object]:
    """把一个 MCP 工具转成 OpenAI function calling schema。"""
    name = str(tool.get("qualified_name") or "").strip()
    description = str(tool.get("description") or "").strip()
    if not description:
        description = f"MCP 工具（{tool.get('server_name') or tool.get('server_id') or ''}）"
    input_schema = tool.get("input_schema")
    if not isinstance(input_schema, dict) or not input_schema:
        input_schema = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": truncate(description, 400) + "（MCP 只读工具）",
            "parameters": input_schema,
        },
    }


def build_mcp_context(results: list[dict[str, object]]) -> str | None:
    """把成功的工具结果拼成最终回答的外部上下文（system 消息）。"""
    successful = [
        item
        for item in results
        if item.get("ok") and str(item.get("text") or "").strip()
    ]
    if not successful:
        return None
    lines = ["【外部工具结果（MCP 只读工具，未经知识库收录，仅作参考）】"]
    for index, item in enumerate(successful[:MCP_MAX_CALLS], start=1):
        args_text = json.dumps(item.get("args") or {}, ensure_ascii=False)[:300]
        body = str(item.get("text") or "")[:MCP_CONTEXT_RESULT_CHARS]
        lines.append(
            f"[T{index}] 工具：{item.get('name')}\n参数：{args_text}\n结果：\n{body}"
        )
    lines.append(
        "请基于上述工具结果回答用户问题；引用工具结果时使用 [T1][T2] 标注，"
        "不要编造工具未返回的内容。"
    )
    return "\n\n".join(lines)


def _mcp_reference_note(reference_doc: dict[str, object] | None) -> str:
    if not reference_doc:
        return ""
    return (
        f"\n用户当前引用的知识库文档：《{reference_doc.get('title', '')}》，"
        "工具结果与文档内容冲突时以工具结果为准并说明来源。"
    )


async def gather_mcp_sources(
    question: str,
    llm_config: dict[str, object],
    reference_doc: dict[str, object] | None = None,
) -> AsyncIterator[dict[str, object]]:
    """MCP 工具调用阶段：产出 ``tool`` 事件，最后产出 ``result`` 事件。

    ``result.data`` = ``{tools, results, error}``：
    - ``tools`` 为可用 MCP 工具（空表示没有可用的已启用工具）；
    - ``results`` 为 ``[{name, args, ok, text, error?}]``（供 SSE done.mcp_results）；
    - 工具阶段任何失败只记录在 ``error``，不抛出，由 chat 降级到常规 RAG。
    """
    try:
        tools = await mcp.get_enabled_tools()
    except Exception as exc:  # 读取配置失败也不应影响回答
        logger.warning("读取 MCP 工具列表失败：%s", exc)
        tools = []
    if not tools:
        yield {"event": "result", "data": {"tools": [], "results": [], "error": None}}
        return

    schema = [mcp_tool_schema(tool) for tool in tools]
    valid_names = {str(tool.get("qualified_name")) for tool in tools}
    messages: list[dict[str, object]] = [
        {
            "role": "system",
            "content": MCP_SYSTEM_PROMPT + _mcp_reference_note(reference_doc),
        },
        {"role": "user", "content": f"用户问题：{question}"},
    ]
    results: list[dict[str, object]] = []
    executed = 0
    error: str | None = None
    try:
        for _round in range(MCP_MAX_ROUNDS):
            payload = await llm.chat_completion_async(
                llm_config,
                messages,  # type: ignore[arg-type]
                tools=schema,
                tool_choice="auto",
                max_tokens=MCP_CALL_MAX_TOKENS,
                temperature=0.1,
                timeout=45.0,
            )
            calls = llm.extract_tool_calls(payload)
            if not calls:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": llm.extract_message_text(payload),
                    "tool_calls": calls,
                }
            )
            for call in calls:
                if executed >= MCP_MAX_CALLS:
                    break
                function = call.get("function") or {}
                name = str(function.get("name") or "").strip()
                try:
                    arguments = json.loads(str(function.get("arguments") or "{}"))
                except json.JSONDecodeError:
                    arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                if name not in valid_names:
                    results.append(
                        {
                            "name": name,
                            "args": arguments,
                            "ok": False,
                            "text": "",
                            "error": f"未知的 MCP 工具：{name}",
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(call.get("id") or ""),
                            "content": f"未知的 MCP 工具：{name}，请改用已提供的工具",
                        }
                    )
                    continue
                executed += 1
                yield {
                    "event": "tool",
                    "data": {"phase": "start", "name": name, "args": arguments},
                }
                try:
                    outcome = await mcp.call_tool(None, name, arguments)
                except Exception as exc:  # call_tool 已兜底，这里再保护一层
                    logger.exception("MCP 工具调用异常：%s", exc)
                    outcome = {
                        "ok": False,
                        "text": "",
                        "error": "工具调用出现异常，请稍后重试",
                    }
                text = str(outcome.get("text") or "")
                ok = bool(outcome.get("ok"))
                item: dict[str, object] = {
                    "name": name,
                    "args": arguments,
                    "ok": ok,
                    "text": text,
                }
                if not ok:
                    item["error"] = str(outcome.get("error") or "工具调用失败")
                results.append(item)
                yield {
                    "event": "tool",
                    "data": {
                        "phase": "end",
                        "name": name,
                        "ok": ok,
                        "summary": mcp.summarize_tool_result(outcome),
                    },
                }
                tool_content = (
                    text if ok else str(outcome.get("error") or text or "工具调用失败")
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "content": tool_content[: mcp.MAX_RESULT_CHARS],
                    }
                )
            if executed >= MCP_MAX_CALLS:
                break
    except llm.LLMError as exc:
        error = str(exc)
        logger.warning("MCP 工具调用阶段失败，降级为常规回答：%s", exc)
    except Exception as exc:  # 工具阶段任何异常都不能影响正常回答
        error = "MCP 工具调用阶段出现异常，已降级为常规回答"
        logger.exception("MCP 工具调用阶段异常：%s", exc)

    yield {
        "event": "result",
        "data": {"tools": tools, "results": results, "error": error},
    }
