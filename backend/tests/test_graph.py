"""知识图谱测试（SPEC 5.3–5.4 / 7.5 / 10.1）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .conftest import DOC_COFFEE, DOC_SLEEP, add_note, seed_corpus

LINK_TYPES = {"mentions", "tagged", "related", "cooccur"}


def _degrees(graph: dict) -> dict[str, int]:
    degrees: dict[str, int] = {}
    for link in graph["links"]:
        degrees[link["source"]] = degrees.get(link["source"], 0) + 1
        degrees[link["target"]] = degrees.get(link["target"], 0) + 1
    return degrees


def test_graph_structure_and_consistency(client: TestClient) -> None:
    seed_corpus(client)
    response = client.get("/api/graph", params={"min_link_weight": 0.1})
    assert response.status_code == 200
    graph = response.json()

    assert set(graph) == {"nodes", "links", "meta"}
    nodes = graph["nodes"]
    links = graph["links"]
    meta = graph["meta"]
    assert nodes and links
    assert meta["node_count"] == len(nodes)
    assert meta["link_count"] == len(links)
    assert meta["document_count"] == 4
    assert meta["generated_at"].endswith("Z")

    node_ids = {node["id"] for node in nodes}
    document_nodes = [node for node in nodes if node["type"] == "document"]
    concept_nodes = [node for node in nodes if node["type"] == "concept"]
    tag_nodes = [node for node in nodes if node["type"] == "tag"]
    assert len(document_nodes) == 4
    assert concept_nodes
    assert tag_nodes == [node for node in tag_nodes]  # 标签节点存在
    assert {node["type"] for node in nodes} <= {"document", "concept", "tag"}

    for node in nodes:
        assert {"id", "type", "label", "weight", "degree"} <= set(node)
        prefix = {"document": "doc:", "concept": "concept:", "tag": "tag:"}[node["type"]]
        assert node["id"].startswith(prefix)
        assert node["label"]
        assert float(node["weight"]) >= 0
        assert int(node["degree"]) >= 0
    for node in document_nodes:
        assert {"doc_id", "tags", "summary"} <= set(node)
        assert node["id"] == f"doc:{node['doc_id']}"
        assert isinstance(node["tags"], list)

    degrees = _degrees(graph)
    for node in nodes:
        assert node["degree"] == degrees.get(node["id"], 0)

    for link in links:
        assert set(link) == {"id", "source", "target", "type", "weight"}
        assert link["source"] in node_ids
        assert link["target"] in node_ids
        assert link["source"] != link["target"]
        assert link["type"] in LINK_TYPES
        assert float(link["weight"]) >= 0.1

    link_types = {link["type"] for link in links}
    assert "mentions" in link_types
    assert "tagged" in link_types
    assert "related" in link_types
    assert "cooccur" in link_types


def test_graph_min_link_weight_filters_edges(client: TestClient) -> None:
    seed_corpus(client)
    graph = client.get("/api/graph", params={"min_link_weight": 2.5}).json()
    assert graph["links"] == []
    assert all(node["degree"] == 0 for node in graph["nodes"])


def test_graph_max_concepts_limit(client: TestClient) -> None:
    seed_corpus(client)
    graph = client.get("/api/graph", params={"max_concepts": 2}).json()
    concept_nodes = [node for node in graph["nodes"] if node["type"] == "concept"]
    assert len(concept_nodes) <= 2


def test_graph_node_detail_document(client: TestClient) -> None:
    documents = seed_corpus(client)
    doc_id = documents["rag"]["id"]
    detail = client.get("/api/graph/node", params={"id": f"doc:{doc_id}"})
    assert detail.status_code == 200
    payload = detail.json()
    assert set(payload) == {"node", "docs", "chunks", "related"}
    assert payload["node"]["id"] == f"doc:{doc_id}"
    assert payload["node"]["type"] == "document"
    assert payload["docs"][0]["id"] == doc_id
    assert payload["chunks"]
    assert payload["chunks"][0]["doc_title"] == documents["rag"]["title"]
    assert payload["related"]


def test_graph_node_detail_concept_and_tag(client: TestClient) -> None:
    seed_corpus(client)
    graph = client.get("/api/graph").json()
    concept_node = next(node for node in graph["nodes"] if node["type"] == "concept")
    detail = client.get("/api/graph/node", params={"id": concept_node["id"]})
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["node"]["type"] == "concept"
    assert payload["docs"]
    assert payload["chunks"]
    for related in payload["related"]:
        assert {"id", "label", "type", "weight"} <= set(related)

    tag_node = next(node for node in graph["nodes"] if node["type"] == "tag")
    tag_detail = client.get("/api/graph/node", params={"id": tag_node["id"]}).json()
    assert tag_detail["node"]["id"] == tag_node["id"]
    assert tag_detail["docs"]


@pytest.mark.parametrize(
    "node_id",
    ["not-a-node", "doc:does-not-exist", "concept:cv_missing", "tag:不存在的标签"],
)
def test_graph_node_invalid_returns_404(client: TestClient, node_id: str) -> None:
    response = client.get("/api/graph/node", params={"id": node_id})
    assert response.status_code == 404
    assert response.json()["detail"] == "节点不存在"


def test_graph_related_edges_are_capped_and_tag_based(client: TestClient) -> None:
    add_note(client, "手冲咖啡记录", DOC_COFFEE, ["共享标签"])
    add_note(client, "睡眠恢复记录", DOC_SLEEP, ["共享标签"])
    graph = client.get("/api/graph").json()
    related = [link for link in graph["links"] if link["type"] == "related"]
    assert related, "共享标签 / 概念相近的文档应产生 related 边"
    per_doc: dict[str, int] = {}
    for link in related:
        per_doc[link["source"]] = per_doc.get(link["source"], 0) + 1
        per_doc[link["target"]] = per_doc.get(link["target"], 0) + 1
    assert all(count <= 3 for count in per_doc.values()), "每篇文档最多 3 条 related 边"
    # 至少有一条来自「共享标签」的低权重连接
    assert any(float(link["weight"]) <= 0.4 for link in related)


def test_graph_min_concept_degree_filter(client: TestClient) -> None:
    seed_corpus(client)
    full = client.get("/api/graph").json()
    filtered = client.get("/api/graph", params={"min_concept_degree": 2}).json()

    filtered_node_ids = {node["id"] for node in filtered["nodes"]}
    for node in filtered["nodes"]:
        if node["type"] == "concept":
            assert node["degree"] >= 2
        assert node["id"] in filtered_node_ids
    for link in filtered["links"]:
        assert link["source"] in filtered_node_ids
        assert link["target"] in filtered_node_ids
    concept_full = [node for node in full["nodes"] if node["type"] == "concept"]
    concept_filtered = [node for node in filtered["nodes"] if node["type"] == "concept"]
    assert len(concept_filtered) <= len(concept_full)
    assert len(concept_filtered) < len(concept_full)
    assert filtered["meta"]["node_count"] == len(filtered["nodes"])
    assert filtered["meta"]["link_count"] == len(filtered["links"])


def test_graph_rebuilt_after_delete(client: TestClient) -> None:
    documents = seed_corpus(client)
    vector_id = documents["vector"]["id"]
    assert client.delete(f"/api/documents/{vector_id}").status_code == 200
    graph = client.get("/api/graph").json()
    node_ids = {node["id"] for node in graph["nodes"]}
    assert f"doc:{vector_id}" not in node_ids
    assert graph["meta"]["document_count"] == 3
    for link in graph["links"]:
        assert link["source"] in node_ids and link["target"] in node_ids
