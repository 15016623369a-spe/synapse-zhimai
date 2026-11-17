"""Lead acceptance for the 3D knowledge graph (WebGL / three.js).

Read-only against the running app: no documents / settings / conversations are modified.
Run after rebuilding frontend dist, with Synapse on 7800:

    PLAYWRIGHT_BROWSERS_PATH=/Users/jace/Documents/deepseek-harness/default-workspace/.playwright \
      /Users/jace/Documents/deepseek-harness/default-workspace/.venv-tools/bin/python scripts/e2e_graph3d.py [base_url]
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7800").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "test-reports" / "3d-e2e"
SHOTS.mkdir(parents=True, exist_ok=True)

PASS: list[str] = []
FAIL: list[str] = []
CONSOLE: list[str] = []
PAGE_ERRORS: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name if cond else f"{name}: {detail}")
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  {detail}"))


def shot(page: Page, name: str) -> None:
    page.screenshot(path=str(SHOTS / f"{name}.png"))
    print(f"     📸 {name}.png")


def img_hash(page: Page) -> str:
    return hashlib.sha256(page.screenshot()).hexdigest()


def canvas_box(page: Page):
    return page.locator(".graph-canvas-wrap").bounding_box()


def find_tooltip_by_hover(page: Page) -> bool:
    """Focus + scan mouse positions near canvas center until a node tooltip appears."""
    box = canvas_box(page)
    if not box:
        return False
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    offsets = [(0, 0), (0, -12), (0, 12), (-14, 0), (14, 0), (-24, -18), (24, 18), (40, 0), (-40, 0)]
    for dx, dy in offsets:
        page.mouse.move(cx + dx, cy + dy)
        page.wait_for_timeout(250)
        if page.locator(".graph3d-tooltip").count() > 0:
            return True
    return False


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, locale="zh-CN")
        page = ctx.new_page()
        page.set_default_timeout(20000)
        page.on(
            "console",
            lambda msg: CONSOLE.append(f"{msg.type}: {msg.text}")
            if msg.type == "error" and "favicon" not in msg.text
            else None,
        )
        page.on("pageerror", lambda exc: PAGE_ERRORS.append(str(exc)))

        print("\n== 3D graph ==")
        page.goto(f"{BASE}/#/graph", wait_until="networkidle")
        wrap = page.locator(".graph-canvas-wrap")
        expect(wrap).to_be_visible(timeout=20000)
        page.wait_for_timeout(4500)  # layout warm-up + first render

        canvases = page.locator(".graph-canvas-wrap canvas")
        visible_webgl = page.evaluate(
            """() => {
                const list = [...document.querySelectorAll('.graph-canvas-wrap canvas')];
                const c = list.find((x) => x.offsetWidth > 100 && x.offsetHeight > 100);
                if (!c) return null;
                return {w: c.width, h: c.height, cls: c.className};
            }"""
        )
        ok("WebGL 3D canvas 已渲染", bool(visible_webgl) and "graph-canvas-3d" in str(visible_webgl), str(visible_webgl))
        ok("画布/容器尺寸正常", bool(visible_webgl) and visible_webgl["w"] >= 1000, str(visible_webgl))
        shot(page, "01-default")

        # auto rotation: two frames should differ
        h1 = img_hash(page)
        page.wait_for_timeout(1400)
        h2 = img_hash(page)
        ok("自动旋转（连续帧不同）", h1 != h2, "frames identical")

        rotate_btn = page.locator(".graph-tool-btn").filter(has_text="自动旋转")
        expect(rotate_btn).to_be_visible()
        aria_on = rotate_btn.get_attribute("aria-pressed")
        ok("自动旋转按钮默认开启", aria_on == "true", f"aria-pressed={aria_on}")
        rotate_btn.click()
        page.wait_for_timeout(400)
        ok("自动旋转可关闭", rotate_btn.get_attribute("aria-pressed") == "false")
        shot(page, "02-autorotate-off")
        rotate_btn.click()
        page.wait_for_timeout(300)

        # drag to orbit
        rotate_btn.click()  # off while testing drag for deterministic frame
        page.wait_for_timeout(300)
        box = canvas_box(page)
        ok("画布可交互区域存在", bool(box) and box["width"] > 600, str(box))
        h_before = img_hash(page)
        assert box
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx + 190, cy + 55, steps=14)
        page.mouse.up()
        page.wait_for_timeout(900)
        h_after = img_hash(page)
        ok("拖拽可以旋转 3D 视角", h_before != h_after, "frame unchanged after drag")
        shot(page, "03-rotated")

        # search + focus + drawer
        search = page.locator('[aria-label="搜索图谱节点"]')
        search.fill("01-RAG")
        page.wait_for_timeout(500)
        search.press("Enter")
        expect(page.locator('[aria-label="节点详情"]')).to_be_visible(timeout=15000)
        page.wait_for_timeout(1600)
        ok("搜索聚焦节点并打开详情抽屉", True)
        shot(page, "04-node-drawer")
        page.locator('[aria-label="关闭节点详情"]').click()
        page.wait_for_timeout(600)

        # hover highlight + tooltip
        page.locator('[aria-label="搜索图谱节点"]').fill("RAG 检索增强生成实践笔记")
        page.wait_for_timeout(400)
        page.locator('[aria-label="搜索图谱节点"]').press("Enter")
        page.wait_for_timeout(1600)
        hovered = find_tooltip_by_hover(page)
        ok("hover 节点弹出 tooltip", hovered)
        if hovered:
            shot(page, "05-hover-tooltip")
        page.locator('[aria-label="关闭节点详情"]').click()
        page.wait_for_timeout(400)

        # zoom controls
        page.locator('[aria-label="放大"]').click()
        page.locator('[aria-label="缩小"]').click()
        page.locator('[aria-label="重置视图"]').click()
        page.wait_for_timeout(900)
        ok("缩放/重置视图控件可用", True)

        # auto rotate enabled screenshot at another moment
        shot(page, "06-default-again")

        # 1024 responsive
        page.set_viewport_size({"width": 1024, "height": 768})
        page.wait_for_timeout(1600)
        overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        ok("1024px 无横向溢出", overflow <= 1, f"overflow={overflow}px")
        shot(page, "07-1024")

        # page regressions
        print("\n== other pages regression ==")
        page.set_viewport_size({"width": 1440, "height": 900})
        checks = [
            ("#/chat", "你好，我是知脉。"),
            ("#/library", "知识库"),
            ("#/settings", "模型接入"),
        ]
        for route, text in checks:
            page.goto(f"{BASE}/{route}")
            page.wait_for_timeout(1600)
            ok(f"{route} 正常渲染", page.get_by_text(text).count() > 0)

        ok("无 console error", not CONSOLE, "; ".join(CONSOLE[:3]))
        ok("无 page error", not PAGE_ERRORS, "; ".join(PAGE_ERRORS[:3]))

        ctx.close()
        browser.close()

    print("\n=== summary ===")
    print(f"PASS {len(PASS)} | FAIL {len(FAIL)}")
    for f in FAIL:
        print(f"  - {f}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
