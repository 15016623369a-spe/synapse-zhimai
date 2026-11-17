#!/usr/bin/env python3
"""Synapse V2 前端验收（mock API + Vite dev server）。

覆盖 SPEC-V2-UI.md 第 7 节：
  1. MCP 卡：渲染 / 开关 / 测试连接 / GitHub token 脱敏 / 展开工具 chips
  2. 语义向量卡：状态 / 切换 provider / 保存 / 测试 / 重建（二次确认 + loading + 结果）/ OCR 状态
  3. 反馈偏好卡：计数 / top domains / 说明 / 刷新
  4. 知识巡检卡：配置 / 周期 / 自动整理 / 立即巡检 loading + 结果 / issues 计数
  5. 对话 MCP 工具调用卡片（含失败警告），并验证 search_web 旧展示不回归
  6. 1024px 无横向溢出；现有设置/对话/图谱无回归；0 console error
  7. 截图输出到 frontend/.screenshots-v2/

运行（mock server + vite 5174 已启动）：
  PLAYWRIGHT_BROWSERS_PATH=/Users/jace/Documents/deepseek-harness/default-workspace/.playwright \
    /Users/jace/Documents/deepseek-harness/default-workspace/.venv-tools/bin/python \
    frontend/scripts/verify_v2.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

BASE = os.environ.get("VERIFY_BASE", "http://127.0.0.1:5174").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".screenshots-v2"
SHOTS.mkdir(parents=True, exist_ok=True)
MCP_ADD_SHOTS = SHOTS / "mcp-add"
MCP_ADD_SHOTS.mkdir(parents=True, exist_ok=True)

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


def attach(page: Page, label: str) -> None:
    page.on(
        "console",
        lambda msg: CONSOLE_ERRORS.append(f"[{label}] {msg.text}")
        if msg.type == "error"
        else None,
    )
    page.on("pageerror", lambda exc: PAGE_ERRORS.append(f"[{label}] {exc}"))
    page.on(
        "response",
        lambda resp: HTTP_ERRORS.append(f"[{label}] {resp.status} {resp.url}")
        if resp.status >= 400
        else None,
    )


def wait_no_toast(page: Page) -> None:
    """等待右上角 toast 自动消失，避免遮挡卡片截图。"""
    try:
        page.wait_for_function("() => document.querySelectorAll('.toast').length === 0", timeout=6000)
    except Exception:  # noqa: BLE001 - 截图优先，不强求
        pass
    page.wait_for_timeout(150)


def shot(page: Page, name: str, locator=None) -> None:
    wait_no_toast(page)
    target = SHOTS / f"{name}.png"
    if locator is not None:
        locator.screenshot(path=str(target))
    else:
        page.screenshot(path=str(target), full_page=False)
    print(f"  📸 {name}.png")


def reset_mock() -> None:
    request = urllib.request.Request(f"{BASE}/api/mock/reset", method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert payload.get("ok") is True


def wait_settings_ready(page: Page) -> None:
    page.goto(f"{BASE}/#/settings", wait_until="domcontentloaded")
    expect(page.locator(".settings-page")).to_be_visible(timeout=20000)
    expect(page.get_by_role("heading", name="模型接入")).to_be_visible(timeout=20000)
    # 4 张 V2 卡数据加载完成（mock 即时返回，真实后端可能略慢）
    expect(page.locator('[data-card="mcp"]').get_by_text("文件系统", exact=True)).to_be_visible(timeout=20000)
    expect(page.locator('[data-card="embedding"]').get_by_text("已建向量分块")).to_be_visible(timeout=20000)
    expect(page.locator('[data-card="reranker"]').get_by_text("arxiv.org", exact=True)).to_be_visible(timeout=20000)
    expect(page.locator('[data-card="patrol"]').get_by_text("巡检报告", exact=True)).to_be_visible(timeout=20000)
    page.wait_for_timeout(500)


def verify_mcp(page: Page) -> None:
    log("\n[MCP 工具卡]")
    card = page.locator('[data-card="mcp"]')
    expect(card).to_be_visible()

    for label in ("文件系统", "知识库数据库", "GitHub"):
        ok(f"MCP 渲染 {label} server", card.get_by_text(label, exact=True).first.is_visible())

    # 状态展示：文件系统已连接 / 数据库未启用 / GitHub 错误
    ok("MCP 显示已连接状态", card.get_by_text("已连接").first.is_visible())
    ok("MCP 显示未启用状态", card.get_by_text("未启用").first.is_visible())
    ok("MCP 显示错误状态", card.get_by_text("错误").first.is_visible())

    # 展开文件系统 → 工具 chips + 测试连接
    card.get_by_role("button", name=re.compile("展开 文件系统 工具")).click()
    page.wait_for_timeout(250)
    ok("MCP 展开后显示工具 chips", card.locator(".mcp-tool-chip").count() >= 4, f"chips={card.locator('.mcp-tool-chip').count()}")
    ok("MCP 显示工具名 list_directory", card.get_by_text("list_directory").first.is_visible())
    card.get_by_role("button", name="测试连接").first.click()
    expect(card.get_by_text(re.compile("连接成功 · 4 个工具"))).to_be_visible(timeout=8000)
    ok("MCP 测试连接成功提示", card.get_by_text(re.compile("连接成功 · 4 个工具")).is_visible())

    # GitHub 展开 → token 脱敏展示（已保存，不显示明文）
    card.get_by_role("button", name=re.compile("展开 GitHub 工具")).click()
    page.wait_for_timeout(250)
    expect(card.get_by_text("已保存（脱敏）")).to_be_visible(timeout=5000)
    ok("GitHub token 脱敏显示「已保存」", card.get_by_text("已保存（脱敏）").is_visible())
    ok("GitHub 错误行可见", card.locator(".mcp-error-line").count() >= 1)
    ok("页面不包含任何 token 明文", "ghp_" not in page.content())

    # 更新 token：输入框为 password 类型，保存后页面无明文、恢复「已保存」
    card.get_by_role("button", name="更新", exact=True).click()
    token_input = card.get_by_label("GitHub Token")
    expect(token_input).to_be_visible(timeout=5000)
    ok("GitHub token 输入框为密码类型", token_input.get_attribute("type") == "password")
    secret = "ghp_V2VERIFY_SECRET_TOKEN_1234567890"
    token_input.fill(secret)
    card.locator(".mcp-token-form").get_by_role("button", name="保存").click()
    expect(card.get_by_text("已保存（脱敏）")).to_be_visible(timeout=8000)
    page.wait_for_timeout(400)
    ok("保存 token 后不返回明文", secret not in page.content() and "已保存（脱敏）" in card.inner_text())
    ok("保存 token 后输入框收起", card.get_by_label("GitHub Token").count() == 0)

    # 开关：启用知识库数据库 → 已连接；再关闭 → 未启用
    sqlite_row = card.locator('[data-server="sqlite"]')
    sqlite_row.get_by_role("switch", name="启用 知识库数据库").click()
    expect(sqlite_row.get_by_text("已连接")).to_be_visible(timeout=8000)
    ok("MCP 开关启用后显示已连接", sqlite_row.get_by_text("已连接").is_visible())
    page.wait_for_timeout(300)
    ok("MCP 顶部徽标统计已启用数量", "3 已启用" in card.inner_text(), card.inner_text()[:80].replace("\n", " "))
    sqlite_row.get_by_role("switch", name="启用 知识库数据库").click()
    expect(sqlite_row.get_by_text("未启用")).to_be_visible(timeout=8000)
    ok("MCP 开关停用后恢复未启用", sqlite_row.get_by_text("未启用").is_visible())

    card.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    shot(page, "01-mcp-card", card)


def verify_mcp_add(page: Page) -> None:
    log("\n[MCP 添加工具向导]")
    card = page.locator('[data-card="mcp"]')
    dialog = page.get_by_role("dialog", name="添加 MCP 工具")

    # ---------- 表单校验 ----------
    card.get_by_role("button", name="添加工具").click()
    expect(dialog).to_be_visible(timeout=10000)
    dialog.get_by_role("tab", name=re.compile("手动添加")).click()
    dialog.get_by_role("button", name="创建并测试").click()
    expect(dialog.get_by_text("请填写显示名称")).to_be_visible(timeout=5000)
    ok("添加工具：空表单校验", dialog.get_by_text("请填写显示名称").is_visible())
    dialog.get_by_label("MCP 显示名称").fill("表单校验工具")
    dialog.get_by_role("button", name="创建并测试").click()
    expect(dialog.get_by_text("请填写启动命令")).to_be_visible(timeout=5000)
    ok("添加工具：命令必填校验", dialog.get_by_text("请填写启动命令").is_visible())

    # ---------- 创建 + 自动测试发现工具 ----------
    dialog.get_by_label("MCP 显示名称").fill("团队报告工具")
    dialog.get_by_label("MCP 启动命令").fill("python")
    dialog.get_by_label("MCP 启动参数").fill("scripts/report_server.py\n--mode=readonly")
    dialog.get_by_label("MCP 环境变量").fill("REPORT_TOKEN=demo-token")
    page.wait_for_timeout(200)
    page.screenshot(path=str(MCP_ADD_SHOTS / "01-form.png"))
    print("  📸 mcp-add/01-form.png")
    dialog.get_by_role("button", name="创建并测试").click()
    expect(dialog.get_by_text(re.compile("已发现 3 个工具"))).to_be_visible(timeout=15000)
    ok("添加工具：创建后自动测试发现工具", dialog.get_by_text(re.compile("已发现 3 个工具")).is_visible())
    ok("添加工具：工具默认全不勾选", dialog.locator("input[type=checkbox]:checked").count() == 0)
    ok("添加工具：无选择时不能启用", dialog.get_by_role("button", name=re.compile("保存并启用")).is_disabled())
    ok("添加工具：写操作工具有警告标记", dialog.get_by_text("可能写数据").count() >= 1)
    page.screenshot(path=str(MCP_ADD_SHOTS / "02-tools.png"))
    print("  📸 mcp-add/02-tools.png")

    # ---------- 勾选信任的只读工具并保存启用 ----------
    dialog.locator(".mcp-wizard-tool").filter(has_text="list_reports").click()
    dialog.locator(".mcp-wizard-tool").filter(has_text="read_report").click()
    ok("添加工具：未勾选写操作工具", "write_report" in dialog.inner_text() and "已选 2 / 3" in dialog.inner_text())
    enable_btn = dialog.get_by_role("button", name=re.compile("保存并启用"))
    expect(enable_btn).to_be_enabled(timeout=5000)
    enable_btn.click()
    expect(dialog).to_be_hidden(timeout=15000)

    custom_row = card.locator(".mcp-server").filter(has_text="团队报告工具")
    expect(custom_row).to_be_visible(timeout=15000)
    ok("自定义 Server 行显示名称", custom_row.get_by_text("团队报告工具").first.is_visible())
    ok("自定义 Server 行显示「自定义 · stdio」", "自定义 · stdio" in custom_row.inner_text())
    ok("自定义 Server 行显示命令摘要", "scripts/report_server.py" in custom_row.inner_text())
    ok("自定义 Server 行显示工具数", "3 工具" in custom_row.inner_text())
    expect(custom_row.get_by_text("已连接")).to_be_visible(timeout=15000)
    ok("保存并启用后处于已连接状态", custom_row.get_by_text("已连接").is_visible())

    # ---------- 展开自定义行：允许工具编辑 + 删除按钮（保存后自动展开） ----------
    expect(custom_row.locator(".mcp-allowed")).to_be_visible(timeout=10000)
    ok("展开后显示允许工具编辑器", custom_row.locator(".mcp-allowed").count() == 1)
    ok("允许工具勾选状态已保存", "2/3" in custom_row.locator(".mcp-allowed").inner_text())
    ok("自定义行有删除按钮", custom_row.locator(".mcp-delete-btn").count() == 1)
    page.screenshot(path=str(MCP_ADD_SHOTS / "03-custom-row.png"))
    print("  📸 mcp-add/03-custom-row.png")

    # ---------- 自定义开关（已保存 allowed_tools 可正常启停） ----------
    custom_switch = custom_row.get_by_role("switch", name="启用 团队报告工具")
    custom_switch.click()
    expect(custom_row.get_by_text("未启用")).to_be_visible(timeout=10000)
    ok("自定义 Server 可停用", custom_row.get_by_text("未启用").is_visible())
    custom_switch.click()
    expect(custom_row.get_by_text("已连接")).to_be_visible(timeout=15000)
    ok("自定义 Server 可重新启用", custom_row.get_by_text("已连接").is_visible())

    # ---------- 测试失败：中文错误 + 取消自动清理草稿 ----------
    rows_before = card.locator(".mcp-server").count()
    card.get_by_role("button", name="添加工具").click()
    expect(dialog).to_be_visible(timeout=10000)
    dialog.get_by_role("tab", name=re.compile("手动添加")).click()
    dialog.get_by_label("MCP 显示名称").fill("不可用工具")
    dialog.get_by_label("MCP 启动命令").fill("invalid-mcp-command")
    dialog.get_by_role("button", name="创建并测试").click()
    expect(dialog.get_by_text("连接测试失败")).to_be_visible(timeout=15000)
    ok("添加工具：测试失败显示中文错误", "无法启动 MCP Server" in dialog.inner_text())
    ok("添加工具：失败态提供返回修改/重新测试", dialog.get_by_role("button", name="返回修改").is_visible())
    page.screenshot(path=str(MCP_ADD_SHOTS / "04-test-failed.png"))
    print("  📸 mcp-add/04-test-failed.png")
    dialog.get_by_role("button", name="取消").click()
    expect(dialog).to_be_hidden(timeout=10000)
    page.wait_for_timeout(600)
    ok("失败取消后草稿已清理", card.locator(".mcp-server").count() == rows_before, f"rows={card.locator('.mcp-server').count()}")

    # ---------- 仅保存（不启用）+ 未勾选不能启用 + 删除 ----------
    card.get_by_role("button", name="添加工具").click()
    expect(dialog).to_be_visible(timeout=10000)
    dialog.get_by_role("tab", name=re.compile("手动添加")).click()
    dialog.get_by_label("MCP 显示名称").fill("仅保存工具")
    dialog.get_by_label("MCP 启动命令").fill("python")
    dialog.get_by_role("button", name="创建并测试").click()
    expect(dialog.get_by_text(re.compile("已发现 3 个工具"))).to_be_visible(timeout=15000)
    ok("未勾选工具时「保存并启用」禁用", dialog.get_by_role("button", name=re.compile("保存并启用")).is_disabled())
    dialog.get_by_role("button", name="仅保存").click()
    expect(dialog).to_be_hidden(timeout=10000)
    saved_row = card.locator(".mcp-server").filter(has_text="仅保存工具")
    expect(saved_row).to_be_visible(timeout=15000)
    ok("仅保存后行存在且未启用", "未启用" in saved_row.inner_text())
    expect(saved_row.locator(".mcp-allowed")).to_be_visible(timeout=10000)
    ok("未勾选时不能启用提示", "未勾选" in saved_row.inner_text())
    saved_row.get_by_role("switch", name="启用 仅保存工具").click()
    page.wait_for_timeout(400)
    ok("未勾选工具启用被拦截（仍未启用）", "未启用" in saved_row.inner_text())
    ok("拦截时显示 toast 提示", page.locator(".toast").count() >= 1)

    # 删除自定义行
    saved_row.locator(".mcp-delete-btn").click()
    confirm = page.get_by_role("dialog", name="删除自定义 MCP Server？")
    expect(confirm).to_be_visible(timeout=8000)
    confirm.get_by_role("button", name="删除工具").click()
    expect(saved_row).to_have_count(0, timeout=15000)
    ok("删除自定义 Server 成功", saved_row.count() == 0)

    # 清理第一个自定义 Server，恢复内置三件套
    team_row = card.locator(".mcp-server").filter(has_text="团队报告工具")
    expect(team_row.locator(".mcp-allowed")).to_be_visible(timeout=10000)
    team_row.locator(".mcp-delete-btn").click()
    confirm2 = page.get_by_role("dialog", name="删除自定义 MCP Server？")
    expect(confirm2).to_be_visible(timeout=8000)
    confirm2.get_by_role("button", name="删除工具").click()
    expect(card.locator(".mcp-server")).to_have_count(3, timeout=15000)
    ok("删除后列表恢复内置三件套", card.locator(".mcp-server").count() == 3)
    ok("内置 Server 无删除按钮", card.locator('[data-server="filesystem"] .mcp-delete-btn').count() == 0)


def verify_mcp_ai(page: Page) -> None:
    log("\n[MCP「AI 帮我配」]")
    card = page.locator('[data-card="mcp"]')
    dialog = page.get_by_role("dialog", name="添加 MCP 工具")

    # ---------- 默认 AI tab + 空输入校验 ----------
    card.get_by_role("button", name="添加工具").click()
    expect(dialog).to_be_visible(timeout=10000)
    expect(dialog.get_by_role("tab", name=re.compile("AI 帮我配"))).to_be_visible(timeout=5000)
    ok("AI 入口默认展示", dialog.get_by_label("AI 需求描述").is_visible())
    dialog.get_by_role("button", name="让 AI 推荐工具").click()
    expect(dialog.get_by_text("请先描述你的需求")).to_be_visible(timeout=5000)
    ok("AI 空需求校验", dialog.get_by_text("请先描述你的需求").is_visible())

    # ---------- 503（未接入模型）：中文提示 + 切手动入口 ----------
    dialog.get_by_label("AI 需求描述").fill("我没有接入模型")
    dialog.get_by_role("button", name="让 AI 推荐工具").click()
    expect(dialog.get_by_text(re.compile("请先在设置接入模型"))).to_be_visible(timeout=10000)
    ok("AI 未接入模型 503 中文提示", "请先在设置接入模型" in dialog.inner_text())
    ok("AI 失败保留切手动入口", dialog.get_by_role("button", name="切换到手动添加").is_visible())
    page.screenshot(path=str(MCP_ADD_SHOTS / "06-ai-error.png"))
    print("  📸 mcp-add/06-ai-error.png")
    dialog.get_by_role("button", name="切换到手动添加").click()
    expect(dialog.get_by_role("tab", name=re.compile("手动添加"))).to_have_attribute(
        "aria-selected", "true", timeout=5000
    )
    ok("AI 失败可切回手动表单", dialog.get_by_label("MCP 显示名称").is_visible())
    dialog.get_by_role("button", name="取消").click()
    expect(dialog).to_be_hidden(timeout=8000)

    # ---------- 502（LLM 失败） ----------
    card.get_by_role("button", name="添加工具").click()
    expect(dialog).to_be_visible(timeout=10000)
    dialog.get_by_label("AI 需求描述").fill("AI失败测试")
    dialog.get_by_role("button", name="让 AI 推荐工具").click()
    expect(dialog.get_by_text(re.compile("AI 推荐失败，请稍后重试"))).to_be_visible(timeout=10000)
    ok("AI LLM 失败 502 中文提示", "AI 推荐失败" in dialog.inner_text())
    dialog.get_by_role("button", name="取消").click()
    expect(dialog).to_be_hidden(timeout=8000)

    # ---------- 正常推荐：内置建议 → 直接启用现有 server ----------
    card.get_by_role("button", name="添加工具").click()
    expect(dialog).to_be_visible(timeout=10000)
    dialog.get_by_label("AI 需求描述").fill("我想查询我的 SQLite 数据库")
    dialog.get_by_role("button", name="让 AI 推荐工具").click()
    expect(dialog.get_by_text(re.compile("AI 推荐 2 个配置"))).to_be_visible(timeout=15000)
    ok("AI 建议卡片数量", dialog.locator(".mcp-ai-card").count() == 2)
    ok("AI 建议卡显示内置/自定义类型", dialog.locator(".mcp-ai-kind").count() >= 2)
    ok("AI 建议卡显示 rationale/test_note", dialog.locator(".mcp-ai-rationale").count() >= 1)
    ok("AI 安全说明文案", "不会自动执行或安装" in dialog.inner_text())
    page.screenshot(path=str(MCP_ADD_SHOTS / "07-ai-suggestions.png"))
    print("  📸 mcp-add/07-ai-suggestions.png")

    sqlite_card = dialog.locator(".mcp-ai-card").filter(has_text="知识库数据库")
    expect(sqlite_card).to_be_visible(timeout=5000)
    sqlite_card.get_by_role("button", name=re.compile("使用这个配置")).click()
    expect(dialog).to_be_hidden(timeout=20000)
    sqlite_row = card.locator('[data-server="sqlite"]')
    expect(sqlite_row.get_by_text("已连接")).to_be_visible(timeout=20000)
    ok("AI 内置建议启用现有 server（未创建重复）", sqlite_row.get_by_text("已连接").is_visible())
    ok("内置建议不新增 server 行", card.locator(".mcp-server").count() == 3)
    page.screenshot(path=str(MCP_ADD_SHOTS / "08-ai-builtin-enabled.png"))
    print("  📸 mcp-add/08-ai-builtin-enabled.png")
    sqlite_row.get_by_role("switch", name="启用 知识库数据库").click()
    expect(sqlite_row.get_by_text("未启用")).to_be_visible(timeout=15000)

    # ---------- 正常推荐：自定义建议 → 预填表单 → 测试 → 勾选 → 启用 → 删除 ----------
    card.get_by_role("button", name="添加工具").click()
    expect(dialog).to_be_visible(timeout=10000)
    dialog.get_by_label("AI 需求描述").fill("我想抓取网页内容并整理")
    dialog.get_by_role("button", name="让 AI 推荐工具").click()
    fetch_card = dialog.locator(".mcp-ai-card").filter(has_text="网页抓取工具")
    expect(fetch_card).to_be_visible(timeout=15000)
    ok("AI 推荐自定义配置含命令预览", "uvx mcp-server-fetch" in fetch_card.inner_text())
    page.screenshot(path=str(MCP_ADD_SHOTS / "09-ai-suggestions-custom.png"))
    print("  📸 mcp-add/09-ai-suggestions-custom.png")
    fetch_card.get_by_role("button", name=re.compile("使用这个配置")).click()
    expect(dialog.get_by_role("tab", name=re.compile("手动添加"))).to_have_attribute(
        "aria-selected", "true", timeout=5000
    )
    ok("AI 自定义建议预填名称", dialog.get_by_label("MCP 显示名称").input_value() == "网页抓取工具")
    ok("AI 自定义建议预填命令", dialog.get_by_label("MCP 启动命令").input_value() == "uvx")
    ok("AI 自定义建议预填参数", "mcp-server-fetch" in dialog.get_by_label("MCP 启动参数").input_value())
    ok("AI 预填提示", "已套用 AI 推荐配置" in dialog.inner_text())
    page.screenshot(path=str(MCP_ADD_SHOTS / "10-ai-custom-prefill.png"))
    print("  📸 mcp-add/10-ai-custom-prefill.png")
    dialog.get_by_role("button", name="创建并测试").click()
    expect(dialog.get_by_text(re.compile("已发现 3 个工具"))).to_be_visible(timeout=15000)
    dialog.get_by_role("button", name=re.compile("全选只读工具")).click()
    dialog.get_by_role("button", name=re.compile("保存并启用")).click()
    expect(dialog).to_be_hidden(timeout=15000)
    ai_row = card.locator(".mcp-server").filter(has_text="网页抓取工具")
    expect(ai_row).to_be_visible(timeout=15000)
    expect(ai_row.get_by_text("已连接")).to_be_visible(timeout=15000)
    expect(ai_row.locator(".mcp-allowed")).to_be_visible(timeout=10000)
    ok("AI 自定义配置走完测试/勾选/启用流程", ai_row.get_by_text("已连接").is_visible())
    page.screenshot(path=str(MCP_ADD_SHOTS / "11-ai-custom-enabled.png"))
    print("  📸 mcp-add/11-ai-custom-enabled.png")
    ai_row.locator(".mcp-delete-btn").click()
    confirm = page.get_by_role("dialog", name="删除自定义 MCP Server？")
    expect(confirm).to_be_visible(timeout=8000)
    confirm.get_by_role("button", name="删除工具").click()
    expect(card.locator(".mcp-server")).to_have_count(3, timeout=15000)
    ok("AI 推荐的自定义工具可删除", card.locator(".mcp-server").count() == 3)


def verify_embedding(page: Page) -> None:
    log("\n[语义向量卡]")
    card = page.locator('[data-card="embedding"]')
    expect(card).to_be_visible()

    ok("向量卡显示当前 provider", card.get_by_text("本地哈希").first.is_visible())
    ok("向量卡显示已建向量分块", card.get_by_text("1,538").first.is_visible())
    ok("向量卡显示 needs_rebuild 黄色提示", card.get_by_text(re.compile("当前配置与已建向量不一致")).is_visible())
    ok("向量卡显示 OCR 状态行", card.get_by_text(re.compile("RapidOCR 已就绪")).is_visible())

    # 切换到本地语义模型 → 模型下拉 → 保存
    card.get_by_role("radio", name=re.compile("本地语义模型")).click()
    page.wait_for_timeout(200)
    model_select = card.get_by_label("fastembed 语义模型")
    ok("fastembed 模型下拉出现", model_select.is_visible())
    options = model_select.locator("option").all_inner_texts()
    ok("fastembed 模型选项含 bge-small-zh", any("bge-small-zh" in text for text in options), str(options))
    card.get_by_role("button", name="保存配置").click()
    expect(card.locator(".v2-stat-value").first).to_have_text(re.compile("本地语义模型"), timeout=8000)
    ok("保存 provider 配置后状态刷新", "本地语义模型" in card.inner_text())
    ok("保存后仍提示需重建", "待重建" in card.inner_text())

    # 截图：needs_rebuild 提示 + fastembed 配置态
    card.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    shot(page, "02-embedding-card", card)

    # 重建全部向量：二次确认 → loading → 结果
    card.get_by_role("button", name=re.compile("重建全部向量")).click()
    dialog = page.get_by_role("dialog", name="重建全部向量？")
    expect(dialog).to_be_visible(timeout=5000)
    ok("重建向量有二次确认弹窗", dialog.is_visible())
    dialog.get_by_role("button", name="开始重建").click()
    expect(card.get_by_text("正在重建全部向量…")).to_be_visible(timeout=5000)
    ok("重建向量 loading 提示", card.get_by_text("正在重建全部向量…").is_visible())
    page.wait_for_timeout(300)
    shot(page, "02b-embedding-rebuilding", card)
    expect(card.get_by_text("全部向量重建完成")).to_be_visible(timeout=15000)
    ok("重建向量结果卡片", card.get_by_text("全部向量重建完成").is_visible())
    ok("重建结果包含 chunks", "1,538" in card.inner_text())
    ok("重建后状态变为已就绪", "已就绪" in card.inner_text())
    shot(page, "02c-embedding-result", card)

    # 测试连接
    card.get_by_role("button", name="测试连接").click()
    expect(card.get_by_text("向量测试通过")).to_be_visible(timeout=8000)
    ok("向量测试连接成功提示", card.get_by_text("向量测试通过").is_visible())

    # API 模式字段
    card.get_by_role("radio", name=re.compile("API（OpenAI 兼容）")).click()
    page.wait_for_timeout(200)
    ok("API 模式显示 Base URL", card.get_by_placeholder("https://api.openai.com/v1").is_visible())
    ok("API 模式显示模型名", card.get_by_placeholder("text-embedding-3-small").is_visible())
    ok("API 模式显示 API Key", card.get_by_label("显示 API Key").is_visible())
    card.get_by_role("radio", name=re.compile("本地语义模型")).click()


def verify_reranker(page: Page) -> None:
    log("\n[反馈偏好 Reranker 卡]")
    card = page.locator('[data-card="reranker"]')
    expect(card).to_be_visible()
    ok("Reranker 显示已采纳计数", card.get_by_text("已采纳").is_visible() and "12" in card.inner_text())
    ok("Reranker 显示已忽略计数", card.get_by_text("已忽略").is_visible() and "8" in card.inner_text())
    ok("Reranker 显示 top domain", card.get_by_text("arxiv.org").first.is_visible())
    ok("Reranker 显示偏好百分比", card.get_by_text("80%").first.is_visible())
    ok("Reranker 显示规则化偏好说明", "规则化偏好" in card.inner_text())
    card.get_by_role("button", name="刷新反馈偏好").click()
    page.wait_for_timeout(500)
    ok("Reranker 刷新后仍显示数据", card.get_by_text("arxiv.org").first.is_visible())
    card.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    shot(page, "03-reranker-card", card)


def verify_patrol(page: Page) -> None:
    log("\n[知识巡检卡]")
    card = page.locator('[data-card="patrol"]')
    expect(card).to_be_visible()
    ok("巡检卡显示启用状态", "定时开启" in card.inner_text())
    ok("巡检卡显示周期下拉", card.get_by_label("巡检周期").is_visible())
    ok("巡检卡显示自动整理开关", card.get_by_label("自动整理待整理文档").is_visible())
    ok("巡检卡显示上次/下次巡检", "上次巡检" in card.inner_text() and "下次巡检" in card.inner_text())

    # 修改周期 12 小时
    card.get_by_label("巡检周期").select_option("12")
    expect(card.get_by_label("巡检周期")).to_have_value("12", timeout=8000)
    page.wait_for_timeout(300)
    ok("巡检周期可修改并保存", card.get_by_label("巡检周期").input_value() == "12")

    # 自动整理开关关闭再打开
    auto_switch = card.get_by_role("switch", name="自动整理待整理文档")
    auto_switch.click()
    expect(card.get_by_text(re.compile("只生成巡检报告，不自动整理文档"))).to_be_visible(timeout=8000)
    ok("自动整理开关可关闭", "只生成巡检报告" in card.inner_text())
    auto_switch.click()
    expect(card.get_by_text(re.compile("每次最多整理"))).to_be_visible(timeout=8000)
    ok("自动整理开关可重新开启", "每次最多整理" in card.inner_text())

    # 立即巡检：loading → 结果
    card.get_by_role("button", name="立即巡检").click()
    expect(card.get_by_role("button", name=re.compile("巡检中…"))).to_be_visible(timeout=4000)
    ok("立即巡检 loading 状态", card.get_by_role("button", name=re.compile("巡检中…")).is_visible())
    ok("立即巡检显示运行提示", card.locator(".patrol-running").count() == 1)
    page.wait_for_timeout(200)
    shot(page, "04b-patrol-loading", card)
    expect(card.get_by_role("button", name="立即巡检")).to_be_visible(timeout=20000)
    page.wait_for_timeout(700)
    ok("巡检结果摘要", "巡检完成" in card.inner_text())
    ok("巡检结果为本轮新报告（耗时刷新）", "12.3s" in card.inner_text(), card.inner_text()[:200].replace("\n", " "))
    ok("巡检结果自动整理数量", "自动整理 2 篇" in card.inner_text(), card.inner_text()[:160].replace("\n", " "))
    ok("巡检结果失败计数", "失败 1 篇" in card.inner_text())
    ok("巡检 enriched 标题 chips", card.locator(".patrol-enriched-chip").count() >= 2)
    ok("巡检 issues 分类计数 chips", card.locator(".patrol-issue-chip").count() == 6)
    ok("巡检结果图谱重建标记", "图谱已重建" in card.inner_text())
    ok("巡检问题示例最多 3 条", card.locator(".patrol-sample").count() <= 3)
    card.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    shot(page, "04-patrol-card", card)


def verify_chat(page: Page) -> None:
    log("\n[对话 MCP 工具调用]")
    page.goto(f"{BASE}/#/chat", wait_until="domcontentloaded")
    expect(page.get_by_label("提问输入框")).to_be_visible(timeout=20000)
    page.wait_for_timeout(800)

    page.get_by_label("提问输入框").fill("列出数据目录下的 Markdown 文件")
    page.get_by_label("发送消息").click()
    expect(page.locator(".tool-call.is-ok")).to_be_visible(timeout=20000)
    expect(page.locator(".tool-call.is-error")).to_be_visible(timeout=20000)
    page.wait_for_timeout(2500)

    ok("MCP 工具卡数量（成功 + 失败）", page.locator(".tool-call").count() == 2)
    ok("MCP 工具卡显示 server/tool 名称", "文件系统" in page.locator(".tool-call.is-ok").inner_text() and "list_directory" in page.locator(".tool-call.is-ok").inner_text())
    ok("MCP 工具卡显示 MCP 徽标", page.locator(".tool-call-badge").first.inner_text() == "MCP")
    ok("MCP 成功摘要", "读取到 3 个 Markdown 文件" in page.locator(".tool-call.is-ok").inner_text())
    ok("MCP 失败红色警告", "GitHub" in page.locator(".tool-call.is-error").inner_text() and "rate limit" in page.locator(".tool-call.is-error").inner_text())
    ok("MCP 参数展示", "query: synapse knowledge graph" in page.locator(".tool-call.is-error").inner_text())
    ok("最终回答包含工具结果", "3 个 Markdown 文件" in page.locator(".assistant-card").last.inner_text())
    page.screenshot(path=str(SHOTS / "05-chat-mcp-tool.png"))
    print("  📸 05-chat-mcp-tool.png")
    ok("对话截图生成", (SHOTS / "05-chat-mcp-tool.png").exists())

    # 兼容旧 search_web 展示 + 联网扩展/引用 UI 不回归
    page.get_by_label("提问输入框").fill("帮我联网搜索补充资料")
    page.get_by_label("发送消息").click()
    expect(page.locator(".web-sources-card")).to_be_visible(timeout=25000)
    page.wait_for_timeout(2500)
    tool_text = page.locator(".tool-call").last.inner_text()
    ok("search_web 旧展示兼容", "联网搜索" in tool_text and "已找到 3 条外部结果" in tool_text, tool_text)
    ok("联网外部参考卡片不回归", page.locator(".web-sources-card").is_visible())
    ok("引用来源区域不回归", page.locator(".citations-block").count() >= 1)
    page.screenshot(path=str(SHOTS / "07-chat-search-web.png"))
    print("  📸 07-chat-search-web.png")


def verify_context_hint(page: Page) -> None:
    log("\n[多轮上下文「已结合上文」提示]")
    page.goto(f"{BASE}/#/chat", wait_until="domcontentloaded")
    expect(page.get_by_label("提问输入框")).to_be_visible(timeout=20000)
    page.wait_for_timeout(800)

    # 第一轮：显式总结论文（mock 返回引用文档、context_used=false）
    page.get_by_label("提问输入框").fill("请总结《Transformer-BP 多目标跟踪研究框架》的核心内容")
    page.get_by_label("发送消息").click()
    expect(page.locator(".msg-reference-chip")).to_be_visible(timeout=20000)
    expect(page.locator(".answer-progress")).to_have_count(0, timeout=30000)
    page.wait_for_timeout(1500)
    ok("第一轮不显示「已结合上文」", page.locator(".msg-context-chip").count() == 0)
    ok("第一轮显示引用文档 chip", page.locator(".msg-reference-chip").count() >= 1)

    # 第二轮追问：mock 返回 context_used + context_summary + 继承文档
    page.get_by_label("提问输入框").fill("创新点呢")
    page.get_by_label("发送消息").click()
    expect(page.locator(".msg-context-chip").first).to_be_visible(timeout=20000)
    expect(page.locator(".answer-progress")).to_have_count(0, timeout=30000)
    page.wait_for_timeout(1500)
    chip_text = page.locator(".msg-context-chip").last.inner_text()
    ok("追问显示「已结合上文」徽标", "已结合上文" in chip_text, chip_text)
    ok("徽标展示 context_summary", "Transformer-BP" in chip_text and "创新点" in chip_text, chip_text)
    ok("追问继承引用文档 chip", page.locator(".msg-reference-chip").count() >= 2)
    ok("追问不显示 gap 卡片", page.locator(".gap-card").count() == 0)
    ok("追问回答带引用来源", page.locator(".citations-block").count() >= 1)
    page.screenshot(path=str(SHOTS / "08-chat-context-hint.png"))
    print("  📸 08-chat-context-hint.png")

    # 扩展面板：mock /api/extend/search 返回 context_used → 顶部提示行
    page.locator(".assistant-card").last.get_by_label("联网搜索补充资料").click()
    expect(page.locator(".extend-panel")).to_be_visible(timeout=15000)
    expect(page.locator(".extend-context-hint")).to_be_visible(timeout=15000)
    hint_text = page.locator(".extend-context-hint").inner_text()
    ok("扩展面板显示已结合上文提示", "已结合上文" in hint_text and "Transformer-BP" in hint_text, hint_text)
    page.screenshot(path=str(SHOTS / "09-extend-context-hint.png"))
    print("  📸 09-extend-context-hint.png")
    page.get_by_label("关闭").click()
    page.wait_for_timeout(300)


def verify_regression(page: Page) -> None:
    log("\n[现有页面回归]")
    # 设置页现有模型卡
    page.goto(f"{BASE}/#/settings", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="模型接入")).to_be_visible(timeout=20000)
    ok("设置页模型接入卡正常", page.get_by_role("button", name="保存配置").first.is_visible())
    ok("设置页数据与存储卡正常", page.get_by_role("heading", name="数据与存储").is_visible())

    # 图谱页（空 mock 数据也应正常渲染）
    page.goto(f"{BASE}/#/graph", wait_until="domcontentloaded")
    expect(page.locator(".graph-view")).to_be_visible(timeout=25000)
    page.wait_for_timeout(1800)
    ok("神经图谱页正常渲染", page.locator(".graph-view").is_visible())
    ok("图谱页无致命错误", not PAGE_ERRORS, " | ".join(PAGE_ERRORS[:3]))


def verify_1024(browser) -> None:
    log("\n[1024px 响应式]")
    context = browser.new_context(
        viewport={"width": 1024, "height": 768}, device_scale_factor=2, locale="zh-CN"
    )
    page = context.new_page()
    attach(page, "1024")
    wait_settings_ready(page)

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
          return {
            docScroll: doc.scrollWidth,
            inner: window.innerWidth,
            pageScroll: scroll ? scroll.scrollWidth : 0,
            pageClient: scroll ? scroll.clientWidth : 0,
            cards,
          };
        }"""
    )
    ok(
        "1024 页面无横向溢出",
        metrics["docScroll"] <= metrics["inner"] + 1
        and metrics["pageScroll"] <= metrics["pageClient"] + 1,
        str(metrics),
    )
    ok(
        "1024 各卡片无横向溢出",
        all(card["scrollWidth"] <= card["clientWidth"] + 1 for card in metrics["cards"]),
        str(metrics["cards"]),
    )
    tops = {card["key"]: card["top"] for card in metrics["cards"]}
    ok(
        "1024 V2 卡片单列顺序正确",
        tops.get("mcp", 0) < tops.get("embedding", 0) < tops.get("reranker", 0) < tops.get("patrol", 0),
        str(tops),
    )

    page.evaluate("() => document.querySelector('.page-scroll')?.scrollTo(0, 0)")
    page.wait_for_timeout(400)
    shot(page, "06-settings-1024")

    # 滚动到 V2 区域再截一张（MCP + 语义向量在 1024 下的叠放）
    page.locator('[data-card="mcp"]').scroll_into_view_if_needed()
    page.wait_for_timeout(400)
    shot(page, "06b-settings-1024-v2")

    # 添加工具弹窗在 1024 下不溢出
    page.locator('[data-card="mcp"]').get_by_role("button", name="添加工具").click()
    wizard = page.get_by_role("dialog", name="添加 MCP 工具")
    expect(wizard).to_be_visible(timeout=10000)
    expect(wizard.get_by_role("tab", name=re.compile("AI 帮我配"))).to_be_visible(timeout=5000)
    wizard_metrics = page.evaluate(
        """() => {
          const modal = document.querySelector('.modal');
          const rect = modal ? modal.getBoundingClientRect() : null;
          return {
            docScroll: document.documentElement.scrollWidth,
            inner: window.innerWidth,
            modalLeft: rect ? rect.left : -1,
            modalRight: rect ? rect.right : -1,
            modalWidth: rect ? rect.width : 0,
          };
        }"""
    )
    ok(
        "1024 添加工具弹窗不溢出",
        wizard_metrics["docScroll"] <= wizard_metrics["inner"] + 1
        and wizard_metrics["modalLeft"] >= 0
        and wizard_metrics["modalRight"] <= wizard_metrics["inner"] + 1,
        str(wizard_metrics),
    )
    page.screenshot(path=str(MCP_ADD_SHOTS / "05-wizard-1024.png"))
    print("  📸 mcp-add/05-wizard-1024.png")
    wizard.get_by_role("button", name="取消").click()
    expect(wizard).to_be_hidden(timeout=8000)

    context.close()


def main() -> int:
    log(f"Synapse V2 前端验收 · {BASE}")
    try:
        reset_mock()
        log("  🔄 mock 状态已重置")
    except Exception as exc:  # noqa: BLE001
        log(f"  ⚠️ mock reset 失败（非 mock 后端可忽略）：{exc}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, device_scale_factor=2, locale="zh-CN"
        )
        page = context.new_page()
        attach(page, "1440")
        page.set_default_timeout(20000)

        wait_settings_ready(page)
        verify_mcp(page)
        verify_mcp_add(page)
        verify_mcp_ai(page)
        verify_embedding(page)
        verify_reranker(page)
        verify_patrol(page)
        verify_chat(page)
        verify_context_hint(page)
        verify_regression(page)
        verify_1024(browser)
        browser.close()

    # 会话中预期不会出现失败请求；唯一例外是 AI 推荐 503/502 错误用例（故意触发，用于验证中文错误提示）。
    expected_console = (
        "Failed to load resource: the server responded with a status of 503",
        "Failed to load resource: the server responded with a status of 502",
    )
    unexpected_console = [
        item for item in CONSOLE_ERRORS if not any(pattern in item for pattern in expected_console)
    ]
    unexpected_http = [
        item
        for item in HTTP_ERRORS
        if "/api/mock/reset" not in item
        and "/favicon" not in item
        and not (
            "/api/mcp/suggest" in item and (" 503 " in item or " 502 " in item)
        )
    ]
    ok("0 console error（忽略 AI 推荐 503/502 预期错误）", not unexpected_console, " | ".join(unexpected_console[:5]))
    ok("0 page error", not PAGE_ERRORS, " | ".join(PAGE_ERRORS[:5]))
    ok("0 非预期 HTTP 错误（忽略 AI 推荐 503/502 预期错误）", not unexpected_http, " | ".join(unexpected_http[:5]))

    log("\n================ 验收汇总 ================")
    log(f"PASS: {len(PASS)}  FAIL: {len(FAIL)}")
    for item in FAIL:
        log(f"  FAIL -> {item}")
    log(f"截图目录：{SHOTS}")
    (SHOTS / "verify.log").write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
