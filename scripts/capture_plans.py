"""Capture final Plans-page screenshots for docs (read-only with demo data)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7800").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "test-reports" / "plans-final"
OUT.mkdir(parents=True, exist_ok=True)
ERRORS: list[str] = []


def shot(page: Page, name: str) -> None:
    page.screenshot(path=str(OUT / f"{name}.png"))
    print(f"📸 {name}.png")


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, locale="zh-CN")
    page = ctx.new_page()
    page.set_default_timeout(20000)
    page.on("console", lambda m: ERRORS.append(m.text) if m.type == "error" else None)

    page.goto(f"{BASE}/#/plans", wait_until="networkidle")
    page.wait_for_timeout(2200)
    expect(page.get_by_role("heading", name="计划与打卡")).to_be_visible()
    shot(page, "plans-overview")

    # nice demo-plan drawer with calendar + notes
    page.get_by_role("button", name=re.compile("查看计划 每日阅读 30 分钟 的详情")).click()
    expect(page.locator('[aria-label="关闭计划详情"]')).to_be_visible()
    page.wait_for_timeout(1200)
    shot(page, "plan-drawer")
    page.get_by_label("关闭计划详情").click()
    page.wait_for_timeout(500)

    # check-in dialog on reading plan (cancel afterwards)
    card = page.locator(".plan-card").filter(has_text="每日阅读 30 分钟").first
    card.get_by_role("button", name=re.compile("^打卡$")).click()
    dialog = page.get_by_role("dialog", name="今日打卡")
    expect(dialog).to_be_visible()
    dialog.locator("textarea").fill("读完 20 页《思考，快与慢》，记一条卡片。")
    shot(page, "checkin-dialog")
    dialog.get_by_role("button", name="取消").click()
    page.wait_for_timeout(400)

    # new plan form
    page.get_by_role("button", name="新建计划").click()
    form = page.get_by_role("dialog", name="新建计划")
    expect(form).to_be_visible()
    form.locator('input[placeholder="例如：每日阅读 30 分钟"]').fill("每天散步 20 分钟")
    form.locator('textarea[placeholder="补充说明这个计划的意图或执行方式"]').fill("午饭后绕小区走一圈，晒太阳")
    form.locator('[aria-label^="选择颜色"]').nth(3).click()
    page.wait_for_timeout(300)
    shot(page, "plan-form")
    form.get_by_role("button", name="取消").click()
    page.wait_for_timeout(300)

    page.set_viewport_size({"width": 1024, "height": 768})
    page.wait_for_timeout(1500)
    shot(page, "plans-1024")
    overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    print("1024 overflow:", overflow)
    print("console errors:", len(ERRORS))
    ctx.close()
    browser.close()
print("done")
