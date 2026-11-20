#!/usr/bin/env python3
"""闭环收敛率（Loop Closure Rate, LCR）评测。

定义::

    LCR = 采纳外部知识后由「答不上」变为「答得上」的题数 / 触发 gap 且完成采纳的题数

做法（全程离线、确定性）：

1. 用 16 篇 demo 文档启动一个隔离实例；
2. 从 MultiHop-RAG 选 20 道题（证据文档互不重叠），先问一遍，确认触发 gap；
3. 通过真实的 ``POST /api/extend/commit`` 把证据文章入库
   （抓取层用 MockTransport 返回文章正文，模拟「用户审核通过」）；
4. 再问一遍：gap 消失且引用到新文档 → 记为一次闭环。

不做 LLM 生成（extractive 模式），只衡量「缺口 → 补课 → 可检索」这一段。
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(EVAL_DIR / "data"))
    parser.add_argument("--state-dir", default=str(EVAL_DIR / "state" / "loop"))
    parser.add_argument("--questions", type=int, default=20)
    parser.add_argument("--rebuild", action="store_true")
    return parser.parse_args()


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    name = "message"
    data: list[str] = []
    for line in raw.splitlines():
        if line.startswith("event:"):
            name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:") :].strip())
        elif not line.strip():
            if data:
                try:
                    events.append((name, json.loads("\n".join(data))))
                except json.JSONDecodeError:
                    pass
            name, data = "message", []
    return events


def ask(client, message: str) -> dict:
    response = client.post("/api/chat", json={"message": message, "top_k": 8})
    response.raise_for_status()
    events = parse_sse(response.text)
    gap = next((payload for name, payload in events if name == "gap"), None)
    done = next(
        (payload for name, payload in events if name == "done"),
        {},
    )
    answer = "".join(
        str(payload.get("text") or "") for name, payload in events if name == "delta"
    )
    return {"gap": gap, "citations": done.get("citations") or [], "answer": answer}


def select_questions(questions: list[dict], limit: int) -> list[dict]:
    """按题型轮转，选出证据文档互不重叠的题目。"""
    buckets: dict[str, list[dict]] = {}
    for item in questions:
        if item.get("question_type") == "null_query":
            continue
        if not item.get("evidence_list"):
            continue
        buckets.setdefault(str(item.get("question_type")), []).append(item)
    order: list[dict] = []
    index = 0
    while any(len(bucket) > index for bucket in buckets.values()):
        for bucket in buckets.values():
            if len(bucket) > index:
                order.append(bucket[index])
        index += 1
    used_titles: set[str] = set()
    selected: list[dict] = []
    for item in order:
        titles = {
            str(evidence.get("title") or "")
            for evidence in item["evidence_list"]
            if evidence.get("title")
        }
        if not titles or titles & used_titles:
            continue
        selected.append(item)
        used_titles.update(titles)
        if len(selected) >= limit:
            break
    return selected


def article_html(title: str, body: str) -> str:
    paragraphs = "".join(
        f"<p>{html.escape(part.strip())}</p>"
        for part in body.split("\n\n")
        if part.strip()
    )
    return (
        "<html><head><title>"
        + html.escape(title)
        + "</title></head><body><article><h1>"
        + html.escape(title)
        + "</h1>"
        + paragraphs
        + "</article></body></html>"
    )


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir).resolve()
    state_dir = Path(args.state_dir).resolve()
    db_dir = state_dir / "data"

    # LCR 必须从干净语料开始，否则上一次采纳的文档会让 before 直接可答
    if state_dir.exists():
        shutil.rmtree(state_dir)
    (db_dir / "demo_docs").mkdir(parents=True, exist_ok=True)
    for source in (ROOT / "data" / "demo_docs").glob("*.md"):
        shutil.copy2(source, db_dir / "demo_docs" / source.name)

    os.environ["SYNAPSE_DATA_DIR"] = str(db_dir)
    os.environ["SYNAPSE_PATROL_AUTOSTART"] = "0"
    os.environ["SYNAPSE_LLM_BASE_URL"] = ""
    os.environ["SYNAPSE_LLM_API_KEY"] = ""
    os.environ["SYNAPSE_LLM_MODEL"] = ""
    os.environ["SYNAPSE_ALLOW_PRIVATE_FETCH"] = "1"
    sys.path.insert(0, str(ROOT / "backend"))

    import httpx  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from app.main import app  # noqa: PLC0415
    from app.services import extend  # noqa: PLC0415

    corpus = json.loads((data_dir / "corpus.json").read_text(encoding="utf-8"))
    questions = json.loads((data_dir / "questions.json").read_text(encoding="utf-8"))
    by_title = {str(item.get("title") or ""): item for item in corpus}
    # 多取一些候选：先用「基础语料是否触发 gap」预筛，再凑够 N 道闭环样本。
    sampled = select_questions(questions, max(args.questions * 6, 60))
    if not sampled:
        raise SystemExit("没有选出可用的题目")

    fixtures: dict[str, tuple[str, str]] = {}
    for item in sampled:
        for evidence in item["evidence_list"]:
            title = str(evidence.get("title") or "")
            article = by_title.get(title)
            if article is None:
                continue
            url = "https://fixture.local/" + hashlib.sha1(title.encode()).hexdigest()[:16]
            fixtures[url] = (title, str(article.get("body") or ""))

    def handler(request: httpx.Request) -> httpx.Response:
        title, body = fixtures.get(str(request.url), ("", ""))
        if not body:
            return httpx.Response(404, text="not found")
        return httpx.Response(
            200,
            text=article_html(title, body),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    extend.HTTP_TRANSPORT = httpx.MockTransport(handler)

    results: list[dict] = []
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        print(f"基础语料：{health['documents']} 篇 / {health['chunks']} chunks")
        scanned = 0
        for index, item in enumerate(sampled, start=1):
            if len(results) >= args.questions:
                break
            scanned += 1
            query = str(item.get("query") or "")
            before = ask(client, query)
            if not before["gap"]:
                continue
            adopted_ids: list[str] = []
            for evidence in item["evidence_list"]:
                title = str(evidence.get("title") or "")
                url = "https://fixture.local/" + hashlib.sha1(title.encode()).hexdigest()[:16]
                if url not in fixtures:
                    continue
                response = client.post(
                    "/api/extend/commit",
                    json={
                        "items": [
                            {
                                "url": url,
                                "title": title,
                                "snippet": fixtures[url][1][:400],
                            }
                        ]
                    },
                )
                if response.status_code != 200:
                    continue
                for added in response.json().get("added") or []:
                    adopted_ids.append(str(added.get("id")))
            if not adopted_ids:
                print(f"  [fail] Q{index} 采纳失败")
                continue
            after = ask(client, query)
            cited_new = any(
                str(citation.get("doc_id")) in adopted_ids
                for citation in after["citations"]
            )
            flipped = after["gap"] is None
            results.append(
                {
                    "question": query,
                    "type": item.get("question_type"),
                    "adopted": len(adopted_ids),
                    "gap_before": True,
                    "gap_after": after["gap"],
                    "cited_new_doc": cited_new,
                    "flipped": flipped,
                }
            )
            print(
                f"  [{len(results)}/{args.questions}] Q{index} "
                f"gap→{'ok' if flipped else 'gap'} | "
                f"cited_new={cited_new} | adopted={len(adopted_ids)}"
            )

    attempts = len(results)
    flips = sum(1 for item in results if item["flipped"])
    print(f"扫描 {scanned} 道题，得到 {attempts} 道 gap 样本")
    cited = sum(1 for item in results if item["cited_new_doc"])
    report = {
        "metric": "Loop Closure Rate (LCR)",
        "definition": "采纳后由 gap 变为可答的题数 / 触发 gap 且完成采纳的题数",
        "attempts": attempts,
        "flipped": flips,
        "lcr": round(flips / attempts, 4) if attempts else 0.0,
        "cited_new_doc_rate": round(cited / attempts, 4) if attempts else 0.0,
        "results": results,
    }
    out_dir = EVAL_DIR / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "loop_closure.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = [
        "# 闭环收敛率（LCR）",
        "",
        f"- 样本：{attempts} 道「基础语料答不上」的题",
        f"- **LCR = {flips}/{attempts} = {report['lcr']:.1%}**",
        f"- 复问时引用到新文档：{cited}/{attempts} = {report['cited_new_doc_rate']:.1%}",
        "",
        "| # | 题型 | 采纳文档 | gap 消失 | 引用新文档 |",
        "|---:|---|---:|---|---|",
    ]
    for index, item in enumerate(results, start=1):
        md.append(
            f"| {index} | {item['type']} | {item['adopted']} | "
            f"{'✅' if item['flipped'] else '❌'} | "
            f"{'✅' if item['cited_new_doc'] else '❌'} |"
        )
    (out_dir / "loop_closure.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
