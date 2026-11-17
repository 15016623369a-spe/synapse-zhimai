"""查询意图路由与确定性查询扩展（不调用 LLM）。

- ``classify_query(question) -> "set" | "compare" | "multi" | "fact"``：
  - set：穷举 / 集合类问题（哪些、列出、所有、包含、出现过、多少篇…）；
  - compare：对比类问题（区别、差异、对比、相比、优缺点、vs…）；
  - multi：多子问题 / 关联类问题（并、同时、分别、各自、结合、关系…，或长问题含多个实体）；
  - fact：默认（单文档事实题）。
- ``expand_queries(question, mode=None) -> list[str]``：最多 3 条去重查询，**始终保留原问题**；
  compare 切分实体、multi 切分分句、set 抽取引号/集合术语，均为规则化实现，可直接进检索。

意图只用于选择 ``doc_limit`` / ``min_docs`` 与扩展查询，不改变 API 请求参数。
"""

from __future__ import annotations

import re

from ..utils import collapse_whitespace

MAX_EXPANDED_QUERIES = 3
MAX_QUERY_CHARS = 120

SET_KEYWORDS: tuple[str, ...] = (
    "哪些", "哪个", "列出", "所有", "全部", "包含", "出现过", "有哪些", "列举",
    "多少篇", "都有", "清单", "汇总", "涉及", "提到了", "提到",
)
COMPARE_KEYWORDS: tuple[str, ...] = (
    "区别", "差异", "对比", "相比", "优缺点", "异同", "哪个好", "vs", "比较",
    "不同", "差别",
)
MULTI_KEYWORDS: tuple[str, ...] = (
    "并", "同时", "以及", "分别", "各自", "结合", "关系", "联系", "分别",
)

_QUOTED_RE = re.compile(
    r"《([^》]{1,200})》|「([^」]{1,200})」|『([^』]{1,200})』|“([^”]{1,200})”|\"([^\"]{1,200})\""
)
_COMPARE_SPLIT_RE = re.compile(
    r"(?:和|与|跟|以及|vs\.?|对比|相比|比较|区别|差异|不同|差别)"
)
_MULTI_SPLIT_RE = re.compile(
    r"(?:并|同时|以及|分别|各自|结合|关系|联系)"
)
_LEADING_NOISE_RE = re.compile(
    r"^(?:请|请问|帮我|帮忙|麻烦|我想知道|我想问|想知道|请问一下|你好|嗯|那么|还有)"
)
_TRAILING_NOISE_RE = re.compile(r"[的了吗呢吧呀啊哦嘛？?！!。，,；;：:\s]+$")
_EN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.\-]*")
_GENERIC_TERMS = frozenset(
    {
        "什么", "怎么", "如何", "哪些", "哪个", "这个", "那个", "一下", "内容",
        "东西", "问题", "方面", "地方", "概念", "笔记", "文档", "知识库", "资料",
        "有没有", "请问", "帮我", "我", "你", "的", "了", "是", "有", "吗", "呢",
        "所有", "全部", "包含", "出现", "列出", "列举", "汇总",
    }
)


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _entity_count(question: str) -> int:
    """粗略实体数：英文实体词 + 中文「A和B」片段。"""
    english = {match.group(0).lower() for match in _EN_RE.finditer(question)}
    chinese_parts = [part for part in re.split(r"[、和与及/]", question) if len(part.strip()) >= 2]
    return len(english) + max(0, len(chinese_parts))


def classify_query(question: str) -> str:
    """确定性查询意图分类：set / compare / multi / fact。"""
    text = collapse_whitespace(str(question or ""))
    if not text:
        return "fact"
    # 对比词（区别/相比/对比…）比「哪个」等集合词更具体，优先判定 compare
    if _has_any(text, COMPARE_KEYWORDS):
        return "compare"
    if _has_any(text, SET_KEYWORDS):
        return "set"
    if _has_any(text, MULTI_KEYWORDS):
        return "multi"
    if len(text) > 40 and _entity_count(text) >= 2:
        return "multi"
    return "fact"


# ---------------------------------------------------------------------------
# 查询扩展
# ---------------------------------------------------------------------------


def _clean_clause(clause: str) -> str:
    text = collapse_whitespace(clause)
    text = _LEADING_NOISE_RE.sub("", text).strip()
    text = _TRAILING_NOISE_RE.sub("", text).strip()
    return text


def _quoted_terms(question: str) -> list[str]:
    terms: list[str] = []
    for match in _QUOTED_RE.finditer(question):
        term = next((group for group in match.groups() if group), "").strip()
        if term and term not in terms:
            terms.append(term)
    return terms


def _set_terms(question: str) -> list[str]:
    """set 模式：引号内术语 + 「哪些/包含/出现」后的关键词。"""
    quoted_terms = _quoted_terms(question)
    if quoted_terms:
        # 有引号术语时优先用它做扩展（如「预算」），不再拼出噪声短语
        return quoted_terms
    terms: list[str] = []
    text = _clean_clause(question)
    # 「哪些笔记里出现了『预算』这个概念」这类没有引号时，取集合词后的名词短语
    match = re.search(
        r"(?:哪些|哪个|列出|所有|全部|包含|出现过|有哪些|列举|涉及|提到|提到了)"
        r"([^，。？！?；;]{2,30})",
        text,
    )
    if match:
        tail = _clean_clause(match.group(1))
        tail = re.sub(r"(?:的|相关|方面|内容|笔记|文档|概念|东西)", "", tail).strip()
        if tail:
            terms.append(tail)
    # 兜底：剩余的高信息量中文/英文词
    if not terms:
        pieces = [
            piece
            for piece in re.split(r"[、，,。？?！!；;：:\s]+", text)
            if 2 <= len(piece) <= 12 and piece not in _GENERIC_TERMS
        ]
        if pieces:
            terms.append(" ".join(dict.fromkeys(pieces[:4])))
    return [term for term in dict.fromkeys(terms) if term]


def _compare_entities(question: str) -> list[str]:
    text = _clean_clause(question)
    text = re.sub(r"(?:的区别|的差异|的对比|相比|比较|有什么不同|有哪些不同).*$", "", text)
    parts = [
        _clean_clause(part)
        for part in _COMPARE_SPLIT_RE.split(text)
    ]
    entities = [
        part
        for part in parts
        if part and part not in _GENERIC_TERMS and len(part) >= 2
    ]
    # 去掉疑问词尾巴
    return [re.sub(r"(?:是|有|的)$", "", entity).strip() for entity in entities if entity][:3]


def _multi_segments(question: str) -> list[str]:
    text = _clean_clause(question)
    parts = [_clean_clause(part) for part in _MULTI_SPLIT_RE.split(text)]
    segments = [
        part
        for part in parts
        if part and len(part) >= 2 and part not in _GENERIC_TERMS
    ]
    return segments[:3]


def expand_queries(question: str, mode: str | None = None) -> list[str]:
    """最多 3 条确定性扩展查询；第一条始终是原问题，去重、截断到 120 字。"""
    original = collapse_whitespace(str(question or ""))
    if not original:
        return []
    resolved_mode = mode or classify_query(original)
    expansions: list[str] = []
    if resolved_mode == "compare":
        expansions.extend(_compare_entities(original))
    elif resolved_mode == "multi":
        expansions.extend(_multi_segments(original))
    elif resolved_mode == "set":
        expansions.extend(_set_terms(original))

    queries: list[str] = [original[:MAX_QUERY_CHARS]]
    for candidate in expansions:
        cleaned = collapse_whitespace(str(candidate or ""))[:MAX_QUERY_CHARS]
        if not cleaned or cleaned in queries:
            continue
        # 扩展词必须能贡献检索信号（至少一个非停止词字符）
        if not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", cleaned):
            continue
        queries.append(cleaned)
        if len(queries) >= MAX_EXPANDED_QUERIES:
            break
    return queries
