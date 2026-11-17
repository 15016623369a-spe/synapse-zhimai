"""Lead acceptance for the web knowledge-extension UI on live 7800.

Asks an out-of-KB question (real DeepSeek + Bing tool calling), verifies gap card,
external sources and the approval panel; does NOT commit anything into the live KB.
Cleans up its own conversations.

Run:
    PLAYWRIGHT_BROWSERS_PATH=... .venv-tools/bin/python scripts/e2e_extend_ui.py [base_url]
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7800").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "test-reports" / "extend-e2e"
SHOTS.mkdir(parents=True, exist_ok=True)

PASS: list[str] = []
FAIL: list[str] = []
CONSOLE: list[str] = []
PAGE_ERRORS: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name}  {detail}")


def api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - localhost
        raw = resp.read()
    return json.loads(raw) if raw else {}


def shot(page: Page, name: str) -> None:
    page.screenshot(path=str(SHOTS / f"{name}.png"))
    print(f"     📸 {name}.png")


def main() -> int:
    conv_before = {c["id"] for c in api("GET", "/api/conversations")["items"]}
    docs_before = api("GET", "/api/documents")["total"]
    print(f"baseline: {docs_before} docs / {len(conv_before)} conversations")
    new_convs: list[str] = []
    browser = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, locale="zh-CN")
            page = ctx.new_page()
            page.set_default_timeout(30000)
            page.on(
                "console",
                lambda m: CONSOLE.append(f"{m.type}: {m.text}")
                if m.type == "error" and "favicon" not in m.text
                else None,
            )
            page.on("pageerror", lambda exc: PAGE_ERRORS.append(str(exc)))

            print("\n== 1. real gap + tool search (DeepSeek + Bing) ==")
            page.goto(f"{BASE}/#/chat", wait_until="networkidle")
            page.wait_for_timeout(1500)
            composer = page.locator('[aria-label="提问输入框"]')
            composer.fill("2026年F1轮胎规则有哪些变化？")
            composer.press("Enter")
            expect(page.locator(".msg-assistant").last).to_be_visible(timeout=90000)
            page.wait_for_timeout(1000)
            try:
                page.locator('[aria-label="停止生成"]').wait_for(state="detached", timeout=120000)
            except Exception:
                pass
            page.wait_for_timeout(1200)
            assistant = page.locator(".msg-assistant").last
            expect(assistant.get_by_text("知识库资料有限")).to_be_visible(timeout=30000)
            ok("真实 gap 提示卡", True)
            expect(assistant.get_by_text("外部参考 · 未入库")).to_be_visible(timeout=30000)
            ok("真实工具搜索外部参考卡", True)
            ok(
                "回答使用外部资料 [W] 标注",
                "W1" in assistant.inner_text() or "W6" in assistant.inner_text(),
                assistant.inner_text()[:200],
            )
            shot(page, "01-gap-and-web-sources")

            convs_after = api("GET", "/api/conversations")["items"]
            new_convs = [c["id"] for c in convs_after if c["id"] not in conv_before]
            ok("会话已创建", len(new_convs) == 1, str(new_convs))

            print("\n== 2. approval panel (real Bing candidates) ==")
            assistant.get_by_role("button", name="查看并选择加入知识库").click()
            panel = page.get_by_role("dialog", name="知识扩展 · 由你确认")
            expect(panel).to_be_visible()
            page.wait_for_timeout(3000)
            expect(panel.get_by_text("候选资料")).to_be_visible(timeout=30000)
            rows = panel.locator(".extend-candidate")
            count = rows.count()
            ok("面板加载真实 Bing 候选", count >= 1, f"candidates={count}")
            ok("provider 为 bing", panel.get_by_text("bing", exact=False).count() >= 1)
            ok(
                "存在可加入的候选（非全部已在知识库）",
                panel.locator("button").filter(has_text="加入知识库").count() >= 1,
            )
            shot(page, "02-extension-panel")
            # close without approving (never write into live KB during acceptance)
            panel.locator('[aria-label="关闭"]').click()
            page.wait_for_timeout(500)

            print("\n== 3. settings status card ==")
            page.goto(f"{BASE}/#/settings", wait_until="networkidle")
            page.wait_for_timeout(1500)
            expect(page.get_by_text("联网知识扩展").first).to_be_visible(timeout=15000)
            ok("设置页联网扩展状态卡", True)
            shot(page, "03-settings-search")

            ok("无 console error", not CONSOLE, "; ".join(CONSOLE[:3]))
            ok("无 page error", not PAGE_ERRORS, "; ".join(PAGE_ERRORS[:3]))
            docs_after = api("GET", "/api/documents")["total"]
            ok("验收未向知识库写入外部文档", docs_after == docs_before, f"before={docs_before} after={docs_after}")
            ctx.close()
    finally:
        for conv_id in new_convs:
            try:
                api("DELETE", f"/api/conversations/{conv_id}")
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    print("\n=== summary ===")
    print(f"PASS {len(PASS)} | FAIL {len(FAIL)}")
    for f in FAIL:
        print(f"  - {f}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
