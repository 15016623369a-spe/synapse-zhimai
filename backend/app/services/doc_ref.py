"""引用文档识别：用户问题里提到知识库中的某篇文档 / arXiv ID / DOI。

用于「请总结《某篇论文》」这类请求：直接锁定知识库里的论文文档，走文档级全文回答，
不再误判知识缺口、也不盲目联网。

识别顺序（按优先级）：
1. arXiv ID / DOI 精确（容忍 ``arXiv:`` 前缀、版本号、下划线/连字符差异）；
2. 书名号 / 引号中的标题；
3. 长文件名 token（容忍 ``NEBBplus`` vs ``NEBPplus`` 这类拼写差异）。
"""

from __future__ import annotations

import logging
import re
from collections import Counter

from .. import db
from . import embedding

logger = logging.getLogger("synapse.doc_ref")

ARXIV_ID_RE = re.compile(r"(?:arxiv[\s:：/]*)?(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>，。；;]+", re.IGNORECASE)
QUOTED_RE = re.compile(
    r"《([^》]{2,200})》|「([^」]{2,200})」|“([^”]{2,200})”|\"([^\"]{2,200})\""
)
LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-.()]{11,}")
NORMALIZE_STRIP_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff ]+")
MULTISPACE_RE = re.compile(r"\s+")

SUMMARY_KEYWORDS = (
    "总结", "概括", "摘要", "核心内容", "主要贡献", "主要内容", "讲了什么",
    "论文内容", "介绍一下", "简介", "summarize", "summary", "abstract",
)
SUMMARY_HINT_WORDS = (
    "方法", "实验", "结论", "贡献", "结果", "评估", "背景", "创新",
)
INSTRUCTION_WORDS = (
    "请", "帮我", "帮忙", "总结", "概括", "摘要", "核心内容", "主要贡献",
    "主要内容", "讲了什么", "论文内容", "资料", "一下", "有哪些", "告诉我",
    "summarize", "summary", "please", "paper content", "核心", "内容",
)
INSTRUCTION_RE = re.compile("|".join(re.escape(word) for word in INSTRUCTION_WORDS), re.IGNORECASE)

#: 文档内容足够长时，引用文档问题不再触发 gap / 联网
REFERENCE_CONTENT_MIN_CHARS = 800
DOCUMENT_CONTEXT_MAX_CHARS = 40000


# ---------------------------------------------------------------------------
# 归一化与 ID 提取
# ---------------------------------------------------------------------------


def normalize_ref_text(text: object) -> str:
    """小写、下划线/连字符/点 → 空格，仅保留字母数字与 CJK。"""
    lowered = str(text or "").lower()
    spaced = re.sub(r"[_\-–—/.]+", " ", lowered)
    cleaned = NORMALIZE_STRIP_RE.sub(" ", spaced)
    return MULTISPACE_RE.sub(" ", cleaned).strip()


def extract_ids(text: object) -> dict[str, list[str]]:
    """从文本中提取 arXiv ID（去版本号）与 DOI（去尾部标点）。"""
    raw = str(text or "")
    arxiv_ids: list[str] = []
    for match in ARXIV_ID_RE.finditer(raw):
        candidate = match.group(1)
        # arXiv 新式 ID 形如 2410.06294；裸 ID / 带 arXiv 前缀 / 带版本号均可
        if candidate not in arxiv_ids:
            arxiv_ids.append(candidate)
    dois: list[str] = []
    for match in DOI_RE.finditer(raw):
        doi = match.group(0).rstrip(".,;:)）】]").lower()
        if doi.startswith("10.") and doi not in dois:
            dois.append(doi)
    return {"arxiv_ids": arxiv_ids, "dois": dois}


def _tokenize(text: object) -> list[str]:
    normalized = normalize_ref_text(text)
    tokens = [token for token in normalized.split() if token]
    return tokens


def _distinctive(tokens: list[str]) -> list[str]:
    return [
        token
        for token in tokens
        if len(token) >= 3 or (any(ch.isdigit() for ch in token) and len(token) >= 2)
    ]


def _levenshtein(first: str, second: str, cap: int = 2) -> int:
    if abs(len(first) - len(second)) > cap:
        return cap + 1
    previous = list(range(len(second) + 1))
    for i, char_a in enumerate(first, start=1):
        current = [i]
        for j, char_b in enumerate(second, start=1):
            cost = 0 if char_a == char_b else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]


def token_match(first: str, second: str) -> bool:
    """容忍拼写差异的 token 匹配（NEBBplus vs NEBPplus）。"""
    if first == second:
        return True
    if len(first) >= 5 and len(second) >= 5:
        allowed = 1 if max(len(first), len(second)) < 10 else 2
        return _levenshtein(first, second, cap=allowed) <= allowed
    return False


def _overlap_score(mention_tokens: list[str], doc_tokens: list[str]) -> float:
    """distinctive token 覆盖率（模糊匹配）+ Jaccard 的混合分。"""
    mention = _distinctive(mention_tokens)
    document = _distinctive(doc_tokens)
    if not mention or not document:
        return 0.0
    matched = sum(
        1 for token in mention if any(token_match(token, other) for other in document)
    )
    coverage = matched / len(mention)
    doc_matched = sum(
        1 for token in document if any(token_match(token, other) for other in mention)
    )
    precision = doc_matched / len(document)
    jaccard = matched / max(1, len(mention) + len(document) - matched)
    return max(coverage, 0.5 * coverage + 0.3 * jaccard + 0.2 * precision)


def _quoted_mentions(message: str) -> list[str]:
    mentions: list[str] = []
    for match in QUOTED_RE.finditer(message):
        mention = next((group for group in match.groups() if group), "")
        if mention.strip():
            mentions.append(mention.strip())
    return mentions


def _long_tokens(message: str) -> list[str]:
    return [match.group(0) for match in LONG_TOKEN_RE.finditer(message)]


def _doc_identity_text(title: str, filename: str | None) -> str:
    return f"{title or ''} {filename or ''}"


def _match_score_for_document(
    document: dict[str, object],
    message: str,
    message_ids: dict[str, list[str]],
    quoted: list[str],
    long_tokens: list[str],
) -> float:
    doc_text = _doc_identity_text(
        str(document.get("title") or ""), str(document.get("filename") or "")
    )
    doc_ids = extract_ids(doc_text)
    score = 0.0
    if message_ids["arxiv_ids"] and (
        set(message_ids["arxiv_ids"]) & set(doc_ids["arxiv_ids"])
    ):
        score = max(score, 1.0)
    if message_ids["dois"] and (set(message_ids["dois"]) & set(doc_ids["dois"])):
        score = max(score, 1.0)

    normalized_doc = normalize_ref_text(doc_text)
    normalized_title = normalize_ref_text(document.get("title"))
    normalized_filename = normalize_ref_text(document.get("filename"))
    normalized_message = normalize_ref_text(message)

    for mention in quoted:
        normalized_mention = normalize_ref_text(mention)
        if not normalized_mention:
            continue
        if (
            normalized_mention in normalized_doc
            or normalized_doc in normalized_mention
            or normalized_mention in normalized_title
            or normalized_mention in normalized_filename
        ):
            score = max(score, 0.95)
            continue
        overlap = _overlap_score(_tokenize(mention), _tokenize(doc_text))
        score = max(score, 0.9 * overlap)

    # 未加书名号但直接粘贴了标题 / 文件名
    for target in (normalized_title, normalized_filename, normalized_doc):
        if target and len(target) >= 8 and target in normalized_message:
            score = max(score, 0.95)

    for token in long_tokens:
        normalized_token = normalize_ref_text(token)
        if not normalized_token:
            continue
        if normalized_token in normalized_doc or normalized_token in normalized_message:
            pass
        overlap = max(
            _overlap_score(_tokenize(token), _tokenize(doc_text)),
            _similarity(normalized_token, normalized_filename),
            _similarity(normalized_token, normalized_title),
        )
        score = max(score, 0.85 * overlap)
    return min(1.0, score)


def _similarity(first: str, second: str) -> float:
    """两个归一化字符串的 token 覆盖率（模糊匹配）。"""
    if not first or not second:
        return 0.0
    if first in second or second in first:
        return 1.0
    return _overlap_score(first.split(), second.split())


def detect_referenced_document(conn, message: str) -> dict[str, object] | None:
    """从问题中识别用户引用的知识库文档；不确定时返回 None。"""
    text = str(message or "").strip()
    if not text:
        return None
    message_ids = extract_ids(text)
    quoted = _quoted_mentions(text)
    long_tokens = _long_tokens(text)
    if not (message_ids["arxiv_ids"] or message_ids["dois"] or quoted or long_tokens):
        return None

    rows = db.fetch_all(
        conn,
        "SELECT id, title, filename, source_type, content, size FROM documents",
    )
    scored: list[tuple[float, dict[str, object]]] = []
    exact_quoted: list[tuple[float, dict[str, object]]] = []
    for row in rows:
        document = {
            "id": str(row["id"]),
            "title": str(row["title"] or ""),
            "filename": row["filename"],
            "source_type": str(row["source_type"] or ""),
            "content": str(row["content"] or ""),
            "size": int(row["size"] or len(str(row["content"] or ""))),
        }
        score = _match_score_for_document(
            document, text, message_ids, quoted, long_tokens
        )
        if score > 0:
            scored.append((score, document))
        # 书名号/引号里的完整标题是强信号：即使其他文档共享 Transformer/
        # multi-object tracking 等通用词，也应优先选中精确标题，而不是判为歧义。
        normalized_doc = normalize_ref_text(
            _doc_identity_text(str(document["title"]), str(document["filename"] or ""))
        )
        normalized_title = normalize_ref_text(document["title"])
        normalized_filename = normalize_ref_text(document["filename"])
        for mention in quoted:
            normalized_mention = normalize_ref_text(mention)
            if len(normalized_mention) < 8:
                continue
            if (
                normalized_mention in normalized_doc
                or normalized_doc in normalized_mention
                or normalized_mention in normalized_title
                or normalized_mention in normalized_filename
            ):
                exact_quoted.append((score, document))
                break

    if not scored:
        return None
    if len(exact_quoted) == 1:
        best_score, best_document = 0.95, exact_quoted[0][1]
    elif len(exact_quoted) > 1:
        # 多个文档都精确包含被引用标题时，才视为歧义
        logger.info("引用文档识别存在多个精确标题匹配，不认定")
        return None
    else:
        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best_document = scored[0]
        if best_score < 0.6:
            return None
        if len(scored) > 1:
            second_score = scored[1][0]
            if best_score - second_score < 0.15:
                logger.info(
                    "引用文档识别存在歧义（%.2f vs %.2f），不认定",
                    best_score,
                    second_score,
                )
                return None

    doc_text = _doc_identity_text(
        str(best_document["title"]), str(best_document["filename"] or "")
    )
    doc_ids = extract_ids(doc_text)
    if not doc_ids["arxiv_ids"]:
        doc_ids = extract_ids(best_document["content"][:2000])
    chunk_count = int(
        db.fetch_value(
            conn,
            "SELECT COUNT(*) FROM chunks WHERE doc_id = ?",
            (best_document["id"],),
            0,
        )
        or 0
    )
    result = dict(best_document)
    result.update(
        {
            "doc_id": best_document["id"],
            "chunk_count": chunk_count,
            "arxiv_id": doc_ids["arxiv_ids"][0] if doc_ids["arxiv_ids"] else None,
            "doi": doc_ids["dois"][0] if doc_ids["dois"] else None,
            "match_score": round(best_score, 3),
        }
    )
    logger.info(
        "识别到引用文档：%s（score=%.2f）", result["title"], best_score
    )
    return result


# ---------------------------------------------------------------------------
# 摘要意图 / 文档级上下文
# ---------------------------------------------------------------------------


def is_summary_intent(message: str, reference_doc: dict[str, object] | None = None) -> bool:
    lowered = str(message or "").lower()
    if any(keyword in lowered for keyword in SUMMARY_KEYWORDS):
        return True
    if not reference_doc:
        return False
    # 只引用了文档、没有具体问题 → 视为摘要
    cleaned = str(message or "")
    for mention in _quoted_mentions(cleaned) + _long_tokens(cleaned):
        cleaned = cleaned.replace(mention, " ")
    for arxiv_id in extract_ids(cleaned)["arxiv_ids"]:
        cleaned = cleaned.replace(arxiv_id, " ")
    cleaned = INSTRUCTION_RE.sub(" ", cleaned)
    remain = "".join(ch for ch in cleaned if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return len(remain) < 4


def _pick_middle_paragraphs(middle: str, budget: int, count: int = 10) -> str:
    if budget <= 0 or not middle.strip():
        return ""
    paragraphs = [part.strip() for part in re.split(r"\n{2,}|\n(?=#)", middle) if part.strip()]
    if not paragraphs:
        return ""
    picked: list[str] = []
    step = max(1, len(paragraphs) // count)
    used = 0
    for index in range(0, len(paragraphs), step):
        paragraph = paragraphs[index]
        if used + len(paragraph) + 2 > budget:
            break
        picked.append(paragraph)
        used += len(paragraph) + 2
        if len(picked) >= count:
            break
    return "\n\n".join(picked)


def build_document_context_text(
    content: str, max_chars: int = DOCUMENT_CONTEXT_MAX_CHARS
) -> str:
    """文档级上下文：短文全量；超长时取前 60% + 中间代表性段落 + 后 20%。"""
    text = str(content or "")
    if len(text) <= max_chars:
        return text
    head_size = int(max_chars * 0.6)
    tail_size = int(max_chars * 0.2)
    middle_budget = max(0, max_chars - head_size - tail_size)
    head = text[:head_size]
    tail = text[-tail_size:] if tail_size else ""
    middle = text[head_size : len(text) - tail_size] if tail_size else text[head_size:]
    picked_middle = _pick_middle_paragraphs(middle, middle_budget)
    parts = [head, "…（中间部分略）…", picked_middle, "…（末尾略）…", tail]
    return "\n\n".join(part for part in parts if part.strip())[:max_chars]


def build_local_document_summary(title: str, content: str) -> str:
    """本地检索模式下引用文档的抽取式摘要（不输出「资料有限」）。"""
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[。！？!?；;])", text) if sentence.strip()]
    lines = [f"《{title}》要点整理：", ""]
    if not sentences:
        lines.append("- 文档内容为空或暂不可读。")
    else:
        picked: list[str] = []
        # 优先摘要/方法/实验/结论相关句子
        for key in ("摘要", "方法", "实验", "结果", "结论", "贡献", "本文"):
            for sentence in sentences[:80]:
                if key in sentence and sentence not in picked and len(picked) < 5:
                    picked.append(sentence)
        for sentence in sentences:
            if len(picked) >= 5:
                break
            if sentence not in picked and len(sentence) >= 20:
                picked.append(sentence)
        for index, sentence in enumerate(picked[:5], start=1):
            lines.append(f"- {sentence[:180]}")
    lines.extend(["", "以上内容基于知识库原文整理；配置大模型后可获得更连贯的总结。"])
    return "\n".join(lines)
