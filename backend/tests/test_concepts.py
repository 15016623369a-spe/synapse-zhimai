"""概念抽取质量测试（SPEC 5.3 + 图谱去噪打磨）。"""

from __future__ import annotations

from app.services import graph
from app.utils import STOPWORDS

from .conftest import DOC_VECTOR

GENERIC_WORDS = [
    "半年",
    "每周",
    "复盘",
    "格式",
    "模板",
    "任务",
    "工作",
    "记录",
    "结论",
    "提前",
    "人多",
    "分钟",
    "小时",
]


def test_extract_concepts_returns_contract_range() -> None:
    concepts = graph.extract_concepts(DOC_VECTOR)
    assert 8 <= len(concepts) <= 12
    norms = set()
    for concept in concepts:
        assert set(concept) == {"label", "norm", "weight"}
        assert str(concept["label"]).strip()
        assert 0.2 <= float(concept["weight"]) <= 1.0
        norm = str(concept["norm"])
        assert norm == norm.lower()
        assert norm not in norms
        norms.add(norm)


def test_extract_concepts_filters_generic_and_ordinal_words() -> None:
    text = (
        "半年时间里，我每周复盘一次，用了模板和格式记录任务，结论是提前规划很重要。"
        "很多人问工作记录要不要写，我的答案是要写，但不要写成人多的大会。"
        "第一轮实验用了 15 分钟，第二块内容隔天整理。"
        "真正有价值的是检索、分块、向量和 BM25 这些知识管理工具。"
    )
    labels = {str(concept["norm"]) for concept in graph.extract_concepts(text)}
    for word in GENERIC_WORDS:
        assert word not in labels, f"泛词 {word} 不应成为概念"
    assert "第一" not in labels and "第二" not in labels
    assert labels, "过滤后仍应保留真正的主题词"


def test_extract_concepts_filters_reversed_token_artifacts() -> None:
    # 「题问」是 jieba 分词边界错误产生的倒序词，不应作为概念
    text = "问题 问题 问题 问题 问题是：检索系统怎么切分块？问题就在分块大小和重叠。"
    labels = {str(concept["norm"]) for concept in graph.extract_concepts(text)}
    assert "题问" not in labels


def test_stopwords_cover_common_generic_words() -> None:
    for word in GENERIC_WORDS:
        assert word in STOPWORDS
