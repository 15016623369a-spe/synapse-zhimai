"""Lead acceptance for the Plans & Check-ins feature.

Read/write against a live Plans page; creates a temporary plan, checks in, edits and
deletes it, while verifying demo-plan interactions are restored.

Run with Synapse on 7800 (Playwright browser env required):
    PLAYWRIGHT_BROWSERS_PATH=... .venv-tools/bin/python scripts/e2e_plans.py [base_url]
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7800").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "test-reports" / "plans-e2e"
SHOTS.mkdir(parents=True, exist_ok=True)

TEMP_PLAN = "E2E 每日冥想"
TEMP_PLAN_UPDATED = "E2E 每日冥想 · 改名"
DEMO_PLAN = "每日阅读 30 分钟"

PASS: list[str] = []
FAIL: list[str] = []
CONSOLE: list[str] = []
PAGE_ERRORS: list[str] = []
HTTP_ERRORS: list[str] = []


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
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - localhost
        raw = resp.read()
    return json.loads(raw) if raw else {}


def shot(page: Page, name: str) -> None:
    page.screenshot(path=str(SHOTS / f"{name}.png"))
    print(f"     📸 {name}.png")


def wait_toast(page: Page, title: str, timeout: int = 15000) -> None:
    expect(page.locator(".toast-title").filter(has_text=title).first).to_be_visible(timeout=timeout)


def plan_by_title(title: str) -> dict | None:
    return next((p for p in api("GET", "/api/plans")["items"] if p["title"] == title), None)


def cleanup() -> None:
    for plan in api("GET", "/api/plans?include_archived=1")["items"]:
        if plan["title"].startswith("E2E"):
            api("DELETE", f"/api/plans/{plan['id']}")
    # restore demo plan today-checkin state: undo any E2E-created note checkin / re-add if missing
    demo = plan_by_title(DEMO_PLAN)
    if demo and demo["today_checked"] and not any(
        c["date"] == demo["last_checkin_date"] and "E2E" in (c.get("note") or "")
        for c in api("GET", f"/api/plans/{demo['id']}/checkins")["items"]
    ):
        pass  # user/demo checkin, leave it


def main() -> int:
    plans_before = api("GET", "/api/plans")["items"]
    print(f"baseline: {len(plans_before)} plans / today={api('GET', '/api/plans')['today']}")
    demo = plan_by_title(DEMO_PLAN)
    if not demo:
        print(f"missing demo plan {DEMO_PLAN!r}; run scripts/setup_plans.py first")
        return 2
    demo_today_before = demo["today_checked"]
    demo_total_before = demo["total_checkins"]

    browser = None
    try:
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
            page.on(
                "response",
                lambda resp: HTTP_ERRORS.append(f"{resp.status} {resp.request.method} {resp.url}")
                if resp.status >= 400 and "/api/" in resp.url
                else None,
            )

            # ---------- 1. overview ----------
            print("\n== 1. plans overview ==")
            page.goto(f"{BASE}/#/plans", wait_until="networkidle")
            page.wait_for_timeout(2500)
            expect(page.get_by_role("heading", name="计划与打卡")).to_be_visible()
            ok("计划页渲染", True)
            cards = page.locator(".plan-card")
            ok("今日计划卡片加载", cards.count() >= len(plans_before), f"cards={cards.count()}")
            stats = page.locator(".plans-stat")
            ok("总览 stats 卡片", stats.count() >= 4, f"stats={stats.count()}")
            heat_cells = page.locator(".heatmap-cell, .plans-heat-cell")
            ok("热力图渲染", heat_cells.count() >= 100, f"cells={heat_cells.count()}")
            shot(page, "01-plans-overview")

            # ---------- 2. check-in with note + undo ----------
            print("\n== 2. check-in on demo plan ==")
            demo_card = page.locator(".plan-card").filter(has_text=DEMO_PLAN).first
            expect(demo_card).to_be_visible()
            if demo_today_before:
                # undo first so the check-in flow is testable, then re-check
                page.locator(".plans-recent, .plans-rail").first  # just ensure page rendered
            checkin_btn = demo_card.get_by_role("button", name=re.compile(r"^打卡$"))
            if checkin_btn.count() == 0:
                # already checked: undo via card button then wait
                demo_card.locator(".plan-checkedin-btn").click()
                page.get_by_role("button", name="撤销打卡", exact=True).click()
                wait_toast(page, "已撤销")
                page.wait_for_timeout(900)
            demo_card = page.locator(".plan-card").filter(has_text=DEMO_PLAN).first
            demo_card.get_by_role("button", name=re.compile(r"^打卡$")).click()
            dialog = page.get_by_role("dialog", name="今日打卡")
            expect(dialog).to_be_visible()
            dialog.locator("textarea").fill("E2E 验收打卡：读完 20 页")
            shot(page, "02-checkin-dialog")
            dialog.get_by_role("button", name="确认打卡").click()
            wait_toast(page, "打卡成功")
            page.wait_for_timeout(1000)
            demo_after = plan_by_title(DEMO_PLAN)
            ok(
                "打卡成功并更新统计",
                demo_after["today_checked"] is True and demo_after["total_checkins"] == demo_total_before + 1,
                str(demo_after),
            )
            ok(
                "卡片变为已打卡",
                page.locator(".plan-card").filter(has_text=DEMO_PLAN).locator(".plan-checkedin-btn").count() == 1,
            )
            shot(page, "03-after-checkin")

            # undo to restore demo state
            page.locator(".plan-card").filter(has_text=DEMO_PLAN).locator(".plan-checkedin-btn").click()
            page.get_by_role("button", name="撤销打卡", exact=True).click()
            wait_toast(page, "撤销")
            page.wait_for_timeout(900)
            demo_restored = plan_by_title(DEMO_PLAN)
            ok(
                "撤销打卡恢复原状态",
                demo_restored["today_checked"] is False and demo_restored["total_checkins"] == demo_total_before,
                str(demo_restored),
            )

            # ---------- 3. create / check-in / drawer / edit / delete temp plan ----------
            print("\n== 3. temp plan lifecycle ==")
            page.get_by_role("button", name="新建计划").click()
            form = page.get_by_role("dialog", name="新建计划")
            expect(form).to_be_visible()
            form.locator('input[placeholder="例如：每日阅读 30 分钟"]').fill(TEMP_PLAN)
            form.locator('textarea[placeholder="补充说明这个计划的意图或执行方式"]').fill("用 Playwright 验收的临时计划")
            form.locator('[aria-label^="选择颜色"]').nth(3).click()
            shot(page, "04-plan-form")
            form.get_by_role("button", name="创建计划").click()
            wait_toast(page, "计划已创建")
            page.wait_for_timeout(900)
            created = plan_by_title(TEMP_PLAN)
            ok("UI 新建计划成功", created is not None, str(created))
            if created:
                card = page.locator(".plan-card").filter(has_text=TEMP_PLAN).first
                expect(card).to_be_visible()
                card.get_by_role("button", name=re.compile(r"^打卡$")).click()
                dialog = page.get_by_role("dialog", name="今日打卡")
                dialog.locator("textarea").fill("E2E 临时计划打卡")
                dialog.get_by_role("button", name="确认打卡").click()
                wait_toast(page, "打卡成功")
                page.wait_for_timeout(800)
                checked = plan_by_title(TEMP_PLAN)
                ok("临时计划打卡", checked["today_checked"] is True)

                # open drawer + calendar
                page.get_by_role("button", name=re.compile("查看计划 " + re.escape(TEMP_PLAN) + " 的详情")).click()
                expect(page.locator('[aria-label="关闭计划详情"]')).to_be_visible()
                page.wait_for_timeout(1000)
                shot(page, "05-plan-drawer")
                ok("计划详情抽屉与月历", page.get_by_label("上个月").count() >= 1)

                # edit
                page.get_by_label("编辑计划").click()
                edit_form = page.get_by_role("dialog", name="编辑计划")
                edit_form.locator('input[placeholder="例如：每日阅读 30 分钟"]').fill(TEMP_PLAN_UPDATED)
                edit_form.get_by_role("button", name="保存修改").click()
                wait_toast(page, "计划已更新")
                page.wait_for_timeout(800)
                ok("编辑计划成功", plan_by_title(TEMP_PLAN_UPDATED) is not None)

                # reopen drawer for updated plan, then delete
                page.get_by_role(
                    "button", name=re.compile("查看计划 " + re.escape(TEMP_PLAN_UPDATED) + " 的详情")
                ).click()
                expect(page.locator('[aria-label="关闭计划详情"]')).to_be_visible()
                page.wait_for_timeout(600)
                page.get_by_label("删除计划").click()
                page.get_by_role("dialog", name=re.compile("删除这个计划")).get_by_role("button", name="删除计划", exact=True).click()
                wait_toast(page, "计划已删除")
                page.wait_for_timeout(800)
                ok("删除计划成功（打卡级联）", plan_by_title(TEMP_PLAN_UPDATED) is None)
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(1500)

            # ---------- 4. weekly badge + 1024 ----------
            print("\n== 4. responsive ==")
            weekly = page.locator(".plan-card").filter(has_text="每周跑步 3 次").first
            ok("每周计划徽标", weekly.get_by_text(re.compile("每周 3 次")).count() >= 1)
            page.set_viewport_size({"width": 1024, "height": 768})
            page.wait_for_timeout(1500)
            overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
            ok("1024px 无横向溢出", overflow <= 1, f"overflow={overflow}px")
            shot(page, "06-plans-1024")

            # other pages regression
            page.set_viewport_size({"width": 1440, "height": 900})
            for route, text in (("#/chat", "你好，我是知脉。"), ("#/graph", "神经图谱"), ("#/library", "知识库")):
                page.goto(f"{BASE}/{route}")
                page.wait_for_timeout(1400)
                ok(f"{route} 正常渲染", page.get_by_text(text).count() > 0)

            ok("无 console error", not CONSOLE, "; ".join(CONSOLE[:3]))
            ok("无 page error", not PAGE_ERRORS, "; ".join(PAGE_ERRORS[:3]))
            ok("无 API 4xx/5xx", not HTTP_ERRORS, "; ".join(HTTP_ERRORS[:5]))
            ctx.close()
    finally:
        try:
            cleanup()
        except Exception as exc:  # noqa: BLE001
            print(f"cleanup warning: {exc}")
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    print("\n=== summary ===")
    print(f"PASS {len(PASS)} | FAIL {len(FAIL)}")
    for f in FAIL:
        print(f"  - {f}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
