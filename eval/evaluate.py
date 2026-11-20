#!/usr/bin/env python3
"""在 MultiHop-RAG 上评测 Synapse 的检索与拒答（离线、可复现）。

指标：
- doc Recall@5：期望文档被召回的比例（多证据题取平均）
- Complete Recall@5：所有期望文档都进 top-5 的题占比（多跳完整召回）
- MRR@10：第一个期望文档的倒数排名
- Hit@5：top-5 至少命中一个期望文档
- Fact Recall@10：期望证据句能在 top-10 chunk 原文里找到的比例
- Gap 误报率：可答题里被判定为「知识缺口」的比例（越低越好）
- Null 拒答率：null_query 里被判定为缺口/拒答的比例（越高越好）

用法：
    backend/.venv/bin/python eval/evaluate.py --sample 600
    backend/.venv/bin/python eval/evaluate.py --all --top-k 10
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = Path(__file__).resolve().parent
TOKEN_RE = re.compile(r"[a-z0-9]+")
SPACE_RE = re.compile(r"\s+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(EVAL_DIR / "data"))
    parser.add_argument("--state-dir", default=str(EVAL_DIR / "state"))
    parser.add_argument("--sample", type=int, default=600, help="分层抽样题数；0 表示全量")
    parser.add_argument("--all", action="store_true", help="跑全部 2556 题")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--out", default=str(EVAL_DIR / "reports"))
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（冒烟）")
    return parser.parse_args()


def normalize(text: str) -> str:
    return SPACE_RE.sub(" ", str(text or "")).strip().lower()


def tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(normalize(text)))


def stratified_sample(questions: list[dict], total: int, seed: int) -> list[dict]:
    if total <= 0 or total >= len(questions):
        return list(questions)
    by_type: dict[str, list[dict]] = {}
    for item in questions:
        by_type.setdefault(str(item.get("question_type") or "unknown"), []).append(item)
    rng = random.Random(seed)
    per_type = max(1, total // len(by_type))
    picked: list[dict] = []
    for items in by_type.values():
        rng.shuffle(items)
        picked.extend(items[:per_type])
    remainder = total - len(picked)
    if remainder > 0:
        rest = [item for item in questions if item not in picked]
        rng.shuffle(rest)
        picked.extend(rest[:remainder])
    return picked


def evidence_titles(question: dict) -> list[str]:
    seen: list[str] = []
    for item in question.get("evidence_list") or []:
        title = str(item.get("title") or "").strip()
        if title and title not in seen:
            seen.append(title)
    return seen


def fact_hit(fact: str, chunks: list[str], threshold: float = 0.8) -> bool:
    target = normalize(fact)
    if not target:
        return False
    for chunk in chunks:
        if target in normalize(chunk):
            return True
    fact_tokens = tokens(fact)
    if not fact_tokens:
        return False
    for chunk in chunks:
        overlap = len(fact_tokens & tokens(chunk)) / len(fact_tokens)
        if overlap >= threshold:
            return True
    return False


def mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 4) if values else 0.0


def opt_mean(values: list[float]) -> float | None:
    """空分母返回 None，报告里显示为「—」，避免把「没有样本」误读成 0。"""
    return round(statistics.fmean(values), 4) if values else None


def main() -> int:
    args = parse_args()
    state_dir = Path(args.state_dir).resolve()
    data_dir = Path(args.data_dir).resolve()

    os.environ["SYNAPSE_DATA_DIR"] = str(state_dir / "data")
    os.environ["SYNAPSE_PATROL_AUTOSTART"] = "0"
    os.environ["SYNAPSE_LLM_BASE_URL"] = ""
    os.environ["SYNAPSE_LLM_API_KEY"] = ""
    os.environ["SYNAPSE_LLM_MODEL"] = ""
    os.environ.pop("SYNAPSE_ALLOW_PRIVATE_FETCH", None)
    sys.path.insert(0, str(ROOT / "backend"))

    from app import db  # noqa: PLC0415
    from app.services import agent, retrieval  # noqa: PLC0415

    title_to_id = json.loads(
        (state_dir / "title_to_id.json").read_text(encoding="utf-8")
    )
    questions = json.loads((data_dir / "questions.json").read_text(encoding="utf-8"))
    if args.all:
        sampled = questions
    else:
        sampled = stratified_sample(questions, args.sample, args.seed)
    if args.limit:
        sampled = sampled[: args.limit]

    print(f"语料 {len(title_to_id)} 篇 | 题目 {len(sampled)} 道 | top_k={args.top_k}")

    conn = db.connect()
    per_type: dict[str, list[dict]] = {}
    rows: list[dict] = []
    started = time.perf_counter()
    try:
        for index, question in enumerate(sampled, start=1):
            query = str(question.get("query") or "").strip()
            if not query:
                continue
            results = retrieval.search_chunks(conn, query, args.top_k, None)
            ranked_docs: list[str] = []
            for result in results:
                doc_id = str(result["doc_id"])
                if doc_id not in ranked_docs:
                    ranked_docs.append(doc_id)
            expected = [
                title_to_id[title]
                for title in evidence_titles(question)
                if title in title_to_id
            ]
            expected_set = set(expected)
            top_docs = ranked_docs[: args.top_k]

            # null 题没有期望文档：doc 指标记 0，不参与 answerable 聚合，
            # 否则 301 道 null 会把整体 doc recall 抬高约 12 个点。
            doc_recall = (
                len(expected_set & set(top_docs)) / len(expected_set)
                if expected_set
                else 0.0
            )
            complete = (
                bool(expected_set) and expected_set <= set(top_docs[:5])
                if expected_set
                else False
            )
            complete10 = (
                bool(expected_set) and expected_set <= set(top_docs)
                if expected_set
                else False
            )
            first_rank = next(
                (
                    rank
                    for rank, doc_id in enumerate(top_docs, start=1)
                    if doc_id in expected_set
                ),
                None,
            )
            mrr = 1.0 / first_rank if first_rank else 0.0
            hit = bool(expected_set & set(top_docs)) if expected_set else not results

            chunks = [str(result.get("text") or "") for result in results]
            facts = [
                str(item.get("fact") or "")
                for item in question.get("evidence_list") or []
            ]
            fact_recall = (
                sum(fact_hit(fact, chunks) for fact in facts) / len(facts)
                if facts
                else 0.0
            )
            gap_info = agent.detect_gap(query, results)
            strict_gap = bool(gap_info and gap_info.get("severity") != "weak")
            weak_gap = bool(gap_info and gap_info.get("severity") == "weak")

            row = {
                "query": query,
                "type": str(question.get("question_type") or "unknown"),
                "expected": len(expected_set),
                "doc_recall@k": round(doc_recall, 4),
                "complete@5": bool(complete),
                "complete@10": bool(complete10),
                "mrr@k": round(mrr, 4),
                "hit@k": bool(hit),
                "fact_recall@k": round(fact_recall, 4),
                "gap": strict_gap,
                "weak": weak_gap,
                "top_docs": top_docs[:5],
            }
            rows.append(row)
            per_type.setdefault(row["type"], []).append(row)
            if index % 50 == 0:
                elapsed = time.perf_counter() - started
                print(f"  {index}/{len(sampled)} 题，{elapsed:.1f}s")

        def summarize(items: list[dict]) -> dict[str, object]:
            answerable = [item for item in items if item["expected"] > 0]
            nulls = [item for item in items if item["expected"] == 0]
            return {
                "questions": len(items),
                # 检索指标只在可答题上聚合；null 题只看拒答率。
                "doc_recall@k": opt_mean([item["doc_recall@k"] for item in answerable]),
                "complete@5": opt_mean(
                    [1.0 if item["complete@5"] else 0.0 for item in answerable]
                ),
                "complete@10": opt_mean(
                    [1.0 if item["complete@10"] else 0.0 for item in answerable]
                ),
                "mrr@k": opt_mean([item["mrr@k"] for item in answerable]),
                "hit@k": opt_mean([1.0 if item["hit@k"] else 0.0 for item in answerable]),
                "fact_recall@k": opt_mean(
                    [item["fact_recall@k"] for item in answerable]
                ),
                "answerable": len(answerable),
                "gap_false_positive": opt_mean(
                    [1.0 if item["gap"] else 0.0 for item in answerable]
                ),
                "weak_false_positive": opt_mean(
                    [1.0 if item["weak"] else 0.0 for item in answerable]
                ),
                "null_questions": len(nulls),
                "null_abstain": opt_mean(
                    [1.0 if item["gap"] else 0.0 for item in nulls]
                ),
                "null_flagged": opt_mean(
                    [1.0 if (item["gap"] or item["weak"]) else 0.0 for item in nulls]
                ),
            }

        report = {
            "dataset": "MultiHop-RAG",
            "license": "ODC-BY",
            "top_k": args.top_k,
            "sample": len(rows),
            "seed": args.seed,
            "elapsed_seconds": round(time.perf_counter() - started, 1),
            "overall": summarize(rows),
            "by_type": {name: summarize(items) for name, items in per_type.items()},
        }
    finally:
        conn.close()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    version = "multihop_rag_v1"
    (out_dir / f"{version}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def fmt(value: object) -> str:
        if value is None:
            return "—"
        if isinstance(value, float):
            return f"{value:.3f}"
        return str(value)

    lines = [
        "# MultiHop-RAG 评测结果",
        "",
        f"- 数据：MultiHop-RAG（ODC-BY），{report['sample']} 题 / 609 篇新闻，top_k={args.top_k}",
        f"- 模式：本地 hash 向量 + BM25 + 文档级两阶段；无 LLM（抽取式/拒答判定）",
        f"- 复现：`backend/.venv/bin/python eval/prepare.py && backend/.venv/bin/python eval/evaluate.py`",
        "",
        "| 指标 | 全部 | " + " | ".join(report["by_type"].keys()) + " |",
        "|---|---:" + "|---:" * len(report["by_type"]) + "|",
    ]
    for key in (
        "questions",
        "doc_recall@k",
        "complete@5",
        "complete@10",
        "mrr@k",
        "hit@k",
        "fact_recall@k",
        "gap_false_positive",
        "weak_false_positive",
        "null_abstain",
        "null_flagged",
    ):
        cells = [fmt(report["overall"].get(key, ""))]
        cells += [fmt(report["by_type"][name].get(key, "")) for name in report["by_type"]]
        lines.append(f"| {key} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "说明：`gap_false_positive` 只在可答题上统计（越低越好），`null_abstain` 只在 null_query 上统计（越高越好）。",
        "这是检索/拒答评测，不代表答案事实性；引用精确率由 `scripts/check_api.py` 与单测覆盖。",
        "",
    ]
    (out_dir / f"{version}.md").write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps(report["overall"], ensure_ascii=False, indent=2))
    for name, stats in report["by_type"].items():
        print(name, json.dumps(stats, ensure_ascii=False))
    print(f"→ {out_dir / (version + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
