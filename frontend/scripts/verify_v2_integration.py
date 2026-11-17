#!/usr/bin/env python3
"""Synapse V2 集成实例（7801）前端验收：4 卡真实数据 + 对话 MCP 工具卡 + 1024。

前提：
  - 集成后端 http://127.0.0.1:7801 已启动（Lead 提供）
  - Vite dev server 5177 → 7801
  - 本脚本只做只读验证（含 MCP/embedding 测试连接与一次对话），不改配置、不跑巡检；
    写操作流程已在 7811/7812 临时库验证（verify_v2_real.py）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

UI_BASE = os.environ.get("INTEGRATION_UI_BASE", "http://127.0.0.1:5177").rstrip("/")
API_BASE = os.environ.get("INTEGRATION_API_BASE", "http://127.0.0.1:7801").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".screenshots-v2" / "integration-7801"
SHOTS.mkdir(parents=True, exist_ok=True)

PASS: list[str] = []
FAIL: list[str] = []
CONSOLE_ERRORS: list[str] = []
PAGE_ERRORS: list[str] = []
HTTP_ERRORS: list[str] = []
LOG_LINES: list[str] = []


def log(message: str) -> None:
    LOG_LINES.append(message)
    print(message)


def ok(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} {detail}")


def attach(page: Page) -> None:
    page.on(
        "console",
        lambda msg: CONSOLE_ERRORS.append(msg.text) if msg.type == "error" else None,
    )
    page.on("pageerror", lambda exc: PAGE_ERRORS.append(str(exc)))
    page.on(
        "response",
        lambda resp: HTTP_ERRORS.append(f"{resp.status} {resp.url}")
        if resp.status >= 400
        else None,
    )


def wait_no_toast(page: Page) -> None:
    try:
        page.wait_for_function("() => document.querySelectorAll('.toast').length === 0", timeout=6000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(150)


def shot(page: Page, name: str, locator=None) -> None:
    wait_no_toast(page)
    target = SHOTS / f"{name}.png"
    if locator is not None:
        locator.screenshot(path=str(target))
    else:
        page.screenshot(path=str(target), full_page=False)
    print(f"  📸 integration-7801/{name}.png")


def api(path: str, method: str = "GET") -> dict:
    request = urllib.request.Request(f"{API_BASE}{path}", method=method)
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def verify_settings(page: Page) -> None:
    log("\n[集成 7801 · 设置页 4 张 V2 卡]")
    page.goto(f"{UI_BASE}/#/settings", wait_until="domcontentloaded")
    expect(page.locator(".settings-page")).to_be_visible(timeout=30000)
    expect(page.get_by_role("heading", name="模型接入")).to_be_visible(timeout=30000)

    # ---------- MCP ----------
    mcp = page.locator('[data-card="mcp"]')
    expect(mcp.get_by_text("文件系统", exact=True)).to_be_visible(timeout=60000)
    expect(mcp.get_by_text(re.compile(r"2 已启用 · 7 工具"))).to_be_visible(timeout=60000)
    ok("集成 MCP 卡：2 已启用 · 7 工具", "2 已启用 · 7 工具" in mcp.inner_text())
    ok("集成 MCP 卡：3 个 server 行", mcp.locator(".mcp-server").count() == 3)
    ok("集成 MCP 卡：GitHub 未启用", mcp.locator('[data-server="github"]').inner_text().find("未启用") >= 0)
    mcp.get_by_role("button", name=re.compile("展开 文件系统 工具")).click()
    page.wait_for_timeout(300)
    ok("集成 MCP 卡：工具 chips", mcp.locator(".mcp-tool-chip").count() >= 4)
    mcp.get_by_role("button", name="测试连接").first.click()
    expect(mcp.get_by_text(re.compile("连接成功 · \\d+ 个工具"))).to_be_visible(timeout=30000)
    ok("集成 MCP 卡：测试连接成功", True)
    mcp.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    shot(page, "01-mcp-card", mcp)

    # ---------- 语义向量 ----------
    emb = page.locator('[data-card="embedding"]')
    expect(emb.get_by_text("本地语义模型").first).to_be_visible(timeout=30000)
    emb_text = emb.inner_text()
    ok("集成语义向量卡：fastembed / 384 维", "本地语义模型" in emb_text and "384 维" in emb_text)
    ok("集成语义向量卡：1,538 分块", "1,538" in emb_text)
    ok("集成语义向量卡：已就绪", "已就绪" in emb_text and "待重建" not in emb_text)
    ok("集成语义向量卡：OCR 状态行", "RapidOCR 已就绪" in emb_text)
    emb.get_by_role("button", name="测试连接").click()
    expect(emb.get_by_text("向量测试通过")).to_be_visible(timeout=60000)
    ok("集成语义向量卡：测试连接通过", True)
    emb.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    shot(page, "02-embedding-card", emb)

    # ---------- 反馈偏好 ----------
    rer = page.locator('[data-card="reranker"]')
    expect(rer.get_by_text("已采纳").first).to_be_visible(timeout=30000)
    rer_text = rer.inner_text()
    ok("集成 Reranker 卡：计数", "已采纳" in rer_text and "已忽略" in rer_text and "8" in rer_text)
    ok("集成 Reranker 卡：top domains", rer.locator(".rerank-domain").count() >= 3)
    ok("集成 Reranker 卡：偏好说明", "规则化偏好" in rer_text)
    rer.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    shot(page, "03-reranker-card", rer)

    # ---------- 知识巡检（7801 已有真实巡检结果） ----------
    patrol = page.locator('[data-card="patrol"]')
    expect(patrol.get_by_label("巡检周期")).to_be_visible(timeout=30000)
    expect(patrol.get_by_text("巡检报告", exact=True)).to_be_visible(timeout=30000)
    patrol_text = patrol.inner_text()
    ok("集成巡检卡：启用状态", "定时开启" in patrol_text)
    ok("集成巡检卡：真实巡检报告", "巡检报告" in patrol_text and "自动整理" in patrol_text)
    ok("集成巡检卡：6 类 issues 计数", patrol.locator(".patrol-issue-chip").count() == 6)
    ok("集成巡检卡：enriched 标题 chips", patrol.locator(".patrol-enriched-chip").count() >= 1)
    patrol.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    shot(page, "04-patrol-card", patrol)


def verify_chat(page: Page) -> None:
    log("\n[集成 7801 · 对话 MCP 工具调用]")
    page.goto(f"{UI_BASE}/#/chat", wait_until="domcontentloaded")
    expect(page.get_by_label("提问输入框")).to_be_visible(timeout=30000)
    page.wait_for_timeout(800)
    page.get_by_label("提问输入框").fill("列出数据目录下的 Markdown 文件")
    page.get_by_label("发送消息").click()

    expect(page.locator(".tool-call").first).to_be_visible(timeout=150000)
    expect(page.locator(".tool-call.is-ok, .tool-call.is-error").first).to_be_visible(timeout=150000)
    ok("集成对话：出现 MCP 工具卡片", page.locator(".tool-call").count() >= 1)
    ok("集成对话：MCP 徽标", page.locator(".tool-call-badge").count() >= 1)
    first_tool = page.locator(".tool-call").first.inner_text()
    ok(
        "集成对话：工具卡片含 server/tool 与结果",
        ("文件系统" in first_tool or "知识库数据库" in first_tool or "GitHub" in first_tool)
        and ("已返回" in first_tool or "返回" in first_tool or "失败" in first_tool),
        first_tool.replace("\n", " ")[:160],
    )
    expect(page.locator(".answer-progress")).to_have_count(0, timeout=150000)
    page.wait_for_timeout(1200)
    answer = page.locator(".assistant-card").last.inner_text()
    ok("集成对话：最终回答非空", len(answer) > 80, answer[:120].replace("\n", " "))
    ok("集成对话：引用来源不回归", "参考来源" in answer or page.locator(".citations-block").count() >= 1)
    ok(
        "集成对话：MCP 成功时不显示 gap 卡片",
        page.locator(".gap-card").count() == 0,
        f"gap-card={page.locator('.gap-card').count()}",
    )
    page.screenshot(path=str(SHOTS / "05-chat-mcp-tool.png"))
    print("  📸 integration-7801/05-chat-mcp-tool.png")
    # 记录会话 id，便于清理
    match = re.search(r"#/chat/([A-Za-z0-9_-]+)", page.url)
    if match:
        try:
            api(f"/api/conversations/{match.group(1)}", method="DELETE")
            log(f"  · 已清理本次验收会话 {match.group(1)}")
        except Exception as exc:  # noqa: BLE001
            log(f"  · 清理会话失败（可忽略）：{exc}")


def verify_1024(browser) -> None:
    log("\n[集成 7801 · 1024px 响应式]")
    context = browser.new_context(viewport={"width": 1024, "height": 768}, device_scale_factor=2, locale="zh-CN")
    page = context.new_page()
    attach(page)
    page.goto(f"{UI_BASE}/#/settings", wait_until="domcontentloaded")
    expect(page.locator(".settings-page")).to_be_visible(timeout=30000)
    expect(page.locator('[data-card="mcp"]').get_by_text("文件系统", exact=True)).to_be_visible(timeout=60000)
    page.wait_for_timeout(1200)
    metrics = page.evaluate(
        """() => {
          const doc = document.documentElement;
          const scroll = document.querySelector('.page-scroll');
          const cards = Array.from(document.querySelectorAll('[data-card]')).map((el) => ({
            key: el.getAttribute('data-card'),
            scrollWidth: el.scrollWidth,
            clientWidth: el.clientWidth,
            top: el.getBoundingClientRect().top + (scroll ? scroll.scrollTop : 0),
          }));
          return { docScroll: doc.scrollWidth, inner: window.innerWidth,
                   pageScroll: scroll ? scroll.scrollWidth : 0,
                   pageClient: scroll ? scroll.clientWidth : 0, cards };
        }"""
    )
    ok(
        "集成 1024：无横向溢出",
        metrics["docScroll"] <= metrics["inner"] + 1
        and metrics["pageScroll"] <= metrics["pageClient"] + 1,
        str(metrics),
    )
    ok(
        "集成 1024：卡片无溢出",
        all(card["scrollWidth"] <= card["clientWidth"] + 1 for card in metrics["cards"]),
        str(metrics["cards"]),
    )
    page.evaluate("() => document.querySelector('.page-scroll')?.scrollTo(0, 0)")
    page.wait_for_timeout(400)
    shot(page, "06-settings-1024")
    page.locator('[data-card="mcp"]').scroll_into_view_if_needed()
    page.wait_for_timeout(400)
    shot(page, "06b-settings-1024-v2")
    context.close()


def main() -> int:
    log(f"Synapse V2 集成实例验收 · UI {UI_BASE} → API {API_BASE}")
    try:
        health = api("/api/health")
        log(f"  ✓ 7801 health：{health.get('status')} embedding={health.get('embedding')} ocr={health.get('ocr')}")
    except Exception as exc:  # noqa: BLE001
        log(f"  ✗ 7801 不可用：{exc}")
        return 1
    mcp_status = api("/api/mcp/status")
    log(f"  · 7801 MCP：{mcp_status.get('enabled_count')} 已启用 / {mcp_status.get('tool_count')} 工具")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, locale="zh-CN")
        page = context.new_page()
        attach(page)
        verify_settings(page)
        verify_chat(page)
        context.close()
        verify_1024(browser)
        browser.close()

    unexpected_http = [item for item in HTTP_ERRORS if "/favicon" not in item]
    ok("集成 0 console error", not CONSOLE_ERRORS, " | ".join(CONSOLE_ERRORS[:5]))
    ok("集成 0 page error", not PAGE_ERRORS, " | ".join(PAGE_ERRORS[:5]))
    ok("集成 0 非预期 HTTP 错误", not unexpected_http, " | ".join(unexpected_http[:5]))

    log("\n============ 集成 7801 验收汇总 ============")
    log(f"PASS: {len(PASS)}  FAIL: {len(FAIL)}")
    for item in FAIL:
        log(f"  FAIL -> {item}")
    log(f"截图目录：{SHOTS}")
    (SHOTS / "verify-integration.log").write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
