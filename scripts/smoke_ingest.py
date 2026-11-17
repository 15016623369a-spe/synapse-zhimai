"""Ingestion smoke test: upload md/txt/html/docx and a local URL, verify indexing, clean up.

Usage:
    backend/.venv/bin/python scripts/smoke_ingest.py [base_url]
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7800").rstrip("/")
HTML = """<!doctype html><html><head><title>本地网页测试</title></head>
<body><h1>知识花园</h1><p>这是一段用于测试网页抓取的正文，提到检索、引用和本地知识库。</p>
<script>console.log('should be stripped')</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_docx(path: Path) -> None:
    from docx import Document

    doc = Document()
    doc.add_heading("Docx 导入测试", level=1)
    doc.add_paragraph("这是一份 Word 文档，内容包含向量检索、混合检索和引用溯源。")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "方法"
    table.cell(0, 1).text = "优点"
    table.cell(1, 0).text = "混合检索"
    table.cell(1, 1).text = "召回稳定"
    doc.save(path)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="synapse-ingest-"))
    created: list[str] = []
    failures: list[str] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url_port = server.server_address[1]
    try:
        (tmp / "note.md").write_text(
            "---\ntitle: Markdown 导入测试\ntags: [\"测试\", \"Markdown\"]\nsummary: 用于验证 Markdown 导入。\n---\n"
            "# Markdown 导入测试\n\n## 要点\n\n- 知识库需要分块与概念抽取。\n- 引用要能回到原文。\n",
            encoding="utf-8",
        )
        (tmp / "plain.txt").write_text(
            "纯文本导入测试：这里是一段很长的中文文本，用来验证 txt 解析与分块逻辑。"
            * 12,
            encoding="utf-8",
        )
        (tmp / "page.html").write_text(HTML, encoding="utf-8")
        make_docx(tmp / "report.docx")

        with httpx.Client(base_url=BASE, timeout=60) as c:
            print(f"base = {BASE}")
            files = [
                ("files", ("note.md", (tmp / "note.md").read_bytes(), "text/markdown")),
                ("files", ("plain.txt", (tmp / "plain.txt").read_bytes(), "text/plain")),
                ("files", ("page.html", (tmp / "page.html").read_bytes(), "text/html")),
                (
                    "files",
                    (
                        "report.docx",
                        (tmp / "report.docx").read_bytes(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    ),
                ),
            ]
            r = c.post("/api/documents/upload", files=files, data={"tags": "导入自检,临时"})
            print("upload status:", r.status_code)
            body = r.json() if r.status_code == 200 else {}
            for item in body.get("items", []):
                if item.get("ok"):
                    doc = item["document"]
                    created.append(doc["id"])
                    ok = doc["chunk_count"] > 0 and doc["status"] == "ready"
                    print(f"  {'✅' if ok else '❌'} {doc['filename']}: chunks={doc['chunk_count']} tags={doc['tags']}")
                    if not ok:
                        failures.append(f"upload indexing failed: {doc['filename']}")
                else:
                    failures.append(f"upload failed: {item.get('filename')}: {item.get('error')}")
                    print(f"  ❌ {item.get('filename')}: {item.get('error')}")

            r = c.post(
                "/api/documents/url",
                json={"url": f"http://127.0.0.1:{url_port}/page.html", "tags": ["网页自检"]},
            )
            print("url status:", r.status_code)
            if r.status_code == 200 and r.json().get("ok"):
                doc = r.json()["document"]
                created.append(doc["id"])
                print(f"  ✅ URL《{doc['title']}》chunks={doc['chunk_count']}")
                if doc["chunk_count"] <= 0:
                    failures.append("url document has no chunks")
            else:
                failures.append(f"url ingest failed: {r.text[:200]}")

            # retrieval should reach at least one freshly ingested document
            r = c.post("/api/search", json={"query": "混合检索 召回 引用溯源", "top_k": 8})
            titles = [x["doc_title"] for x in r.json().get("results", [])]
            print("search top:", titles[:5])
            if not any(t for t in titles if any(k in t for k in ("Docx", "Markdown", "网页", "文本"))):
                # fall back to checking any created doc id appears
                ids = {x["doc_id"] for x in r.json().get("results", [])}
                if not ids & set(created):
                    failures.append("freshly ingested docs not retrievable")
    finally:
        if created:
            with httpx.Client(base_url=BASE, timeout=30) as c:
                for doc_id in created:
                    c.delete(f"/api/documents/{doc_id}")
                print(f"cleaned up {len(created)} test documents")
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== summary ===")
    print(f"PASS: {len(created)} documents ingested" + ("" if not failures else f" | FAIL {len(failures)}"))
    for f in failures:
        print(" -", f)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
