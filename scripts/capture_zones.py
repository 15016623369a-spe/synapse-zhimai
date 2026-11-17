"""Capture final zoned-graph screenshots for docs (read-only).

Assumes curated zones already exist. Saves to test-reports/zone-final/.
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7800").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "test-reports" / "zone-final"
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

    page.goto(f"{BASE}/#/graph", wait_until="networkidle")
    page.wait_for_timeout(3800)
    page.get_by_role("tab", name="分区图谱").click()
    page.wait_for_timeout(3800)
    expect(page.locator('[aria-label="分区筛选"]')).to_be_visible()
    shot(page, "zones-all")

    chip = page.locator('[aria-label="分区筛选"] button').filter(has_text="AI 与大模型").first
    chip.click()
    page.wait_for_timeout(2600)
    shot(page, "zones-single")

    page.locator('[aria-label="分区筛选"] button').filter(has_text="全部区域").first.click()
    page.wait_for_timeout(1500)
    page.get_by_role("button", name="管理分区").click()
    expect(page.get_by_role("dialog", name="管理分区")).to_be_visible()
    page.wait_for_timeout(900)
    shot(page, "manager-zones")
    page.get_by_role("dialog", name="管理分区").get_by_role("tab", name=__import__("re").compile("文档归档")).click()
    page.wait_for_timeout(1100)
    shot(page, "manager-docs")
    page.get_by_role("dialog", name="管理分区").get_by_role("button", name="完成").click()
    page.wait_for_timeout(900)

    page.set_viewport_size({"width": 1024, "height": 768})
    page.wait_for_timeout(2200)
    shot(page, "zones-1024")
    overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    print(f"1024 overflow: {overflow}px")

    page.set_viewport_size({"width": 1440, "height": 900})
    page.get_by_role("tab", name="全局图谱").click()
    page.wait_for_timeout(2600)
    shot(page, "global-with-switch")
    print(f"console errors: {len(ERRORS)}")
    ctx.close()
    browser.close()

print("done")
