"""知识整理（AI Enrichment）：从文档正文提炼标题 / 摘要 / 关键词 / 标签。

- 有 LLM 时一次非流式 completion 输出 JSON；失败自动回退本地中英文 keyphrase；
- 只修改元数据（documents.title/summary/tags、concepts/doc_concepts）并重建图谱，
  **绝不修改 documents.content / chunks / embedding**；
- 关键词同时用于概念节点与 tag 节点（图谱关键词可视化）。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter

import jieba
import jieba.analyse

from .. import db
from ..utils import (
    collapse_whitespace,
    is_stopword,
    looks_like_academic_text,
    looks_like_filename_title,
    new_id,
    normalize_tags,
    now_iso,
    truncate,
)
from . import graph, llm

logger = logging.getLogger("synapse.enrich")

ENRICH_VERSION = 1
KEYWORD_MIN = 8
KEYWORD_MAX = 12
TAG_MAX = 5
SUMMARY_MAX = 160
CONTEXT_MAX_CHARS = 9000

#: 英文学术通用词 / 噪声词（不进入关键词）
ENGLISH_GENERIC = frozenset(
    {
        "paper", "papers", "method", "methods", "result", "results", "figure", "figures",
        "table", "tables", "reference", "references", "et", "al", "university",
        "institute", "department", "school", "email", "abstract", "experiment",
        "experiments", "approach", "approaches", "proposed", "propose", "using",
        "based", "data", "model", "models", "system", "systems", "where", "which",
        "these", "those", "their", "there", "however", "therefore", "section",
        "sections", "algorithm", "algorithms", "framework", "performance", "analysis",
        "show", "shows", "shown", "also", "such", "each", "more", "most", "much",
        "than", "then", "when", "with", "without", "from", "into", "over", "under",
        "this", "that", "have", "has", "been", "being", "will", "would", "can",
        "could", "should", "may", "might", "must", "not", "all", "any", "both",
        "new", "novel", "good", "better", "best", "high", "low", "large", "small",
        "first", "second", "third", "one", "two", "three", "four", "five", "fig",
        "eq", "equation", "equations", "ieee", "acm", "springer", "arxiv", "doi",
        "copyright", "author", "authors", "keywords", "index", "terms", "page",
        "pages", "volume", "vol", "issue", "conference", "workshop", "journal",
        "transactions", "proceedings", "international", "national", "challenge",
    }
)
_MATH_TOKEN_RE = re.compile(r"^[^a-zA-Z]*$")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_URL_RE = re.compile(r"https?://\S+")
_AUTHOR_LINE_RE = re.compile(
    r"(university|institute|department|school of|laborator|@|arxiv:|preprint)",
    re.IGNORECASE,
)

ENRICH_SYSTEM_PROMPT = (
    "你是「Synapse 知脉」的知识库整理助手。请只根据给出的文档内容输出 JSON："
    '{"title":"正式标题（仅当原标题像文件名时给出，否则空字符串）",'
    '"summary":"不超过 120 字的中文摘要，保留关键数字与结论",'
    '"keywords":["8–12 个具体关键词，中英文短语均可，如 multiobject tracking / belief propagation"],'
    '"tags":["3–5 个短标签，用于知识图谱连接"]}'
    "只输出 JSON，不要解释。"
)


# ---------------------------------------------------------------------------
# 文本处理
# ---------------------------------------------------------------------------


def _context_text(content: str) -> str:
    text = str(content or "")
    if len(text) <= CONTEXT_MAX_CHARS:
        return text
    head = text[:6000]
    middle_start = max(0, len(text) // 2 - 750)
    middle = text[middle_start : middle_start + 1500]
    tail = text[-1500:]
    return f"{head}\n\n…（中间略）…\n\n{middle}\n\n…（末尾略）…\n\n{tail}"


def _clean_summary(text: str, title: str = "") -> str:
    cleaned = _EMAIL_RE.sub(" ", str(text or ""))
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if title and cleaned.lower().startswith(title.lower()):
        cleaned = cleaned[len(title) :].strip(" .:—-")
    return truncate(cleaned, SUMMARY_MAX, suffix="")


def derive_title_from_text(content: str, filename: str | None = None) -> str:
    """从正文开头推断正式标题（仅用于标题像文件名的文档）。"""
    lines = [line.strip() for line in str(content or "").splitlines() if line.strip()]
    picked: list[str] = []
    for line in lines[:12]:
        lowered = line.lower()
        if _AUTHOR_LINE_RE.search(line) or lowered.startswith(("abstract", "摘要")):
            break
        if len(line) < 3:
            continue
        picked.append(line)
        if len(picked) >= 2 or sum(len(item) for item in picked) > 180:
            break
    if not picked:
        return ""
    candidate = collapse_whitespace(" ".join(picked))
    if len(candidate) < 6 or looks_like_filename_title(candidate):
        return ""
    return truncate(candidate, 180, suffix="")


def _english_candidates(text: str) -> list[tuple[str, float]]:
    words = [
        (match.group(0), match.start())
        for match in re.finditer(r"[A-Za-z][A-Za-z\-]*", text)
    ]
    def is_valid(word: str) -> bool:
        lowered = word.lower().strip("-")
        if len(lowered) < 3:
            return False
        if lowered in ENGLISH_GENERIC or is_stopword(lowered):
            return False
        if _MATH_TOKEN_RE.match(lowered):
            return False
        if not any(vowel in lowered for vowel in "aeiou"):
            # 允许纯大写缩写（如 RNN / LSTM），但排除噪声短词
            return word.isupper() and 2 <= len(word) <= 6
        return True

    valid = [(word, pos) for word, pos in words if is_valid(word)]
    scores: Counter[str] = Counter()
    for index, (word, position) in enumerate(valid):
        bonus = 1.5 if position < 3000 else 1.0
        scores[word.lower().strip("-")] += bonus
        for size in (2, 3):
            if index + size > len(valid):
                break
            window = valid[index : index + size]
            last_position = window[-1][1]
            first_position = window[0][1]
            span = text[first_position : last_position + len(window[-1][0])]
            if span.count(" ") > size + 1 or "  " in span:
                continue
            phrase_tokens = [item[0].lower().strip("-") for item in window]
            if any(token in ENGLISH_GENERIC for token in phrase_tokens):
                continue
            phrase = " ".join(phrase_tokens)
            scores[phrase] += (1.0 + 0.25 * (size - 1)) * bonus

    # 技术缩写 / 大写专名加权
    for word, _position in words:
        if word.isupper() and 2 <= len(word) <= 6 and word.lower() not in ENGLISH_GENERIC:
            scores[word] += 0.5

    ranked = sorted(
        scores.items(),
        key=lambda item: (
            -item[1],
            -len(item[0].split()),
            -len(item[0]),
            item[0],
        ),
    )
    return ranked


def _chinese_keywords(text: str) -> list[str]:
    try:
        tagged = jieba.analyse.extract_tags(text, topK=20, withWeight=True)
    except Exception:  # pragma: no cover - jieba 兜底
        tagged = []
    result: list[str] = []
    for word, _weight in tagged:
        label = str(word).strip()
        if not (2 <= len(label) <= 12) or is_stopword(label):
            continue
        if not re.search(r"[\u4e00-\u9fff]", label):
            continue
        if label.isdigit():
            continue
        if label not in result:
            result.append(label)
        if len(result) >= 8:
            break
    return result


def extract_local_keyphrases(
    content: str, title: str = "", *, limit: int = KEYWORD_MAX
) -> list[str]:
    """本地中英文 keyphrase 提取（无 LLM / LLM 失败时使用）。"""
    text = _context_text(content).replace("\n", " ")
    academic = looks_like_academic_text(content) or ".pdf" in str(title).lower()
    english = [phrase for phrase, _score in _english_candidates(text)]
    chinese = _chinese_keywords(text)

    ordered: list[str] = []
    if academic:
        ordered.extend(english[:10])
        ordered.extend(chinese[:4])
    else:
        ordered.extend(chinese[:8])
        ordered.extend(english[:6])

    result: list[str] = []
    seen: set[str] = set()
    for phrase in ordered:
        key = phrase.lower().strip()
        if not key or key in seen or len(key) < 2:
            continue
        seen.add(key)
        result.append(truncate(phrase, 60, suffix=""))
        if len(result) >= limit:
            break
    # 英文论文兜底：从标题/正文再补一些技术词
    if len(result) < KEYWORD_MIN and english:
        for phrase in english:
            key = phrase.lower()
            if key not in seen:
                seen.add(key)
                result.append(truncate(phrase, 60, suffix=""))
            if len(result) >= limit:
                break
    return result[:limit]


def build_local_summary(content: str, title: str = "") -> str:
    text = str(content or "")
    lowered = text.lower()
    marker = None
    for candidate in ("abstract", "摘要"):
        position = lowered.find(candidate)
        if position >= 0:
            marker = position + len(candidate)
            break
    if marker is not None:
        snippet = text[marker : marker + 600]
    else:
        plain = collapse_whitespace(text)
        snippet = plain
    return _clean_summary(snippet, title)


# ---------------------------------------------------------------------------
# LLM 整理
# ---------------------------------------------------------------------------


def _parse_llm_payload(content: str) -> dict[str, object] | None:
    match = re.search(r"\{[\s\S]*\}", content or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _clean_keywords(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        label = collapse_whitespace(str(item or "")).strip(" ，,;；")
        if not (2 <= len(label) <= 60):
            continue
        key = label.lower()
        if key in seen or label.isdigit():
            continue
        seen.add(key)
        result.append(label)
    return result


def _call_llm_enrich(doc: dict[str, object], context: str, llm_config: dict[str, object]):
    user_content = (
        f"原标题：{doc.get('title')}\n文件名：{doc.get('filename') or '无'}\n"
        f"来源类型：{doc.get('source_type')}\n现有标签：{doc.get('tags') or []}\n\n"
        f"文档内容（截取）：\n{context}"
    )
    messages = [
        {"role": "system", "content": ENRICH_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    payload = llm.chat_completion(
        llm_config, messages, max_tokens=700, temperature=0.1, timeout=60.0
    )
    return _parse_llm_payload(llm.extract_message_text(payload))


# ---------------------------------------------------------------------------
# 概念 / 元数据更新
# ---------------------------------------------------------------------------


def _replace_doc_concepts(conn, doc_id: str, keywords: list[str], now: str) -> None:
    conn.execute("DELETE FROM doc_concepts WHERE doc_id = ?", (doc_id,))
    maximum = max(1, len(keywords))
    for index, label in enumerate(keywords):
        norm = label.strip().lower()
        if not norm:
            continue
        row = db.fetch_one(conn, "SELECT id FROM concepts WHERE norm = ?", (norm,))
        if row is None:
            concept_id = new_id("cv")
            conn.execute(
                "INSERT INTO concepts(id, label, norm, kind, created_at) VALUES(?, ?, ?, 'concept', ?)",
                (concept_id, label, norm, now),
            )
        else:
            concept_id = str(row["id"])
        weight = round(1.0 - 0.6 * (index / maximum), 3)
        conn.execute(
            "INSERT OR REPLACE INTO doc_concepts(doc_id, concept_id, weight) VALUES(?, ?, ?)",
            (doc_id, concept_id, max(0.2, weight)),
        )


def _load_document(conn, doc_id: str) -> sqlite3.Row | None:
    return db.fetch_one(
        conn,
        "SELECT id, title, filename, source_type, summary, tags, content, "
        "enrich_version FROM documents WHERE id = ?",
        (doc_id,),
    )


def enrich_document(
    conn,
    doc_id: str,
    *,
    force: bool = False,
    llm_config: dict[str, object] | None = None,
) -> dict[str, object]:
    """整理单篇文档（元数据 + 概念）；不修改 content/chunks/embedding。"""
    row = _load_document(conn, doc_id)
    if row is None:
        return {"id": doc_id, "skipped": True, "reason": "文档不存在"}
    if int(row["enrich_version"] or 0) > 0 and not force:
        return {"id": doc_id, "skipped": True, "reason": "已整理"}

    doc = {
        "id": str(row["id"]),
        "title": str(row["title"] or ""),
        "filename": row["filename"],
        "source_type": str(row["source_type"] or ""),
        "summary": row["summary"],
        "tags": json.loads(row["tags"] or "[]"),
        "content": str(row["content"] or ""),
    }
    context = _context_text(doc["content"])
    local_keywords = extract_local_keyphrases(doc["content"], doc["title"])
    local_summary = build_local_summary(doc["content"], doc["title"])
    local_title = derive_title_from_text(doc["content"], doc["filename"])
    mode = "local"

    llm_data: dict[str, object] | None = None
    config = llm_config or llm.get_config()
    if llm.is_configured(config):
        try:
            llm_data = _call_llm_enrich(doc, context, config)
            if llm_data:
                mode = "llm"
        except llm.LLMError as exc:
            logger.info("LLM 整理失败，回退本地：%s", exc)
        except Exception:  # pragma: no cover - 兜底
            logger.exception("LLM 整理异常，回退本地")

    title = doc["title"]
    summary = local_summary
    keywords = local_keywords
    tags: list[str] = []
    if llm_data:
        llm_keywords = _clean_keywords(llm_data.get("keywords"))
        keywords = llm_keywords[:KEYWORD_MAX]
        if len(keywords) < KEYWORD_MIN:
            for candidate in local_keywords:
                if len(keywords) >= KEYWORD_MIN:
                    break
                if candidate.lower() not in {item.lower() for item in keywords}:
                    keywords.append(candidate)
        tags = _clean_keywords(llm_data.get("tags"))[:TAG_MAX]
        if len(tags) < 2:
            tags = keywords[:3]
        llm_summary = collapse_whitespace(str(llm_data.get("summary") or ""))
        if llm_summary:
            summary = truncate(llm_summary, SUMMARY_MAX, suffix="")
        llm_title = collapse_whitespace(str(llm_data.get("title") or ""))
        if (
            looks_like_filename_title(title)
            and llm_title
            and len(llm_title) >= 6
            and not looks_like_filename_title(llm_title)
        ):
            title = truncate(llm_title, 180, suffix="")
    else:
        tags = keywords[:4]
        if looks_like_filename_title(title) and local_title:
            title = local_title

    if not keywords:
        keywords = local_keywords or [truncate(title, 40, suffix="") or "文档"]
    if not tags:
        tags = keywords[:3]

    existing_tags = [str(item) for item in (doc["tags"] or [])]
    merged_tags = normalize_tags([*tags, *existing_tags])
    now = now_iso()
    conn.execute(
        "UPDATE documents SET title = ?, summary = ?, tags = ?, enrich_version = ?, "
        "updated_at = ? WHERE id = ?",
        (
            title,
            summary or doc["summary"],
            json.dumps(merged_tags, ensure_ascii=False),
            ENRICH_VERSION,
            now,
            doc_id,
        ),
    )
    _replace_doc_concepts(conn, doc_id, keywords, now)
    db.add_activity(
        conn, "enrich", f"整理文档《{truncate(title, 30, suffix='')}》：{len(keywords)} 个关键词"
    )
    logger.info("文档整理完成：%s（%s，%d 个关键词）", doc_id, mode, len(keywords))
    return {
        "id": doc_id,
        "skipped": False,
        "mode": mode,
        "title": title,
        "summary": summary,
        "tags": merged_tags,
        "keywords": keywords,
    }


# ---------------------------------------------------------------------------
# 批量整理 / 状态
# ---------------------------------------------------------------------------


def _target_rows(conn, scope: str, doc_ids: list[str] | None, force: bool) -> list:
    if scope == "doc_ids":
        if not doc_ids:
            return []
        placeholders = ",".join("?" for _ in doc_ids)
        sql = (
            "SELECT id, source_type, enrich_version FROM documents "
            f"WHERE id IN ({placeholders})"
        )
        sql += " ORDER BY created_at ASC"
        return db.fetch_all(conn, sql, list(doc_ids))
    clauses = []
    params: list[object] = []
    if scope == "pdf":
        clauses.append("source_type = 'pdf'")
    if not force:
        clauses.append("enrich_version = 0")
    sql = "SELECT id, source_type, enrich_version FROM documents"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at ASC"
    return db.fetch_all(conn, sql, params)


def enrich_status(conn) -> dict[str, object]:
    total = db.table_count(conn, "documents")
    enriched = int(
        db.fetch_value(
            conn, "SELECT COUNT(*) FROM documents WHERE enrich_version > 0", default=0
        )
        or 0
    )
    pending_pdf = int(
        db.fetch_value(
            conn,
            "SELECT COUNT(*) FROM documents WHERE source_type = 'pdf' AND enrich_version = 0",
            default=0,
        )
        or 0
    )
    pending_all = total - enriched
    sample = db.fetch_all(
        conn,
        "SELECT id, title, source_type FROM documents WHERE enrich_version = 0 "
        "ORDER BY created_at DESC LIMIT 5",
    )
    config = llm.get_config()
    configured = llm.is_configured(config)
    return {
        "llm_configured": configured,
        "can_enrich": True,
        "mode": "llm" if configured else "local",
        "total_documents": total,
        "enriched_documents": enriched,
        "pending_pdf": pending_pdf,
        "pending_all": pending_all,
        "sample_pending": [
            {"id": str(row["id"]), "title": str(row["title"]), "source_type": str(row["source_type"])}
            for row in sample
        ],
    }


def run_enrich(
    *,
    scope: str = "pdf",
    doc_ids: list[str] | None = None,
    force: bool = False,
    limit: int = 12,
    llm_config: dict[str, object] | None = None,
) -> dict[str, object]:
    """批量整理：逐篇独立事务，最后统一重建图谱一次。"""
    scope = str(scope or "pdf").strip().lower()
    if scope not in {"pdf", "all", "doc_ids"}:
        raise ValueError("scope 只能是 pdf、all 或 doc_ids")
    limit = max(1, min(int(limit or 12), 12))
    with db.get_db() as conn:
        targets = _target_rows(conn, scope, doc_ids, force)[:limit]
    config = llm_config or llm.get_config()
    processed = 0
    updated: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []
    for target in targets:
        doc_id = str(target["id"])
        try:
            with db.get_db() as conn:
                result = enrich_document(
                    conn, doc_id, force=force, llm_config=config
                )
        except Exception as exc:  # 单篇失败不影响其它
            logger.exception("文档整理失败：%s", doc_id)
            failed.append({"id": doc_id, "error": str(exc) or "整理失败"})
            continue
        if result.get("skipped"):
            skipped.append({"id": doc_id, "reason": str(result.get("reason") or "已整理")})
            continue
        processed += 1
        updated.append(
            {
                "id": doc_id,
                "mode": result.get("mode", "local"),
                "title": result.get("title"),
                "summary": result.get("summary"),
                "tags": result.get("tags"),
                "keywords": result.get("keywords"),
            }
        )
    graph_rebuilt = False
    if processed:
        with db.get_db() as conn:
            graph.rebuild(conn)
        graph_rebuilt = True
    return {
        "ok": True,
        "mode": "llm" if llm.is_configured(config) else "local",
        "processed": processed,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "graph_rebuilt": graph_rebuilt,
    }
