"""检索测试（SPEC 6.1–6.2 / 7.3 / 10.1）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import seed_corpus

RESULT_KEYS = {
    "chunk_id",
    "doc_id",
    "doc_title",
    "text",
    "snippet",
    "score",
    "source_type",
}


def test_search_hits_expected_document(client: TestClient) -> None:
    documents = seed_corpus(client)
    response = client.post(
        "/api/search", json={"query": "RAG 的分块大小和重叠是多少？", "top_k": 6}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "RAG 的分块大小和重叠是多少？"
    results = payload["results"]
    assert results

    top_doc_ids = [result["doc_id"] for result in results[:3]]
    assert documents["rag"]["id"] in top_doc_ids
    for result in results:
        assert set(result) == RESULT_KEYS
        assert result["chunk_id"].startswith("c_")
        assert result["doc_id"].startswith("d_")
        assert result["doc_title"]
        assert result["text"]
        assert 0 < result["score"] <= 1
        assert len(result["snippet"]) <= 300
        assert result["source_type"] == "note"

    scores = [result["score"] for result in results]
    assert scores == sorted(scores, reverse=True)


def test_search_same_document_at_most_two_chunks(client: TestClient) -> None:
    seed_corpus(client)
    results = client.post(
        "/api/search", json={"query": "检索 向量 分块 知识管理", "top_k": 12}
    ).json()["results"]
    assert len(results) <= 12
    per_doc: dict[str, int] = {}
    for result in results:
        per_doc[result["doc_id"]] = per_doc.get(result["doc_id"], 0) + 1
    assert all(count <= 2 for count in per_doc.values())


def test_search_respects_doc_ids_scope(client: TestClient) -> None:
    documents = seed_corpus(client)
    coffee_id = documents["coffee"]["id"]
    response = client.post(
        "/api/search",
        json={"query": "水温 研磨度 咖啡", "top_k": 6, "doc_ids": [coffee_id]},
    )
    results = response.json()["results"]
    assert results
    assert all(result["doc_id"] == coffee_id for result in results)


def test_search_empty_database_returns_no_results(client: TestClient) -> None:
    response = client.post("/api/search", json={"query": "任何问题"})
    assert response.status_code == 200
    assert response.json()["results"] == []


def test_search_requires_query(client: TestClient) -> None:
    response = client.post("/api/search", json={"query": "   "})
    assert response.status_code == 400
    assert "查询" in response.json()["detail"]


def test_search_top_k_is_clamped(client: TestClient) -> None:
    seed_corpus(client)
    results = client.post(
        "/api/search", json={"query": "检索", "top_k": 99}
    ).json()["results"]
    assert len(results) <= 12

    results_zero = client.post(
        "/api/search", json={"query": "检索", "top_k": 0}
    ).json()["results"]
    assert 1 <= len(results_zero) <= 12
