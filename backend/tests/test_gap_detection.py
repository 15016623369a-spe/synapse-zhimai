"""知识缺口检测（v2.4 修复）。

背景：旧规则把「问题词出现在语料外」直接当成缺口证据，导致
「手冲咖啡粉水比」这种正常问题（分词把「粉水比」切成「咖啡粉 / 水比」）
也被判成资料不足，UI 出现「有 3 条引用的答案 + 知识库资料有限」自相矛盾。

新规则只把两类信号当缺口：
1. 没召回任何片段；
2. 英文/引号硬实体在语料里完全未登录；
3. 语料内登录词的 IDF 覆盖率低于阈值。
"""

from __future__ import annotations

from collections import Counter

import pytest
from fastapi.testclient import TestClient

from app.services import agent, embedding

from .conftest import chat_events, events_by_name, joined_deltas, seed_corpus


def _patch_corpus_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    df = Counter(
        {
            "手冲": 3,
            "水温": 3,
            "研磨": 3,
            "rag": 4,
            "评估": 4,
            "指标": 4,
            "分块": 4,
            "重叠": 4,
        }
    )
    monkeypatch.setattr(agent, "_corpus_stats", lambda: (df, 100))


def test_query_entities_extracts_ascii_and_quoted() -> None:
    entities = agent._query_entities("Transformer 的 BP 和「置信传播」是什么？")
    assert "transformer" in entities
    assert "bp" in entities
    assert "置信传播" in entities
    # 普通中文词不会被当成硬实体
    assert agent._query_entities("我的手冲咖啡粉水比是多少？") == []


def test_english_question_does_not_treat_words_as_entities() -> None:
    """整句英文里每个词都是普通词，不能按「长词 = 硬实体」处理。"""
    assert agent._query_entities(
        "Who is the individual associated with the cryptocurrency industry?"
    ) == []
    # 带数字的型号仍然算硬实体
    assert agent._query_entities("Was GPT-4 mentioned in the article?") == ["gpt-4"]


def test_english_question_words_are_stopwords() -> None:
    terms = embedding.tokenize_terms(
        "Who is the individual associated with the cryptocurrency industry?"
    )
    assert "who" not in terms
    assert "the" not in terms
    assert "cryptocurrency" in terms


def test_gap_ignores_oov_segmentation_noise(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_corpus_stats(monkeypatch)
    results = [
        {
            "text": "我自己记的参数是粉水比 1:15、水温 92℃、中细研磨、V60 焖蒸 30 秒。",
            "score": 0.98,
        }
    ]
    # 「咖啡粉 / 水比」未登录，但 手冲/水温/研磨 都命中 → 不是缺口
    assert agent.detect_gap("我的手冲咖啡粉水比、水温和研磨度是多少？", results) is None


def test_gap_hits_unseen_entity(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_corpus_stats(monkeypatch)
    results = [
        {"text": "2026 年的计划与规则记录，包括每周复盘与预算。", "score": 0.98},
    ]
    gap = agent.detect_gap("Transformer 的置信传播算法要迭代几次？", results)
    assert gap is not None
    assert gap["needs_more"] is True
    assert "transformer" in str(gap["reason"]).lower()


def test_gap_returns_weak_when_two_terms_spread_across_sentences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个中等权重词分散在不同句子：chunk 覆盖够、单句覆盖不够 → 弱提示。"""
    terms = ["检索", "向量", "分块", "重叠", "融合", "召回"]
    df = Counter({term: 5 for term in terms})
    monkeypatch.setattr(agent, "_corpus_stats", lambda: (df, 100))
    results = [{"text": "检索很重要。向量也很重要。", "score": 0.98}]
    gap = agent.detect_gap("检索 向量 分块 重叠 融合 召回 分别是什么？", results)
    assert gap is not None
    assert gap["severity"] == "weak"
    assert gap["needs_more"] is True
    assert gap["coverage"] < 0.4
    assert gap["sentence_coverage"] < 0.22


def test_gap_hits_low_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_corpus_stats(monkeypatch)
    results = [{"text": "手冲咖啡的水温是 92 度，研磨度中细。", "score": 0.98}]
    gap = agent.detect_gap("RAG 评估指标有哪些？", results)
    assert gap is not None
    assert gap["needs_more"] is True


def test_chat_coffee_question_has_no_false_gap(seeded_client: TestClient) -> None:
    seed_corpus(seeded_client)
    events, _ = chat_events(
        seeded_client, "我的手冲咖啡粉水比、水温和研磨度是多少？", top_k=6
    )
    assert events_by_name(events, "gap") == []
    done = events_by_name(events, "done")[0]
    assert done["gap"] is None
    answer = joined_deltas(events)
    assert "[1]" in answer
    assert "资料有限" not in answer


def test_chat_unanswerable_question_has_gap(seeded_client: TestClient) -> None:
    seed_corpus(seeded_client)
    events, _ = chat_events(seeded_client, "Transformer 的置信传播算法要迭代几次？")
    gaps = events_by_name(events, "gap")
    assert gaps and gaps[0]["needs_more"] is True
    answer = joined_deltas(events)
    assert "资料有限" in answer
