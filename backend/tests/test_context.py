"""多轮对话上下文测试（SPEC-V2 RERANK-PATROL 之后的多轮上下文修复）。

覆盖：
- 追问识别（短句 / 指代 / 「创新点呢」；普通问题不误判）；
- 上下文解析：从 assistant citations / user 引用继承文档、search_text、context_summary，
  扩展面板已落库当前 user 消息的场景；
- chat：追问继承上文文档 → meta.reference / 文档级全文 / 不 gap / 文档答案；
- /extend/search：conversation_id → 上下文检索词（含论文标题）、学术意图、
  context_used/context_summary、无上下文时行为不变；
- search.openalex_search 解析。
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.services import context as context_service
from app.services import search
from app.utils import new_id, now_iso

from .conftest import add_note, chat_events, events_by_name, joined_deltas

PAPER_TITLE = "Transformer-BP 多目标跟踪研究框架"

PAPER_CONTENT = (
    "# Transformer-BP 多目标跟踪研究框架\n\n"
    "## 1 总体判断\n"
    "Transformer + Belief Propagation（BP）是一个很有潜力的多目标跟踪研究方向。"
    "Transformer 负责学习复杂的时空表征和目标交互，BP 负责显式建模多目标之间的数据关联与概率依赖，"
    "从而形成「深度学习 + 概率图模型」的混合跟踪器。\n\n"
    "## 2 为什么 Transformer 本身还需要 BP\n"
    "Transformer 擅长 temporal attention、spatial attention、cross-attention 与特征学习，"
    "但 attention 权重并不等价于严格的数据关联概率。数据关联本质上是一个概率图模型上的全局推断问题，"
    "而 BP 正适合进行这种结构化概率推理。\n\n"
    "## 3 架构：Transformer-BP Tracker\n"
    "Observation → Transformer → Factor Graph → BP → Track Update。"
    "Transformer 输出目标特征与交互关系，因子图把数据关联建模为概率变量，BP 迭代传递消息完成全局推断。\n\n"
    "## 4 研究问题\n"
    "值得深入研究的问题包括：Attention 与 BP message passing 的关系、BP 是否改善数据关联一致性、"
    "BP 的收敛性、以及 Transformer approximation 与 BP approximation 的误差界。"
    "可以把 BP 展开成 K 层形成 Unrolled BP Transformer，并让每层参数可学习。\n\n"
    "## 5 与 JPDA、MHT、RFS 的关系\n"
    "JPDA、MHT 与随机有限集方法都可以写成因子图上的推断，BP 提供了一个统一的近似推断框架。\n\n"
    "## 6 与 Transformer MOT 的关系\n"
    "Transformer 目标跟踪框架通常包含检测、特征关联与轨迹更新三个阶段，"
    "BP 可以插入数据关联环节，用因子图上的消息传递替代简单的 IoU / 匈牙利匹配，"
    "从而显式建模目标之间的排斥约束与多传感器观测融合。\n\n"
    "## 7 实现建议\n"
    "先在一个小规模 MOT 数据集上实现 BP 数据关联层，与基线 Transformer 跟踪器对比关联准确率，"
    "再逐步加入 related noise（相关噪声）与可学习 BP 的展开结构，最后再考虑多传感器融合。\n"
)


@pytest.fixture(autouse=True)
def _offline_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 mock 搜索、不访问真实网络。"""
    monkeypatch.setenv("SYNAPSE_SEARCH_PROVIDER", "mock")
    monkeypatch.setenv("SYNAPSE_SEARCH_ENABLED", "1")
    monkeypatch.delenv("SYNAPSE_SEARCH_MOCK_RESULTS", raising=False)


# ---------------------------------------------------------------------------
# 辅助：直接构造会话历史
# ---------------------------------------------------------------------------


def _insert_conversation(
    client: TestClient,
    messages: list[tuple[str, str, list[dict[str, object]] | None]],
) -> str:
    """插入一个会话与若干消息；``citations`` 为 None 表示空引用。"""
    conversation_id = new_id("conv")
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO conversations(id, title, created_at, updated_at) VALUES(?, ?, ?, ?)",
            (conversation_id, "多轮上下文测试", now_iso(), now_iso()),
        )
        for role, content, citations in messages:
            conn.execute(
                "INSERT INTO messages(id, conversation_id, role, content, citations, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (
                    new_id("m"),
                    conversation_id,
                    role,
                    content,
                    json.dumps(citations or [], ensure_ascii=False),
                    now_iso(),
                ),
            )
    return conversation_id


def _citation(doc_id: str, title: str) -> dict[str, object]:
    return {
        "index": 1,
        "doc_id": doc_id,
        "doc_title": title,
        "chunk_id": "c_1",
        "source_type": "pdf",
        "snippet": "摘要片段",
        "score": 0.99,
    }


def _add_paper(client: TestClient, title: str = PAPER_TITLE) -> dict:
    return add_note(client, title, PAPER_CONTENT, ["论文", "多目标跟踪"])


def _create_followup_history(client: TestClient) -> tuple[str, dict]:
    """真实场景：先总结论文（带 citations），再追问「创新点呢」失败。"""
    document = _add_paper(client)
    conversation_id = _insert_conversation(
        client,
        [
            ("user", f"请总结《{PAPER_TITLE}》的核心内容。", None),
            ("assistant", "根据知识库，这篇文档介绍了 Transformer 与 BP 的混合跟踪框架。",
             [_citation(document["id"], PAPER_TITLE)]),
        ],
    )
    return conversation_id, document


# ---------------------------------------------------------------------------
# 追问识别
# ---------------------------------------------------------------------------


def test_detect_follow_up_heuristics(client: TestClient) -> None:
    assert context_service.detect_follow_up("创新点呢") == (True, "aspect")
    assert context_service.detect_follow_up("创新点有哪些")[0] is True
    assert context_service.detect_follow_up("它的创新点")[0] is True
    assert context_service.detect_follow_up("详细说说")[0] is True
    assert context_service.detect_follow_up("继续")[0] is True
    assert context_service.detect_follow_up("再总结一下")[0] is True
    assert context_service.detect_follow_up("详细说说 RAG 分块参数")[0] is False
    # 普通问题 / 自包含引用不误判
    assert context_service.detect_follow_up("RAG 方法有哪些")[0] is False
    assert context_service.detect_follow_up("那向量检索用的是什么方法？")[0] is False
    assert context_service.detect_follow_up("量子计算的原理是什么？")[0] is False
    assert context_service.detect_follow_up(f"请总结《{PAPER_TITLE}》的核心内容。")[0] is False
    assert context_service.detect_follow_up("总结 arXiv 2410.06294 的核心内容")[0] is False
    assert context_service.detect_follow_up("")[0] is False


# ---------------------------------------------------------------------------
# 上下文解析
# ---------------------------------------------------------------------------


def test_resolve_context_inherits_assistant_citation(client: TestClient) -> None:
    conversation_id, document = _create_followup_history(client)
    with db.get_db() as conn:
        resolved = context_service.resolve_conversation_context(
            conn, conversation_id, "创新点呢"
        )
    assert resolved is not None
    assert resolved["follow_up"] is True
    assert resolved["reason"] == "aspect"
    assert resolved["reference_doc"]["doc_id"] == document["id"]
    assert "content" not in resolved["reference_doc"]  # 紧凑卡片不含正文
    assert resolved["previous_question"].startswith("请总结")
    # 紧凑上下文卡片：不拼接上一轮完整问题 / 回答
    assert resolved["search_text"] == f"{PAPER_TITLE} 的核心内容 → 追问 创新点"
    assert len(resolved["search_text"]) <= 120
    assert resolved["context_summary"] == f"已结合上文《{PAPER_TITLE}》· 追问创新点"
    assert len(resolved["context_summary"]) <= 60
    assert resolved["previous_intent"] == "总结"
    assert 3 <= len(resolved["topic_terms"]) <= 6
    assert "Transformer-BP" in resolved["topic_terms"]
    assert any("Belief Propagation" in term for term in resolved["topic_terms"])
    assert resolved["context_chars"] > 0
    assert resolved["token_budget"] > 0


def test_resolve_context_with_persisted_current_message(client: TestClient) -> None:
    """扩展面板场景：当前 user 消息已落库，仍应继承更早的引用文档。"""
    conversation_id, document = _create_followup_history(client)
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO messages(id, conversation_id, role, content, citations, created_at) "
            "VALUES(?, ?, 'user', ?, '[]', ?)",
            (new_id("m"), conversation_id, "创新点呢", now_iso()),
        )
        resolved = context_service.resolve_conversation_context(
            conn, conversation_id, "创新点呢"
        )
    assert resolved is not None
    assert resolved["reference_doc"]["doc_id"] == document["id"]
    assert resolved["previous_question"].startswith("请总结")
    # 扩展面板可能只回传去掉语气词的短句
    with db.get_db() as conn:
        same = context_service.resolve_conversation_context(conn, conversation_id, "创新点")
    assert same is not None and same["reference_doc"]["doc_id"] == document["id"]


def test_resolve_context_from_user_reference_when_no_citations(client: TestClient) -> None:
    document = _add_paper(client)
    conversation_id = _insert_conversation(
        client,
        [
            ("user", f"请总结《{PAPER_TITLE}》的核心内容。", None),
            ("assistant", "好的，我先概括一下。", None),
        ],
    )
    with db.get_db() as conn:
        resolved = context_service.resolve_conversation_context(
            conn, conversation_id, "创新点呢"
        )
    assert resolved is not None
    assert resolved["reference_doc"]["doc_id"] == document["id"]


def test_resolve_context_returns_none_for_non_followup(client: TestClient) -> None:
    conversation_id, _document = _create_followup_history(client)
    with db.get_db() as conn:
        assert (
            context_service.resolve_conversation_context(
                conn, conversation_id, "量子计算的原理是什么？"
            )
            is None
        )
        assert (
            context_service.resolve_conversation_context(
                conn, conversation_id, f"请总结《{PAPER_TITLE}》的创新点。"
            )
            is None
        )
        assert context_service.resolve_conversation_context(conn, None, "创新点呢") is None
        assert context_service.resolve_conversation_context(conn, "conv_missing", "创新点呢") is None


def test_context_search_queries_prefer_title_or_arxiv_id(client: TestClient) -> None:
    document = _add_paper(client)
    with db.get_db() as conn:
        resolved = context_service.resolve_conversation_context(
            conn, _create_followup_history(client)[0], "创新点呢"
        )
    queries = context_service.build_search_queries(resolved, "创新点呢")
    assert queries
    assert any(PAPER_TITLE in query for query in queries)
    assert any("belief propagation" in query.lower() for query in queries)
    for forbidden in ("novelty", "innovation", "contribution"):
        assert all(forbidden not in query.lower() for query in queries)

    # 有 arXiv ID 的文档优先用 ID 检索
    arxiv_context = {
        "reference_doc": {
            "doc_id": document["id"],
            "title": "NEBPplus Neural Enhanced Background Prediction",
            "filename": "NEBPplus_arXiv2410.06294.pdf",
        },
        "search_text": "NEBPplus 的核心内容 → 追问 创新点",
        "topic_terms": ["NEBPplus", "Neural Enhanced Background Prediction"],
    }
    queries = context_service.build_search_queries(arxiv_context, "创新点呢")
    assert queries[0] == "2410.06294"


# ---------------------------------------------------------------------------
# chat：追问继承文档
# ---------------------------------------------------------------------------


def test_chat_followup_inherits_reference_document(client: TestClient) -> None:
    document = _add_paper(client)
    first_events, _raw = chat_events(
        client, f"请总结《{PAPER_TITLE}》的核心内容。"
    )
    first_meta = events_by_name(first_events, "meta")[0]
    assert first_meta["reference"]["doc_id"] == document["id"]
    conversation_id = first_meta["conversation_id"]

    events, _raw = chat_events(client, "创新点呢", conversation_id=conversation_id)
    names = [event["event"] for event in events]
    meta = events_by_name(events, "meta")[0]
    done = events_by_name(events, "done")[0]

    assert meta["reference"]["doc_id"] == document["id"]
    assert meta["context_used"] is True
    assert meta["context_summary"] == f"已结合上文《{PAPER_TITLE}》· 追问创新点"
    assert len(meta["context_summary"]) <= 60
    assert "gap" not in names  # 内容 ≥800 字，继承文档后不再误报知识缺口
    assert done["reference"]["doc_id"] == document["id"]
    assert done["context_used"] is True
    citations = events_by_name(events, "sources")[0]["citations"]
    assert citations and citations[0]["doc_id"] == document["id"]
    answer = joined_deltas(events)
    assert "知识库中没有找到足够信息" not in answer
    assert answer.strip()


def test_chat_followup_from_user_reference_history(client: TestClient) -> None:
    document = _add_paper(client)
    conversation_id = _insert_conversation(
        client,
        [
            ("user", f"请总结《{PAPER_TITLE}》的核心内容。", None),
            ("assistant", "这篇资料介绍了 Transformer-BP 混合跟踪框架。", None),
        ],
    )
    events, _raw = chat_events(client, "它的创新点呢", conversation_id=conversation_id)
    meta = events_by_name(events, "meta")[0]
    assert meta["reference"]["doc_id"] == document["id"]
    assert meta["context_used"] is True
    assert "gap" not in [event["event"] for event in events]


def test_chat_followup_without_document_uses_context_text(client: TestClient) -> None:
    conversation_id = _insert_conversation(
        client,
        [
            ("user", "请总结 Transformer 目标跟踪的最新研究进展。", None),
            ("assistant", "知识库里暂时没有找到直接相关的资料。", None),
        ],
    )
    events, _raw = chat_events(client, "创新点呢", conversation_id=conversation_id)
    meta = events_by_name(events, "meta")[0]
    assert meta["reference"] is None
    assert meta["context_used"] is True
    assert meta["context_summary"].startswith("已结合上文：")
    done = events_by_name(events, "done")[0]
    assert done["context_used"] is True


def test_chat_fresh_conversation_does_not_inherit(client: TestClient) -> None:
    _add_paper(client)
    events, _raw = chat_events(client, "创新点呢")
    meta = events_by_name(events, "meta")[0]
    assert meta["reference"] is None
    assert meta["context_used"] is False
    assert meta["context_summary"] is None


def test_chat_explicit_reference_still_beats_history(client: TestClient) -> None:
    paper = _add_paper(client)
    other = add_note(client, "手冲咖啡参数记录", "水温 92 度，研磨度中细，粉水比 1:15。" * 40, ["生活"])
    conversation_id = _insert_conversation(
        client,
        [
            ("user", f"请总结《{PAPER_TITLE}》的核心内容。", None),
            ("assistant", "这篇资料介绍了 Transformer-BP 混合跟踪框架。", None),
        ],
    )
    events, _raw = chat_events(
        client, "请总结《手冲咖啡参数记录》的核心内容。", conversation_id=conversation_id
    )
    meta = events_by_name(events, "meta")[0]
    assert meta["reference"]["doc_id"] == other["id"]
    assert meta["reference"]["doc_id"] != paper["id"]
    assert meta["context_used"] is False


# ---------------------------------------------------------------------------
# /extend/search：上下文检索词 / 学术意图
# ---------------------------------------------------------------------------


def _patch_academic_sources(
    monkeypatch: pytest.MonkeyPatch, candidates: list[search.SearchResult]
) -> dict[str, list[str]]:
    captured: dict[str, list[str]] = {"web": [], "openalex": []}

    def fake_openalex_search(query: str, max_results: int = 5):
        captured["openalex"].append(query)
        return candidates[:max_results]

    def fake_search_web(query: str, max_results: int | None = None):
        captured["web"].append(query)
        return []

    monkeypatch.setattr(search, "arxiv_search", lambda *args, **kwargs: [])
    monkeypatch.setattr(search, "openalex_search", fake_openalex_search)
    monkeypatch.setattr(search, "search_web", fake_search_web)
    return captured


def test_extend_search_uses_context_queries_and_doc(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation_id, document = _create_followup_history(client)
    candidates = [
        search.SearchResult(
            url="https://doi.org/10.1109/iccv51070.2023.00897",
            title="3DMOTFormer: Graph Transformer for Online 3D Multi-Object Tracking",
            snippet="Graph Transformer for online 3D multi-object tracking with data association.",
            source="openalex",
            authority="academic",
            relevance=0.8,
            extra={"doi": "10.1109/iccv51070.2023.00897"},
        )
    ]
    captured = _patch_academic_sources(monkeypatch, candidates)
    # 上下文场景不应走 LLM 泛词规划
    monkeypatch.setattr(
        search,
        "plan_queries",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("上下文追问不应调用 plan_queries")
        ),
    )

    response = client.post(
        "/api/extend/search",
        json={"question": "创新点呢", "conversation_id": conversation_id},
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["context_used"] is True
    assert payload["context_summary"] == f"已结合上文《{PAPER_TITLE}》· 追问创新点"
    assert len(payload["context_summary"]) <= 60
    assert payload["intent"] == "academic"
    # 检索词必须包含论文标题（而不是 novelty / innovation 泛词）
    assert payload["queries"]
    assert any(PAPER_TITLE in query for query in payload["queries"])
    assert any("belief propagation" in query.lower() for query in payload["queries"])
    assert all(
        forbidden not in " ".join(payload["queries"]).lower()
        for forbidden in ("novelty", "innovation")
    )
    # OpenAlex 用上下文英文术语检索
    assert captured["openalex"]
    assert "transformer" in captured["openalex"][0]
    assert "belief" in captured["openalex"][0]
    # 候选来自学术来源且与上下文相关
    assert payload["candidates"]
    top = payload["candidates"][0]
    assert top["authority"] == "academic"
    assert "transformer" in top["title"].lower()
    assert "-1" not in [] and payload["filtered_count"] >= 0


def test_extend_search_without_conversation_keeps_behavior(client: TestClient) -> None:
    payload = client.post(
        "/api/extend/search", json={"question": "RAG 的评估指标有哪些？"}
    ).json()
    assert payload["context_used"] is False
    assert payload["context_summary"] is None
    assert payload["intent"] == "general"
    assert payload["candidates"]


def test_extend_search_context_without_doc_uses_previous_question(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation_id = _insert_conversation(
        client,
        [
            ("user", "RAG 的评估指标有哪些？", None),
            ("assistant", "知识库里资料有限。", None),
        ],
    )
    captured: list[str] = []

    def fake_search_web(query: str, max_results: int | None = None):
        captured.append(query)
        return [
            search.SearchResult(
                url="https://example.com/rag-evaluation",
                title="RAG 评估指标与检索实践",
                snippet="RAG 常用评估指标包括忠实度、上下文召回率与引用准确率。",
                source="mock",
                authority="unknown",
            )
        ]

    monkeypatch.setattr(search, "search_web", fake_search_web)
    response = client.post(
        "/api/extend/search",
        json={"question": "创新点呢", "conversation_id": conversation_id},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["context_used"] is True
    assert payload["context_summary"].startswith("已结合上文：")
    assert any("RAG" in query or "rag" in query.lower() for query in payload["queries"])


# ---------------------------------------------------------------------------
# search.openalex_search 解析
# ---------------------------------------------------------------------------


def test_openalex_search_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "api.openalex.org" in str(request.url)
        assert "search=" in str(request.url)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W123",
                        "title": "Graph Transformer for Multi-Object Tracking",
                        "doi": "https://doi.org/10.1234/mot.2024",
                        "publication_year": 2024,
                        "abstract_inverted_index": {
                            "Graph": [0],
                            "tracking": [1],
                            "method": [2],
                        },
                    },
                    {"id": "https://openalex.org/W124", "title": ""},
                ]
            },
        )

    monkeypatch.setattr(search, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    results = search.openalex_search("transformer multi-object tracking", max_results=5)
    assert len(results) == 1
    assert results[0].source == "openalex"
    assert results[0].authority == "academic"
    assert results[0].url == "https://doi.org/10.1234/mot.2024"
    assert "Graph tracking method" in results[0].snippet


def test_openalex_search_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    monkeypatch.setattr(search, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(search.SearchUnavailable):
        search.openalex_search("transformer tracking")


def test_arxiv_rate_limit_degrades_to_search_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """arXiv 429 限流应降级为 SearchUnavailable（不能 500 冒泡）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    monkeypatch.setattr(search, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(search.SearchUnavailable):
        search.arxiv_search("Transformer-BP")


# ---------------------------------------------------------------------------
# 上下文工程：少而准（长度 / prompt / 不注入全文）
# ---------------------------------------------------------------------------


def test_context_card_is_compact(client: TestClient) -> None:
    conversation_id, document = _create_followup_history(client)
    long_assistant = "这是上一轮很长的回答。" * 400  # 3600 字
    with db.get_db() as conn:
        conn.execute(
            "UPDATE messages SET content = ? WHERE conversation_id = ? AND role = 'assistant'",
            (long_assistant, conversation_id),
        )
        resolved = context_service.resolve_conversation_context(
            conn, conversation_id, "创新点呢"
        )
    assert resolved is not None
    # 不拼接上一轮完整问答
    assert long_assistant[:20] not in resolved["search_text"]
    assert long_assistant[:20] not in resolved["context_summary"]
    assert resolved["context_chars"] <= 300
    assert resolved["token_budget"] <= 200
    assert len(resolved["search_text"]) <= 120
    assert len(resolved["context_summary"]) <= 60
    assert len(resolved["topic_terms"]) <= 6


def test_follow_up_chain_context_does_not_grow(client: TestClient) -> None:
    conversation_id, document = _create_followup_history(client)
    with db.get_db() as conn:
        for role, content in (
            ("user", "创新点呢"),
            ("assistant", "知识库中没有找到足够信息。"),
            ("user", "方法呢"),
            ("assistant", "知识库中没有找到足够信息。"),
        ):
            conn.execute(
                "INSERT INTO messages(id, conversation_id, role, content, citations, created_at) "
                "VALUES(?, ?, ?, ?, '[]', ?)",
                (new_id("m"), conversation_id, role, content, now_iso()),
            )
        resolved = context_service.resolve_conversation_context(
            conn, conversation_id, "实验呢"
        )
    assert resolved is not None
    # 只继承一次：检索文本仍以根问题为起点，不随追问链膨胀
    assert resolved["search_text"].count("请总结") <= 1
    assert len(resolved["search_text"]) <= 120
    assert resolved["context_chars"] <= 300
    assert resolved["topic_terms"]
    assert len(resolved["topic_terms"]) <= 6
    assert resolved["reference_doc"]["doc_id"] == document["id"]


def test_chat_doc_scoped_followup_does_not_inject_full_document(
    client: TestClient,
) -> None:
    document = _add_paper(client)
    first_events, _raw = chat_events(client, f"请总结《{PAPER_TITLE}》的核心内容。")
    conversation_id = events_by_name(first_events, "meta")[0]["conversation_id"]

    events, _raw = chat_events(client, "创新点呢", conversation_id=conversation_id)
    names = [event["event"] for event in events]
    done = events_by_name(events, "done")[0]
    diagnostics = done["context_diagnostics"]
    citations = events_by_name(events, "sources")[0]["citations"]

    assert "gap" not in names
    assert diagnostics["context_used"] is True
    # 非总结追问：只做 doc-scoped 检索，不注入整篇正文
    assert diagnostics["doc_context_chars"] == 0
    assert 1 <= diagnostics["retrieval_chunks"] <= 8
    assert len(citations) <= 8
    assert all(len(citation["snippet"]) <= 400 for citation in citations)
    # 历史裁剪：最多 4 条
    assert diagnostics["history_messages"] <= 4
    assert diagnostics["context_chars"] <= 300


def test_chat_summary_followup_uses_document_level_context(
    client: TestClient,
) -> None:
    document = _add_paper(client)
    first_events, _raw = chat_events(client, f"请总结《{PAPER_TITLE}》的核心内容。")
    conversation_id = events_by_name(first_events, "meta")[0]["conversation_id"]

    events, _raw = chat_events(client, "再总结一下", conversation_id=conversation_id)
    names = [event["event"] for event in events]
    done = events_by_name(events, "done")[0]
    diagnostics = done["context_diagnostics"]
    citations = events_by_name(events, "sources")[0]["citations"]

    assert "gap" not in names
    assert done["context_used"] is True
    # 明确总结意图：使用文档级上下文（整篇 ≤ 40k 字）
    assert diagnostics["doc_context_chars"] >= len(PAPER_CONTENT) - 10
    assert len(citations) == 1


def test_chat_history_trimmed_and_assistant_truncated(client: TestClient) -> None:
    document = _add_paper(client)
    long_answer = "这是一段很长的历史助手回答。" * 300  # 3900 字
    conversation_id = _insert_conversation(
        client,
        [
            ("user", f"请总结《{PAPER_TITLE}》的核心内容。", None),
            ("assistant", long_answer, [_citation(document["id"], PAPER_TITLE)]),
            ("user", "创新点呢", None),
            ("assistant", long_answer, None),
            ("user", "方法呢", None),
            ("assistant", long_answer, None),
        ],
    )
    events, _raw = chat_events(client, "实验呢", conversation_id=conversation_id)
    diagnostics = events_by_name(events, "done")[0]["context_diagnostics"]
    assert diagnostics["history_messages"] <= 4
    # 若未裁剪，history 字符数会 > 11000；裁剪后 assistant 每条 ≤700
    assert diagnostics["history_chars"] <= 4 * 760
    assert diagnostics["token_budget"] <= 300


def test_extend_context_does_not_include_previous_assistant_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "SECRET_ASSISTANT_ANSWER_MARKER"
    conversation_id = _create_followup_history(client)[0]
    with db.get_db() as conn:
        conn.execute(
            "UPDATE messages SET content = ? WHERE conversation_id = ? AND role = 'assistant'",
            (f"{marker} " + "上一轮回答内容。" * 300, conversation_id),
        )
    candidates = [
        search.SearchResult(
            url="https://doi.org/10.1109/iccv51070.2023.00897",
            title="3DMOTFormer: Graph Transformer for Online 3D Multi-Object Tracking",
            snippet="Graph Transformer for online multi-object tracking.",
            source="openalex",
            authority="academic",
            relevance=0.8,
        )
    ]
    _patch_academic_sources(monkeypatch, candidates)
    payload = client.post(
        "/api/extend/search",
        json={"question": "创新点呢", "conversation_id": conversation_id},
    ).json()
    dumped = json.dumps(payload, ensure_ascii=False)
    assert marker not in dumped
    assert payload["context_used"] is True
    assert len(payload["context_summary"]) <= 60
    assert payload["queries"]
    assert all(marker not in query for query in payload["queries"])
