#!/usr/bin/env python3
"""多轮上下文「已结合上文」提示 · 真实后端 7814 验收（Vite 5178 → 7814）。

流程（真实 DeepSeek + 真实 Bing/OpenAlex，7814 隔离数据 tmp/context-fix/）：
  1. 新对话先问「请总结《Transformer-BP 多目标跟踪研究框架》的核心内容」→ 引用文档 chip、
     不显示「已结合上文」（首轮不是追问）。
  2. 追问「创新点呢」→ 继承论文文档、显示「已结合上文」徽标 + context_summary、
     无 gap 卡片、回答带引用。
  3. 打开扩展面板 → 真实 /extend/search 带 conversation_id，顶部显示「已结合上文」提示行。
  4. 0 console/page error；会话结束 DELETE 清理；截图 frontend/.screenshots-v2/context-fix/。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

UI_BASE = os.environ.get("CONTEXT_UI_BASE", "http://127.0.0.1:5178").rstrip("/")
API_BASE = os.environ.get("CONTEXT_API_BASE", "http://127.0.0.1:7814").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".screenshots-v2" / "context-fix"
SHOTS.mkdir(parents=True, exist_ok=True)

PASS: list[str] = []
FAIL: list[str] = []
CONSOLE_ERRORS: list[str] = []
PAGE_ERRORS: list[str] = []
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


def shot(page: Page, name: str, locator=None) -> None:
    try:
        page.wait_for_function("() => document.querySelectorAll('.toast').length === 0", timeout=6000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(150)
    target = SHOTS / f"{name}.png"
    if locator is not None:
        locator.screenshot(path=str(target))
    else:
        page.screenshot(path=str(target), full_page=False)
    print(f"  📸 context-fix/{name}.png")


def api(path: str, method: str = "GET") -> dict:
    request = urllib.request.Request(f"{API_BASE}{path}", method=method)
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def send_and_wait(page: Page, text: str) -> None:
    """发送消息并等待本轮 SSE 完成 + 前端流式渲染结束。"""
    page.get_by_label("提问输入框").fill(text)
    with page.expect_response(lambda response: "/api/chat" in response.url, timeout=180000):
        page.get_by_label("发送消息").click()
    page.wait_for_timeout(400)
    expect(page.locator(".answer-progress")).to_have_count(0, timeout=120000)
    page.wait_for_timeout(800)


def main() -> int:
    log(f"多轮上下文提示真实验收 · UI {UI_BASE} → API {API_BASE}")
    try:
        health = api("/api/health")
        log(f"  ✓ 7814 health：{health.get('status')} llm={health.get('llm', {}).get('model')}")
    except Exception as exc:  # noqa: BLE001
        log(f"  ✗ 7814 不可用：{exc}")
        return 1

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, locale="zh-CN")
        page = context.new_page()
        page.set_default_timeout(20000)
        page.on("console", lambda msg: CONSOLE_ERRORS.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: PAGE_ERRORS.append(str(exc)))

        page.goto(f"{UI_BASE}/#/chat", wait_until="domcontentloaded")
        expect(page.get_by_label("提问输入框")).to_be_visible(timeout=30000)
        page.wait_for_timeout(800)

        # ---------- 第一轮：显式总结论文（非追问） ----------
        log("\n[第一轮：显式总结论文]")
        send_and_wait(page, "请总结《Transformer-BP 多目标跟踪研究框架》的核心内容")
        ref_chips = page.locator(".msg-reference-chip").count()
        ok("首轮识别引用文档", ref_chips >= 1, f"chips={ref_chips}")
        ok("首轮不显示「已结合上文」", page.locator(".msg-context-chip").count() == 0)
        first_answer = page.locator(".assistant-card").last.inner_text()
        ok("首轮回答非空", len(first_answer) > 120, first_answer[:100].replace("\n", " "))

        # ---------- 第二轮：追问继承上文 ----------
        log("\n[第二轮：追问「创新点呢」]")
        page.get_by_label("提问输入框").fill("创新点呢")
        with page.expect_response(lambda response: "/api/chat" in response.url, timeout=180000):
            page.get_by_label("发送消息").click()
        expect(page.locator(".msg-context-chip").first).to_be_visible(timeout=60000)
        page.wait_for_timeout(400)
        expect(page.locator(".answer-progress")).to_have_count(0, timeout=120000)
        page.wait_for_timeout(1500)

        chip_text = page.locator(".msg-context-chip").last.inner_text()
        ok("追问显示「已结合上文」徽标", "已结合上文" in chip_text, chip_text)
        ok("徽标含 context_summary", "Transformer-BP" in chip_text and "创新点" in chip_text, chip_text)
        ok("追问继承引用文档 chip", page.locator(".msg-reference-chip").count() >= 2, f"chips={page.locator('.msg-reference-chip').count()}")
        ok("追问不显示 gap 卡片", page.locator(".gap-card").count() == 0)
        second_answer = page.locator(".assistant-card").last.inner_text()
        ok("追问回答基于论文且非空", len(second_answer) > 200 and "创新" in second_answer, second_answer[:120].replace("\n", " "))
        ok("追问回答带引用来源", page.locator(".citations-block").count() >= 2)
        page.evaluate(
            """() => {
              const cards = document.querySelectorAll('.assistant-card');
              const el = cards[cards.length - 1];
              const scroller = document.querySelector('.chat-scroll');
              if (!el || !scroller) return;
              const top = el.getBoundingClientRect().top
                - scroller.getBoundingClientRect().top + scroller.scrollTop;
              scroller.scrollTop = Math.max(0, top - 14);
            }"""
        )
        page.wait_for_timeout(500)
        shot(page, "01-chat-context-hint")

        # ---------- 扩展面板：真实 /extend/search 带上下文 ----------
        log("\n[扩展面板：带 conversation_id 的真实扩展搜索]")
        page.locator(".assistant-card").last.get_by_label("联网搜索补充资料").click()
        expect(page.locator(".extend-panel")).to_be_visible(timeout=20000)
        expect(page.locator(".extend-context-hint")).to_be_visible(timeout=60000)
        hint_text = page.locator(".extend-context-hint").inner_text()
        ok("扩展面板显示「已结合上文」提示", "已结合上文" in hint_text and "Transformer-BP" in hint_text, hint_text)
        page.wait_for_timeout(600)
        ok("扩展面板显示学术候选", page.locator(".extend-candidate").count() >= 1, f"candidates={page.locator('.extend-candidate').count()}")
        shot(page, "02-extend-context-hint")
        try:
            page.get_by_label("关闭").click()
        except Exception:  # noqa: BLE001
            page.keyboard.press("Escape")
        page.wait_for_timeout(400)

        # ---------- 清理本次验收会话 ----------
        match = re.search(r"#/chat/([A-Za-z0-9_-]+)", page.url)
        if match:
            try:
                api(f"/api/conversations/{match.group(1)}", method="DELETE")
                log(f"  · 已清理本次验收会话 {match.group(1)}")
            except Exception as exc:  # noqa: BLE001
                log(f"  · 清理会话失败（可忽略）：{exc}")

        ok("0 console error", not CONSOLE_ERRORS, " | ".join(CONSOLE_ERRORS[:5]))
        ok("0 page error", not PAGE_ERRORS, " | ".join(PAGE_ERRORS[:5]))
        context.close()
        browser.close()

    log("\n============ 多轮上下文提示验收汇总 ============")
    log(f"PASS: {len(PASS)}  FAIL: {len(FAIL)}")
    for item in FAIL:
        log(f"  FAIL -> {item}")
    log(f"截图目录：{SHOTS}")
    (SHOTS / "verify-context.log").write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
