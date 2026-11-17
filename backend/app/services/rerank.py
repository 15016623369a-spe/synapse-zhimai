"""反馈驱动的规则化 Reranker（轻量个性化排序）。

用户对「联网知识扩展」候选的采纳 / 忽略记录在 ``knowledge_feedback``：
- 采纳 → 候选 URL 入库为 ``source_type='web'`` 文档；
- 忽略 → 记录 rejected URL。

本模块把这些反馈变成**可解释的规则化偏好**（不训练模型、不删除用户反馈）：

- 按 host 汇总 accepted/rejected，平滑偏好 ``(acc+1)/(acc+rej+2)``（偏置 0.5）；
- 精确 URL 命中：accepted +1 / rejected -1；
- 同 host 偏好：``(pref-0.5)*0.8``（样本 < 2 时减半）；
- 文本 token（title+snippet，``embedding.tokenize_terms``）与 accepted / rejected
  词集的 Jaccard 重叠，分别 ×0.35 / ×(-0.25)。

最终排序分 ``0.60*relevance + 0.15*authority + 0.25*((feedback+1)/2)``：
没有反馈时反馈项恒为 0.5，排序仍以原本的 relevance / authority 为主。
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from .. import db
from . import embedding, search

logger = logging.getLogger("synapse.rerank")

FEEDBACK_ACCEPTED = "accepted"
FEEDBACK_REJECTED = "rejected"

#: 排序权重（保持相关度主权重）
WEIGHT_RELEVANCE = 0.60
WEIGHT_AUTHORITY = 0.15
WEIGHT_FEEDBACK = 0.25

#: 同 host 偏好强度 / 低样本减弱 / 文本重叠权重
DOMAIN_BIAS = 0.8
MIN_DOMAIN_SAMPLES = 2
LOW_SAMPLE_FACTOR = 0.5
TERM_ACCEPT_BOOST = 0.35
TERM_REJECT_PENALTY = -0.25
MAX_DOMAINS = 10
MIN_OVERLAP = 0.05

SUMMARY_DESCRIPTION = "你忽略的域名会在后续外部资料中降权；采纳的来源会加分。"

LABEL_ACCEPTED_URL = "你采纳过该来源"
LABEL_REJECTED_URL = "你曾忽略该来源"
LABEL_ACCEPTED_SIMILAR = "你采纳过类似来源"
LABEL_REJECTED_SIMILAR = "你曾忽略过类似来源"


# ---------------------------------------------------------------------------
# URL / host 工具
# ---------------------------------------------------------------------------


def normalize_host(url: object) -> str:
    """从 URL 提取小写 host（去掉 ``www.`` 前缀）；非法 URL 返回空串。"""
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        host = (urlparse(text).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host


def host_key(url: object) -> str:
    """host 聚合键：同一域名的 ``www.`` 变体合并。"""
    host = normalize_host(url)
    if host.startswith("www."):
        host = host[4:]
    return host


def _normalize_feedback_url(url: object) -> str:
    """反馈 URL 的轻量规范化（scheme/host 小写、去 fragment、去尾斜杠）。"""
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return text
    if not parsed.scheme or not parsed.netloc:
        return text
    path = parsed.path.rstrip("/") or ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}" + (
        f"?{parsed.query}" if parsed.query else ""
    )


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


# ---------------------------------------------------------------------------
# 偏好画像
# ---------------------------------------------------------------------------


def _feedback_text(row) -> str:
    """反馈行可用的文本（title + query；表里没有 snippet 字段）。"""
    return " ".join(
        str(row[column] or "")
        for column in ("title", "query")
        if column in row.keys()
    )


def load_profile(conn) -> dict[str, object]:
    """读取全部反馈，构建 host / URL / term 偏好画像。

    - ``domains``：按样本数降序（最多 10 个），含平滑偏好分；
    - ``domain_map``：host → 偏好分，供排序快速查表；
    - ``accepted_urls`` / ``rejected_urls``：规范化 URL 集合；
    - ``accepted_terms`` / ``rejected_terms``：title+query 的 token 集合。
    """
    rows = db.fetch_all(
        conn,
        "SELECT decision, url, title, query, doc_id, created_at FROM knowledge_feedback "
        "ORDER BY created_at ASC",
    )
    accepted = 0
    rejected = 0
    domain_counts: dict[str, list[int]] = {}
    accepted_urls: set[str] = set()
    rejected_urls: set[str] = set()
    accepted_terms: set[str] = set()
    rejected_terms: set[str] = set()

    for row in rows:
        decision = str(row["decision"] or "").strip().lower()
        if decision not in {FEEDBACK_ACCEPTED, FEEDBACK_REJECTED}:
            continue
        url = _normalize_feedback_url(row["url"])
        host = host_key(row["url"])
        is_accepted = decision == FEEDBACK_ACCEPTED
        if is_accepted:
            accepted += 1
            if url:
                accepted_urls.add(url)
        else:
            rejected += 1
            if url:
                rejected_urls.add(url)
        if host:
            bucket = domain_counts.setdefault(host, [0, 0])
            bucket[0 if is_accepted else 1] += 1
        terms = {
            token.lower()
            for token in embedding.tokenize_terms(_feedback_text(row))
            if token.strip()
        }
        if terms:
            (accepted_terms if is_accepted else rejected_terms).update(terms)

    domains: list[dict[str, object]] = []
    domain_map: dict[str, dict[str, float]] = {}
    for host, (acc, rej) in domain_counts.items():
        score = (acc + 1) / (acc + rej + 2)
        domains.append(
            {
                "host": host,
                "accepted": acc,
                "rejected": rej,
                "samples": acc + rej,
                "score": round(score, 2),
            }
        )
        domain_map[host] = {
            "score": score,
            "accepted": float(acc),
            "rejected": float(rej),
            "samples": float(acc + rej),
        }
    domains.sort(key=lambda item: (-int(item["samples"]), -float(item["score"]), str(item["host"])))
    domains = domains[:MAX_DOMAINS]

    return {
        "accepted": accepted,
        "rejected": rejected,
        "total": accepted + rejected,
        "domains": domains,
        "domain_map": domain_map,
        "accepted_urls": accepted_urls,
        "rejected_urls": rejected_urls,
        "accepted_terms": accepted_terms,
        "rejected_terms": rejected_terms,
    }


# ---------------------------------------------------------------------------
# 反馈信号
# ---------------------------------------------------------------------------


def _domain_signal(host: str, profile: dict[str, object]) -> tuple[float, str]:
    """同 host 偏好信号：``(pref-0.5)*0.8``，样本 < 2 时减半。"""
    domain_map: dict[str, dict[str, float]] = profile["domain_map"]  # type: ignore[assignment]
    entry = domain_map.get(host)
    if not entry:
        return 0.0, ""
    contribution = (float(entry["score"]) - 0.5) * DOMAIN_BIAS
    if int(entry["samples"]) < MIN_DOMAIN_SAMPLES:
        contribution *= LOW_SAMPLE_FACTOR
    if contribution > 0.02:
        return contribution, LABEL_ACCEPTED_SIMILAR
    if contribution < -0.02:
        return contribution, LABEL_REJECTED_SIMILAR
    return contribution, ""


def _term_signal(
    title: object, snippet: object, profile: dict[str, object]
) -> tuple[float, str]:
    """与 accepted / rejected 词集的 Jaccard 重叠加分 / 降权。"""
    tokens = {
        token.lower()
        for token in embedding.tokenize_terms(f"{title or ''} {snippet or ''}")
        if token.strip()
    }
    if not tokens:
        return 0.0, ""
    accepted_terms: set[str] = profile["accepted_terms"]  # type: ignore[assignment]
    rejected_terms: set[str] = profile["rejected_terms"]  # type: ignore[assignment]
    accept_overlap = _jaccard(tokens, accepted_terms)
    reject_overlap = _jaccard(tokens, rejected_terms)
    score = 0.0
    if accept_overlap >= MIN_OVERLAP:
        score += accept_overlap * TERM_ACCEPT_BOOST
    if reject_overlap >= MIN_OVERLAP:
        score += reject_overlap * TERM_REJECT_PENALTY
    if score > 0.01:
        return score, LABEL_ACCEPTED_SIMILAR
    if score < -0.01:
        return score, LABEL_REJECTED_SIMILAR
    return score, ""


def feedback_signal(
    candidate: dict[str, object], profile: dict[str, object]
) -> tuple[float, str]:
    """单个候选的反馈分（-1..1）与中文解释标签。"""
    raw_url = str(candidate.get("url") or "")
    url = _normalize_feedback_url(raw_url)
    accepted_urls: set[str] = profile["accepted_urls"]  # type: ignore[assignment]
    rejected_urls: set[str] = profile["rejected_urls"]  # type: ignore[assignment]
    if url and url in rejected_urls:
        return -1.0, LABEL_REJECTED_URL
    if url and url in accepted_urls:
        return 1.0, LABEL_ACCEPTED_URL

    score = 0.0
    label = ""
    domain_score, domain_label = _domain_signal(
        host_key(raw_url), profile
    )
    if domain_score or domain_label:
        score += domain_score
        label = label or domain_label
    term_score, term_label = _term_signal(
        candidate.get("title"), candidate.get("snippet"), profile
    )
    if term_score or term_label:
        score += term_score
        label = label or term_label
    return _clamp(score), label


# ---------------------------------------------------------------------------
# 排序
# ---------------------------------------------------------------------------


def _authority_norm(candidate: dict[str, object]) -> float:
    """候选权威度归一值（0..1）；优先用既有 ``authority_score``。"""
    if candidate.get("authority_score") is not None:
        return _clamp(_safe_float(candidate.get("authority_score"), 0.5), 0.0, 1.0)
    category = str(
        candidate.get("authority_category")
        or candidate.get("authority")
        or "unknown"
    )
    return _clamp(search.authority_score(category), 0.0, 1.0)


def final_score(
    relevance: float, authority: float, feedback_score: float
) -> float:
    """最终排序分：相关度为主，反馈项在没有反馈时恒为 0.5。"""
    return (
        WEIGHT_RELEVANCE * relevance
        + WEIGHT_AUTHORITY * authority
        + WEIGHT_FEEDBACK * ((feedback_score + 1.0) / 2.0)
    )


def rerank_candidates(
    conn,
    candidates: list[dict[str, object]],
    question: str = "",
    reference_doc: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """给候选附加 ``feedback_score`` / ``feedback_label`` 并按最终分排序。

    - 候选应已完成 relevance / authority 打分与过滤；
    - 不丢弃候选（精确 rejected URL 由既有过滤逻辑排除）；
    - ``question`` / ``reference_doc`` 预留给后续个性化规则，当前排序不依赖它们。
    """
    if not candidates:
        return []
    profile = load_profile(conn)
    scored: list[dict[str, object]] = []
    for candidate in candidates:
        item = dict(candidate)
        relevance = _clamp(_safe_float(item.get("relevance")), 0.0, 1.0)
        authority = _authority_norm(item)
        feedback_score, label = feedback_signal(item, profile)
        item["feedback_score"] = round(_clamp(feedback_score), 2)
        item["feedback_label"] = label
        item["rerank_score"] = round(
            final_score(relevance, authority, _clamp(feedback_score)), 4
        )
        scored.append(item)

    scored.sort(
        key=lambda item: (
            -_safe_float(item.get("rerank_score")),
            -_safe_float(item.get("relevance")),
            _safe_float(item.get("feedback_score")),
            str(item.get("url") or ""),
        )
    )
    return scored


# ---------------------------------------------------------------------------
# 可解释 API
# ---------------------------------------------------------------------------


def summary(conn) -> dict[str, object]:
    """反馈偏好摘要：计数 + top domains + 说明（不修改/不删除反馈）。"""
    profile = load_profile(conn)
    domains = [
        {
            "host": str(item["host"]),
            "accepted": int(item["accepted"]),
            "rejected": int(item["rejected"]),
            "score": round(float(item["score"]), 2),
        }
        for item in profile["domains"]  # type: ignore[union-attr]
    ]
    return {
        "accepted": int(profile["accepted"]),
        "rejected": int(profile["rejected"]),
        "domains": domains,
        "description": SUMMARY_DESCRIPTION,
    }
