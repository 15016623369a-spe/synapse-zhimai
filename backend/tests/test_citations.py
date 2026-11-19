"""引用裁剪与相关性过滤（v2.4）。

旧行为：生成前把全部检索结果定为 citations，回答里只用了 [1][2][3]，
接口却返回 6 条「参考来源」——引用面板展示的是检索池，不是回答实际引用。
这里覆盖：
- 只保留回答里出现的 [n]；
- 幻觉编号（[9] 但只有 4 条）会被记录，不静默；
- 模型完全没标引用时给前 3 条并打 unverified；
- 低相关片段在进入上下文前就被过滤；
- 文档级全文引用不伪造 score。
"""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from app.routers.chat import _filter_answer_sources, _prune_citations
from app.services import llm
from app.services.retrieval import relevant_snippet, to_citations
from app.utils import make_snippet

from .conftest import chat_events, events_by_name, seed_corpus


def _citation(index: int, score: float | None) -> dict[str, object]:
    return {
        "index": index,
        "doc_id": f"d_{index}",
        "doc_title": f"文档 {index}",
        "chunk_id": f"c_{index}",
        "source_type": "md",
        "snippet": "片段",
        "score": score,
        "rank": index,
        "relevance": "high",
        "kind": "chunk",
    }


def _configure_mock_llm(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={
            "llm": {
                "base_url": "https://mock.api/v1/",
                "model": "mock-chat",
                "api_key": "sk-test-secret-123",
                "temperature": 0.2,
            }
        },
    )
    assert response.status_code == 200


def _mock_stream(monkeypatch, pieces: list[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        lines = [
            f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]}, ensure_ascii=False)}\n\n"
            for piece in pieces
        ]
        lines.append("data: [DONE]\n\n")
        return httpx.Response(
            200,
            content="".join(lines).encode("utf-8"),
            headers={"content-type": "text/event-stream; charset=utf-8"},
        )

    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(handler))


def test_filter_drops_low_relevance_chunks() -> None:
    results = [
        {"score": 0.98, "kind": "chunk"},
        {"score": 0.96, "kind": "chunk"},
        {"score": 0.60, "kind": "chunk"},
        {"score": 0.30, "kind": "chunk"},
    ]
    kept = _filter_answer_sources(results)
    assert [item["score"] for item in kept] == [0.98, 0.96, 0.60]


def test_filter_keeps_document_kind_without_score() -> None:
    results = [{"score": None, "kind": "document"}]
    assert _filter_answer_sources(results) == results


def test_prune_keeps_only_cited_sources() -> None:
    citations = [_citation(index, 0.98) for index in range(1, 5)]
    pruned, invalid = _prune_citations("结论 A [1]，结论 B [3]。", citations)
    assert [item["index"] for item in pruned] == [1, 3]
    assert invalid == []


def test_prune_reports_hallucinated_index() -> None:
    citations = [_citation(index, 0.98) for index in range(1, 3)]
    pruned, invalid = _prune_citations("只有一条 [7]。", citations)
    assert invalid == [7]
    # 没有有效引用时兜底展示前 3 条，并标 unverified
    assert pruned and all(item["unverified"] is True for item in pruned)


def test_prune_falls_back_when_model_cites_nothing() -> None:
    citations = [_citation(index, 0.98) for index in range(1, 6)]
    pruned, invalid = _prune_citations("这是一段没有引用编号的回答。", citations)
    assert invalid == []
    assert len(pruned) == 3
    assert all(item["unverified"] is True for item in pruned)


def test_document_citation_has_no_fake_score() -> None:
    citations = to_citations(
        [
            {
                "doc_id": "d_1",
                "doc_title": "整篇文档",
                "chunk_id": "c_1",
                "source_type": "pdf",
                "text": "正文",
                "snippet": "正文",
                "score": None,
                "kind": "document",
                "relevance": "document",
            }
        ]
    )
    assert citations[0]["score"] is None
    assert citations[0]["kind"] == "document"
    assert citations[0]["relevance"] == "document"


def test_relevant_snippet_centers_on_query_sentence() -> None:
    table = "门票 400 430 无。咖啡与购物 500 780。豆子买多了 合计 9000 9880。" * 10
    text = (
        table
        + "我自己记的参数是粉水比 1:15、水温 92 度、中细研磨、V60 焖蒸 30 秒。"
        + "咖啡店巡礼去了三家：岚山的 Arabica、停车场里的 Weekenders、Kurasu。"
    )
    head = make_snippet(text)
    assert "粉水比" not in head  # 旧实现只截开头，正好错过答案
    snippet = relevant_snippet("手冲咖啡的粉水比和水温是多少？", text)
    assert "粉水比" in snippet
    assert len(snippet) <= 300


def test_llm_citations_are_pruned_to_used(
    client: TestClient, monkeypatch
) -> None:
    seed_corpus(client)
    _configure_mock_llm(client)
    _mock_stream(monkeypatch, ["分块大小是 450 到 650 字，", "块间重叠 80 字 [1]。"])

    events, _ = chat_events(client, "RAG 的分块参数是多少？")
    done = events_by_name(events, "done")[0]
    assert len(done["citations"]) == 1
    assert done["citations"][0]["index"] == 1
    assert done["context_diagnostics"]["invalid_citations"] == []


def test_llm_hallucinated_citation_is_flagged(
    client: TestClient, monkeypatch
) -> None:
    seed_corpus(client)
    _configure_mock_llm(client)
    _mock_stream(monkeypatch, ["这是一段引用 [9] 的回答。"])

    events, _ = chat_events(client, "RAG 的分块参数是多少？")
    done = events_by_name(events, "done")[0]
    assert done["context_diagnostics"]["invalid_citations"] == [9]
    assert done["citations"]
    assert all(item.get("unverified") is True for item in done["citations"])
