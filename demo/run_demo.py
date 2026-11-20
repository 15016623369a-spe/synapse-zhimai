#!/usr/bin/env python3
"""起一个自包含的闭环演示环境（不依赖外网、不花 LLM 额度）。

- 16 篇中文 demo 文档作为基础语料；
- 一个本地 fixture 页面作为「外部待补充资料」；
- mock search provider 把候选指向本地 fixture；
- 前端/后端同端口 :7909，录制脚本另起 Playwright。

用法：
    backend/.venv/bin/python demo/run_demo.py --reset
    # 浏览器打开 http://127.0.0.1:7909
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7909)
    parser.add_argument("--fixture-port", type=int, default=7900)
    parser.add_argument("--reset", action="store_true", help="清空 demo 状态后重建")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state = DEMO / "state"
    data_dir = state / "data"
    if args.reset and state.exists():
        shutil.rmtree(state)
    (data_dir / "demo_docs").mkdir(parents=True, exist_ok=True)
    for source in (ROOT / "data" / "demo_docs").glob("*.md"):
        shutil.copy2(source, data_dir / "demo_docs" / source.name)

    handler = partial(SimpleHTTPRequestHandler, directory=str(DEMO / "fixture"))
    fixture = ThreadingHTTPServer(("127.0.0.1", args.fixture_port), handler)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()

    fixture_url = f"http://127.0.0.1:{args.fixture_port}/doc.html"
    mock_results = [
        {
            "url": fixture_url,
            "title": "SEA 2026 年市场进入策略备忘录",
            "snippet": (
                "2026 年优先进入印尼和越南，新加坡作为区域总部；"
                "第一阶段签本地渠道商，第二阶段建 5 人团队，第三阶段再考虑区域仓。"
                "获客成本高于 25 美元或复购率低于 18% 就暂停扩张。"
            ),
        }
    ]
    env = dict(os.environ)
    env.update(
        {
            "SYNAPSE_DATA_DIR": str(data_dir),
            "SYNAPSE_PATROL_AUTOSTART": "0",
            "SYNAPSE_SEARCH_PROVIDER": "mock",
            "SYNAPSE_SEARCH_MOCK_RESULTS": json.dumps(mock_results, ensure_ascii=False),
            "SYNAPSE_ALLOW_PRIVATE_FETCH": "1",
            "SYNAPSE_LLM_BASE_URL": "",
            "SYNAPSE_LLM_API_KEY": "",
            "SYNAPSE_LLM_MODEL": "",
        }
    )
    process = subprocess.Popen(
        [
            str(ROOT / "backend" / ".venv" / "bin" / "python"),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
        ],
        cwd=str(ROOT / "backend"),
        env=env,
    )

    base = f"http://127.0.0.1:{args.port}"
    for _ in range(60):
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as response:
                payload = json.load(response)
            print(f"✓ demo 已启动：{base}（{payload['documents']} 篇文档）")
            print(f"✓ fixture：{fixture_url}")
            break
        except Exception:
            time.sleep(0.5)
    else:
        process.terminate()
        raise SystemExit("启动超时")

    def shutdown(*_args):  # noqa: ANN002, ANN003
        process.terminate()
        fixture.shutdown()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    print("按 Ctrl+C 停止。")
    process.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
