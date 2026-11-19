"""混合检索 V3：文档级两阶段 + 查询意图路由 + 多查询合并。

流程（``search_chunks`` 公开签名不变）：
1. 意图路由（``query_intent.classify_query``）→ 扩展查询（≤3，确定性、不调 LLM）；
2. 每个 query 变体做 BM25 + 本地向量 cosine → RRF 融合，候选池放宽到约 80 chunk
   （命中 rare term 的候选即使词覆盖低也保留）；
3. 文档级打分：``max(chunk_fusion) + 0.35*mean(top2) + title/tag/summary IDF 命中
   + rare-term 命中 + exact phrase``；多查询按 ``0.7*max + 0.3*mean`` 聚合；
4. 按意图选文档（fact 6/2、multi 8/4、compare 8/4、set 12/5），chunk 按文档轮转配额，
   每文档默认 ≤2 个；最终结果数仍受 top_k 限制；
5. 输出字段与旧版完全一致（chunk_id/doc_id/doc_title/text/snippet/score/source_type）。
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict

import numpy as np

from .. import db
from ..utils import collapse_whitespace, make_snippet, parse_tags_json, round_score
from . import embedding, query_intent

logger = logging.getLogger("synapse.retrieval")

_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?；;\n])")


def relevant_snippet(query: str, text: str, limit: int = 280) -> str:
    """摘出与问题最相关的一段原文，而不是永远截开头。

    旧实现直接取前 280 字，导致「手冲咖啡」问题的引用里全是预算表格；
    这里按句子级 query 词命中挑中心句，再向前后扩展到 limit。
    """
    compact = collapse_whitespace(text)
    if len(compact) <= limit:
        return compact
    sentences = [
        sentence.strip()
        for sentence in _SENTENCE_BOUNDARY.split(compact)
        if sentence.strip()
    ]
    if not sentences:
        return make_snippet(compact, limit)
    terms = set(embedding.tokenize_terms(query))
    if terms:
        scores = []
        for index, sentence in enumerate(sentences):
            overlap = len(terms & set(embedding.tokenize_terms(sentence)))
            scores.append((overlap, -index))
        best = max(range(len(sentences)), key=lambda index: scores[index])
    else:
        best = 0

    chosen = [sentences[best]]
    if len(chosen[0]) > limit:
        return make_snippet(sentences[best], limit)
    total = len(chosen[0])
    # 只向后扩展：让引用片段从「命中问题的句子」开始，避免一上来又是预算/目录。
    cursor = best + 1
    while cursor < len(sentences) and total + len(sentences[cursor]) <= limit:
        chosen.append(sentences[cursor])
        total += len(sentences[cursor])
        cursor += 1
    snippet = "".join(chosen).strip()
    return snippet if snippet else make_snippet(compact, limit)


BM25_K1 = 1.5
BM25_B = 0.75
RRF_K = 60
MAX_CHUNKS_PER_DOC = 2
DEFAULT_TOP_K = 6
MAX_TOP_K = 12

#: 无词面命中时，向量相似度低于该值视为无关结果（本地哈希向量噪声较大）
DENSE_MIN_SCORE = 0.18
#: 查询词 IDF 加权覆盖率低于该阈值，认为问题与知识库无关（rare term 命中可豁免）
MIN_TERM_COVERAGE = 0.2
#: 覆盖率计算时 IDF 上限：语料中不存在的查询词（df=0）不应独占权重
COVERAGE_IDF_CAP = 3.0

#: 候选 chunk 池大小（文档级打分前）
CANDIDATE_POOL = 80
#: rare term：IDF 高于该值的查询词即使词覆盖低也纳入候选，并给文档加分
RARE_IDF_THRESHOLD = 2.2
RARE_TERM_BONUS = 0.12
#: 文档级打分权重
TOP2_MEAN_WEIGHT = 0.35
TITLE_TERM_WEIGHT = 0.20
TAG_TERM_WEIGHT = 0.15
SUMMARY_TERM_WEIGHT = 0.08
TITLE_PHRASE_BONUS = 0.15
TAG_PHRASE_BONUS = 0.08
#: 多查询文档分聚合
MULTI_QUERY_MAX_WEIGHT = 0.7
#: 文档分相对下限（避免把明显不相关的文档塞满结果）
DOC_SCORE_FLOOR = 0.28

#: 不同意图的文档数上限 / 下限
DOC_LIMITS = {"fact": 6, "multi": 8, "compare": 8, "set": 12}
MIN_DOCS = {"fact": 2, "multi": 4, "compare": 4, "set": 5}

_WS_RE = re.compile(r"\s+")
_COMPACT_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def clamp_top_k(top_k: int | None) -> int:
    try:
        value = int(top_k) if top_k is not None else DEFAULT_TOP_K
    except (TypeError, ValueError):
        value = DEFAULT_TOP_K
    return max(1, min(value, MAX_TOP_K))


def _compact(text: object) -> str:
    return _COMPACT_RE.sub("", str(text or "").lower())


def _load_chunks(conn, doc_ids: list[str] | None) -> list[dict[str, object]]:
    sql = (
        "SELECT c.id AS chunk_id, c.doc_id, c.text, c.embedding, "
        "d.title AS doc_title, d.source_type "
        "FROM chunks c JOIN documents d ON d.id = c.doc_id"
    )
    params: list[object] = []
    if doc_ids:
        placeholders = ",".join("?" for _ in doc_ids)
        sql += f" WHERE c.doc_id IN ({placeholders})"
        params.extend(doc_ids)
    sql += " ORDER BY c.doc_id, c.idx"
    rows = db.fetch_all(conn, sql, params)
    return [
        {
            "chunk_id": str(row["chunk_id"]),
            "doc_id": str(row["doc_id"]),
            "text": str(row["text"]),
            "embedding": row["embedding"],
            "doc_title": str(row["doc_title"]),
            "source_type": str(row["source_type"]),
        }
        for row in rows
    ]


def _load_document_meta(
    conn, doc_ids: list[str] | None
) -> dict[str, dict[str, object]]:
    sql = "SELECT id, title, tags, summary FROM documents"
    params: list[object] = []
    if doc_ids:
        placeholders = ",".join("?" for _ in doc_ids)
        sql += f" WHERE id IN ({placeholders})"
        params.extend(doc_ids)
    rows = db.fetch_all(conn, sql, params)
    meta: dict[str, dict[str, object]] = {}
    for row in rows:
        title = str(row["title"] or "")
        tags = parse_tags_json(row["tags"])
        summary = str(row["summary"] or "")[:200]
        meta[str(row["id"])] = {
            "title": title,
            "tags": tags,
            "summary": summary,
            "title_terms": set(embedding.tokenize_terms(title)),
            "tag_terms": set(
                term for tag in tags for term in embedding.tokenize_terms(tag)
            ),
            "summary_terms": set(embedding.tokenize_terms(summary)),
            "compact_title": _compact(title),
            "compact_tags": [_compact(tag) for tag in tags],
        }
    return meta


def _bm25_scores(
    query_terms: list[str],
    token_lists: list[list[str]],
    doc_freq: Counter[str],
    avgdl: float,
) -> list[float]:
    count = len(token_lists)
    if count == 0:
        return []
    query_counts = Counter(query_terms)
    if avgdl <= 0:
        avgdl = 1.0
    scores: list[float] = []
    for tokens in token_lists:
        term_freq = Counter(tokens)
        dl = len(tokens)
        score = 0.0
        for term, query_tf in query_counts.items():
            tf = term_freq.get(term)
            if not tf:
                continue
            df = doc_freq.get(term, 0)
            idf = math.log(1.0 + (count - df + 0.5) / (df + 0.5))
            norm = BM25_K1 * (1.0 - BM25_B + BM25_B * dl / avgdl)
            score += query_tf * idf * (tf * (BM25_K1 + 1.0)) / (tf + norm)
        scores.append(score)
    return scores


def _dense_scores(query: str, chunks: list[dict[str, object]]) -> np.ndarray:
    query_vector = embedding.embed_text(query)
    chunk_vectors = [embedding.unpack_vector(chunk["embedding"]) for chunk in chunks]
    dense = np.zeros(len(chunks), dtype=np.float32)
    usable = [
        index for index, vector in enumerate(chunk_vectors) if vector.size == query_vector.size
    ]
    if usable and query_vector.size > 0:
        matrix = np.vstack([chunk_vectors[index] for index in usable])
        scores = embedding.cosine_matrix(query_vector, matrix).tolist()
        for position, index in enumerate(usable):
            dense[index] = float(scores[position])
    return dense


def _ranks(scores: list[float]) -> dict[int, int]:
    order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    return {index: rank for rank, index in enumerate(order, start=1)}


def _term_idf(term: str, doc_freq: Counter[str], count: int) -> float:
    df = doc_freq.get(term, 0)
    return math.log(1.0 + (count - df + 0.5) / (df + 0.5))


def _coverage_idf(term: str, doc_freq: Counter[str], count: int) -> float:
    """覆盖率口径的 IDF：对 df=0 的未登录词设上限，避免其独占权重。"""
    return min(COVERAGE_IDF_CAP, _term_idf(term, doc_freq, count))


def _phrase_bonus(query: str, text: str, coverage: float, total_terms: int) -> float:
    """命中原句 / 短语（按 IDF 加权词覆盖）的加成，范围 0.05–0.15。"""
    compact_query = _WS_RE.sub("", query).lower()
    compact_text = _WS_RE.sub("", text).lower()
    if compact_query and compact_query in compact_text:
        return 0.15
    if total_terms and coverage >= 0.99:
        return 0.15
    if coverage >= 0.6:
        return 0.08
    if coverage >= 0.35:
        return 0.05
    return 0.0


def _title_bonus(title_coverage: float) -> float:
    if title_coverage <= 0:
        return 0.0
    return min(0.12, 0.6 * title_coverage)


def _normalize_scores(raw_scores: list[float]) -> list[float]:
    if not raw_scores:
        return []
    maximum = max(raw_scores)
    if maximum <= 0:
        return [0.0 for _ in raw_scores]
    return [round_score(0.35 + 0.63 * (score / maximum)) for score in raw_scores]


# ---------------------------------------------------------------------------
# 单 query 变体打分
# ---------------------------------------------------------------------------


def _variant_candidates(
    variant: str,
    chunks: list[dict[str, object]],
    token_lists: list[list[str]],
    doc_freq: Counter[str],
    count: int,
    avgdl: float,
    doc_meta: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    """一个 query 变体的候选 chunk（带 fusion）与每文档聚合所需信息。"""
    variant_terms = embedding.tokenize_terms(variant)
    if not variant_terms:
        return []
    term_set = set(variant_terms)
    total_idf = sum(_coverage_idf(term, doc_freq, count) for term in term_set) or 1.0

    bm25 = _bm25_scores(variant_terms, token_lists, doc_freq, avgdl)
    dense = _dense_scores(variant, chunks)
    bm25_ranks = _ranks(bm25)
    dense_ranks = _ranks(dense)

    candidates: list[dict[str, object]] = []
    for index, chunk in enumerate(chunks):
        dense_score = float(dense[index])
        bm25_score = float(bm25[index])
        matched_terms = term_set & set(token_lists[index])
        matched_idf = sum(_coverage_idf(term, doc_freq, count) for term in matched_terms)
        coverage = matched_idf / total_idf
        rare_terms = {
            term
            for term in matched_terms
            if _term_idf(term, doc_freq, count) >= RARE_IDF_THRESHOLD
        }
        if coverage < MIN_TERM_COVERAGE and not rare_terms:
            continue
        if bm25_score <= 0 and dense_score < DENSE_MIN_SCORE:
            continue
        meta = doc_meta.get(str(chunk["doc_id"])) or {}
        title_terms = meta.get("title_terms") or set()
        title_coverage = (
            sum(
                _coverage_idf(term, doc_freq, count)
                for term in (term_set & title_terms)  # type: ignore[arg-type]
            )
            / total_idf
        )
        fusion = (
            1.0 / (RRF_K + bm25_ranks[index])
            + 1.0 / (RRF_K + dense_ranks[index])
            + _phrase_bonus(variant, str(chunk["text"]), coverage, len(term_set))
            + _title_bonus(title_coverage)
        )
        candidates.append(
            {
                "chunk_id": str(chunk["chunk_id"]),
                "doc_id": str(chunk["doc_id"]),
                "chunk": chunk,
                "index": index,
                "fusion": fusion,
                "coverage": coverage,
                "rare": bool(rare_terms),
                "rare_terms": rare_terms,
                "title_terms": title_terms,
                "term_set": term_set,
                "total_idf": total_idf,
                "variant": variant,
            }
        )
    candidates.sort(key=lambda item: (-float(item["fusion"]), str(item["chunk_id"])))
    return candidates[:CANDIDATE_POOL]


def _doc_score_for_variant(
    doc_candidates: list[dict[str, object]],
    meta: dict[str, dict[str, object]],
    doc_id: str,
    doc_freq: Counter[str],
    count: int,
) -> float:
    """文档级打分：max chunk fusion + 0.35*top2 均值 + title/tag/summary + rare/phrase。"""
    top = doc_candidates[:2]
    base = float(top[0]["fusion"])
    top2_mean = sum(float(item["fusion"]) for item in top) / len(top)
    score = base + TOP2_MEAN_WEIGHT * top2_mean

    term_set = top[0]["term_set"]  # type: ignore[index]
    total_idf = max(1e-6, float(top[0]["total_idf"]) or 1.0)
    doc_meta = meta.get(doc_id) or {}
    for key, weight in (
        ("title_terms", TITLE_TERM_WEIGHT),
        ("tag_terms", TAG_TERM_WEIGHT),
        ("summary_terms", SUMMARY_TERM_WEIGHT),
    ):
        terms = doc_meta.get(key) or set()
        hit = sum(
            _coverage_idf(term, doc_freq, count)
            for term in (term_set & terms)  # type: ignore[arg-type]
        )
        if hit:
            score += weight * min(1.0, hit / total_idf)

    variant = str(top[0].get("variant") or "")
    compact_variant = _compact(variant)
    if compact_variant:
        compact_title = str(doc_meta.get("compact_title") or "")
        if compact_title and compact_variant in compact_title:
            score += TITLE_PHRASE_BONUS
        for compact_tag in doc_meta.get("compact_tags") or []:
            if compact_tag and compact_variant in str(compact_tag):
                score += TAG_PHRASE_BONUS
                break
    if any(bool(item.get("rare")) for item in doc_candidates):
        score += RARE_TERM_BONUS
    return score


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def search_chunks(
    conn,
    query: str,
    top_k: int | None = None,
    doc_ids: list[str] | None = None,
    max_per_doc: int | None = MAX_CHUNKS_PER_DOC,
) -> list[dict[str, object]]:
    query = (query or "").strip()
    if not query:
        return []
    chunks = _load_chunks(conn, doc_ids)
    if not chunks:
        return []

    limit = clamp_top_k(top_k)
    if max_per_doc is None:
        per_doc_limit = MAX_CHUNKS_PER_DOC
    else:
        per_doc_limit = max(1, min(int(max_per_doc), MAX_TOP_K))

    mode = query_intent.classify_query(query)
    expanded_queries = query_intent.expand_queries(query, mode)

    token_lists = [embedding.chunk_terms(chunk["text"]) for chunk in chunks]
    doc_freq: Counter[str] = Counter()
    for tokens in token_lists:
        doc_freq.update(set(tokens))
    count = len(token_lists)
    avgdl = sum(len(tokens) for tokens in token_lists) / count
    doc_meta = _load_document_meta(conn, doc_ids)

    chunk_best: dict[str, dict[str, object]] = {}
    doc_query_scores: dict[str, dict[int, float]] = defaultdict(dict)
    for variant_index, variant in enumerate(expanded_queries):
        candidates = _variant_candidates(
            variant, chunks, token_lists, doc_freq, count, avgdl, doc_meta
        )
        if not candidates:
            continue
        per_doc: dict[str, list[dict[str, object]]] = defaultdict(list)
        for candidate in candidates:
            per_doc[str(candidate["doc_id"])].append(candidate)
            chunk_id = str(candidate["chunk_id"])
            existing = chunk_best.get(chunk_id)
            if existing is None:
                chunk_best[chunk_id] = {
                    **candidate,
                    "matched_queries": {variant_index},
                    "rare_terms": set(candidate["rare_terms"]),  # type: ignore[arg-type]
                }
            else:
                existing["matched_queries"].add(variant_index)  # type: ignore[union-attr]
                if candidate["rare"]:
                    existing["rare"] = True
                    existing["rare_terms"].update(candidate["rare_terms"])  # type: ignore[union-attr]
                if float(candidate["fusion"]) > float(existing["fusion"]):
                    existing["fusion"] = candidate["fusion"]
        for doc_id, doc_candidates in per_doc.items():
            doc_candidates.sort(key=lambda item: (-float(item["fusion"]), str(item["chunk_id"])))
            doc_query_scores[doc_id][variant_index] = _doc_score_for_variant(
                doc_candidates, doc_meta, doc_id, doc_freq, count
            )

    if not chunk_best:
        return []

    # 多查询文档分聚合：0.7*max + 0.3*mean
    doc_scores: dict[str, float] = {}
    for doc_id, scores in doc_query_scores.items():
        values = list(scores.values())
        if not values:
            continue
        average = sum(values) / len(values)
        doc_scores[doc_id] = (
            MULTI_QUERY_MAX_WEIGHT * max(values) + (1 - MULTI_QUERY_MAX_WEIGHT) * average
        )
    if not doc_scores:
        return []

    ranked_docs = sorted(doc_scores, key=lambda doc_id: (-doc_scores[doc_id], doc_id))
    doc_limit = DOC_LIMITS.get(mode, DOC_LIMITS["fact"])
    min_docs = MIN_DOCS.get(mode, MIN_DOCS["fact"])
    top_score = doc_scores[ranked_docs[0]]
    floor = top_score * DOC_SCORE_FLOOR
    selected_docs = [doc_id for doc_id in ranked_docs if doc_scores[doc_id] >= floor][
        :doc_limit
    ]
    if not selected_docs:
        selected_docs = ranked_docs[:1]
    # 至少 min_docs（在存在候选的文档范围内）
    required = min(min_docs, len(ranked_docs))
    if len(selected_docs) < required:
        for doc_id in ranked_docs:
            if doc_id not in selected_docs:
                selected_docs.append(doc_id)
            if len(selected_docs) >= required:
                break
    doc_order = {doc_id: index for index, doc_id in enumerate(selected_docs)}

    # 每个文档内的候选 chunk（按融合分排序）
    doc_chunks: dict[str, list[dict[str, object]]] = defaultdict(list)
    for candidate in chunk_best.values():
        doc_id = str(candidate["doc_id"])
        if doc_id in doc_order:
            doc_chunks[doc_id].append(candidate)
    for doc_id in doc_chunks:
        doc_chunks[doc_id].sort(
            key=lambda item: (-float(item["fusion"]), str(item["chunk_id"]))
        )

    # chunk 选择：先按文档轮转第一个 chunk（保证覆盖），再补第二/第三，最后按分数补足
    selected_chunks: list[dict[str, object]] = []
    used_per_doc: Counter[str] = Counter()
    for doc_id in selected_docs:
        if len(selected_chunks) >= limit:
            break
        if not doc_chunks.get(doc_id):
            continue
        selected_chunks.append(doc_chunks[doc_id][0])
        used_per_doc[doc_id] += 1
    while len(selected_chunks) < limit:
        added = False
        for doc_id in selected_docs:
            if len(selected_chunks) >= limit:
                break
            if used_per_doc[doc_id] >= per_doc_limit:
                continue
            chunks_for_doc = doc_chunks.get(doc_id) or []
            if used_per_doc[doc_id] >= len(chunks_for_doc):
                continue
            selected_chunks.append(chunks_for_doc[used_per_doc[doc_id]])
            used_per_doc[doc_id] += 1
            added = True
        if not added:
            break
    if len(selected_chunks) < limit:
        chosen_ids = {str(item["chunk_id"]) for item in selected_chunks}
        remaining = [
            item
            for item in chunk_best.values()
            if str(item["chunk_id"]) not in chosen_ids
        ]
        remaining.sort(key=lambda item: (-float(item["fusion"]), str(item["chunk_id"])))
        for item in remaining:
            if len(selected_chunks) >= limit:
                break
            doc_id = str(item["doc_id"])
            if used_per_doc[doc_id] >= per_doc_limit:
                continue
            selected_chunks.append(item)
            used_per_doc[doc_id] += 1

    # 结果按融合分降序（保证 score 单调递减、引用编号与相关度一致）
    selected_chunks.sort(key=lambda item: (-float(item["fusion"]), str(item["chunk_id"])))
    normalized = _normalize_scores([float(item["fusion"]) for item in selected_chunks])
    output: list[dict[str, object]] = []
    for candidate, score in zip(selected_chunks, normalized):
        chunk = candidate["chunk"]
        output.append(
            {
                "chunk_id": str(chunk["chunk_id"]),  # type: ignore[index]
                "doc_id": str(chunk["doc_id"]),  # type: ignore[index]
                "doc_title": str(chunk["doc_title"]),  # type: ignore[index]
                "text": str(chunk["text"]),  # type: ignore[index]
                "snippet": relevant_snippet(query, str(chunk["text"])),  # type: ignore[index]
                "score": score,
                "source_type": str(chunk["source_type"]),  # type: ignore[index]
            }
        )
    return output


def search(
    query: str,
    top_k: int | None = DEFAULT_TOP_K,
    doc_ids: list[str] | None = None,
) -> dict[str, object]:
    """检索入口：``{query, mode, expanded_queries, doc_count, results}``。"""
    mode = query_intent.classify_query(query)
    expanded_queries = query_intent.expand_queries(query, mode)
    with db.get_db() as conn:
        results = search_chunks(conn, query, top_k, doc_ids)
    return {
        "query": query,
        "mode": mode,
        "expanded_queries": expanded_queries,
        "doc_count": len({str(result["doc_id"]) for result in results}),
        "results": results,
    }


def _relevance_label(score: float | None, top: float) -> str:
    """把「归一化分数」翻译成可解释的档位，避免把 0.98 当成 98% 置信度。"""
    if score is None:
        return "document"
    if top <= 0:
        return "low"
    ratio = score / top
    if ratio >= 0.85:
        return "high"
    if ratio >= 0.6:
        return "medium"
    return "low"


def to_citations(results: list[dict[str, object]]) -> list[dict[str, object]]:
    """把检索结果转成引用列表（编号 1-based，与 LLM 资料顺序一致）。

    新增 ``rank`` / ``relevance`` / ``kind``：
    - ``score`` 是 RRF 融合后的归一化分（Top-1 恒为 0.98），**不是概率**；
    - 前端应展示 ``relevance``（high/medium/low/document）而不是百分比；
    - 文档级全文引用 ``kind == "document"``，没有 chunk 级相关度。
    """
    top = 0.0
    for result in results:
        value = result.get("score")
        if value is not None:
            top = max(top, float(value))
    citations: list[dict[str, object]] = []
    for index, result in enumerate(results, start=1):
        raw_score = result.get("score")
        score = None if raw_score is None else round_score(float(raw_score))
        kind = str(result.get("kind") or "chunk")
        citations.append(
            {
                "index": index,
                "doc_id": str(result["doc_id"]),
                "doc_title": str(result["doc_title"]),
                "chunk_id": str(result["chunk_id"]),
                "source_type": str(result["source_type"]),
                "snippet": str(result.get("snippet") or make_snippet(str(result["text"]))),
                "score": score,
                "rank": index,
                "relevance": str(result.get("relevance") or _relevance_label(score, top)),
                "kind": kind,
            }
        )
    return citations
