"""分区（Zones）后端测试（SPEC-ZONES 第 3 节）。"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db

from .conftest import DOC_COFFEE, DOC_RAG, DOC_VECTOR, add_note

ZONE_KEYS = {
    "id",
    "name",
    "color",
    "description",
    "sort_order",
    "document_count",
    "created_at",
    "updated_at",
}


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _create_zone(client: TestClient, name: str, **kwargs) -> dict:
    response = client.post("/api/zones", json={"name": name, **kwargs})
    assert response.status_code == 200, response.text
    zone = response.json()["zone"]
    assert set(zone) == ZONE_KEYS
    return zone


def _assign(client: TestClient, doc_ids: list[str], zone_id: str | None) -> dict:
    response = client.put(
        "/api/documents/zone", json={"doc_ids": doc_ids, "zone_id": zone_id}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _zone_graph(client: TestClient, **params) -> dict:
    response = client.get("/api/graph", params={"mode": "zones", **params})
    assert response.status_code == 200, response.text
    return response.json()


def _zone_of_documents(client: TestClient) -> dict[str, str | None]:
    return {
        item["id"]: item["zone_id"]
        for item in client.get("/api/documents").json()["items"]
    }


# ---------------------------------------------------------------------------
# 区域 CRUD
# ---------------------------------------------------------------------------


def test_zones_empty_then_create_with_defaults(client: TestClient) -> None:
    payload = client.get("/api/zones").json()
    assert payload == {"items": [], "unassigned_count": 0}

    add_note(client, "未分区笔记", "这是一段没有分区的笔记内容。")
    assert client.get("/api/zones").json()["unassigned_count"] == 1

    zone = _create_zone(client, "AI 与大模型")
    assert zone["name"] == "AI 与大模型"
    assert zone["color"] == "#4FD8FF"
    assert zone["description"] is None
    assert zone["sort_order"] == 1
    assert zone["document_count"] == 0
    assert zone["id"].startswith("z_")

    second = _create_zone(client, "生活与健康")
    assert second["color"] == "#A78BFA"
    assert second["sort_order"] == 2

    items = client.get("/api/zones").json()["items"]
    assert [item["name"] for item in items] == ["AI 与大模型", "生活与健康"]


def test_zone_create_validation(client: TestClient) -> None:
    _create_zone(client, "AI 与大模型")

    duplicate = client.post("/api/zones", json={"name": "  ai 与大模型  "})
    assert duplicate.status_code == 400
    assert duplicate.json()["detail"] == "分区名称已存在"

    empty = client.post("/api/zones", json={"name": "   "})
    assert empty.status_code == 400
    assert "名称不能为空" in empty.json()["detail"]

    too_long = client.post("/api/zones", json={"name": "a" * 31})
    assert too_long.status_code == 400
    assert "30" in too_long.json()["detail"]

    for bad_color in ("red", "#12345", "#GGGGGG"):
        response = client.post("/api/zones", json={"name": "颜色测试", "color": bad_color})
        assert response.status_code == 400, bad_color
        assert "颜色" in response.json()["detail"]

    explicit = client.post(
        "/api/zones", json={"name": "显式颜色", "color": "#a78bfa"}
    ).json()["zone"]
    assert explicit["color"] == "#A78BFA"

    long_description = client.post(
        "/api/zones", json={"name": "描述过长", "description": "描" * 201}
    )
    assert long_description.status_code == 400


def test_zone_update_name_color_description(client: TestClient) -> None:
    zone = _create_zone(client, "旧名字", description="旧描述")
    response = client.put(
        f"/api/zones/{zone['id']}",
        json={"name": "新名字", "color": "#34d399", "description": "新描述"},
    )
    assert response.status_code == 200
    updated = response.json()["zone"]
    assert updated["name"] == "新名字"
    assert updated["color"] == "#34D399"
    assert updated["description"] == "新描述"
    assert updated["sort_order"] == zone["sort_order"]

    cleared = client.put(
        f"/api/zones/{zone['id']}", json={"description": None}
    ).json()["zone"]
    assert cleared["description"] is None

    blank_name = client.put(f"/api/zones/{zone['id']}", json={"name": ""})
    assert blank_name.status_code == 400

    missing = client.put("/api/zones/z_missing", json={"name": "x"})
    assert missing.status_code == 404
    assert missing.json()["detail"] == "分区不存在"


def test_zone_delete_unassigns_documents(client: TestClient) -> None:
    document = add_note(client, "待归档笔记", "这是一段用于测试删除分区的笔记内容。")
    zone = _create_zone(client, "临时分区")
    _assign(client, [document["id"]], zone["id"])
    assert client.get("/api/zones").json()["items"][0]["document_count"] == 1

    response = client.delete(f"/api/zones/{zone['id']}")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "unassigned_count": 1}

    assert client.get("/api/zones").json()["items"] == []
    detail = client.get(f"/api/documents/{document['id']}")
    assert detail.status_code == 200
    assert detail.json()["zone_id"] is None
    assert detail.json()["zone_name"] is None

    again = client.delete(f"/api/zones/{zone['id']}")
    assert again.status_code == 404


# ---------------------------------------------------------------------------
# 文档归档
# ---------------------------------------------------------------------------


def test_assign_documents_batch_and_activity(client: TestClient) -> None:
    first = add_note(client, "笔记一", "检索增强生成需要分块与向量检索。")
    second = add_note(client, "笔记二", "卡片盒笔记法用双链组织知识。")
    third = add_note(client, "笔记三", "手冲咖啡的水温是 92 度。")
    zone = _create_zone(client, "AI 与大模型")

    result = _assign(client, [first["id"], second["id"], "d_missing"], zone["id"])
    assert result == {
        "ok": True,
        "updated": 2,
        "skipped": 1,
        "zone": {"id": zone["id"], "name": "AI 与大模型"},
    }

    zones_by_doc = _zone_of_documents(client)
    assert zones_by_doc[first["id"]] == zone["id"]
    assert zones_by_doc[second["id"]] == zone["id"]
    assert zones_by_doc[third["id"]] is None
    listed = {
        item["id"]: item
        for item in client.get("/api/documents").json()["items"]
    }
    assert listed[first["id"]]["zone_name"] == "AI 与大模型"
    assert listed[third["id"]]["zone_id"] is None
    assert client.get("/api/zones").json()["items"][0]["document_count"] == 2

    moved_out = _assign(client, [first["id"]], None)
    assert moved_out == {"ok": True, "updated": 1, "skipped": 0, "zone": None}
    assert _zone_of_documents(client)[first["id"]] is None
    zones_payload = client.get("/api/zones").json()
    assert zones_payload["unassigned_count"] == 2
    assert zones_payload["items"][0]["document_count"] == 1

    stats = client.get("/api/stats").json()
    assert any(
        entry["kind"] == "zone" and "移入分区" in entry["detail"]
        for entry in stats["recent"]
    )


def test_assign_unknown_zone_returns_404(client: TestClient) -> None:
    document = add_note(client, "笔记", "一段用于测试的笔记内容。")
    response = client.put(
        "/api/documents/zone",
        json={"doc_ids": [document["id"]], "zone_id": "z_missing"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "分区不存在"
    assert _zone_of_documents(client)[document["id"]] is None


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------

_LEGACY_SCHEMA = """
CREATE TABLE documents(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT, source_type TEXT NOT NULL,
  source_url TEXT, tags TEXT NOT NULL DEFAULT '[]',
  summary TEXT, content TEXT, size INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ready',
  error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE conversations(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE messages(
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL, content TEXT NOT NULL, citations TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def test_migration_is_idempotent_and_preserves_existing_data(tmp_path) -> None:
    legacy_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy_path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO documents(id, title, filename, source_type, tags, content, size, "
        "status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)",
        (
            "d_legacy",
            "历史文档",
            "legacy.md",
            "md",
            '["AI"]',
            "历史正文内容",
            6,
            "2026-11-01T00:00:00Z",
            "2026-11-01T00:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO conversations(id, title, created_at, updated_at) VALUES(?, ?, ?, ?)",
        ("conv_legacy", "历史会话", "2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO messages(id, conversation_id, role, content, created_at) "
        "VALUES(?, ?, 'user', '历史消息', '2026-11-01T00:00:00Z')",
        ("m_legacy", "conv_legacy"),
    )
    conn.executemany(
        "INSERT INTO settings(key, value) VALUES(?, ?)",
        [
            ("schema_version", "1"),
            ("llm_base_url", "https://legacy.example/v1"),
            ("llm_api_key", "sk-legacy-secret"),
            ("llm_model", "legacy-model"),
        ],
    )
    conn.commit()
    conn.close()

    db.init_db(legacy_path)
    db.init_db(legacy_path)  # 幂等：重复迁移不报错、不丢数据

    conn = sqlite3.connect(legacy_path)
    conn.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
        assert "zone_id" in columns
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "zones" in tables

        document = conn.execute("SELECT * FROM documents WHERE id='d_legacy'").fetchone()
        assert document is not None
        assert document["title"] == "历史文档"
        assert document["zone_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        settings = {
            row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")
        }
        assert settings["llm_base_url"] == "https://legacy.example/v1"
        assert settings["llm_api_key"] == "sk-legacy-secret"
        assert settings["llm_model"] == "legacy-model"
        assert settings["schema_version"] == "6"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 分区图谱
# ---------------------------------------------------------------------------


def _seed_two_zones(client: TestClient) -> dict:
    """构建：区1 两篇相似文档、区2 一篇相似文档、区外一篇咖啡文档。"""
    documents = {
        "rag": add_note(client, "RAG 检索增强实践", DOC_RAG, ["AI", "共享标签"]),
        "vector": add_note(client, "向量检索调参", DOC_VECTOR, ["AI", "检索"]),
        "rag_copy": add_note(client, "RAG 实践副本", DOC_RAG, ["AI", "共享标签"]),
        "coffee": add_note(client, "手冲咖啡参数", DOC_COFFEE, ["生活"]),
    }
    zone_ai = _create_zone(client, "AI 与技术")
    zone_life = _create_zone(client, "生活与健康")
    _assign(client, [documents["rag"]["id"], documents["vector"]["id"]], zone_ai["id"])
    _assign(client, [documents["rag_copy"]["id"]], zone_life["id"])
    documents["zone_ai"] = zone_ai
    documents["zone_life"] = zone_life
    return documents


def test_zones_graph_isolation_and_suffixes(client: TestClient) -> None:
    documents = _seed_two_zones(client)
    zone_ai = documents["zone_ai"]["id"]
    zone_life = documents["zone_life"]["id"]

    graph = _zone_graph(client, min_concept_degree=1)
    assert set(graph) == {"nodes", "links", "zones", "meta"}
    nodes = graph["nodes"]
    links = graph["links"]
    assert nodes
    assert links

    zones_meta = graph["zones"]
    assert [zone["id"] for zone in zones_meta] == [zone_ai, zone_life, None]
    assert zones_meta[-1]["name"] == "未分区"
    assert zones_meta[-1]["color"] == "#5B6F8C"
    assert [zone["document_count"] for zone in zones_meta] == [2, 1, 1]
    assert graph["meta"]["document_count"] == 4
    assert graph["meta"]["zone_count"] == 3
    assert graph["meta"]["node_count"] == len(nodes)
    assert graph["meta"]["link_count"] == len(links)
    assert sum(zone["node_count"] for zone in zones_meta) == len(nodes)
    assert sum(zone["link_count"] for zone in zones_meta) == len(links)
    assert all(zone["node_count"] > 0 for zone in zones_meta)

    zone_ids = {zone["id"] for zone in zones_meta}
    zone_by_name = {zone["id"]: zone for zone in zones_meta}
    node_zone: dict[str, str | None] = {}
    for node in nodes:
        assert {"zone_id", "zone_name", "zone_color"} <= set(node)
        assert node["zone_id"] in zone_ids
        zone_meta = zone_by_name[node["zone_id"]]
        assert node["zone_name"] == zone_meta["name"]
        assert node["zone_color"] == zone_meta["color"]
        node_zone[node["id"]] = node["zone_id"]
        if node["type"] in {"concept", "tag"}:
            expected_suffix = "@" + (node["zone_id"] or "unassigned")
            assert node["id"].endswith(expected_suffix)
        else:
            assert node["id"] == f"doc:{node['doc_id']}"

    # 组与组之间零连线
    for link in links:
        assert node_zone[link["source"]] == node_zone[link["target"]]
        assert link["id"].endswith("@" + (node_zone[link["source"]] or "unassigned"))

    # 跨区共享的概念在两个区各出现一次，id 带各自后缀
    labels_ai = {
        node["label"]
        for node in nodes
        if node["type"] == "concept" and node["zone_id"] == zone_ai
    }
    labels_life = {
        node["label"]
        for node in nodes
        if node["type"] == "concept" and node["zone_id"] == zone_life
    }
    shared_labels = labels_ai & labels_life
    assert shared_labels, "相似文档的区域应出现相同概念"
    shared_nodes = [
        node
        for node in nodes
        if node["type"] == "concept" and node["label"] in shared_labels
    ]
    assert len(shared_nodes) >= 2
    assert any(node["id"].endswith(f"@{zone_ai}") for node in shared_nodes)
    assert any(node["id"].endswith(f"@{zone_life}") for node in shared_nodes)

    # 共享标签同样按区重复出现
    ai_tag_nodes = [
        node
        for node in nodes
        if node["type"] == "tag" and node["label"] == "共享标签"
    ]
    assert {node["id"] for node in ai_tag_nodes} == {
        f"tag:共享标签@{zone_ai}",
        f"tag:共享标签@{zone_life}",
    }

    # 未分区组：文档节点 zone_id=null、zone_name=未分区
    unassigned_nodes = [node for node in nodes if node["zone_id"] is None]
    assert unassigned_nodes
    assert all(node["zone_name"] == "未分区" for node in unassigned_nodes)
    assert all(node["zone_color"] == "#5B6F8C" for node in unassigned_nodes)


def test_zones_graph_group_limits(client: TestClient) -> None:
    _seed_two_zones(client)
    limited = _zone_graph(client, max_concepts=2)
    for zone in limited["zones"]:
        zone_concepts = [
            node
            for node in limited["nodes"]
            if node["type"] == "concept" and node["zone_id"] == zone["id"]
        ]
        assert len(zone_concepts) <= 2

    degree_filtered = _zone_graph(client, min_concept_degree=2)
    for node in degree_filtered["nodes"]:
        if node["type"] == "concept":
            assert node["degree"] >= 2
    full = _zone_graph(client)
    assert len(degree_filtered["nodes"]) <= len(full["nodes"])


def test_zones_graph_unknown_mode_returns_400(client: TestClient) -> None:
    response = client.get("/api/graph", params={"mode": "bogus"})
    assert response.status_code == 400
    assert "不支持的模式" in response.json()["detail"]


def test_global_graph_unchanged_with_mode_global(client: TestClient) -> None:
    _seed_two_zones(client)
    default = client.get("/api/graph").json()
    explicit = client.get("/api/graph", params={"mode": "global"}).json()
    assert set(default) == {"nodes", "links", "meta"}
    assert "zones" not in default
    default["meta"].pop("generated_at")
    explicit["meta"].pop("generated_at")
    assert default == explicit
    # 全局模式仍然跨区域展示全部文档与连线
    document_nodes = [node for node in default["nodes"] if node["type"] == "document"]
    assert len(document_nodes) == 4
    zone_ids = {node["zone_id"] for node in default["nodes"]} if "zone_id" in default["nodes"][0] else set()
    assert not zone_ids  # 全局模式节点不带 zone 字段（与旧版一致）


def test_zoned_node_scope_and_suffix(client: TestClient) -> None:
    documents = _seed_two_zones(client)
    zone_ai = documents["zone_ai"]["id"]
    zone_life = documents["zone_life"]["id"]
    ai_doc_ids = {documents["rag"]["id"], documents["vector"]["id"]}
    graph = _zone_graph(client)
    ai_concept = next(
        node
        for node in graph["nodes"]
        if node["type"] == "concept" and node["zone_id"] == zone_ai
    )
    scoped = client.get(
        "/api/graph/node", params={"id": ai_concept["id"], "zone_id": zone_ai}
    )
    assert scoped.status_code == 200
    payload = scoped.json()
    assert payload["node"]["id"] == ai_concept["id"]
    assert payload["node"]["zone_id"] == zone_ai
    assert payload["node"]["zone_name"] == documents["zone_ai"]["name"]
    assert payload["docs"]
    assert {document["id"] for document in payload["docs"]} <= ai_doc_ids
    for chunk in payload["chunks"]:
        assert chunk["doc_id"] in ai_doc_ids
    for related in payload["related"]:
        if related["type"] in {"concept", "tag"}:
            assert related["id"].endswith(f"@{zone_ai}")
        else:
            assert related["id"][4:] in ai_doc_ids

    # 只带后缀、不传 zone_id 也可自动识别区域
    suffix_only = client.get("/api/graph/node", params={"id": ai_concept["id"]})
    assert suffix_only.status_code == 200
    assert suffix_only.json()["node"]["zone_id"] == zone_ai

    # 后缀与 zone_id 不匹配 → 404
    mismatch = client.get(
        "/api/graph/node", params={"id": ai_concept["id"], "zone_id": zone_life}
    )
    assert mismatch.status_code == 404
    # 未知区域 → 404
    unknown_zone = client.get(
        "/api/graph/node", params={"id": "concept:cv_missing", "zone_id": "z_missing"}
    )
    assert unknown_zone.status_code == 404
    assert unknown_zone.json()["detail"] == "分区不存在"
    # 区域隔离：本区文档用跨区 zone_id 查详情 → 404
    cross_scope = client.get(
        "/api/graph/node",
        params={"id": f"doc:{documents['rag']['id']}", "zone_id": zone_life},
    )
    assert cross_scope.status_code == 404
    # 文档节点在本区域内可见
    scoped_doc = client.get(
        "/api/graph/node",
        params={"id": f"doc:{documents['rag']['id']}", "zone_id": zone_ai},
    )
    assert scoped_doc.status_code == 200
    assert [document["id"] for document in scoped_doc.json()["docs"]] == [
        documents["rag"]["id"]
    ]

    # 未分区作用域
    unassigned_doc = client.get(
        "/api/graph/node",
        params={"id": f"doc:{documents['coffee']['id']}", "zone_id": "unassigned"},
    )
    assert unassigned_doc.status_code == 200
    assert unassigned_doc.json()["node"]["zone_id"] is None
    assert unassigned_doc.json()["node"]["zone_name"] == "未分区"

    # 全局详情保持旧行为（节点不带 zone 字段）
    global_graph = client.get("/api/graph").json()
    global_concept = next(node for node in global_graph["nodes"] if node["type"] == "concept")
    global_detail = client.get("/api/graph/node", params={"id": global_concept["id"]})
    assert global_detail.status_code == 200
    assert "zone_id" not in global_detail.json()["node"]


def test_zones_graph_empty_zone_has_zero_counts(client: TestClient) -> None:
    document = add_note(client, "笔记", "一段测试内容，包含检索与分块。")
    zone = _create_zone(client, "空分区")
    _assign(client, [document["id"]], zone["id"])
    empty_zone = _create_zone(client, "待使用分区")

    graph = _zone_graph(client)
    empty_meta = next(item for item in graph["zones"] if item["id"] == empty_zone["id"])
    assert empty_meta["document_count"] == 0
    assert empty_meta["node_count"] == 0
    assert empty_meta["link_count"] == 0
    # 所有文档均已被归档，不应出现未分区组
    assert all(item["id"] is not None for item in graph["zones"])


def test_stats_zones_count(client: TestClient) -> None:
    assert client.get("/api/stats").json()["zones"] == 0
    _create_zone(client, "区域一")
    _create_zone(client, "区域二")
    assert client.get("/api/stats").json()["zones"] == 2
