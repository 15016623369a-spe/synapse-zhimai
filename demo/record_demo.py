#!/usr/bin/env python3
"""录制 90 秒闭环 demo：答不上 → 审核入库 → 再问答上。

前置：先启动 ``backend/.venv/bin/python demo/run_demo.py --reset``。

运行（需要 Playwright 的 chromium）：
    PLAYWRIGHT_BROWSERS_PATH=$PWD/.playwright \
      .venv-tools/bin/python demo/record_demo.py
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DEMO = Path(__file__).resolve().parent
QUESTION = "SEA 2026 年市场进入策略是什么？"

OVERLAY_JS = """
(text) => {
  let el = document.getElementById('demo-overlay');
  if (!el) {
    el = document.createElement('div');
    el.id = 'demo-overlay';
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.style.cssText = [
    'position:fixed','left:18px','top:16px','z-index:99999',
    'padding:8px 14px','border-radius:999px',
    'background:rgba(8,15,28,.88)','color:#fbbf24',
    'font:600 14px/1.45 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif',
    'border:1px solid rgba(251,191,36,.35)','backdrop-filter:blur(8px)',
    'box-shadow:0 8px 24px rgba(0,0,0,.35)'
  ].join(';');
}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:7909")
    parser.add_argument("--out", default=str(DEMO / "recordings"))
    parser.add_argument("--keep-webm", action="store_true")
    return parser.parse_args()


def newest_video(directory: Path) -> Path:
    videos = sorted(directory.glob("*.webm"), key=lambda path: path.stat().st_mtime)
    if not videos:
        raise SystemExit("没有生成 webm")
    return videos[-1]


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            record_video_dir=str(out_dir),
            record_video_size={"width": 1280, "height": 800},
        )
        page = context.new_page()
        page.goto(args.base, wait_until="networkidle")
        page.wait_for_selector(".composer-textarea", timeout=20_000)
        page.evaluate(OVERLAY_JS, "Synapse · 知脉：知识闭环 90 秒")
        page.wait_for_timeout(3_000)

        # Act 1：知识库答不上
        page.evaluate(OVERLAY_JS, "① 问一个知识库里没有的问题")
        page.fill(".composer-textarea", QUESTION)
        page.keyboard.press("Enter")
        page.wait_for_selector(".gap-card", timeout=30_000)
        page.wait_for_timeout(4_000)

        # Act 2：人工审核后入库
        page.evaluate(OVERLAY_JS, "② 你审核候选，确认后才入库")
        page.click(".gap-cta")
        page.wait_for_selector(".modal", timeout=30_000)
        page.wait_for_timeout(3_000)
        checkbox = page.query_selector(".extend-candidate input[type=checkbox]")
        if checkbox and not checkbox.is_checked():
            checkbox.click()
        page.wait_for_timeout(1_200)
        page.get_by_role("button", name="加入知识库（1 条）").click()
        page.wait_for_timeout(4_000)
        page.click(".modal-close")
        page.wait_for_timeout(1_200)

        # Act 3：再问，答上了
        page.evaluate(OVERLAY_JS, "③ 再问一次：缺口被补上")
        gap_cards_before = len(page.query_selector_all(".gap-card"))
        page.fill(".composer-textarea", QUESTION)
        page.keyboard.press("Enter")
        page.wait_for_selector(".citation-chip", timeout=30_000)
        page.wait_for_timeout(4_000)
        # 历史消息里还留着第一轮的 gap 卡；只比较数量，判断新一轮有没有新增
        if len(page.query_selector_all(".gap-card")) > gap_cards_before:
            raise SystemExit("复问仍出现 gap-card，demo 失败")
        page.click(".citation-chip")
        page.wait_for_selector(".citations-panel", timeout=10_000)
        page.wait_for_timeout(4_000)
        page.evaluate(OVERLAY_JS, "引用可回原文 · 闭环收敛率 LCR 75%")
        page.wait_for_timeout(5_000)

        context.close()
        browser.close()

    video = newest_video(out_dir)
    media = ROOT / "docs" / "media"
    media.mkdir(parents=True, exist_ok=True)
    # Playwright 自带的 ffmpeg 只带 VP9，没有 libx264；直接输出 webm 更稳。
    target = media / "demo-loop.webm"
    ffmpeg = ROOT.parent / ".playwright" / "ffmpeg-1013" / "ffmpeg-mac"
    subprocess.run(
        [
            str(ffmpeg),
            "-y",
            "-i",
            str(video),
            "-c:v",
            "libvpx-vp9",
            "-b:v",
            "0",
            "-crf",
            "34",
            "-an",
            str(target),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    poster = media / "demo-loop-poster.png"
    subprocess.run(
        [str(ffmpeg), "-y", "-ss", "27", "-i", str(video), "-frames:v", "1", str(poster)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not args.keep_webm:
        video.unlink(missing_ok=True)
    print(f"✓ {target} ({target.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"✓ {poster}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
