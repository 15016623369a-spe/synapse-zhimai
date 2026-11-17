"""Lead acceptance for plan groups + one-off schedules.

Creates a temporary group/plan, completes & undoes today's demo schedule, creates a
one-off schedule via the type-switch form, then restores demo state.

Run with Synapse on 7800 and Playwright env:
    PLAYWRIGHT_BROWSERS_PATH=... .venv-tools/bin/python scripts/e2e_plan_groups.py [base_url]
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7800").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "test-reports" / "plan-groups-e2e"
SHOTS.mkdir(parents=True, exist_ok=True)
TODAY = date.today().isoformat()

TEST_GROUP = "E2E 验收分组"
TEST_PLAN = "E2E 组内习惯"
TEST_SCHEDULE = "E2E 牙科复诊"

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


def get_plan(title: str) -> dict | None:
    return next((p for p in api("GET", "/api/plans")["items"] if p["title"] == title), None)


def get_group(name: str) -> dict | None:
    return next((g for g in api("GET", "/api/plan-groups")["items"] if g["name"] == name), None)


def cleanup() -> None:
    # delete any E2E plan, ungroup/delete E2E groups, restore 瑜伽课 completion state
    for plan in api("GET", "/api/plans?include_archived=1")["items"]:
        if plan["title"].startswith("E2E"):
            api("DELETE", f"/api/plans/{plan['id']}")
    for group in api("GET", "/api/plan-groups")["items"]:
        if group["name"].startswith("E2E"):
            api("DELETE", f"/api/plan-groups/{group['id']}")
    yoga = get_plan("瑜伽课")
    if yoga and yoga.get("checked"):
        api("DELETE", f"/api/plans/{yoga['id']}/checkins/{yoga['plan_date']}")


def main() -> int:
    groups_before = api("GET", "/api/plan-groups")["items"]
    plans_before = api("GET", "/api/plans")["items"]
    print(f"baseline: {len(plans_before)} plans / {len(groups_before)} groups / today={TODAY}")
    yoga_before = get_plan("瑜伽课")
    if not yoga_before:
        print("missing demo schedule 瑜伽课; run scripts/setup_plan_groups.py first")
        return 2

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
            print("\n== 1. grouped plans overview ==")
            page.goto(f"{BASE}/#/plans", wait_until="networkidle")
            page.wait_for_timeout(2600)
            expect(page.get_by_role("heading", name="计划与打卡")).to_be_visible()
            ok("分组头部渲染", page.get_by_text("日常习惯").count() + page.get_by_text("生活行程").count() >= 2)
            ok("今日行程区块", page.get_by_text("今日行程").count() >= 1 and page.get_by_text("瑜伽课").count() >= 1)
            ok("即将到来（体检）", page.get_by_text("体检").count() >= 1)
            shot(page, "01-overview")

            # ---------- 2. create group + plan in group ----------
            print("\n== 2. group create + plan in group ==")
            page.get_by_role("button", name="管理分组").click()
            manager = page.get_by_role("dialog", name="管理分组")
            expect(manager).to_be_visible()
            manager.locator('input[placeholder="例如：运动健康"]').fill(TEST_GROUP)
            manager.get_by_role("button", name="创建分组").click()
            wait_toast(page, "分组已创建")
            page.wait_for_timeout(600)
            ok("UI 新建分组", get_group(TEST_GROUP) is not None)
            shot(page, "02-group-manager")
            manager.get_by_role("button", name="完成").click()
            page.wait_for_timeout(1000)

            group_block = page.locator(".plan-group").filter(has_text=TEST_GROUP).first
            expect(group_block).to_be_visible()
            group_block.get_by_role("button", name="添加计划").click()
            form = page.get_by_role("dialog", name="新建计划")
            expect(form).to_be_visible()
            form.locator('input[placeholder="例如：每日阅读 30 分钟"]').fill(TEST_PLAN)
            form.locator('textarea[placeholder="补充说明这个计划或行程的内容"]').fill("用于 Lead 验收的分组计划")
            form.locator("select").last.select_option(label=TEST_GROUP)
            form.get_by_role("button", name="创建计划").click()
            wait_toast(page, "计划已创建")
            page.wait_for_timeout(800)
            plan = get_plan(TEST_PLAN)
            group = get_group(TEST_GROUP)
            ok("分组内新建计划成功", plan is not None and group is not None and plan.get("group_id") == group["id"])
            shot(page, "03-group-plan")

            # ---------- 3. delete group -> plan ungrouped ----------
            print("\n== 3. delete group (plan preserved) ==")
            page.get_by_role("button", name="管理分组").click()
            manager = page.get_by_role("dialog", name="管理分组")
            expect(manager).to_be_visible()
            manager.get_by_label(f"删除分组 {TEST_GROUP}").click()
            page.get_by_role("button", name="删除分组", exact=True).click()
            wait_toast(page, "分组已删除")
            page.wait_for_timeout(700)
            ok("分组已删除、计划转未分组", get_group(TEST_GROUP) is None and get_plan(TEST_PLAN) is not None)
            plan_after = get_plan(TEST_PLAN)
            ok("未分组计划保留", plan_after is not None and plan_after.get("group_id") is None)
            manager.get_by_role("button", name="完成").click()
            page.wait_for_timeout(600)

            # ---------- 4. complete & undo today's schedule ----------
            print("\n== 4. schedule complete + undo ==")
            schedule = page.locator(".schedule-card").filter(has_text="瑜伽课").first
            expect(schedule).to_be_visible()
            schedule.get_by_role("button", name=re.compile(r"^完成$")).click()
            dialog = page.get_by_role("dialog", name="完成行程")
            expect(dialog).to_be_visible()
            dialog.locator("textarea").fill("练完拉伸 10 分钟")
            shot(page, "04-complete-dialog")
            dialog.get_by_role("button", name="确认完成").click()
            wait_toast(page, "行程已完成")
            page.wait_for_timeout(1000)
            checked = get_plan("瑜伽课")
            overview = api("GET", "/api/plans/overview?days=120")["stats"]
            ok(
                "今日行程完成并更新统计",
                checked is not None and checked.get("checked") is True and overview.get("today_schedule_checked") == 1,
                str(overview),
            )
            shot(page, "05-schedule-completed")
            page.locator(".schedule-card").filter(has_text="瑜伽课").locator(".schedule-completed-btn").click()
            page.get_by_role("button", name="撤销完成", exact=True).click()
            wait_toast(page, "已撤销完成")
            page.wait_for_timeout(800)
            ok("撤销完成恢复", get_plan("瑜伽课").get("checked") is False)

            # ---------- 5. one-off schedule form ----------
            print("\n== 5. one-off schedule form ==")
            page.get_by_role("button", name="新建计划").click()
            form = page.get_by_role("dialog", name="新建计划")
            expect(form).to_be_visible()
            form.get_by_role("button", name=re.compile("单日行程")).click()
            form.locator('input[placeholder="例如：19:00 瑜伽课"]').fill(TEST_SCHEDULE)
            form.locator('textarea[placeholder="补充说明这个计划或行程的内容"]').fill("复诊完顺便洗牙")
            form.locator('input[type="date"]').fill(TODAY)
            form.locator('input[type="time"]').nth(0).fill("15:00")
            form.locator('input[type="time"]').nth(1).fill("15:30")
            form.locator('input[placeholder="例如：市体检中心"]').fill("口腔医院 3 楼")
            form.locator("select").last.select_option(label="生活行程")
            shot(page, "06-schedule-form")
            form.get_by_role("button", name="创建行程").click()
            wait_toast(page, "行程已创建")
            page.wait_for_timeout(900)
            created = get_plan(TEST_SCHEDULE)
            ok(
                "UI 创建单日行程",
                created is not None
                and created.get("frequency") == "once"
                and created.get("plan_date") == TODAY
                and created.get("start_time") == "15:00"
                and created.get("location") == "口腔医院 3 楼",
                str(created),
            )
            ok("今日行程列表出现新行程", page.locator(".schedule-card").filter(has_text=TEST_SCHEDULE).count() >= 1)
            shot(page, "07-schedule-created")

            # ---------- 6. 1024 + regressions ----------
            print("\n== 6. responsive / regression ==")
            page.set_viewport_size({"width": 1024, "height": 768})
            page.wait_for_timeout(1500)
            overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
            ok("1024px 无横向溢出", overflow <= 1, f"overflow={overflow}px")
            shot(page, "08-1024")

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
