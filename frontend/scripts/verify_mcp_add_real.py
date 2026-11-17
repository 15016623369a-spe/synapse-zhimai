#!/usr/bin/env python3
"""MCP 添加工具/AI 帮我配 · 真实后端联调（隔离环境，不碰 live 7800/data）。

环境：
  - 5179 → 7816：本脚本启动的临时后端（tmp/mcp-add-test 数据副本 + 临时 fs root），
    走完整「手动添加自定义 filesystem server → 测试 → 勾选 → 启用 → 对话调用 → 删除」。
  - 5180 → 7815：mcp-dev 的 /api/mcp/suggest 真实后端（临时 DB 副本）。
    AI 建议验证：builtin 建议启用现有 preset（验证后停用恢复）；custom 建议仅预填表单，
    不创建/不执行 uvx（按 mcp-dev 约定）。

截图输出：frontend/.screenshots-v2/mcp-add/real-*.png
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

MANUAL_UI = os.environ.get("MCP_ADD_UI_BASE", "http://127.0.0.1:5179").rstrip("/")
MANUAL_API = os.environ.get("MCP_ADD_API_BASE", "http://127.0.0.1:7816").rstrip("/")
AI_UI = os.environ.get("MCP_AI_UI_BASE", "http://127.0.0.1:5180").rstrip("/")
AI_API = os.environ.get("MCP_AI_API_BASE", "http://127.0.0.1:7815").rstrip("/")

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".screenshots-v2" / "mcp-add"
SHOTS.mkdir(parents=True, exist_ok=True)

VENV_PYTHON = str(ROOT.parent / "backend" / ".venv" / "bin" / "python")
FS_ROOT = str(ROOT.parent / "tmp" / "mcp-add-test" / "root")

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


def attach(page: Page, label: str) -> None:
    page.on(
        "console",
        lambda msg: CONSOLE_ERRORS.append(f"[{label}] {msg.text}")
        if msg.type == "error"
        else None,
    )
    page.on("pageerror", lambda exc: PAGE_ERRORS.append(f"[{label}] {exc}"))


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
    print(f"  📸 mcp-add/{name}.png")


def api(base: str, path: str, method: str = "GET") -> dict:
    request = urllib.request.Request(f"{base}{path}", method=method)
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def open_settings(page: Page, base: str) -> None:
    page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
    expect(page.locator(".settings-page")).to_be_visible(timeout=30000)
    expect(page.locator('[data-card="mcp"]').get_by_text("文件系统", exact=True)).to_be_visible(
        timeout=60000
    )


def verify_manual_custom(browser) -> None:
    log("\n[A. 手动添加自定义 MCP Server（7816 临时后端，完整流程）]")
    context = browser.new_context(
        viewport={"width": 1440, "height": 900}, device_scale_factor=2, locale="zh-CN"
    )
    page = context.new_page()
    page.set_default_timeout(30000)
    attach(page, "manual")
    open_settings(page, MANUAL_UI)
    card = page.locator('[data-card="mcp"]')
    ok("临时后端 MCP 初始为 3 个未启用 preset", card.locator(".mcp-server").count() == 3)

    # ---------- 手动创建自定义 filesystem server ----------
    card.get_by_role("button", name="添加工具").click()
    dialog = page.get_by_role("dialog", name="添加 MCP 工具")
    expect(dialog).to_be_visible(timeout=10000)
    dialog.get_by_role("tab", name=re.compile("手动添加")).click()
    dialog.get_by_label("MCP 显示名称").fill("验证文件工具")
    dialog.get_by_label("MCP 启动命令").fill(VENV_PYTHON)
    dialog.get_by_label("MCP 启动参数").fill(
        "\n".join(["-m", "app.mcp_servers.filesystem_server", "--root", FS_ROOT])
    )
    dialog.get_by_role("button", name="创建并测试").click()
    expect(dialog.get_by_text(re.compile("已发现 4 个工具"))).to_be_visible(timeout=60000)
    ok("真实创建并测试发现 filesystem 4 个工具", "已发现 4 个工具" in dialog.inner_text())
    for tool_name in ("list_directory", "read_text_file", "search_files", "get_file_info"):
        ok(f"真实工具列表包含 {tool_name}", tool_name in dialog.inner_text())
    shot(page, "real-01-tools")

    # 全选只读（filesystem 工具均为只读）并启用
    dialog.get_by_role("button", name=re.compile("全选只读工具")).click()
    dialog.get_by_role("button", name=re.compile("保存并启用")).click()
    expect(dialog).to_be_hidden(timeout=30000)
    row = card.locator(".mcp-server").filter(has_text="验证文件工具")
    expect(row).to_be_visible(timeout=30000)
    expect(row.get_by_text("已连接")).to_be_visible(timeout=30000)
    ok("真实自定义 server 已启用并连接", row.get_by_text("已连接").is_visible())
    ok("自定义行显示「自定义 · stdio」", "自定义 · stdio" in row.inner_text())
    ok("自定义行显示命令摘要", "filesystem_server" in row.inner_text())
    ok("自定义行显示 4 工具", "4 工具" in row.inner_text())
    expect(row.locator(".mcp-allowed")).to_be_visible(timeout=10000)
    ok("允许工具编辑显示 4/4", "4/4" in row.locator(".mcp-allowed").inner_text())
    shot(page, "real-02-custom-row")

    # ---------- 对话调用自定义 MCP 工具 ----------
    page.goto(f"{MANUAL_UI}/#/chat", wait_until="domcontentloaded")
    expect(page.get_by_label("提问输入框")).to_be_visible(timeout=30000)
    page.wait_for_timeout(800)
    page.get_by_label("提问输入框").fill("列出根目录下的 Markdown 文件")
    page.get_by_label("发送消息").click()
    expect(page.locator(".tool-call").first).to_be_visible(timeout=180000)
    expect(page.locator(".tool-call.is-ok, .tool-call.is-error").first).to_be_visible(timeout=180000)
    expect(page.locator(".answer-progress")).to_have_count(0, timeout=180000)
    page.wait_for_timeout(1500)
    tool_text = page.locator(".tool-call").first.inner_text()
    ok("真实对话出现工具卡", page.locator(".tool-call").count() >= 1)
    ok("自定义 MCP 工具卡显示「自定义 MCP」", "自定义 MCP" in page.locator(".tool-call").first.inner_text(), tool_text.replace("\n", " ")[:160])
    ok(
        "自定义 MCP 工具卡显示工具名",
        any(name in page.locator(".tool-call").first.inner_text() for name in ("list_directory", "search_files")),
        tool_text.replace("\n", " ")[:160],
    )
    answer = page.locator(".assistant-card").last.inner_text()
    ok("真实对话回答非空", len(answer) > 60, answer[:120].replace("\n", " "))
    page.screenshot(path=str(SHOTS / "real-03-chat-custom-tool.png"))
    print("  📸 mcp-add/real-03-chat-custom-tool.png")
    match = re.search(r"#/chat/([A-Za-z0-9_-]+)", page.url)
    if match:
        try:
            api(MANUAL_API, f"/api/conversations/{match.group(1)}", method="DELETE")
            log(f"  · 已清理验收会话 {match.group(1)}")
        except Exception as exc:  # noqa: BLE001
            log(f"  · 清理会话失败（可忽略）：{exc}")

    # ---------- 删除自定义 server ----------
    open_settings(page, MANUAL_UI)
    card = page.locator('[data-card="mcp"]')
    row = card.locator(".mcp-server").filter(has_text="验证文件工具")
    expect(row).to_be_visible(timeout=30000)
    if row.locator(".mcp-delete-btn").count() == 0:
        row.get_by_role("button", name=re.compile("展开 验证文件工具 工具")).click()
        page.wait_for_timeout(300)
    row.locator(".mcp-delete-btn").click()
    confirm = page.get_by_role("dialog", name="删除自定义 MCP Server？")
    expect(confirm).to_be_visible(timeout=10000)
    confirm.get_by_role("button", name="删除工具").click()
    expect(card.locator(".mcp-server")).to_have_count(3, timeout=30000)
    ok("真实自定义 server 删除成功", card.locator(".mcp-server").count() == 3)
    ok("删除后内置三件套保留", card.get_by_text("文件系统", exact=True).first.is_visible())
    context.close()


def verify_ai_suggest(browser) -> None:
    log("\n[B. AI 帮我配（7815 真实 /api/mcp/suggest + DeepSeek）]")
    context = browser.new_context(
        viewport={"width": 1440, "height": 900}, device_scale_factor=2, locale="zh-CN"
    )
    page = context.new_page()
    page.set_default_timeout(30000)
    attach(page, "ai")
    open_settings(page, AI_UI)
    card = page.locator('[data-card="mcp"]')

    # ---------- AI 推荐 builtin ----------
    card.get_by_role("button", name="添加工具").click()
    dialog = page.get_by_role("dialog", name="添加 MCP 工具")
    expect(dialog).to_be_visible(timeout=10000)
    expect(dialog.get_by_role("tab", name=re.compile("AI 帮我配"))).to_be_visible(timeout=5000)
    dialog.get_by_label("AI 需求描述").fill("我需要读取本地文件")
    dialog.get_by_role("button", name="让 AI 推荐工具").click()
    expect(dialog.locator(".mcp-ai-card").first).to_be_visible(timeout=60000)
    ok("真实 AI 建议卡片返回", dialog.locator(".mcp-ai-card").count() >= 1)
    ok("真实建议含内置类型", dialog.get_by_text("内置").count() >= 1)
    ok("真实建议含 rationale", dialog.locator(".mcp-ai-rationale").count() >= 1)
    ok("真实建议来源说明（notes）", "未执行" in dialog.inner_text() or "仅用于预填" in dialog.inner_text())
    shot(page, "real-ai-01-suggestions")

    filesystem_card = dialog.locator(".mcp-ai-card").filter(has_text="文件系统").first
    expect(filesystem_card).to_be_visible(timeout=5000)
    filesystem_card.get_by_role("button", name=re.compile("使用这个配置")).click()
    expect(dialog).to_be_hidden(timeout=60000)
    rows = card.locator(".mcp-server")
    ok("内置建议不创建重复 server", rows.count() == 3, f"rows={rows.count()}")
    fs_row = card.locator('[data-server="filesystem"]')
    expect(fs_row.get_by_text("已连接")).to_be_visible(timeout=60000)
    ok("真实内置建议启用现有 filesystem preset", fs_row.get_by_text("已连接").is_visible())
    shot(page, "real-ai-02-builtin-enabled")
    fs_row.get_by_role("switch", name="启用 文件系统").click()
    expect(fs_row.get_by_text("未启用")).to_be_visible(timeout=30000)
    ok("验收后恢复内置 preset 为未启用", fs_row.get_by_text("未启用").is_visible())

    # ---------- AI 推荐 custom：仅预填，不创建/不执行 ----------
    card.get_by_role("button", name="添加工具").click()
    dialog = page.get_by_role("dialog", name="添加 MCP 工具")
    expect(dialog).to_be_visible(timeout=10000)
    dialog.get_by_label("AI 需求描述").fill("我需要网页抓取")
    dialog.get_by_role("button", name="让 AI 推荐工具").click()
    custom_card = dialog.locator(".mcp-ai-card").filter(has_text="网页抓取")
    expect(custom_card).to_be_visible(timeout=60000)
    ok("真实 AI 返回自定义建议", custom_card.locator(".mcp-ai-kind").count() == 1)
    ok("真实自定义建议含命令预览", "uvx mcp-server-fetch" in custom_card.inner_text())
    shot(page, "real-ai-03-custom-suggestion")
    custom_card.get_by_role("button", name=re.compile("使用这个配置")).click()
    expect(dialog.get_by_role("tab", name=re.compile("手动添加"))).to_have_attribute(
        "aria-selected", "true", timeout=10000
    )
    ok("真实自定义建议预填名称", dialog.get_by_label("MCP 显示名称").input_value() == "网页抓取")
    ok("真实自定义建议预填命令", dialog.get_by_label("MCP 启动命令").input_value() == "uvx")
    ok(
        "真实自定义建议预填参数",
        "mcp-server-fetch" in dialog.get_by_label("MCP 启动参数").input_value(),
    )
    ok("预填提示可见", "已套用 AI 推荐配置" in dialog.inner_text())
    shot(page, "real-ai-04-custom-prefill")
    dialog.get_by_role("button", name="取消").click()
    expect(dialog).to_be_hidden(timeout=10000)
    page.wait_for_timeout(500)
    ok("取消未创建新 server（按约定不执行 uvx）", card.locator(".mcp-server").count() == 3)
    context.close()


def main() -> int:
    log("MCP 添加工具 / AI 帮我配 · 真实联调验收")
    for label, base in (("手动自定义 7816", MANUAL_API), ("AI 建议 7815", AI_API)):
        try:
            health = api(base, "/api/health")
            mcp = api(base, "/api/mcp/status")
            log(f"  ✓ {label}：health={health.get('status')} mcp={mcp.get('enabled_count')}/{mcp.get('tool_count')}")
        except Exception as exc:  # noqa: BLE001
            log(f"  ✗ {label} 不可用：{exc}")
            return 1
    if not Path(VENV_PYTHON).exists():
        log(f"  ✗ venv python 不存在：{VENV_PYTHON}")
        return 1
    if not Path(FS_ROOT).is_dir():
        log(f"  ✗ 临时 filesystem root 不存在：{FS_ROOT}")
        return 1

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        verify_manual_custom(browser)
        verify_ai_suggest(browser)
        browser.close()

    ok("真实联调 0 console error", not CONSOLE_ERRORS, " | ".join(CONSOLE_ERRORS[:5]))
    ok("真实联调 0 page error", not PAGE_ERRORS, " | ".join(PAGE_ERRORS[:5]))

    log("\n============ MCP 添加工具真实联调汇总 ============")
    log(f"PASS: {len(PASS)}  FAIL: {len(FAIL)}")
    for item in FAIL:
        log(f"  FAIL -> {item}")
    log(f"截图目录：{SHOTS}")
    (SHOTS / "verify-real.log").write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
