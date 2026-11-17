"""Lead-owned Playwright end-to-end acceptance for Synapse at http://127.0.0.1:7800.

Prereqs:
    1. Synapse running on 7800 (backend serving built frontend)
    2. OpenAI-compatible fake LLM running on 127.0.0.1:7899 (scripts/fake_llm.py)

Run:
    PLAYWRIGHT_BROWSERS_PATH=/Users/jace/Documents/deepseek-harness/default-workspace/.playwright \
      /Users/jace/Documents/deepseek-harness/default-workspace/.venv-tools/bin/python scripts/e2e.py

Screenshots → test-reports/e2e/
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

BASE = "http://127.0.0.1:7800"
ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "test-reports" / "e2e"
SHOTS.mkdir(parents=True, exist_ok=True)

PASS: list[str] = []
FAIL: list[str] = []
CONSOLE_ERRORS: list[str] = []
HTTP_ERRORS: list[str] = []
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
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - localhost only
        raw = resp.read()
    return json.loads(raw) if raw else {}


def shot(page: Page, name: str) -> None:
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=False)
    print(f"     📸 {name}.png")


def wait_stream_done(page: Page, timeout: int = 45000) -> None:
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        if page.locator('[aria-label="停止生成"]').count() == 0:
            return
        page.wait_for_timeout(120)
    raise AssertionError("streaming did not finish in time")


def wait_toast(page: Page, title: str, timeout: int = 15000) -> None:
    expect(page.locator(".toast-title").filter(has_text=title).first).to_be_visible(timeout=timeout)


def wait_answer(page: Page, index: int = -1, timeout: int = 45000) -> None:
    wait_stream_done(page, timeout)
    page.wait_for_timeout(350)
    expect(page.locator(".msg-assistant").nth(index)).to_be_visible(timeout=20000)


def reset_state() -> None:
    """Clean conversations and LLM config so the walkthrough starts from a known state."""
    try:
        convs = api("GET", "/api/conversations").get("items", [])
        for conv in convs:
            api("DELETE", f"/api/conversations/{conv['id']}")
        for doc in api("GET", "/api/documents").get("items", []):
            if doc["title"].startswith("E2E"):
                api("DELETE", f"/api/documents/{doc['id']}")
        api(
            "PUT",
            "/api/settings",
            {"llm": {"base_url": "", "model": "", "api_key": "", "temperature": 0.2}},
        )
        health = api("GET", "/api/health")
        print(f"   state: docs={health['documents']} chunks={health['chunks']} llm={health['llm']['configured']}")
    except Exception as exc:  # noqa: BLE001
        print(f"   reset_state warning: {exc}")


def main() -> int:
    reset_state()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, device_scale_factor=2, locale="zh-CN"
        )
        page = context.new_page()
        page.set_default_timeout(15000)

        page.on(
            "console",
            lambda msg: CONSOLE_ERRORS.append(f"{msg.type}: {msg.text}")
            if msg.type in ("error",)
            else None,
        )
        page.on("pageerror", lambda exc: PAGE_ERRORS.append(str(exc)))
        page.on(
            "response",
            lambda resp: HTTP_ERRORS.append(f"{resp.status} {resp.request.method} {resp.url}")
            if resp.status >= 400 and "/api/" in resp.url
            else None,
        )

        # ============================ 1. 对话（本地检索模式） ============================
        print("\n== 1. chat / local extractive ==")
        page.goto(f"{BASE}/#/chat", wait_until="networkidle")
        expect(page.get_by_text("你好，我是知脉。")).to_be_visible()
        expect(page.get_by_text("本地检索模式").first).to_be_visible()
        ok("欢迎态渲染", True)
        shot(page, "01-chat-welcome")

        page.get_by_role("button", name="RAG 的检索流程是怎样的？").click()
        wait_answer(page, -1)
        expect(page.locator(".msg-assistant").last).to_contain_text("根据你的知识库")
        expect(page.locator(".msg-assistant").last).to_contain_text("参考来源")
        ok("本地检索回答与引用", True)
        shot(page, "02-chat-answer-local")

        panel = page.locator('[aria-label="引用来源面板"]')
        if panel.count() == 0:
            page.locator('[aria-label="查看引用来源"]').first.click()
        expect(page.locator('[aria-label="引用来源面板"]')).to_be_visible()
        ok("引用来源面板", True)
        shot(page, "03-chat-citations")

        # follow-up turn
        composer = page.locator('[aria-label="提问输入框"]')
        composer.fill("那向量检索和 BM25 为什么要融合？")
        composer.press("Enter")
        wait_answer(page, -1)
        ok("多轮追问", page.locator(".msg-user").count() >= 2)

        # history popover
        page.locator(".chat-header-actions button").filter(has_text="历史").click()
        expect(page.locator('[role="menu"][aria-label="历史会话"]')).to_be_visible()
        page.wait_for_timeout(400)
        shot(page, "04-chat-history")

        # new conversation (also closes the history dropdown)
        page.get_by_role("button", name="新对话").click()
        expect(page.get_by_text("你好，我是知脉。")).to_be_visible()
        ok("新建对话回到欢迎态", True)

        # ============================ 2. 知识库 ============================
        print("\n== 2. library ==")
        page.get_by_role("link", name="知识库").click()
        expect(page.get_by_text("知识库", exact=True).first).to_be_visible()
        page.wait_for_timeout(700)
        ok("知识库页渲染", page.locator(".doc-card").count() >= 16)
        shot(page, "05-library")

        # add a note via modal
        page.get_by_role("button", name="添加笔记").click()
        dialog = page.locator('[aria-label="添加笔记"]')
        expect(dialog).to_be_visible()
        dialog.locator('input[placeholder="例如：周末阅读方法总结"]').fill("E2E 验收临时笔记")
        dialog.locator('textarea[placeholder="写下你的笔记、想法或摘录…"]').fill(
            "# E2E 验收临时笔记\n\n这是一篇 Lead 端到端验收创建的临时笔记，关键词：突触脉冲、验收锚点。\n\n"
            "## 验收要点\n\n- 上传后应能被检索到\n- 删除后图谱连接应同步移除\n"
        )
        dialog.locator('input[placeholder="例如：学习方法, 阅读"]').fill("E2E, 验收")
        dialog.get_by_role("button", name="保存笔记").click()
        wait_toast(page, "笔记已添加")
        ok("添加笔记成功", True)

        search_input = page.locator('input[placeholder="搜索文档标题、摘要或标签…"]')
        search_input.fill("E2E 验收临时笔记")
        page.wait_for_timeout(600)
        expect(page.get_by_text("E2E 验收临时笔记").first).to_be_visible()
        shot(page, "06-library-search")
        page.locator(".doc-card").filter(has_text="E2E 验收临时笔记").click()
        drawer = page.get_by_role("dialog", name="文档详情")
        expect(drawer).to_be_visible()
        page.wait_for_timeout(500)
        ok("文档详情抽屉（分块预览）", page.locator(".doc-drawer, [aria-label='关闭文档详情']").count() > 0)
        shot(page, "07-library-drawer")

        drawer.get_by_role("button", name="删除", exact=True).click()
        page.get_by_role("button", name="删除文档", exact=True).click()
        wait_toast(page, "文档已删除")
        page.wait_for_timeout(700)
        docs_after = api("GET", "/api/documents").get("total")
        ok("删除文档并恢复数量", docs_after == 16, f"docs={docs_after}")
        search_input.fill("")

        # ============================ 3. 神经图谱 ============================
        print("\n== 3. knowledge graph ==")
        page.get_by_role("link", name="神经图谱").click()
        expect(page.locator('[aria-label="知识图谱画布"]')).to_be_visible(timeout=20000)
        page.wait_for_timeout(1800)
        ok("图谱渲染", page.get_by_text("节点").count() > 0)
        shot(page, "08-graph")

        graph_search = page.locator('[aria-label="搜索图谱节点"]')
        graph_search.fill("RAG 检索增强生成实践笔记")
        graph_search.press("Enter")
        expect(page.locator('[aria-label="节点详情"]')).to_be_visible(timeout=15000)
        page.wait_for_timeout(900)
        ok("图谱搜索定位 + 节点详情", True)
        shot(page, "09-graph-node")

        page.get_by_role("button", name="就这个主题提问").click()
        page.wait_for_timeout(500)
        composer = page.locator('[aria-label="提问输入框"]')
        expect(composer).to_have_value(
            "请总结《RAG 检索增强生成实践笔记》的核心内容。", timeout=15000
        )
        ok("图谱节点一键跳转提问并预填", True)
        composer.fill("")

        page.get_by_role("link", name="神经图谱").click()
        page.wait_for_timeout(1200)
        page.locator('[aria-label="放大"]').click()
        page.locator('[aria-label="缩小"]').click()
        page.locator('[aria-label="重置视图"]').click()
        page.get_by_role("button", name="重新布局").click()
        page.wait_for_timeout(900)
        ok("图谱缩放/重排控件", True)

        # ============================ 4. 设置 + LLM 模式 ============================
        print("\n== 4. settings + LLM mode ==")
        page.get_by_role("link", name="设置").click()
        expect(page.get_by_text("模型接入", exact=True).first).to_be_visible()
        page.wait_for_timeout(500)
        shot(page, "10-settings")

        page.get_by_role("button", name="DeepSeek", exact=True).click()
        base_input = page.locator('input[placeholder="https://api.deepseek.com/v1"]')
        base_input.fill("http://127.0.0.1:7899/v1")
        page.locator('input[placeholder="deepseek-chat"]').fill("synapse-mock")
        page.locator('input[type="password"]').fill("test-key-123")
        page.get_by_role("button", name="保存配置").click()
        wait_toast(page, "模型配置已保存")
        page.wait_for_timeout(500)
        page.get_by_role("button", name="测试连接").click()
        expect(page.locator(".test-result-ok, .test-result").filter(has_text="连接成功").first).to_be_visible(timeout=20000)
        ok("设置保存 + 测试连接", True)
        shot(page, "11-settings-llm")

        page.get_by_role("link", name="对话").click()
        page.wait_for_timeout(700)
        composer = page.locator('[aria-label="提问输入框"]')
        composer.fill("我的知识库里关于混合检索的要点有哪些？")
        composer.press("Enter")
        expect(page.locator(".msg-assistant").last).to_contain_text("检索质量", timeout=40000)
        wait_stream_done(page)
        expect(page.locator(".msg-assistant").last).to_contain_text("AI 生成")
        ok("LLM 模式流式回答", True)
        shot(page, "12-chat-llm")

        # restore local-only config
        page.get_by_role("link", name="设置").click()
        page.wait_for_timeout(700)
        page.get_by_role("button", name="清除模型配置").click()
        page.get_by_role("button", name="清除配置").click()
        wait_toast(page, "模型配置已清除")
        page.wait_for_timeout(500)
        settings = api("GET", "/api/settings")
        ok(
            "清除配置恢复本地模式",
            settings["llm"]["configured"] is False and settings["llm"]["api_key_set"] is False,
            json.dumps(settings["llm"], ensure_ascii=False),
        )

        # ============================ 5. 响应式 / 无溢出 ============================
        print("\n== 5. responsive ==")
        page.set_viewport_size({"width": 1024, "height": 768})
        for route, name in (("chat", "13-1024-chat"), ("library", "13-1024-library"), ("graph", "13-1024-graph"), ("settings", "13-1024-settings")):
            page.goto(f"{BASE}/#/{route}")
            page.wait_for_timeout(1500)
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - window.innerWidth"
            )
            ok(f"1024px /{route} 无横向溢出", overflow <= 1, f"overflow={overflow}px")
            if route in ("chat", "graph"):
                shot(page, name)

        # ============================ 汇总 ============================
        bad_console = [e for e in CONSOLE_ERRORS if "favicon" not in e]
        ok("无 console error", not bad_console, "; ".join(bad_console[:3]))
        ok("无 page error", not PAGE_ERRORS, "; ".join(PAGE_ERRORS[:3]))
        ok("无 API 4xx/5xx", not HTTP_ERRORS, "; ".join(HTTP_ERRORS[:5]))

        context.close()
        browser.close()

    # cleanup generated data
    try:
        for conv in api("GET", "/api/conversations").get("items", []):
            api("DELETE", f"/api/conversations/{conv['id']}")
        docs = api("GET", "/api/documents").get("items", [])
        for doc in docs:
            if doc["title"].startswith("E2E"):
                api("DELETE", f"/api/documents/{doc['id']}")
    except Exception as exc:  # noqa: BLE001
        print(f"cleanup warning: {exc}")

    print("\n=== summary ===")
    print(f"PASS {len(PASS)} | FAIL {len(FAIL)}")
    for f in FAIL:
        print(f"  - {f}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
