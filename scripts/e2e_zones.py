"""Lead acceptance for the Zoned 3D knowledge graph (分区图谱).

Creates a temporary zone + archives two docs through the real UI, verifies isolation
at the API level, exercises rename/single-zone/delete, then restores the original
document↔zone mapping. Read-only with respect to documents/content.

Run after backend migration + frontend rebuild, with Synapse on 7800:
    PLAYWRIGHT_BROWSERS_PATH=... .venv-tools/bin/python scripts/e2e_zones.py [base_url]
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7800").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "test-reports" / "zones-e2e"
SHOTS.mkdir(parents=True, exist_ok=True)

TEST_ZONE = "E2E 验收分区"
TEST_ZONE_RENAMED = "E2E 验收分区改名"
ASSIGN_TITLES = ["资产配置", "应急金该存多少"]

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


def main() -> int:
    docs_before = api("GET", "/api/documents")["items"]
    zones_before = api("GET", "/api/zones")["items"]
    original_zones = {d["id"]: d.get("zone_id") for d in docs_before}
    print(f"baseline: {len(docs_before)} docs / {len(zones_before)} zones")
    # safety: this scenario expects a clean (freshly migrated) library
    ok("初始无同自检分区", not any(z["name"].startswith("E2E") for z in zones_before))

    created_zone_ids: list[str] = []
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

            # ---------- 1. enter zones mode (empty state) ----------
            print("\n== 1. zones mode / empty state ==")
            page.goto(f"{BASE}/#/graph", wait_until="networkidle")
            page.wait_for_timeout(3200)
            page.get_by_role("tab", name="分区图谱").click()
            page.wait_for_timeout(1800)
            expect(page.locator('[aria-label="分区筛选"]')).to_be_visible()
            expect(page.get_by_text("还没有分区")).to_be_visible()
            ok("分区模式空态引导", True)
            shot(page, "01-zones-empty")

            # ---------- 2. create zone via UI ----------
            print("\n== 2. create zone ==")
            page.get_by_role("button", name="管理分区").click()
            dialog = page.get_by_role("dialog", name="管理分区")
            expect(dialog).to_be_visible()
            dialog.get_by_label("新建分区名称").fill(TEST_ZONE)
            dialog.locator('[aria-label^="选择颜色"]').nth(4).click()
            dialog.get_by_role("button", name="创建", exact=True).click()
            wait_toast(page, "分区已创建")
            page.wait_for_timeout(500)
            zones = api("GET", "/api/zones")["items"]
            created = next((z for z in zones if z["name"] == TEST_ZONE), None)
            ok("UI 新建分区成功（API 可见）", created is not None)
            if created:
                created_zone_ids.append(created["id"])
            shot(page, "02-manager-created")

            # ---------- 3. archive two docs via UI ----------
            print("\n== 3. archive docs ==")
            dialog.get_by_role("tab", name=re.compile("文档归档")).click()
            page.wait_for_timeout(900)
            ok("文档归档列表加载", dialog.locator(".zone-doc-item").count() >= 20)
            shot(page, "03-manager-docs")

            for idx, title in enumerate(ASSIGN_TITLES):
                search = dialog.get_by_label("搜索待归档文档")
                search.fill(title)
                page.wait_for_timeout(500)
                checkbox = dialog.locator('[aria-label^="选择文档 ' + title + '"]').first
                expect(checkbox).to_be_visible()
                checkbox.check()
                if idx == 0:
                    dialog.get_by_label("选择目标分区").select_option(label=TEST_ZONE)
                page.get_by_role("button", name=re.compile("应用")).click()
                wait_toast(page, "文档已移入分区")
                page.wait_for_timeout(600)
                page.wait_for_timeout(350)
                search.fill("")
                page.wait_for_timeout(300)

            docs_after = api("GET", "/api/documents")["items"]
            name_by_id = {z["id"]: z["name"] for z in api("GET", "/api/zones")["items"]}
            assigned = [
                d for d in docs_after if any(t in d["title"] for t in ASSIGN_TITLES)
            ]
            ok(
                "两篇文档已归档到测试分区",
                all(d.get("zone_id") in name_by_id and name_by_id[d["zone_id"]] == TEST_ZONE for d in assigned)
                and len(assigned) == 2,
                str([(d["title"], d.get("zone_id")) for d in assigned]),
            )
            ok(
                "归档后行状态立即更新",
                dialog.locator(".zone-doc-item").filter(has_text=TEST_ZONE).count() >= 2,
            )
            shot(page, "04-manager-archived")

            # ---------- 4. API isolation check ----------
            print("\n== 4. graph isolation (API) ==")
            zg = api("GET", "/api/graph?mode=zones&min_concept_degree=2")
            node_group = {n["id"]: (n.get("zone_id") or "__unassigned__") for n in zg["nodes"]}
            cross = [
                l for l in zg["links"]
                if node_group.get(l["source"]) != node_group.get(l["target"])
                or node_group.get(l["source"]) is None
            ]
            ok("分区图谱跨区连线为 0", not cross, f"{len(cross)} cross links")
            e2e_meta = next((z for z in zg["zones"] if z["name"] == TEST_ZONE), None)
            ok("分区图谱包含测试区域", e2e_meta is not None, str([z["name"] for z in zg["zones"]]))
            ok(
                "概念/标签节点带区域后缀",
                all(("@" in n["id"]) for n in zg["nodes"] if n["type"] in ("concept", "tag")),
            )
            ok(
                "分区节点带 zone 字段",
                all("zone_id" in n and "zone_name" in n for n in zg["nodes"]),
            )

            # ---------- 5. close manager, single-zone isolation ----------
            print("\n== 5. single zone view ==")
            dialog.get_by_role("button", name="完成").click()
            page.wait_for_timeout(1500)
            chips = page.locator('[aria-label="分区筛选"]')
            chip = chips.locator("button").filter(has_text=TEST_ZONE).first
            if chip.count():
                chip.click()
                page.wait_for_timeout(1800)
                hud_text = page.locator(".graph-hud").inner_text()
                ok("点击区域 chip 进入单区隔离", bool(re.search(r"1\s*区域", hud_text)), hud_text.replace("\n", " | "))
                shot(page, "05-single-zone")
                chips.locator("button").filter(has_text="全部区域").first.click()
                page.wait_for_timeout(1200)
            else:
                ok("区域 chip 出现", False, "chip not found")

            # ---------- 6. rename + delete ----------
            print("\n== 6. rename + delete ==")
            page.get_by_role("button", name="管理分区").click()
            dialog = page.get_by_role("dialog", name="管理分区")
            expect(dialog).to_be_visible()
            dialog.get_by_label(f"重命名 {TEST_ZONE}").click()
            rename_input = dialog.get_by_label(re.compile("^重命名分区 " + re.escape(TEST_ZONE) + "$"))
            rename_input.fill(TEST_ZONE_RENAMED)
            dialog.get_by_role("button", name="保存", exact=True).first.click()
            wait_toast(page, "分区已重命名")
            page.wait_for_timeout(500)
            zones = api("GET", "/api/zones")["items"]
            ok("重命名成功", any(z["name"] == TEST_ZONE_RENAMED for z in zones))

            dialog.get_by_label(f"删除分区 {TEST_ZONE_RENAMED}").click()
            page.get_by_role("button", name="删除分区", exact=True).click()
            wait_toast(page, "分区已删除")
            page.wait_for_timeout(800)
            zones = api("GET", "/api/zones")["items"]
            ok("删除分区成功", not any(z["name"] == TEST_ZONE_RENAMED for z in zones))
            docs_restored = api("GET", "/api/documents")["items"]
            restored = [d for d in docs_restored if any(t in d["title"] for t in ASSIGN_TITLES)]
            ok(
                "删除分区后文档仍存在且回到未分区",
                len(docs_restored) == len(docs_before)
                and all(d.get("zone_id") is None for d in restored),
                str([(d["title"], d.get("zone_id")) for d in restored]),
            )
            dialog.get_by_role("button", name="完成").click()
            page.wait_for_timeout(800)

            # ---------- 7. global mode regression + 1024 ----------
            print("\n== 7. global regression / responsive ==")
            page.get_by_role("tab", name="全局图谱").click()
            page.wait_for_timeout(2200)
            expect(page.locator(".graph-canvas-wrap")).to_be_visible()
            ok("切回全局图谱正常", True)
            shot(page, "06-global-back")

            page.get_by_role("tab", name="分区图谱").click()
            page.wait_for_timeout(1200)
            page.set_viewport_size({"width": 1024, "height": 768})
            page.wait_for_timeout(1600)
            overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
            ok("1024px 无横向溢出", overflow <= 1, f"overflow={overflow}px")
            shot(page, "07-1024")

            ok("无 console error", not CONSOLE, "; ".join(CONSOLE[:3]))
            ok("无 page error", not PAGE_ERRORS, "; ".join(PAGE_ERRORS[:3]))
            ok("无 API 4xx/5xx", not HTTP_ERRORS, "; ".join(HTTP_ERRORS[:5]))
            ctx.close()
    finally:
        # restore original doc↔zone mapping, delete leftover E2E zones
        try:
            current = api("GET", "/api/documents")["items"]
            by_id = {d["id"]: d for d in current}
            restore_by_zone: dict[str, list[str]] = {}
            for doc_id, zone_id in original_zones.items():
                if doc_id in by_id and by_id[doc_id].get("zone_id") != zone_id:
                    if zone_id is None:
                        api("PUT", "/api/documents/zone", {"doc_ids": [doc_id], "zone_id": None})
                    else:
                        restore_by_zone.setdefault(zone_id, []).append(doc_id)
            for zone_id, doc_ids in restore_by_zone.items():
                api("PUT", "/api/documents/zone", {"doc_ids": doc_ids, "zone_id": zone_id})
            for zone in api("GET", "/api/zones")["items"]:
                if zone["name"].startswith("E2E"):
                    api("DELETE", f"/api/zones/{zone['id']}")
            # second pass: anything still in E2E zones already deleted → unassigned; re-apply restore
            for zone_id, doc_ids in restore_by_zone.items():
                api("PUT", "/api/documents/zone", {"doc_ids": doc_ids, "zone_id": zone_id})
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
