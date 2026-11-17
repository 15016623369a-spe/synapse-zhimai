"""多轮对话上下文解析：追问识别 + 紧凑上下文卡片（少而准）。

背景：用户先问「请总结《某篇论文》的核心内容」，再追问「创新点呢」时，单独用当前短句
检索 / 规划检索词会丢失上文（知识库误报不足、Bing 返回无关结果）。

设计原则：**用尽可能少、但信号密度高的 tokens**，不把上一轮完整问答 / 整篇文档塞进上下文。

- ``resolve_conversation_context(conn, conversation_id, message)`` 返回**紧凑上下文卡片**::

      {
        "follow_up": True,
        "reason": "aspect",
        "reference_doc": {doc_id, title, filename, source_type, size, arxiv_id, doi},  # 无正文
        "topic_terms": ["Transformer-BP", "Belief Propagation", "多目标跟踪", ...],      # 3–6 个
        "previous_intent": "总结",
        "search_text": "Transformer-BP 多目标跟踪研究框架 的核心内容 → 追问 创新点",   # ≤120 字
        "context_summary": "已结合上文《…》· 追问创新点",                              # ≤60 字
        "context_chars": 78, "token_budget": 34,   # 实际长度（字符 / 估算 token）
      }

  只继承最近一次有效文档，不随轮次链式膨胀；不是追问或没有历史时返回 ``None``。
- ``build_search_queries(context, question)``：追问场景的确定性检索词
  （arXiv ID / DOI / 论文标题 / topic_terms），避免 novelty / innovation 泛词。
"""

from __future__ import annotations

import json
import logging
import re

from .. import db
from ..utils import collapse_whitespace, estimate_tokens, truncate
from . import doc_ref, embedding

logger = logging.getLogger("synapse.context")

#: 向前读取的消息条数上限
HISTORY_LOOKBACK = 12
#: 紧凑卡片长度上限
MAX_SEARCH_TEXT_CHARS = 120
MAX_SUMMARY_CHARS = 60
MAX_TOPIC_TERMS = 6
MIN_TOPIC_TERMS = 3
#: 检索词条数上限
MAX_CONTEXT_QUERIES = 4
MAX_QUERY_CHARS = 120

#: 指代 / 承接词：出现且没有引入新主题时视为追问
PRONOUN_WORDS = (
    "它们", "它", "他们", "他", "她们", "她",
    "这个", "那个", "这些", "那些", "这篇", "那篇", "这份", "那份",
    "该文档", "该论文", "该", "此", "上述", "上面", "前面", "刚才", "之前", "上文",
    "那", "这",
)
#: 继续 / 展开类表达
CONTINUATION_WORDS = (
    "继续", "接着", "接下来", "然后", "还有", "展开", "详细", "具体", "补充",
    "进一步", "再说", "多说", "说说", "讲讲", "举个例子", "举例",
)
#: 只问某个方面的问法（「创新点呢」「方法有哪些」）
ASPECT_WORDS = (
    "创新点", "创新", "贡献", "总结", "概括", "摘要", "复述", "全文",
    "方法", "实验", "结论", "结果", "局限", "不足",
    "优点", "缺点", "细节", "数据", "背景", "动机", "架构", "实现", "评估",
    "对比", "相关工作", "未来工作", "意义", "价值", "原理", "公式", "参数",
    "证明", "优势", "问题", "应用", "影响", "为什么", "怎么", "如何", "依据",
    "解释", "区别", "例子", "原因", "流程", "步骤", "假设", "验证",
)

_FOLLOWUP_MARKERS = tuple(
    sorted(
        set(PRONOUN_WORDS) | set(CONTINUATION_WORDS) | set(ASPECT_WORDS),
        key=len,
        reverse=True,
    )
)
_PUNCT_RE = re.compile(r"[\s。？！?!.，,；;：:、]+")
_TAIL_PARTICLE_RE = re.compile(r"[呢吗吧呀啊哦嘛]+$")

#: 上一轮意图 → 检索文本里的短语
INTENT_PHRASES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("总结", ("总结", "概括", "核心内容", "摘要", "介绍了什么", "讲了什么"), "核心内容"),
    ("创新点", ("创新点", "创新", "贡献"), "创新点"),
    ("方法", ("方法", "架构", "原理", "实现", "怎么做到"), "方法"),
    ("实验", ("实验", "结果", "评估", "数据"), "实验"),
    ("背景", ("背景", "动机", "为什么"), "背景"),
)
#: topic_terms 里应过滤的低信号词
GENERIC_TOPIC_TERMS = frozenset(
    {
        "研究", "框架", "内容", "核心内容", "方法", "问题", "结果", "总结", "简介",
        "摘要", "论文", "资料", "思路", "工作", "方向", "系统", "模型", "数据",
        "理论", "应用", "技术", "分析", "研究框架", "the", "and", "for", "with",
        "based", "using", "paper", "method", "approach", "analysis", "review",
    }
)
_ENGLISH_PHRASE_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9\-]*(?:\s+[A-Za-z][A-Za-z0-9\-]*){0,2}"
)


# ---------------------------------------------------------------------------
# 追问识别
# ---------------------------------------------------------------------------


def _normalize_question(text: object) -> str:
    lowered = _PUNCT_RE.sub("", str(text or "").lower())
    return _TAIL_PARTICLE_RE.sub("", lowered)


def is_same_question(first: object, second: object) -> bool:
    """两句话是否同义（去空白/标点/句末语气词后相同）。"""
    normalized_first = _normalize_question(first)
    normalized_second = _normalize_question(second)
    return bool(normalized_first) and normalized_first == normalized_second


def _leftover_tokens(message: str) -> list[str]:
    """去掉指代/承接/方面词之后剩下的内容词；为空说明没有引入新主题。"""
    cleaned = collapse_whitespace(message)
    for marker in _FOLLOWUP_MARKERS:
        cleaned = cleaned.replace(marker, " ")
    return [
        token.lower()
        for token in embedding.tokenize_terms(cleaned)
        if token.strip() and not token.isdigit()
    ]


def detect_follow_up(message: str) -> tuple[bool, str]:
    """判断当前消息是否为多轮追问；返回 ``(是否追问, 原因)``。

    明确带书名号 / arXiv ID / DOI / 长文件名的消息视为「自包含问题」，不算追问。
    """
    text = collapse_whitespace(str(message or ""))
    if not text:
        return False, ""
    if doc_ref.QUOTED_RE.search(text):
        return False, ""
    ids = doc_ref.extract_ids(text)
    if ids["arxiv_ids"] or ids["dois"]:
        return False, ""
    if doc_ref.LONG_TOKEN_RE.search(text):
        return False, ""

    leftover = _leftover_tokens(text)
    if leftover:
        return False, ""
    if any(marker in text for marker in PRONOUN_WORDS):
        return True, "pronoun"
    if any(marker in text for marker in CONTINUATION_WORDS):
        return True, "continuation"
    if any(marker in text for marker in ASPECT_WORDS):
        return True, "aspect"
    return False, ""


# ---------------------------------------------------------------------------
# 意图 / 主题词
# ---------------------------------------------------------------------------


def detect_previous_intent(question: str) -> str:
    """识别上一轮用户意图（总结 / 创新点 / 方法 / 实验 / 背景），否则空串。"""
    text = str(question or "").lower()
    for intent, keywords, _phrase in INTENT_PHRASES:
        if any(keyword in text for keyword in keywords):
            return intent
    return ""


def _intent_phrase(intent: str) -> str:
    for name, _keywords, phrase in INTENT_PHRASES:
        if name == intent:
            return phrase
    return ""


def _english_phrases(text: str, limit: int) -> list[str]:
    phrases: list[str] = []
    seen: set[str] = set()
    for match in _ENGLISH_PHRASE_RE.finditer(text or ""):
        phrase = collapse_whitespace(match.group(0)).strip()
        if not phrase:
            continue
        key = phrase.lower()
        if key in seen or key in GENERIC_TOPIC_TERMS:
            continue
        if len(phrase) < 3 and not (phrase.isupper() and len(phrase) >= 2):
            continue
        seen.add(key)
        phrases.append(phrase)
        if len(phrases) >= limit:
            break
    return phrases


def _chinese_terms(text: str, limit: int) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in embedding.tokenize_terms(text or ""):
        if not re.search(r"[\u4e00-\u9fff]", token) or len(token) < 2:
            continue
        if token in GENERIC_TOPIC_TERMS or token in seen:
            continue
        seen.add(token)
        terms.append(token)
        if len(terms) >= limit:
            break
    return terms


def build_topic_terms(
    previous_question: str,
    document: dict[str, object] | None,
    limit: int = MAX_TOPIC_TERMS,
) -> list[str]:
    """3–6 个高信号实体：标题英文短语 + 摘要英文短语 + 标签 / 中文实体词。"""
    terms: list[str] = []
    seen: set[str] = set()

    def _append(candidates: list[str]) -> None:
        for candidate in candidates:
            key = candidate.lower().strip()
            if not key or key in seen or candidate in GENERIC_TOPIC_TERMS:
                continue
            seen.add(key)
            terms.append(candidate)
            if len(terms) >= limit:
                return

    if document:
        title = collapse_whitespace(str(document.get("title") or ""))
        summary = collapse_whitespace(str(document.get("summary") or ""))
        tags = [str(tag) for tag in document.get("tags") or [] if str(tag).strip()]
        _append(_english_phrases(title, limit=2))
        _append(_english_phrases(summary, limit=3))
        _append(tags[:3])
        if len(terms) < MIN_TOPIC_TERMS:
            _append(_chinese_terms(title, limit=2))
        if len(terms) < MIN_TOPIC_TERMS:
            _append(_chinese_terms(summary, limit=2))
    _append(_english_phrases(previous_question, limit=2))
    if len(terms) < MIN_TOPIC_TERMS:
        _append(_chinese_terms(previous_question, limit=3))
    return terms[:limit]


def topic_search_terms(context: dict[str, object] | None) -> list[str]:
    """把上下文卡片的 topic_terms 转成英文学术检索词（OpenAlex / arXiv 用）。"""
    if not context:
        return []
    tokens: list[str] = []
    seen: set[str] = set()
    for term in context.get("topic_terms") or []:
        for chunk in re.findall(r"[A-Za-z][A-Za-z0-9\-]{1,}", str(term)):
            for piece in chunk.split("-"):
                token = piece.lower()
                if len(token) < 3 or token in seen:
                    continue
                seen.add(token)
                tokens.append(token)
    return tokens


# ---------------------------------------------------------------------------
# 消息 / 文档读取
# ---------------------------------------------------------------------------


def _parse_citations(raw: object) -> list[dict[str, object]]:
    if not raw:
        return []
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _load_messages(conn, conversation_id: str) -> list[dict[str, object]]:
    rows = db.fetch_all(
        conn,
        "SELECT id, role, content, citations, created_at FROM messages "
        "WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC",
        (conversation_id,),
    )
    messages: list[dict[str, object]] = []
    for row in rows:
        role = str(row["role"] or "")
        if role not in {"user", "assistant"}:
            continue
        messages.append(
            {
                "id": str(row["id"]),
                "role": role,
                "content": str(row["content"] or ""),
                "citations": _parse_citations(row["citations"]),
            }
        )
    return messages[-HISTORY_LOOKBACK:]


def _parse_tags(raw: object) -> list[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def load_document(conn, doc_id: str) -> dict[str, object] | None:
    """按 ID 读取文档（含正文；仅供 chat 明确总结场景使用，不进入上下文卡片）。"""
    row = db.fetch_one(
        conn,
        "SELECT id, title, filename, source_type, content, size, summary, tags "
        "FROM documents WHERE id = ?",
        (doc_id,),
    )
    if row is None:
        return None
    ids = doc_ref.extract_ids(f"{row['title']} {row['filename'] or ''}")
    chunk_count = int(
        db.fetch_value(
            conn,
            "SELECT COUNT(*) FROM chunks WHERE doc_id = ?",
            (str(row["id"]),),
            0,
        )
        or 0
    )
    return {
        "doc_id": str(row["id"]),
        "id": str(row["id"]),
        "title": str(row["title"] or ""),
        "filename": row["filename"],
        "source_type": str(row["source_type"] or ""),
        "content": str(row["content"] or ""),
        "size": int(row["size"] or len(str(row["content"] or ""))),
        "summary": str(row["summary"] or ""),
        "tags": _parse_tags(row["tags"]),
        "chunk_count": chunk_count,
        "arxiv_id": ids["arxiv_ids"][0] if ids["arxiv_ids"] else None,
        "doi": ids["dois"][0] if ids["dois"] else None,
    }


def compact_reference(document: dict[str, object] | None) -> dict[str, object] | None:
    """文档 → 上下文卡片里的紧凑 reference（不含正文）。"""
    if not document:
        return None
    return {
        "doc_id": str(document.get("doc_id") or document.get("id") or ""),
        "title": str(document.get("title") or ""),
        "filename": document.get("filename"),
        "source_type": str(document.get("source_type") or ""),
        "size": int(document.get("size") or 0),
        "chunk_count": int(document.get("chunk_count") or 0),
        "arxiv_id": document.get("arxiv_id"),
        "doi": document.get("doi"),
    }


def _citation_doc_id(citations: list[dict[str, object]]) -> str:
    for citation in citations:
        doc_id = str(citation.get("doc_id") or "").strip()
        if doc_id:
            return doc_id
    return ""


def _find_inherited_document(
    conn, messages: list[dict[str, object]]
) -> dict[str, object] | None:
    """从历史继承文档：优先最近的 assistant citations，其次 user 消息里的引用。"""
    for item in reversed(messages):
        if item["role"] != "assistant":
            continue
        doc_id = _citation_doc_id(item["citations"])  # type: ignore[arg-type]
        if not doc_id:
            continue
        document = load_document(conn, doc_id)
        if document is not None:
            logger.info("多轮追问继承 assistant 引用文档：%s", document["title"])
            return document
    for item in reversed(messages):
        if item["role"] != "user":
            continue
        found = doc_ref.detect_referenced_document(conn, str(item["content"]))
        if found is not None and found.get("doc_id"):
            document = load_document(conn, str(found["doc_id"]))
            if document is not None:
                logger.info("多轮追问继承 user 引用文档：%s", document["title"])
                return document
    return None


def _root_question(messages: list[dict[str, object]], message: str) -> str:
    """最近一条「非追问」的用户问题（多轮追问链的根问题，避免链式膨胀）。"""
    nearest = ""
    for item in reversed(messages):
        if item["role"] != "user":
            continue
        content = str(item["content"] or "").strip()
        if not content or is_same_question(content, message):
            continue
        if not nearest:
            nearest = content
        is_follow_up, _reason = detect_follow_up(content)
        if not is_follow_up:
            return content
    return nearest


# ---------------------------------------------------------------------------
# 紧凑卡片
# ---------------------------------------------------------------------------


def _focus_question(question: str) -> str:
    """当前问题去掉指令 / 语气词后的焦点短语。"""
    from . import search as search_service  # 延迟导入：仅查询规划用

    text = collapse_whitespace(str(question or ""))
    text = search_service.clean_question_for_query(text)
    text = _TAIL_PARTICLE_RE.sub("", text).strip(" ，,。；;")
    return truncate(text, 40, suffix="").strip()


def _build_search_text(
    document: dict[str, object] | None,
    previous_question: str,
    previous_intent: str,
    message: str,
) -> str:
    focus = _focus_question(message)
    if document is not None:
        head = collapse_whitespace(str(document.get("title") or ""))
    else:
        head = _focus_question(previous_question)
    if not head:
        head = truncate(collapse_whitespace(previous_question), 60, suffix="")
    phrase = _intent_phrase(previous_intent)
    if phrase:
        head = f"{head} 的{phrase}"
    suffix = f" → 追问 {focus}" if focus else " → 追问"
    return truncate(f"{head}{suffix}".strip(), MAX_SEARCH_TEXT_CHARS, suffix="")


def _build_summary(
    document: dict[str, object] | None, previous_question: str, message: str
) -> str:
    focus = _focus_question(message)
    if document is not None:
        title = truncate(str(document.get("title") or ""), 28, suffix="")
        summary = f"已结合上文《{title}》· 追问{focus}"
    elif previous_question:
        summary = f"已结合上文：{truncate(previous_question, 40, suffix='')}"
    else:
        summary = "已结合上文"
    return truncate(summary, MAX_SUMMARY_CHARS, suffix="")


def resolve_conversation_context(
    conn, conversation_id: str | None, message: str
) -> dict[str, object] | None:
    """解析多轮上下文，返回紧凑上下文卡片；不是追问 / 没有历史时返回 ``None``。"""
    conversation_id = str(conversation_id or "").strip()
    text = collapse_whitespace(str(message or ""))
    if not conversation_id or not text:
        return None
    follow_up, reason = detect_follow_up(text)
    if not follow_up:
        return None

    messages = _load_messages(conn, conversation_id)
    if not messages:
        return None
    # 扩展面板在对话失败后调用：当前 user 消息可能已经落库，先排除它
    if messages and messages[-1]["role"] == "user" and is_same_question(
        messages[-1]["content"], text
    ):
        messages = messages[:-1]
    if not messages:
        return None

    document = _find_inherited_document(conn, messages)
    previous_question = _root_question(messages, text)
    if not previous_question and document is None:
        return None
    latest_question = ""
    for item in reversed(messages):
        if item["role"] != "user":
            continue
        content = str(item["content"] or "").strip()
        if content and not is_same_question(content, text):
            latest_question = content
            break

    reference = compact_reference(document)
    previous_intent = detect_previous_intent(previous_question or latest_question)
    topic_terms = build_topic_terms(previous_question, document)
    search_text = _build_search_text(
        reference, previous_question, previous_intent, text
    )
    context_summary = _build_summary(reference, previous_question, text)
    context_chars = (
        len(search_text)
        + len(context_summary)
        + sum(len(term) for term in topic_terms)
    )
    token_budget = estimate_tokens(
        " ".join([search_text, context_summary, *topic_terms])
    )
    return {
        "follow_up": True,
        "reason": reason,
        "reference_doc": reference,
        "topic_terms": topic_terms,
        "previous_intent": previous_intent,
        "search_text": search_text,
        "context_summary": context_summary,
        "context_chars": context_chars,
        "token_budget": token_budget,
        # 便于测试 / 调试，不进入 prompt
        "previous_question": previous_question,
        "latest_question": latest_question,
    }


# ---------------------------------------------------------------------------
# 追问检索词
# ---------------------------------------------------------------------------


def build_search_queries(
    context: dict[str, object] | None,
    question: str,
    limit: int = 3,
) -> list[str]:
    """追问场景的确定性检索词：arXiv ID / DOI / 论文标题 / topic_terms + 当前焦点。"""
    context = context or {}
    reference = context.get("reference_doc") or {}
    search_text = str(context.get("search_text") or "")
    title = collapse_whitespace(str(reference.get("title") or ""))
    filename = str(reference.get("filename") or "")
    identity = f"{title} {filename} {search_text}"
    ids = doc_ref.extract_ids(identity)
    focused = _focus_question(question)
    topic_terms = [str(term) for term in context.get("topic_terms") or []]

    candidates: list[str] = []
    if ids["arxiv_ids"]:
        candidates.append(ids["arxiv_ids"][0])
    if ids["dois"]:
        candidates.append(ids["dois"][0])
    if title:
        if focused:
            candidates.append(f"{title} {focused}")
        candidates.append(title)
    if topic_terms:
        candidates.append(" ".join(topic_terms[:5]))
    if not candidates and focused:
        candidates.append(focused)
    if not candidates and search_text:
        candidates.append(search_text)

    queries: list[str] = []
    for query in candidates:
        cleaned = truncate(collapse_whitespace(query), MAX_QUERY_CHARS, suffix="").strip()
        if not cleaned or cleaned in queries:
            continue
        queries.append(cleaned)
        if len(queries) >= limit:
            break
    if not queries and search_text:
        queries.append(truncate(search_text, MAX_QUERY_CHARS, suffix=""))
    return queries
