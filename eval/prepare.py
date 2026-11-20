#!/usr/bin/env python3
"""把 MultiHop-RAG 导入一个独立的 Synapse 数据目录，供离线评测使用。

- 数据集：https://huggingface.co/datasets/yixuantt/MultiHopRAG （ODC-BY）
  - corpus.json        609 篇英文新闻正文
  - MultiHopRAG.json   2556 道题（comparison / inference / temporal / null）
- 不碰你的个人知识库：默认写到 ``eval/state/data``，可用 ``--state-dir`` 覆盖。
- 导入时 ``rebuild=False``，最后统一重建一次图谱，避免 609 次全量 rebuild。

用法：
    backend/.venv/bin/python eval/prepare.py            # 下载 + 导入
    backend/.venv/bin/python eval/prepare.py --rebuild  # 清空后重新导入
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = Path(__file__).resolve().parent

DATASET_FILES = {
    "corpus.json": {
        "url": "https://huggingface.co/datasets/yixuantt/MultiHopRAG/resolve/main/corpus.json",
        "sha256": "20b61b5ab84de84a927420c5d265b7ec8d859ae49980699958a787ade9e4d28f",
    },
    "questions.json": {
        "url": "https://huggingface.co/datasets/yixuantt/MultiHopRAG/resolve/main/MultiHopRAG.json",
        "sha256": "03cfb4926461f868684903aadc8024447bdda5bb3f6804741424cce338515bff",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(EVAL_DIR / "data"))
    parser.add_argument("--state-dir", default=str(EVAL_DIR / "state"))
    parser.add_argument("--rebuild", action="store_true", help="清空 state 后重新导入")
    parser.add_argument("--limit", type=int, default=0, help="只导入前 N 篇（调试用）")
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, spec in DATASET_FILES.items():
        target = data_dir / name
        if target.exists() and _sha256(target) == spec["sha256"]:
            print(f"✓ {name} 已存在")
            continue
        print(f"↓ 下载 {name} …")
        request = urllib.request.Request(
            spec["url"], headers={"User-Agent": "SynapseEval/1.0"}
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            target.write_bytes(response.read())
        actual = _sha256(target)
        if actual != spec["sha256"]:
            raise SystemExit(f"{name} sha256 不匹配：{actual}")
        print(f"✓ {name} {target.stat().st_size / 1024 / 1024:.1f} MB")


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir).resolve()
    state_dir = Path(args.state_dir).resolve()
    db_dir = state_dir / "data"

    download(data_dir)

    if args.rebuild and state_dir.exists():
        shutil.rmtree(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    # 必须在 import app 之前设置：config 在导入时读取环境变量
    os.environ["SYNAPSE_DATA_DIR"] = str(db_dir)
    os.environ["SYNAPSE_PATROL_AUTOSTART"] = "0"
    os.environ["SYNAPSE_LLM_BASE_URL"] = ""
    os.environ["SYNAPSE_LLM_API_KEY"] = ""
    os.environ["SYNAPSE_LLM_MODEL"] = ""
    os.environ.pop("SYNAPSE_ALLOW_PRIVATE_FETCH", None)
    sys.path.insert(0, str(ROOT / "backend"))

    from app import db  # noqa: PLC0415
    from app.services import graph, ingest  # noqa: PLC0415

    db.init_db()

    corpus = json.loads((data_dir / "corpus.json").read_text(encoding="utf-8"))
    if args.limit:
        corpus = corpus[: args.limit]

    with db.get_db() as conn:
        existing = db.table_count(conn, "documents")
    if existing >= len(corpus):
        print(f"✓ 已有 {existing} 篇文档，跳过导入（--rebuild 可重来）")
    else:
        started = time.perf_counter()
        imported = 0
        for index, article in enumerate(corpus, start=1):
            body = str(article.get("body") or "").strip()
            title = str(article.get("title") or "").strip()
            if not title or not body:
                continue
            parsed = ingest.ParsedDocument(
                title=title,
                content=body,
                source_type="web",
                # 来源站点作为 tag：新闻类问题经常用「据 TechCrunch 报道」定位文档，
                # 这是通用的 web 入库元数据，不是针对某个 benchmark 的特判。
                tags=[
                    "multihop-rag",
                    str(article.get("category") or "news"),
                    str(article.get("source") or "").strip(),
                ],
                summary="",
                source_url=str(article.get("url") or "") or None,
            )
            try:
                ingest.create_document(
                    parsed, rebuild=False, record_activity=False
                )
                imported += 1
            except Exception as exc:  # 单篇失败不阻塞整体
                print(f"! 第 {index} 篇导入失败：{title[:40]} — {exc}")
            if index % 50 == 0:
                elapsed = time.perf_counter() - started
                print(f"  {index}/{len(corpus)} 篇，{elapsed:.1f}s")
        with db.get_db() as conn:
            graph.rebuild(conn)
        print(f"✓ 导入 {imported} 篇，用时 {time.perf_counter() - started:.1f}s")

    with db.get_db() as conn:
        documents = db.fetch_all(conn, "SELECT id, title FROM documents")
        chunks = db.table_count(conn, "chunks")
    mapping = {str(row["title"]): str(row["id"]) for row in documents}
    (state_dir / "title_to_id.json").write_text(
        json.dumps(mapping, ensure_ascii=False, indent=0), encoding="utf-8"
    )
    meta = {
        "dataset": "MultiHop-RAG",
        "license": "ODC-BY",
        "documents": len(documents),
        "chunks": chunks,
        "sha256": {name: spec["sha256"] for name, spec in DATASET_FILES.items()},
    }
    (state_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"✓ 完成：{len(documents)} 篇 / {chunks} chunks → {state_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
