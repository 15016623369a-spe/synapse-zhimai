"""End-to-end API contract checker for Synapse.

Runs against a live server and verifies the shapes/behaviour described in SPEC.md §7.
Usage:
    backend/.venv/bin/python scripts/check_api.py [base_url]
"""
from __future__ import annotations

import json
import sys
from typing import Any

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7800").rstrip("/")

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name}  {detail}")


def expect_keys(obj: Any, keys: list[str], where: str) -> bool:
    if not isinstance(obj, dict):
        check(f"{where} is object", False, f"got {type(obj).__name__}")
        return False
    missing = [k for k in keys if k not in obj]
    check(f"{where} keys", not missing, f"missing {missing}")
    return not missing


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=60) as c:
        print("\n== health ==")
        r = c.get("/api/health")
        check("health 200", r.status_code == 200, r.text[:200])
        h = r.json() if r.status_code == 200 else {}
        expect_keys(h, ["status", "version", "documents", "chunks", "concepts", "llm", "embedding", "ocr"], "health")
        check("seed documents > 0", h.get("documents", 0) > 0, f"documents={h.get('documents')}")
        ocr_info = h.get("ocr", {})
        check("ocr status", ocr_info.get("engine") == "rapidocr" and int(ocr_info.get("max_pages", 0)) >= 1, str(ocr_info))
        check("seed chunks > 0", h.get("chunks", 0) > 0, f"chunks={h.get('chunks')}")

        print("\n== documents ==")
        r = c.get("/api/documents")
        check("documents 200", r.status_code == 200, r.text[:200])
        body = r.json()
        expect_keys(body, ["items", "total"], "documents")
        docs = body.get("items", [])
        check("documents non-empty", len(docs) > 0, f"total={body.get('total')}")
        if docs:
            d = docs[0]
            expect_keys(
                d,
                ["id", "title", "source_type", "tags", "summary", "chunk_count", "status", "created_at"],
                "document",
            )
            check("tags is list", isinstance(d.get("tags"), list), str(type(d.get("tags"))))
        doc = docs[0] if docs else None
        if doc:
            r = c.get(f"/api/documents/{doc['id']}/chunks")
            check("chunks 200", r.status_code == 200, r.text[:200])
            chunks = r.json().get("items", [])
            check("chunks non-empty", len(chunks) > 0, f"got {len(chunks)}")
            if chunks:
                expect_keys(
                    chunks[0], ["id", "doc_id", "idx", "text", "char_start", "char_end"], "chunk"
                )

        print("\n== search ==")
        r = c.post("/api/search", json={"query": "检索增强生成和向量检索有什么要点？", "top_k": 6})
        check("search 200", r.status_code == 200, r.text[:200])
        results = r.json().get("results", []) if r.status_code == 200 else []
        check("search results non-empty", len(results) > 0, f"got {len(results)}")
        if results:
            expect_keys(
                results[0],
                ["chunk_id", "doc_id", "doc_title", "text", "snippet", "score", "source_type"],
                "search result",
            )
            check("search sorted desc", all(results[i]["score"] >= results[i + 1]["score"] - 1e-9 for i in range(len(results) - 1)), "scores not descending")

        print("\n== graph ==")
        r = c.get("/api/graph")
        check("graph 200", r.status_code == 200, r.text[:200])
        g = r.json() if r.status_code == 200 else {}
        nodes, links = g.get("nodes", []), g.get("links", [])
        check("graph nodes >= 20", len(nodes) >= 20, f"got {len(nodes)}")
        check("graph links >= 20", len(links) >= 20, f"got {len(links)}")
        types = {n.get("type") for n in nodes}
        check("graph has document nodes", "document" in types, str(types))
        check("graph has concept nodes", "concept" in types, str(types))
        ids = {n["id"] for n in nodes}
        if links:
            bad = [l for l in links if l.get("source") not in ids or l.get("target") not in ids]
            check("graph links reference visible nodes", not bad, f"{len(bad)} dangling links")
            expect_keys(links[0], ["id", "source", "target", "type", "weight"], "graph link")
        concept = next((n for n in nodes if n.get("type") == "concept"), None)
        if concept:
            r = c.get("/api/graph/node", params={"id": concept["id"]})
            check("graph node detail 200", r.status_code == 200, r.text[:200])
            if r.status_code == 200:
                expect_keys(r.json(), ["node", "docs", "chunks", "related"], "graph node detail")

        print("\n== stats ==")
        r = c.get("/api/stats")
        check("stats 200", r.status_code == 200, r.text[:200])
        st = r.json() if r.status_code == 200 else {}
        expect_keys(st, ["documents", "chunks", "concepts", "links", "tags", "llm_configured", "recent"], "stats")

        print("\n== settings (masking) ==")
        r = c.get("/api/settings")
        check("settings 200", r.status_code == 200, r.text[:200])
        s = r.json() if r.status_code == 200 else {}
        expect_keys(s, ["llm", "embedding", "data"], "settings")
        llm = s.get("llm", {})
        check("settings has api_key_set flag", "api_key_set" in llm, str(llm))
        check("settings never leaks api_key value", "api_key" not in llm, "api_key field exposed")
        check(
            "embedding provider valid",
            s.get("embedding", {}).get("provider") in {"hash", "local", "fastembed", "openai"},
            str(s.get("embedding")),
        )

        print("\n== create note / search / delete ==")
        note = {
            "title": "契约自检临时笔记",
            "content": "# 临时笔记\n\n这是一篇用于 API 契约自检的临时文档，内容包含关键词：突触、知识图谱、检索。\n\n"
            "## 要点\n\n- 突触连接是知识图谱的隐喻。\n- 混合检索提升召回。\n",
            "tags": ["自检", "临时"],
            "source_type": "note",
        }
        r = c.post("/api/documents/text", json=note)
        check("create note 200", r.status_code == 200, r.text[:300])
        created = r.json().get("document") if r.status_code == 200 else None
        if created:
            r = c.post("/api/search", json={"query": "突触 知识图谱 隐喻", "top_k": 5})
            titles = [x["doc_title"] for x in r.json().get("results", [])]
            check("new note retrievable", any("契约自检" in t for t in titles), str(titles))
            g2 = c.get("/api/graph").json()
            check(
                "new note appears in graph",
                any(n.get("doc_id") == created["id"] for n in g2.get("nodes", [])),
                "note node missing",
            )
            r = c.delete(f"/api/documents/{created['id']}")
            check("delete note 200", r.status_code == 200, r.text[:200])
            r = c.get(f"/api/documents/{created['id']}")
            check("deleted note 404", r.status_code == 404, str(r.status_code))

        print("\n== chat SSE (local extractive) ==")
        events: list[tuple[str, dict]] = []
        convo_id = None
        try:
            with c.stream(
                "POST",
                "/api/chat",
                json={"message": "我的知识库里关于知识管理有哪些要点？"},
                headers={"Accept": "text/event-stream"},
                timeout=90,
            ) as resp:
                check("chat 200", resp.status_code == 200, str(resp.status_code))
                ev, data = "message", ""
                for line in resp.iter_lines():
                    if line.startswith("event:"):
                        ev = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        data += line.split(":", 1)[1].strip()
                    elif line == "":
                        if data:
                            try:
                                payload = json.loads(data)
                            except json.JSONDecodeError:
                                payload = {}
                            events.append((ev, payload))
                        ev, data = "message", ""
        except httpx.HTTPError as exc:
            check("chat stream completed", False, repr(exc))
        kinds = [e for e, _ in events]
        check("SSE has meta", "meta" in kinds, str(kinds))
        check("SSE has sources", "sources" in kinds, str(kinds))
        check("SSE has delta", "delta" in kinds, str(kinds))
        check("SSE has done", "done" in kinds, str(kinds))
        text = "".join(p.get("text", "") for e, p in events if e == "delta")
        check("answer length > 40", len(text) > 40, f"len={len(text)}")
        sources = next((p.get("citations") for e, p in events if e == "sources"), [])
        check("citations non-empty", bool(sources), str(sources))
        if sources:
            expect_keys(
                sources[0],
                ["index", "doc_id", "doc_title", "chunk_id", "source_type", "snippet", "score"],
                "citation",
            )
        done = next((p for e, p in events if e == "done"), {})
        convo_id = done.get("conversation_id") or next(
            (p.get("conversation_id") for e, p in events if e == "meta"), None
        )
        check("done has assistant message_id", bool(done.get("message_id")), str(done))

        if convo_id:
            print("\n== conversations ==")
            r = c.get(f"/api/conversations/{convo_id}")
            check("conversation 200", r.status_code == 200, r.text[:200])
            msgs = r.json().get("messages", []) if r.status_code == 200 else []
            check("conversation has 2 messages", len(msgs) == 2, f"got {len(msgs)}")
            check(
                "assistant message persisted",
                any(m.get("role") == "assistant" and len(m.get("content", "")) > 40 for m in msgs),
                str([m.get("role") for m in msgs]),
            )
            r = c.get("/api/conversations")
            check("conversations list 200", r.status_code == 200, r.text[:200])
            r = c.delete(f"/api/conversations/{convo_id}")
            check("conversation delete 200", r.status_code == 200, r.text[:200])

    print("\n=== summary ===")
    print(f"PASS {len(PASS)} | FAIL {len(FAIL)}")
    for f in FAIL:
        print(f"  - {f}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
