"""Retrieval quality evaluation for the Synapse demo corpus.

Reads data/demo_questions.md, calls POST /api/search for every question, then reports
document hit-rate and expected-keyword coverage.

Usage:
    backend/.venv/bin/python scripts/eval_retrieval.py [base_url]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:7800"
TOP_K = 8


def parse_questions(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"^###\s*Q\d+[.、]\s*", text, flags=re.MULTILINE)[1:]
    items: list[dict] = []
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        question = lines[0]
        docs: list[str] = []
        keywords: list[str] = []
        for line in lines[1:]:
            if "期望命中文档" in line:
                raw = line.split("：", 1)[-1] if "：" in line else line.split(":", 1)[-1]
                docs = [
                    d.strip().strip("`")
                    for d in re.split(r"[,，、\s]+", raw)
                    if d.strip() and "无" not in d
                ]
            elif "期望关键词" in line:
                raw = line.split("：", 1)[-1] if "：" in line else line.split(":", 1)[-1]
                keywords = [k.strip() for k in re.split(r"[,，、]+", raw) if k.strip()]
        items.append({"question": question, "docs": docs, "keywords": keywords})
    return items


def main() -> int:
    qpath = ROOT / "data" / "demo_questions.md"
    if not qpath.exists():
        print(f"missing {qpath}")
        return 1
    questions = parse_questions(qpath)
    with httpx.Client(base_url=BASE, timeout=30) as client:
        docs = client.get("/api/documents").json()["items"]
        id2file = {d["id"]: d.get("filename") or d["title"] for d in docs}
        print(f"documents: {len(docs)} | questions: {len(questions)} | base: {BASE}\n")

        hit_total = 0
        kw_total = 0
        kw_hit = 0
        for i, item in enumerate(questions, 1):
            results = client.post(
                "/api/search", json={"query": item["question"], "top_k": TOP_K}
            ).json()["results"]
            got_files = {id2file.get(r["doc_id"], r["doc_title"]) for r in results}
            expected = set(item["docs"])
            hits = expected & got_files
            if expected:
                hit = bool(hits)
            else:
                # “无答案”题：期望检索不到任何文档
                hit = len(results) == 0
                hits = set()
            hit_total += int(hit)
            text = " ".join(r["text"] for r in results)
            found = [k for k in item["keywords"] if k and k in text]
            kw_total += len(item["keywords"])
            kw_hit += len(found)
            mark = "✅" if hit else "❌"
            print(f"{mark} Q{i}: {item['question']}")
            print(f"   expected={sorted(expected) or '-'} hit={sorted(hits) or '-'}")
            print(f"   top={[id2file.get(r['doc_id'], r['doc_title']) for r in results[:4]]}")
            print(f"   keywords {len(found)}/{len(item['keywords'])}: {found}")

        n = max(len(questions), 1)
        print("\n=== summary ===")
        print(f"document hit-rate@k{TOP_K}: {hit_total}/{n} = {hit_total / n:.0%}")
        print(f"keyword coverage: {kw_hit}/{kw_total} = {kw_hit / max(kw_total, 1):.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
