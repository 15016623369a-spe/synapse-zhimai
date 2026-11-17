"""通用 API 测试：health / stats / settings / 会话 / 管理 / 静态托管 / 种子。"""

from __future__ import annotations

import json
import re

import httpx
from fastapi.testclient import TestClient

from app import config, db
from app.services import graph, llm, seed

from .conftest import CORPUS, add_note, chat_events, seed_corpus


def test_health_structure(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    assert set(payload) == {
        "status",
        "version",
        "documents",
        "chunks",
        "concepts",
        "llm",
        "embedding",
        "ocr",
        "search",
    }
    assert payload["status"] == "ok"
    assert payload["version"] == "1.0.0"
    assert payload["documents"] == 0
    assert payload["chunks"] == 0
    assert payload["llm"] == {"configured": False, "model": "", "base_url": ""}
    assert payload["embedding"] == {"provider": "hash", "dim": 384}
    assert payload["ocr"] == {"available": True, "engine": "rapidocr", "max_pages": 30}


def test_health_counts_reflect_documents(client: TestClient) -> None:
    seed_corpus(client)
    payload = client.get("/api/health").json()
    assert payload["documents"] == 4
    assert payload["chunks"] >= 4
    assert payload["concepts"] > 0


def test_stats_structure(client: TestClient) -> None:
    seed_corpus(client)
    payload = client.get("/api/stats").json()
    assert set(payload) == {
        "documents",
        "chunks",
        "concepts",
        "links",
        "zones",
        "tags",
        "llm_configured",
        "plans",
        "recent",
    }
    assert payload["zones"] == 0
    assert payload["plans"] == {"total": 0, "today_checked": 0, "groups": 0}
    assert payload["documents"] == 4
    assert payload["chunks"] >= 4
    assert payload["concepts"] > 0
    assert payload["links"] > 0
    assert payload["llm_configured"] is False
    assert payload["tags"]
    for tag in payload["tags"]:
        assert set(tag) == {"name", "count"}
        assert tag["count"] >= 1
    counts = [tag["count"] for tag in payload["tags"]]
    assert counts == sorted(counts, reverse=True)
    assert payload["recent"]
    for entry in payload["recent"]:
        assert set(entry) == {"kind", "detail", "created_at"}
    assert any("新增文档" in entry["detail"] for entry in payload["recent"])


def test_settings_initial_defaults(client: TestClient) -> None:
    payload = client.get("/api/settings").json()
    assert set(payload) == {"llm", "embedding", "data"}
    assert payload["llm"]["configured"] is False
    assert payload["llm"]["api_key_set"] is False
    assert payload["llm"]["temperature"] == 0.2
    assert payload["embedding"] == {"provider": "hash", "dim": 384}
    assert payload["data"] == {"documents": 0, "chunks": 0}


def test_settings_update_never_leaks_api_key(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={
            "llm": {
                "base_url": "https://api.deepseek.com/v1/",
                "model": "deepseek-chat",
                "api_key": "sk-super-secret-key",
                "temperature": 0.5,
            }
        },
    )
    assert response.status_code == 200
    assert "sk-super-secret-key" not in response.text
    payload = response.json()
    assert payload["llm"]["base_url"] == "https://api.deepseek.com/v1"
    assert payload["llm"]["model"] == "deepseek-chat"
    assert payload["llm"]["configured"] is True
    assert payload["llm"]["api_key_set"] is True
    assert payload["llm"]["temperature"] == 0.5
    assert "api_key" not in payload["llm"]

    fetched = client.get("/api/settings")
    assert "sk-super-secret-key" not in fetched.text
    assert fetched.json()["llm"]["api_key_set"] is True

    # 省略 api_key = 保持不变
    updated = client.put("/api/settings", json={"llm": {"model": "new-model"}}).json()
    assert updated["llm"]["api_key_set"] is True
    assert updated["llm"]["base_url"] == "https://api.deepseek.com/v1"
    assert updated["llm"]["model"] == "new-model"

    # 空串 = 清除 key
    cleared = client.put("/api/settings", json={"llm": {"api_key": ""}}).json()
    assert cleared["llm"]["api_key_set"] is False

    # 清除 base_url / model
    blank = client.put(
        "/api/settings", json={"llm": {"base_url": "", "model": ""}}
    ).json()
    assert blank["llm"]["configured"] is False


def test_settings_temperature_is_clamped(client: TestClient) -> None:
    client.put("/api/settings", json={"llm": {"temperature": 5}})
    assert client.get("/api/settings").json()["llm"]["temperature"] == 2.0
    client.put("/api/settings", json={"llm": {"temperature": -1}})
    assert client.get("/api/settings").json()["llm"]["temperature"] == 0.0


def test_settings_test_connection_success(client: TestClient, monkeypatch) -> None:
    client.put(
        "/api/settings",
        json={
            "llm": {
                "base_url": "https://mock.api/v1",
                "model": "mock-chat",
                "api_key": "sk-test-secret-123",
            }
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 8
        assert payload["stream"] is False
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "pong"}}], "model": "mock-chat"},
        )

    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    response = client.post("/api/settings/test", json={})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["model"] == "mock-chat"
    assert payload["message"] == "连接成功"
    assert isinstance(payload["latency_ms"], int)
    assert payload["latency_ms"] >= 0
    assert "sk-test-secret-123" not in response.text


def test_settings_test_connection_failure_returns_200(
    client: TestClient, monkeypatch
) -> None:
    client.put(
        "/api/settings",
        json={"llm": {"base_url": "https://mock.api/v1", "model": "mock-chat"}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    response = client.post("/api/settings/test")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert "鉴权" in payload["message"]


def test_settings_test_connection_messages_are_actionable(
    client: TestClient, monkeypatch
) -> None:
    client.put(
        "/api/settings",
        json={"llm": {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b"}},
    )

    # 404：提示检查 Base URL / 服务是否启动（Ollama 常见场景）
    monkeypatch.setattr(
        llm,
        "HTTP_TRANSPORT",
        httpx.MockTransport(lambda request: httpx.Response(404, json={})),
    )
    payload = client.post("/api/settings/test", json={}).json()
    assert payload["ok"] is False
    assert "Base URL" in payload["message"]
    assert "模型服务已启动" in payload["message"]

    # 连接被拒绝：提示确认服务是否启动
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(refuse))
    payload = client.post("/api/settings/test", json={}).json()
    assert payload["ok"] is False
    assert "无法连接模型服务" in payload["message"]
    assert "已启动" in payload["message"]


def test_settings_test_without_config(client: TestClient) -> None:
    response = client.post("/api/settings/test", json={})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert "尚未配置" in payload["message"]


def test_conversation_delete(client: TestClient) -> None:
    seed_corpus(client)
    events, _ = chat_events(client, "RAG 是什么？")
    conversation_id = events[0]["data"]["conversation_id"]
    assert client.delete(f"/api/conversations/{conversation_id}").status_code == 200
    assert client.get(f"/api/conversations/{conversation_id}").status_code == 404
    assert client.get("/api/conversations").json()["items"] == []
    assert client.delete("/api/conversations/conv_missing").status_code == 404


def test_admin_reseed_requires_demo_docs(client: TestClient) -> None:
    response = client.post("/api/admin/reseed", json={"force": True})
    assert response.status_code == 400
    assert "演示文档目录为空" in response.json()["detail"]


def test_admin_reseed_imports_demo_docs(seeded_client: TestClient) -> None:
    seed_corpus(seeded_client)
    events, _ = chat_events(seeded_client, "RAG 分块参数？")
    conversation_id = events[0]["data"]["conversation_id"]

    total_before = seeded_client.get("/api/documents").json()["total"]
    assert total_before == len(CORPUS) + 4

    response = seeded_client.post("/api/admin/reseed", json={"force": True})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["imported"] == len(CORPUS)
    assert payload["skipped"] == 0

    assert seeded_client.get("/api/documents").json()["total"] == len(CORPUS)
    # 会话不受 reseed 影响
    conversations = seeded_client.get("/api/conversations").json()["items"]
    assert any(item["id"] == conversation_id for item in conversations)


def test_startup_seed_loads_demo_docs(seeded_client: TestClient) -> None:
    assert seeded_client.get("/api/health").json()["documents"] == len(CORPUS)
    documents = seeded_client.get("/api/documents").json()
    assert documents["total"] == len(CORPUS)
    stats = seeded_client.get("/api/stats").json()
    assert any(entry["detail"] == "载入演示知识库" for entry in stats["recent"])
    graph = seeded_client.get("/api/graph").json()
    assert graph["meta"]["document_count"] == len(CORPUS)
    assert graph["links"]


def test_spa_fallback_and_unknown_api(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    dist_index = config.frontend_dist_dir() / "index.html"
    if dist_index.is_file():
        assert "text/html" in response.headers["content-type"]
    else:
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["api"] == "/api/health"

    unknown_api = client.get("/api/unknown-endpoint")
    assert unknown_api.status_code == 404
    assert unknown_api.json()["detail"] == "接口不存在"

    spa_route = client.get("/some/deep/route")
    assert spa_route.status_code == 200
    if dist_index.is_file():
        assert "text/html" in spa_route.headers["content-type"]
    else:
        assert spa_route.json()["status"] == "ok"


def test_static_serving_and_path_traversal_safety(client: TestClient) -> None:
    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/")
    assert client.head("/").status_code == 200

    dist_dir = config.frontend_dist_dir()
    index_file = dist_dir / "index.html"
    if not index_file.is_file():
        return
    index_html = index_file.read_text(encoding="utf-8")
    asset_paths = re.findall(r'(?:src|href)="(/[^"]+)"', index_html)
    assert asset_paths, "构建产物应引用至少一个静态资源"
    for asset_path in asset_paths:
        response = client.get(asset_path)
        assert response.status_code == 200, asset_path
        assert response.content, asset_path
    # SPA 深层路由回退到 index.html
    spa = client.get("/library/deep/route")
    assert spa.status_code == 200
    assert "text/html" in spa.headers["content-type"]
    # 路径穿越与不存在的资源都必须 404，且不泄露文件
    for bad_path in (
        "/assets/%2e%2e/%2e%2e/SPEC.md",
        "/..%2f..%2fSPEC.md",
        "/assets/missing-file.js",
    ):
        response = client.get(bad_path)
        assert response.status_code == 404, bad_path
        assert response.json()["detail"] == "资源不存在"


def test_error_responses_are_chinese_json(client: TestClient) -> None:
    missing = client.get("/api/documents/d_missing")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "文档不存在"}

    invalid = client.post("/api/search", json={"query": 123})
    assert invalid.status_code == 422
    assert "请求参数不合法" in invalid.json()["detail"]

    bad_body = client.post("/api/documents/text", json={"content": ""})
    assert bad_body.status_code in {400, 422}
    assert "detail" in bad_body.json()


def test_concept_reindex_is_idempotent(client: TestClient) -> None:
    add_note(client, "概念索引测试", "检索增强生成需要分块、向量和混合检索，这是知识管理的一部分。")
    with db.get_db() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (seed.CONCEPT_VERSION_KEY,))
        conn.commit()
    assert seed.ensure_concept_index() is True
    with db.get_db() as conn:
        stored = db.setting_get(conn, seed.CONCEPT_VERSION_KEY)
        concept_count = db.table_count(conn, "concepts")
        link_count = db.table_count(conn, "links")
    assert stored == graph.CONCEPT_EXTRACTION_VERSION
    assert concept_count > 0
    assert link_count > 0
    # 版本已是最新，再次调用不重复重建
    assert seed.ensure_concept_index() is False


def test_note_creation_and_stats_activity(client: TestClient) -> None:
    document = add_note(client, "动态测试笔记", "这是一段用于测试统计动态的笔记内容。")
    stats = client.get("/api/stats").json()
    assert stats["documents"] == 1
    assert any(
        entry["kind"] == "upload" and document["title"] in entry["detail"]
        for entry in stats["recent"]
    )
