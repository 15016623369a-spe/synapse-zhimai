"""知识整理（Enrichment）+ PDF 解析增强测试（SPEC-ENRICH 第 5 节）。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.services import embedding, enrich, ingest, llm

from .conftest import add_note

PAPER_TEXT = (
    "NEBPplus: Neural Enhanced Background Prediction\n\n"
    "Abstract: We propose NEBPplus for multiobject tracking, combining belief "
    "propagation with transformer features and a factor graph formulation. "
    "Experiments on nuScenes show that multiobject tracking accuracy improves. "
    "The belief propagation module and the factor graph are trained end-to-end. "
) * 20

ENGLISH_SAMPLE = (
    "Multiobject tracking remains challenging. We combine belief propagation "
    "with a transformer backbone and a factor graph formulation. The belief "
    "propagation layer improves multiobject tracking robustness. Our factor graph "
    "encoder uses a transformer decoder; multiobject tracking experiments on "
    "nuScenes validate the belief propagation design. "
) * 10


def _insert_pdf(content: str = PAPER_TEXT, title: str = "Paper_2024_arXiv2410.06294.pdf",
                filename: str = "Paper_2024_arXiv2410.06294.pdf") -> str:
    doc_id = "d_enrich_pdf"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO documents(id, title, filename, source_type, tags, summary, "
            "content, size, status, error, zone_id, enrich_version, created_at, updated_at) "
            "VALUES(?, ?, ?, 'pdf', '[]', NULL, ?, ?, 'ready', NULL, NULL, 0, ?, ?)",
            (doc_id, title, filename, content, len(content), now, now),
        )
        conn.execute(
            "INSERT INTO chunks(id, doc_id, idx, text, char_start, char_end, "
            "token_count, embedding, created_at) VALUES(?, ?, 0, ?, 0, ?, 20, ?, ?)",
            (
                f"c_{doc_id}",
                doc_id,
                content[:500],
                min(500, len(content)),
                embedding.pack_vector(embedding.embed_text(content[:500])),
                now,
            ),
        )
    return doc_id


def _content_hash(doc_id: str) -> tuple[str, str]:
    with db.get_db() as conn:
        row = db.fetch_one(
            conn, "SELECT content FROM documents WHERE id = ?", (doc_id,)
        )
        content = str(row["content"])
        chunks = db.fetch_all(
            conn,
            "SELECT id, text, embedding FROM chunks WHERE doc_id = ? ORDER BY idx",
            (doc_id,),
        )
    chunk_hash = hashlib.sha256()
    for chunk in chunks:
        chunk_hash.update(str(chunk["id"]).encode())
        chunk_hash.update(str(chunk["text"]).encode())
        chunk_hash.update(bytes(chunk["embedding"] or b""))
    return hashlib.sha256(content.encode()).hexdigest(), chunk_hash.hexdigest()


# ---------------------------------------------------------------------------
# PDF 解析增强
# ---------------------------------------------------------------------------


def test_clean_pdf_pages_removes_headers_and_fixes_hyphenation() -> None:
    pages = [
        "IEEE TRANSACTIONS ON PATTERN ANALYSIS\n1\nLearning multi-\nobject tracking\nPage 1",
        "IEEE TRANSACTIONS ON PATTERN ANALYSIS\n2\ntrain- ing with belief propagation\nPage 2",
        "IEEE TRANSACTIONS ON PATTERN ANALYSIS\n3\nfactor graph and transformer\nPage 3",
    ]
    cleaned = ingest.clean_pdf_pages(pages)
    assert "IEEE TRANSACTIONS" not in cleaned
    assert "Page 1" not in cleaned and "Page 3" not in cleaned
    assert "multiobject tracking" in cleaned
    assert "training with belief propagation" in cleaned


def test_pdf_title_from_first_page() -> None:
    pages = [
        "IEEE TRANSACTIONS ON PATTERN ANALYSIS\n"
        "Neural Enhanced Belief Propagation for Multiobject Tracking\n"
        "Wei Liang, Meyer et al.\n"
        "Abstract: This paper proposes ..."
    ]
    title = ingest._extract_pdf_title(pages, "", "Wei_Liang_2024_arXiv2410.06294.pdf")
    assert "Neural Enhanced Belief Propagation" in title


def test_pdf_title_metadata_preferred() -> None:
    pages = ["Some Header\nActual First Line\nAbstract: ..."]
    title = ingest._extract_pdf_title(pages, "Attention is All you Need", "x.pdf")
    assert title == "Attention is All you Need"


def test_local_keyphrases_english_paper() -> None:
    keywords = enrich.extract_local_keyphrases(ENGLISH_SAMPLE, "Multiobject Tracking")
    joined = " ".join(keywords).lower()
    assert "multiobject tracking" in joined or "multiobject" in joined
    assert "belief propagation" in joined
    assert "factor graph" in joined
    assert "transformer" in joined
    tokens = set(re.findall(r"[a-z]+", joined))
    assert not ({"ieee", "where", "xi", "bj", "yk"} & tokens)
    assert 8 <= len(keywords) <= 12


def test_local_keyphrases_chinese_document() -> None:
    text = (
        "检索增强生成通过分块、向量检索与混合检索提升问答质量。"
        "卡片盒笔记法强调双链与知识管理，费曼学习法用于复习。"
    ) * 8
    keywords = enrich.extract_local_keyphrases(text, "知识管理笔记")
    assert any("检索" in keyword or "知识" in keyword for keyword in keywords)
    assert 8 <= len(keywords) <= 12


def test_local_summary_extracts_abstract() -> None:
    summary = enrich.build_local_summary(PAPER_TEXT, "NEBPplus")
    assert summary
    assert "multiobject tracking" in summary.lower() or "NEBPplus" in summary
    assert len(summary) <= enrich.SUMMARY_MAX


# ---------------------------------------------------------------------------
# enrich service / API（本地回退）
# ---------------------------------------------------------------------------


def test_enrich_document_local_updates_metadata_only(client: TestClient) -> None:
    doc_id = _insert_pdf()
    before_hash = _content_hash(doc_id)

    payload = client.post(
        "/api/admin/enrich",
        json={"scope": "doc_ids", "doc_ids": [doc_id], "limit": 12},
    ).json()
    assert payload["ok"] is True
    assert payload["processed"] == 1
    updated = payload["updated"][0]
    assert updated["mode"] == "local"
    assert 8 <= len(updated["keywords"]) <= 12
    assert updated["tags"]

    detail = client.get(f"/api/documents/{doc_id}").json()
    assert detail["summary"]
    assert detail["tags"]
    assert "enrich" in [entry["kind"] for entry in client.get("/api/stats").json()["recent"]]

    after_hash = _content_hash(doc_id)
    assert after_hash == before_hash  # content / chunks / embedding 完全不变

    with db.get_db() as conn:
        enrich_version = db.fetch_value(
            conn, "SELECT enrich_version FROM documents WHERE id = ?", (doc_id,)
        )
        concept_count = db.fetch_value(
            conn,
            "SELECT COUNT(*) FROM doc_concepts WHERE doc_id = ?",
            (doc_id,),
        )
    assert int(enrich_version) == enrich.ENRICH_VERSION
    assert int(concept_count) >= 8


def test_enrich_layout_title_replaced_when_filename_like(client: TestClient) -> None:
    doc_id = _insert_pdf()
    payload = client.post(
        "/api/admin/enrich", json={"scope": "doc_ids", "doc_ids": [doc_id]}
    ).json()
    title = payload["updated"][0]["title"]
    assert "arXiv2410" not in title or "NEBPplus" in title
    assert "_" not in title


def test_enrich_skips_done_and_supports_force(client: TestClient) -> None:
    doc_id = _insert_pdf()
    first = client.post(
        "/api/admin/enrich", json={"scope": "doc_ids", "doc_ids": [doc_id]}
    ).json()
    assert first["processed"] == 1
    second = client.post(
        "/api/admin/enrich", json={"scope": "doc_ids", "doc_ids": [doc_id]}
    ).json()
    assert second["processed"] == 0
    assert second["skipped"][0]["reason"] == "已整理"
    forced = client.post(
        "/api/admin/enrich",
        json={"scope": "doc_ids", "doc_ids": [doc_id], "force": True},
    ).json()
    assert forced["processed"] == 1


def test_enrich_status(client: TestClient) -> None:
    doc_id = _insert_pdf()
    add_note(client, "普通笔记", "普通中文笔记内容。", ["笔记"])
    status = client.get("/api/admin/enrich/status").json()
    assert status["total_documents"] == 2
    assert status["pending_pdf"] == 1
    assert status["pending_all"] == 2
    assert status["can_enrich"] is True
    assert status["mode"] in {"llm", "local"}
    assert status["sample_pending"]

    client.post("/api/admin/enrich", json={"scope": "pdf"})
    status = client.get("/api/admin/enrich/status").json()
    assert status["pending_pdf"] == 0
    assert status["enriched_documents"] == 1
    assert doc_id not in [item["id"] for item in status["sample_pending"]]


def test_enrich_scope_pdf_only_and_limit(client: TestClient) -> None:
    pdf_id = _insert_pdf()
    note = add_note(client, "笔记文档", "这是普通笔记，不应进入 pdf scope。")
    pdf_result = client.post("/api/admin/enrich", json={"scope": "pdf"}).json()
    assert pdf_result["processed"] == 1
    assert pdf_result["updated"][0]["id"] == pdf_id
    note_detail = client.get(f"/api/documents/{note['id']}").json()
    assert note_detail["tags"] == []

    second_pdf = "d_enrich_pdf2"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO documents(id, title, filename, source_type, tags, content, size, "
            "status, enrich_version, created_at, updated_at) "
            "VALUES(?, ?, ?, 'pdf', '[]', ?, ?, 'ready', 0, ?, ?)",
            (second_pdf, "Second.pdf", "Second.pdf", PAPER_TEXT, len(PAPER_TEXT), now, now),
        )
    limited = client.post(
        "/api/admin/enrich", json={"scope": "pdf", "limit": 1}
    ).json()
    assert limited["processed"] == 1
    assert len(limited["updated"]) == 1


def test_enrich_unknown_scope_returns_400(client: TestClient) -> None:
    response = client.post("/api/admin/enrich", json={"scope": "bogus"})
    assert response.status_code == 400
    assert "scope" in response.json()["detail"]


def test_enrich_updates_graph_keyword_nodes(client: TestClient) -> None:
    doc_id = _insert_pdf()
    before = client.get("/api/graph").json()
    before_tags = {
        node["id"] for node in before["nodes"] if node["type"] == "tag"
    }
    client.post("/api/admin/enrich", json={"scope": "doc_ids", "doc_ids": [doc_id]})
    after = client.get("/api/graph").json()
    after_tags = {node["id"] for node in after["nodes"] if node["type"] == "tag"}
    assert len(after_tags) > len(before_tags)
    tag_links = [
        link
        for link in after["links"]
        if link["type"] == "tagged" and link["source"] == f"doc:{doc_id}"
    ]
    assert tag_links


# ---------------------------------------------------------------------------
# LLM 模式 / 失败回退
# ---------------------------------------------------------------------------

LLM_JSON = {
    "title": "Neural Enhanced Belief Propagation for Multiobject Tracking",
    "summary": "论文提出 NEBPplus，将信念传播与 Transformer 特征结合，用于多目标跟踪。",
    "keywords": [
        "multiobject tracking",
        "belief propagation",
        "transformer",
        "factor graph",
        "neural enhanced belief propagation",
        "multi-object tracking",
        "message passing",
        "data association",
        "track initialization",
        "nuScenes",
    ],
    "tags": ["多目标跟踪", "信念传播", "Transformer", "因子图"],
}


def _enable_mock_llm(client: TestClient) -> None:
    client.put(
        "/api/settings",
        json={"llm": {"base_url": "https://mock.api/v1", "model": "mock", "api_key": "sk-test"}},
    )


def _mock_llm_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(LLM_JSON, ensure_ascii=False)}}
                ]
            },
        )

    monkeypatch.setattr(llm, "HTTP_SYNC_TRANSPORT", httpx.MockTransport(handler))


def test_enrich_llm_mode_updates_title_summary_keywords(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id = _insert_pdf()
    before_hash = _content_hash(doc_id)
    _enable_mock_llm(client)
    _mock_llm_json(monkeypatch)

    result = client.post(
        "/api/admin/enrich", json={"scope": "doc_ids", "doc_ids": [doc_id]}
    ).json()
    assert result["mode"] == "llm"
    updated = result["updated"][0]
    assert updated["mode"] == "llm"
    assert updated["title"] == LLM_JSON["title"]
    assert len(updated["keywords"]) >= 8
    assert "多目标跟踪" in updated["tags"]
    detail = client.get(f"/api/documents/{doc_id}").json()
    assert detail["title"] == LLM_JSON["title"]
    assert _content_hash(doc_id) == before_hash


def test_enrich_llm_failure_falls_back_to_local(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id = _insert_pdf()
    _enable_mock_llm(client)

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(llm, "HTTP_SYNC_TRANSPORT", httpx.MockTransport(fail))
    result = client.post(
        "/api/admin/enrich", json={"scope": "doc_ids", "doc_ids": [doc_id]}
    ).json()
    assert result["processed"] == 1
    assert result["updated"][0]["mode"] == "local"


def test_upload_pdf_auto_enrich(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    parsed = ingest.ParsedDocument(
        title="GarciaSatorras_Welling_2021_NeuralEnhancedBP_AISTATS2021",
        content=PAPER_TEXT,
        source_type="pdf",
        filename="GarciaSatorras_Welling_2021_NeuralEnhancedBP_AISTATS2021.pdf",
    )
    monkeypatch.setattr(ingest, "_parse_pdf", lambda filename, data: parsed)
    response = client.post(
        "/api/documents/upload",
        files={"files": ("paper.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["ok"] is True
    document = item["document"]
    assert document["summary"]
    assert document["tags"]
    assert document["title"] != parsed.title  # 文件名式标题被本地整理替换
    assert document["source_type"] == "pdf"


def test_auto_enrich_can_be_disabled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNAPSE_AUTO_ENRICH", "0")
    parsed = ingest.ParsedDocument(
        title="Some_Paper.pdf",
        content=PAPER_TEXT,
        source_type="pdf",
        filename="Some_Paper.pdf",
    )
    monkeypatch.setattr(ingest, "_parse_pdf", lambda filename, data: parsed)
    response = client.post(
        "/api/documents/upload",
        files={"files": ("paper.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    document = response.json()["items"][0]["document"]
    assert document["tags"] == []


# ---------------------------------------------------------------------------
# 迁移 v5 → v6
# ---------------------------------------------------------------------------

_LEGACY_V5_SCHEMA = """
CREATE TABLE documents(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT, source_type TEXT NOT NULL,
  source_url TEXT, tags TEXT NOT NULL DEFAULT '[]',
  summary TEXT, content TEXT, size INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ready',
  error TEXT, zone_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE chunks(
  id TEXT PRIMARY KEY, doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL, text TEXT NOT NULL, char_start INTEGER, char_end INTEGER,
  token_count INTEGER NOT NULL DEFAULT 0, embedding BLOB, created_at TEXT NOT NULL
);
CREATE TABLE conversations(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def test_migration_v5_to_v6_is_idempotent(tmp_path) -> None:
    legacy_path = tmp_path / "legacy_v5.db"
    conn = sqlite3.connect(legacy_path)
    conn.executescript(_LEGACY_V5_SCHEMA)
    conn.execute(
        "INSERT INTO documents(id, title, source_type, tags, content, size, created_at, updated_at) "
        "VALUES('d_legacy', '历史文档', 'md', '[]', '正文', 2, '2026-11-01T00:00:00Z', "
        "'2026-11-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO chunks(id, doc_id, idx, text, created_at) "
        "VALUES('c_legacy', 'd_legacy', 0, '正文', '2026-11-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('schema_version', '5')"
    )
    conn.commit()
    conn.close()

    db.init_db(legacy_path)
    db.init_db(legacy_path)

    conn = db.connect(legacy_path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            conn.execute(
                "SELECT value FROM settings WHERE key='schema_version'"
            ).fetchone()[0]
            == "6"
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
        assert "enrich_version" in columns
        document = conn.execute("SELECT * FROM documents WHERE id='d_legacy'").fetchone()
        assert document["title"] == "历史文档"
        assert int(document["enrich_version"]) == 0
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
    finally:
        conn.close()
