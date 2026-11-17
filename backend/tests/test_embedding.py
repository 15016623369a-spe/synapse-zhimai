"""真实语义 embedding 测试（SPEC-V2-OCR-EMBED 功能 B）。

覆盖：默认 hash 兼容、动态维度 pack/unpack/cosine、status/config 校验、
API key 脱敏、needs_rebuild、mock 重建（fastembed/openai）、重建失败不破坏、
openai 兼容 /embeddings（MockTransport + 分批 32）、失败回退 hash、health 展示。
真实模型加载不在单元测试里执行（用 monkeypatch），真实验证见 7812 手工验收。
"""

from __future__ import annotations

import json
import logging

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db
from app.services import embedding

from .conftest import add_note

STATUS_KEYS = {
    "provider",
    "model",
    "dim",
    "total_chunks",
    "ready",
    "needs_rebuild",
    "has_api_key",
    "providers",
    "last_rebuild",
}


def _chunks(client: TestClient) -> list[dict]:
    documents = client.get("/api/documents").json()["items"]
    items: list[dict] = []
    for document in documents:
        items.extend(
            client.get(f"/api/documents/{document['id']}/chunks").json()["items"]
        )
    return items


def _embedding_blobs() -> list[bytes | None]:
    with db.get_db() as conn:
        rows = db.fetch_all(conn, "SELECT embedding FROM chunks ORDER BY id")
    return [row["embedding"] for row in rows]


# ---------------------------------------------------------------------------
# 默认 hash 兼容 / 动态维度
# ---------------------------------------------------------------------------


def test_provider_info_defaults_to_hash(data_dir) -> None:
    db.init_db()
    assert embedding.provider_info() == {"provider": "hash", "dim": 384}
    vector = embedding.embed_text("知识图谱与语义检索")
    assert vector.shape == (384,)
    assert float(np.linalg.norm(vector)) == pytest.approx(1.0, abs=1e-5)
    batch = embedding.embed_texts(["知识图谱与语义检索"])
    np.testing.assert_allclose(batch[0], vector, rtol=1e-6, atol=1e-6)


def test_pack_unpack_dynamic_dimension_and_cosine() -> None:
    vector = np.arange(7, dtype=np.float32)
    blob = embedding.pack_vector(vector)
    assert len(blob) == 7 * 4
    restored = embedding.unpack_vector(blob)
    assert restored.size == 7
    np.testing.assert_allclose(restored, vector)
    assert embedding.cosine(vector, vector) == pytest.approx(1.0)
    assert embedding.cosine(vector, np.ones(5, dtype=np.float32)) == 0.0
    assert embedding.unpack_vector(None).size == 0
    assert embedding.unpack_vector(b"abc").size == 0  # 损坏 BLOB → 零长度
    assert embedding.cosine(np.zeros(4, dtype=np.float32), vector) == 0.0


def test_hash_embedding_deterministic_and_compatible(data_dir) -> None:
    db.init_db()
    first = embedding.embed_text("RAG 检索增强生成")
    second = embedding.embed_text("RAG 检索增强生成")
    np.testing.assert_allclose(first, second)
    assert embedding.pack_vector(first) == embedding.pack_vector(second)
    other = embedding.embed_text("手冲咖啡参数")
    assert embedding.cosine(first, other) < 0.9


# ---------------------------------------------------------------------------
# status / config
# ---------------------------------------------------------------------------


def test_status_structure(client: TestClient) -> None:
    payload = client.get("/api/admin/embedding/status").json()
    assert set(payload) == STATUS_KEYS
    assert payload["provider"] == "hash"
    assert payload["model"] == ""
    assert payload["dim"] == 384
    assert payload["total_chunks"] == 0
    assert payload["ready"] is True
    assert payload["needs_rebuild"] is False
    assert payload["has_api_key"] is False
    assert payload["providers"] == ["hash", "fastembed", "openai"]
    assert payload["last_rebuild"] is None


def test_config_validation(client: TestClient) -> None:
    invalid = client.post("/api/admin/embedding/config", json={"provider": "bogus"})
    assert invalid.status_code == 400
    assert "不支持的向量提供方" in invalid.json()["detail"]

    missing = client.post("/api/admin/embedding/config", json={})
    assert missing.status_code == 400
    assert "缺少向量提供方" in missing.json()["detail"]

    bad_model = client.post(
        "/api/admin/embedding/config",
        json={"provider": "fastembed", "model": "no/such-model"},
    )
    assert bad_model.status_code == 400
    assert "fastembed" in bad_model.json()["detail"]

    saved = client.post(
        "/api/admin/embedding/config",
        json={"provider": "fastembed", "model": "BAAI/bge-small-zh-v1.5"},
    )
    assert saved.status_code == 200
    payload = saved.json()
    assert payload["provider"] == "fastembed"
    assert payload["model"] == "BAAI/bge-small-zh-v1.5"
    assert payload["dim"] == 512
    assert payload["ready"] is True

    # 省略 model 时沿用上一次可用的 fastembed 模型
    again = client.post("/api/admin/embedding/config", json={"provider": "fastembed"})
    assert again.status_code == 200
    assert again.json()["model"] == "BAAI/bge-small-zh-v1.5"


def test_config_api_key_masked(client: TestClient) -> None:
    response = client.post(
        "/api/admin/embedding/config",
        json={
            "provider": "openai",
            "model": "text-embedding-3-small",
            "base_url": "https://api.example.com/v1/",
            "api_key": "sk-embed-secret-value",
        },
    )
    assert response.status_code == 200
    assert "sk-embed-secret-value" not in response.text
    payload = response.json()
    assert payload["provider"] == "openai"
    assert payload["model"] == "text-embedding-3-small"
    assert payload["has_api_key"] is True

    fetched = client.get("/api/admin/embedding/status")
    assert "sk-embed-secret-value" not in fetched.text
    assert fetched.json()["has_api_key"] is True
    assert "api_key" not in fetched.json()

    cleared = client.post(
        "/api/admin/embedding/config", json={"provider": "openai", "api_key": ""}
    ).json()
    assert cleared["has_api_key"] is False
    # base_url 已去掉末尾斜杠（再保存一次到配置里）
    with db.get_db() as conn:
        assert db.setting_get(conn, "embedding_base_url") == "https://api.example.com/v1"


def test_needs_rebuild_after_provider_switch(client: TestClient) -> None:
    add_note(client, "RAG 笔记", "RAG 检索增强生成使用分块与向量。", ["AI"])
    assert client.get("/api/admin/embedding/status").json()["needs_rebuild"] is False

    switched = client.post(
        "/api/admin/embedding/config", json={"provider": "fastembed"}
    ).json()
    assert switched["needs_rebuild"] is True
    assert switched["total_chunks"] >= 1

    back = client.post("/api/admin/embedding/config", json={"provider": "hash"}).json()
    assert back["needs_rebuild"] is False


# ---------------------------------------------------------------------------
# rebuild
# ---------------------------------------------------------------------------


def test_rebuild_with_mocked_fastembed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_note(client, "RAG 笔记", "RAG 检索增强生成需要分块、向量与引用。", ["AI"])
    add_note(client, "知识管理", "卡片盒笔记法强调卡片之间的连接。", ["笔记"])
    before = _chunks(client)
    client.post("/api/admin/embedding/config", json={"provider": "fastembed"})

    def fake_fastembed(model_name: str, texts: list[str], batch_size: int):
        base = np.zeros(384, dtype=np.float32)
        base[0] = 1.0
        return [base.copy() for _ in texts]

    monkeypatch.setattr(embedding, "_fastembed_embed", fake_fastembed)
    response = client.post("/api/admin/embedding/rebuild")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["provider"] == "fastembed"
    assert payload["dim"] == 384
    assert payload["chunks"] == len(before)
    assert payload["seconds"] >= 0

    after = _chunks(client)
    assert [item["text"] for item in after] == [item["text"] for item in before]
    assert [item["id"] for item in after] == [item["id"] for item in before]
    for blob in _embedding_blobs():
        assert len(bytes(blob or b"")) == 384 * 4

    status = client.get("/api/admin/embedding/status").json()
    assert status["needs_rebuild"] is False
    assert status["provider"] == "fastembed"
    assert status["last_rebuild"]
    with db.get_db() as conn:
        assert db.setting_get(conn, "embedding_version") == "1"
        assert db.setting_get(conn, "embedding_dim") == "384"


def test_rebuild_dynamic_dimension_openai(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_note(client, "RAG 笔记", "RAG 检索增强生成需要分块与向量。", ["AI"])
    add_note(client, "知识管理", "卡片盒笔记法强调卡片之间的连接。", ["笔记"])
    total = len(_chunks(client))
    client.post(
        "/api/admin/embedding/config",
        json={
            "provider": "openai",
            "model": "text-embedding-3-small",
            "base_url": "https://api.example.com/v1",
        },
    )

    def fake_openai(cfg, texts: list[str], batch_size: int):
        vectors = []
        for index, _text in enumerate(texts):
            vector = np.array(
                [1.0, float(index + 1), 0.5, 0.25, 2.0, 0.1, 0.3, 0.7],
                dtype=np.float32,
            )
            vectors.append(vector / np.linalg.norm(vector))
        return vectors

    monkeypatch.setattr(embedding, "_openai_embed", fake_openai)
    response = client.post("/api/admin/embedding/rebuild")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["dim"] == 8
    assert payload["chunks"] == total
    for blob in _embedding_blobs():
        assert len(bytes(blob or b"")) == 8 * 4

    status = client.get("/api/admin/embedding/status").json()
    assert status["provider"] == "openai"
    assert status["dim"] == 8
    assert status["needs_rebuild"] is False

    # 动态维度下检索仍可用（dense 分数按 8 维计算）
    search = client.post("/api/search", json={"query": "RAG 检索 分块", "top_k": 5})
    assert search.status_code == 200
    assert search.json()["results"]


def test_rebuild_failure_keeps_old_vectors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_note(client, "RAG 笔记", "RAG 检索增强生成需要分块与向量。", ["AI"])
    client.post(
        "/api/admin/embedding/config",
        json={
            "provider": "openai",
            "model": "text-embedding-3-small",
            "base_url": "https://api.example.com/v1",
        },
    )
    before = _embedding_blobs()

    def broken(cfg, texts: list[str], batch_size: int):
        raise embedding.EmbeddingRemoteError("上游 embedding 服务不可用")

    monkeypatch.setattr(embedding, "_openai_embed", broken)
    response = client.post("/api/admin/embedding/rebuild")
    assert response.status_code == 502
    assert "不可用" in response.json()["detail"]
    assert _embedding_blobs() == before
    status = client.get("/api/admin/embedding/status").json()
    assert status["needs_rebuild"] is True
    with db.get_db() as conn:
        assert db.setting_get(conn, "embedding_version") is None


# ---------------------------------------------------------------------------
# OpenAI 兼容接口（MockTransport，不访问真实网络）
# ---------------------------------------------------------------------------


def test_openai_test_endpoint_with_mock_transport(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content.decode("utf-8"))
        count = len(payload["input"])
        data = [
            {"index": index, "embedding": [1.0, float(index + 1), 0.5, 0.2]}
            for index in range(count)
        ]
        return httpx.Response(200, json={"data": data})

    monkeypatch.setattr(embedding, "HTTP_SYNC_TRANSPORT", httpx.MockTransport(handler))
    response = client.post(
        "/api/admin/embedding/test",
        json={
            "provider": "openai",
            "model": "text-embedding-3-small",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-test-key",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["provider"] == "openai"
    assert payload["dim"] == 4
    assert 0.0 < payload["similarity"] <= 1.0
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/embeddings"
    assert requests[0].headers["authorization"] == "Bearer sk-test-key"
    # 测试不保存配置
    assert client.get("/api/admin/embedding/status").json()["provider"] == "hash"


def test_openai_embed_batches_of_32(monkeypatch: pytest.MonkeyPatch) -> None:
    batch_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        count = len(payload["input"])
        batch_sizes.append(count)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [1.0, float(index), 0.5]}
                    for index in range(count)
                ]
            },
        )

    monkeypatch.setattr(embedding, "HTTP_SYNC_TRANSPORT", httpx.MockTransport(handler))
    cfg = {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "base_url": "https://api.example.com/v1",
        "api_key": "",
    }
    vectors = embedding._openai_embed(cfg, ["文本"] * 40, 32)
    assert len(vectors) == 40
    assert vectors[0].size == 3
    assert batch_sizes == [32, 8]  # 分批 32


def test_openai_http_error_raises_chinese_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api key"})

    monkeypatch.setattr(embedding, "HTTP_SYNC_TRANSPORT", httpx.MockTransport(handler))
    cfg = {
        "provider": "openai",
        "model": "m",
        "base_url": "https://api.example.com/v1",
        "api_key": "bad",
    }
    with pytest.raises(embedding.EmbeddingRemoteError) as excinfo:
        embedding._openai_embed(cfg, ["文本"], 32)
    assert "鉴权失败" in str(excinfo.value)
    assert excinfo.value.status_code == 502


# ---------------------------------------------------------------------------
# 失败回退 / health 展示
# ---------------------------------------------------------------------------


def test_embed_text_falls_back_to_hash_when_provider_fails(
    data_dir, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db.init_db()
    with db.get_db() as conn:
        db.setting_set(conn, "embedding_provider", "fastembed")

    def broken(model_name: str, texts: list[str], batch_size: int):
        raise embedding.EmbeddingError("本地语义模型加载失败")

    monkeypatch.setattr(embedding, "_fastembed_embed", broken)
    with caplog.at_level(logging.WARNING, logger="synapse.embedding"):
        vector = embedding.embed_text("语义检索测试")
    assert vector.shape == (384,)
    assert any("回退" in record.getMessage() for record in caplog.records)


def test_health_embedding_reflects_current_provider(client: TestClient) -> None:
    default = client.get("/api/health").json()
    assert default["embedding"] == {"provider": "hash", "dim": 384}

    client.post(
        "/api/admin/embedding/config",
        json={"provider": "fastembed", "model": "BAAI/bge-small-zh-v1.5"},
    )
    fastembed_health = client.get("/api/health").json()
    assert fastembed_health["embedding"] == {"provider": "fastembed", "dim": 512}

    client.post(
        "/api/admin/embedding/config",
        json={
            "provider": "openai",
            "model": "text-embedding-3-small",
            "base_url": "https://api.example.com/v1",
        },
    )
    openai_health = client.get("/api/health").json()
    assert openai_health["embedding"]["provider"] == "openai"
    assert openai_health["embedding"]["dim"] == 0  # 重建前维度未知
