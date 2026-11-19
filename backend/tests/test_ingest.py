"""文档解析与入库测试（SPEC 5.1 / 7.2 / 10.1）。"""

from __future__ import annotations

import io
import json

import docx
import httpx
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.services import ingest

from .conftest import DOC_COFFEE, DEMO_MARKDOWN, add_note, seed_corpus, upload_file


def test_upload_markdown_with_frontmatter(client: TestClient) -> None:
    markdown = DEMO_MARKDOWN.format(
        title="RAG 检索增强生成实践笔记",
        tags=json.dumps(["AI", "RAG"], ensure_ascii=False),
        summary="一句话摘要，用于卡片和侧栏展示。",
        body=DOC_COFFEE,
    )
    payload = upload_file(client, "rag-notes.md", markdown.encode("utf-8"))
    item = payload["items"][0]
    assert item["ok"] is True
    document = item["document"]
    assert document["title"] == "RAG 检索增强生成实践笔记"
    assert document["filename"] == "rag-notes.md"
    assert document["source_type"] == "md"
    assert document["tags"] == ["AI", "RAG"]
    assert document["summary"] == "一句话摘要，用于卡片和侧栏展示。"
    assert document["status"] == "ready"
    assert document["error"] is None
    assert document["size"] > 100
    assert document["chunk_count"] >= 1
    assert document["source_url"] is None

    detail = client.get(f"/api/documents/{document['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == document["id"]

    chunks = client.get(f"/api/documents/{document['id']}/chunks").json()["items"]
    assert chunks
    assert "title:" not in chunks[0]["text"]
    assert chunks[0]["char_start"] == 0


def test_invalid_frontmatter_falls_back_to_filename(client: TestClient) -> None:
    markdown = "---\ntitle: 坏标题\ntags: 不是 JSON 数组\n---\n\n正文内容，讲检索与分块。"
    payload = upload_file(client, "bad-front.md", markdown.encode("utf-8"))
    item = payload["items"][0]
    assert item["ok"] is True
    document = item["document"]
    assert document["title"] == "bad-front"
    assert document["tags"] == []
    assert document["chunk_count"] >= 1


def test_upload_txt(client: TestClient) -> None:
    content = "这是一篇纯文本笔记。" * 30
    payload = upload_file(client, "plain-note.txt", content.encode("utf-8"))
    item = payload["items"][0]
    assert item["ok"] is True
    assert item["document"]["title"] == "plain-note"
    assert item["document"]["source_type"] == "txt"


def test_upload_unsupported_type_returns_error_item(client: TestClient) -> None:
    payload = upload_file(client, "virus.exe", b"MZ\x90\x00")
    item = payload["items"][0]
    assert item["ok"] is False
    assert item["filename"] == "virus.exe"
    assert "不支持" in item["error"]
    assert client.get("/api/documents").json()["total"] == 0


def test_upload_docx(client: TestClient) -> None:
    document = docx.Document()
    document.add_heading("Word 文档标题", level=1)
    document.add_paragraph("这是 Word 文档的正文内容，包含检索、知识管理和笔记相关讨论。" * 6)
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "参数"
    table.cell(0, 1).text = "取值"
    table.cell(1, 0).text = "分块大小"
    table.cell(1, 1).text = "450 到 650 字"
    buffer = io.BytesIO()
    document.save(buffer)

    payload = upload_file(client, "word-note.docx", buffer.getvalue())
    item = payload["items"][0]
    assert item["ok"] is True
    assert item["document"]["source_type"] == "docx"
    assert item["document"]["chunk_count"] >= 1


def test_upload_html_strips_script(client: TestClient) -> None:
    html = (
        "<html><head><title>网页标题</title></head><body>"
        "<script>alert('不应出现')</script>"
        "<h1>检索笔记</h1><p>这是网页正文，讨论向量检索与混合检索。</p>"
        "</body></html>"
    )
    payload = upload_file(client, "page.html", html.encode("utf-8"))
    item = payload["items"][0]
    assert item["ok"] is True
    assert item["document"]["title"] == "网页标题"
    assert item["document"]["source_type"] == "html"
    chunks = client.get(f"/api/documents/{item['document']['id']}/chunks").json()["items"]
    assert chunks
    assert "不应出现" not in chunks[0]["text"]


def test_upload_pdf_without_text_reports_error(client: TestClient) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    payload = upload_file(client, "scan.pdf", buffer.getvalue())
    item = payload["items"][0]
    assert item["ok"] is False
    assert item["error"] == "未提取到文本（可能为扫描件）"


def test_upload_multiple_files_partial_failure(client: TestClient) -> None:
    response = client.post(
        "/api/documents/upload",
        files=[
            ("files", ("good.md", "# 好文档\n\n正文内容，讲检索。".encode("utf-8"))),
            ("files", ("bad.xyz", b"123")),
        ],
        data={"tags": "AI, 检索"},
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    good = [item for item in items if item["ok"]]
    bad = [item for item in items if not item["ok"]]
    assert len(good) == 1
    assert len(bad) == 1
    assert good[0]["document"]["tags"] == ["AI", "检索"]
    assert client.get("/api/documents").json()["total"] == 1


def test_text_note_and_detail_and_delete(client: TestClient) -> None:
    document = add_note(client, "我的手冲咖啡笔记", DOC_COFFEE, ["生活", "咖啡"])
    assert document["source_type"] == "note"
    assert document["tags"] == ["生活", "咖啡"]
    assert document["summary"]
    assert document["chunk_count"] >= 1

    detail = client.get(f"/api/documents/{document['id']}")
    assert detail.status_code == 200
    assert detail.json()["title"] == "我的手冲咖啡笔记"

    chunks = client.get(f"/api/documents/{document['id']}/chunks").json()["items"]
    assert chunks
    assert chunks[0]["id"].startswith("c_")
    assert chunks[0]["doc_id"] == document["id"]
    assert chunks[0]["char_start"] < chunks[0]["char_end"]

    deleted = client.delete(f"/api/documents/{document['id']}")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}
    assert client.get(f"/api/documents/{document['id']}").status_code == 404
    assert client.get(f"/api/documents/{document['id']}/chunks").status_code == 404


def test_text_note_requires_content(client: TestClient) -> None:
    response = client.post("/api/documents/text", json={"title": "空", "content": "   "})
    assert response.status_code == 400
    assert response.json()["detail"]


def test_url_import_with_mocked_transport(
    client: TestClient, monkeypatch
) -> None:
    html = (
        "<html><head><title>示例文章</title></head><body>"
        "<article><h1>示例文章</h1><p>这是一篇用于测试网页导入的文章，"
        "讨论检索增强生成与知识管理。</p></article></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "example.com"
        return httpx.Response(
            200, text=html, headers={"content-type": "text/html; charset=utf-8"}
        )

    # 本机 DNS 可能把 example.com 解析到保留网段；测试只关心解析逻辑，
    # SSRF 校验由下面的独立用例覆盖。
    monkeypatch.setenv("SYNAPSE_ALLOW_PRIVATE_FETCH", "1")
    monkeypatch.setattr(ingest, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    response = client.post(
        "/api/documents/url",
        json={"url": "https://example.com/article", "tags": ["AI"]},
    )
    assert response.status_code == 200, response.text
    document = response.json()["document"]
    assert document["title"] == "示例文章"
    assert document["source_type"] == "url"
    assert document["source_url"] == "https://example.com/article"
    assert document["tags"] == ["AI"]
    assert document["chunk_count"] >= 1


def test_url_import_rejects_invalid_scheme(client: TestClient) -> None:
    response = client.post("/api/documents/url", json={"url": "ftp://example.com/a"})
    assert response.status_code == 400
    assert "http/https" in response.json()["detail"]


def test_url_import_blocks_private_address(client: TestClient) -> None:
    """documents/url 必须和知识扩展一样拦内网地址（SECURITY.md 的承诺）。"""
    response = client.post(
        "/api/documents/url", json={"url": "http://127.0.0.1:9/secret"}
    )
    assert response.status_code == 400, response.text
    assert "内网" in response.json()["detail"]


def test_url_import_blocks_redirect_to_private(
    client: TestClient, monkeypatch
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1:9/secret"})

    def fake_guard(url: str) -> None:
        if "127.0.0.1" in url:
            raise ingest.IngestError("抓取被拒绝：暂不支持抓取内网地址")

    monkeypatch.setattr(ingest, "_ensure_url_safe", fake_guard)
    monkeypatch.setattr(ingest, "HTTP_TRANSPORT", httpx.MockTransport(handler))

    response = client.post(
        "/api/documents/url", json={"url": "https://example.com/redirect"}
    )
    assert response.status_code == 400, response.text
    assert "内网" in response.json()["detail"]
    # 第二跳在发请求之前就被拦截
    assert len(calls) == 1


def test_document_list_filters_and_pagination(client: TestClient) -> None:
    documents = seed_corpus(client)

    all_docs = client.get("/api/documents").json()
    assert all_docs["total"] == 4
    assert len(all_docs["items"]) == 4

    by_keyword = client.get("/api/documents", params={"q": "咖啡"}).json()
    assert by_keyword["total"] >= 1
    assert any(item["id"] == documents["coffee"]["id"] for item in by_keyword["items"])

    by_tag = client.get("/api/documents", params={"tag": "AI"}).json()
    assert by_tag["total"] == 2
    assert {item["id"] for item in by_tag["items"]} == {
        documents["rag"]["id"],
        documents["vector"]["id"],
    }

    first_page = client.get("/api/documents", params={"limit": 2, "offset": 0}).json()
    second_page = client.get("/api/documents", params={"limit": 2, "offset": 2}).json()
    assert len(first_page["items"]) == 2
    assert len(second_page["items"]) == 2
    assert {item["id"] for item in first_page["items"]}.isdisjoint(
        {item["id"] for item in second_page["items"]}
    )


def test_delete_document_updates_graph(client: TestClient) -> None:
    documents = seed_corpus(client)
    deleted_id = documents["coffee"]["id"]
    assert client.delete(f"/api/documents/{deleted_id}").status_code == 200

    graph = client.get("/api/graph").json()
    node_ids = {node["id"] for node in graph["nodes"]}
    assert f"doc:{deleted_id}" not in node_ids
    assert graph["meta"]["document_count"] == 3
    assert client.get("/api/documents").json()["total"] == 3
