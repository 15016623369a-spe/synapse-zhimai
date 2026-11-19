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
        # 只在「有期望文档」的题上算召回与 MRR（无答案题单独看 hit-rate）
        recall_sum = 0.0
        mrr_sum = 0.0
        answerable = 0
        for i, item in enumerate(questions, 1):
            results = client.post(
                "/api/search", json={"query": item["question"], "top_k": TOP_K}
            ).json()["results"]
            ranked_files = [id2file.get(r["doc_id"], r["doc_title"]) for r in results]
            got_files = set(ranked_files)
            expected = set(item["docs"])
            hits = expected & got_files
            if expected:
                hit = bool(hits)
                recall = len(hits) / len(expected)
                first_rank = next(
                    (
                        rank
                        for rank, name in enumerate(ranked_files, start=1)
                        if name in expected
                    ),
                    None,
                )
                mrr = 1.0 / first_rank if first_rank else 0.0
                answerable += 1
                recall_sum += recall
                mrr_sum += mrr
            else:
                # “无答案”题：期望检索不到任何文档
                hit = len(results) == 0
                hits = set()
                recall = 1.0 if hit else 0.0
                mrr = 1.0 if hit else 0.0
            hit_total += int(hit)
            text = " ".join(r["text"] for r in results)
            found = [k for k in item["keywords"] if k and k in text]
            kw_total += len(item["keywords"])
            kw_hit += len(found)
            mark = "✅" if hit else "❌"
            print(f"{mark} Q{i}: {item['question']}")
            print(f"   expected={sorted(expected) or '-'} hit={sorted(hits) or '-'}")
            print(f"   top={ranked_files[:4]}")
            extra = f" recall={recall:.2f} mrr={mrr:.3f}" if expected else ""
            print(f"   keywords {len(found)}/{len(item['keywords'])}: {found}{extra}")

        n = max(len(questions), 1)
        print("\n=== summary ===")
        print(f"document hit-rate@k{TOP_K}: {hit_total}/{n} = {hit_total / n:.0%}")
        print(
            f"expected-doc recall (avg, n={answerable}): "
            f"{recall_sum / max(answerable, 1):.3f}"
        )
        print(f"MRR@k{TOP_K} (n={answerable}): {mrr_sum / max(answerable, 1):.3f}")
        print(f"keyword coverage: {kw_hit}/{kw_total} = {kw_hit / max(kw_total, 1):.0%}")
        print(
            "\n注：这是 10 题 / 小语料上的回归指标，不是通用准确率；"
            "无答案题不计入 recall/MRR。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
