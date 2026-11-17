"""MCP Client 配置 / 短连接 / 工具调用测试（真实子进程 + 临时 FastMCP server）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app import db
from app.services import mcp

from .mcp_helpers import (
    custom_server,
    fake_server_script,
    filesystem_server,
    github_server,
    read_mcp_servers,
    write_mcp_servers,
)


# ---------------------------------------------------------------------------
# 默认配置 / 脱敏
# ---------------------------------------------------------------------------


def test_default_presets_seeded_and_disabled(data_dir: Path) -> None:
    db.init_db()
    with db.get_db() as conn:
        servers = mcp.get_servers(conn)
    assert [server["id"] for server in servers] == ["filesystem", "sqlite", "github"]
    assert all(server["enabled"] is False for server in servers)
    assert all(server["builtin"] in mcp.BUILTIN_IDS for server in servers)
    assert all(server["transport"] == "stdio" for server in servers)
    assert all(server["allowed_tools"] is None for server in servers)

    # 默认 preset 写入 settings，第二次读取不会重复写入
    with db.get_db() as conn:
        assert db.setting_get(conn, "mcp_servers") is not None
        again = mcp.get_servers(conn)
    assert len(again) == 3

    github_preset = servers[2]
    public = mcp.public_server(github_preset)
    assert "env" not in public  # 永不返回 env 值
    assert public["has_token"] is False
    assert "GITHUB_TOKEN" not in json.dumps(public)


def test_token_redaction_and_env_merge(data_dir: Path) -> None:
    db.init_db()
    server = github_server(token="ghp_secret_token")
    public = mcp.public_server(server)
    assert public["has_token"] is True
    assert "ghp_secret_token" not in json.dumps(public)
    # 配置的 token 会合并进子进程环境
    assert mcp.effective_env(server)["GITHUB_TOKEN"] == "ghp_secret_token"

    cleared = github_server(token="")
    assert mcp.public_server(cleared)["has_token"] is False


def test_patch_env_empty_string_keeps_null_clears(data_dir: Path) -> None:
    db.init_db()
    write_mcp_servers([github_server(token="ghp_keep")])
    with db.get_db() as conn:
        server = mcp.find_server(conn, "github")
        assert server is not None
        updated = mcp.apply_patch(conn, "github", {"env": {"GITHUB_TOKEN": ""}})
    assert updated["env"]["GITHUB_TOKEN"] == "ghp_keep"
    assert mcp.public_server(updated)["has_token"] is True

    with db.get_db() as conn:
        cleared = mcp.apply_patch(conn, "github", {"env": {"GITHUB_TOKEN": None}})
    assert "GITHUB_TOKEN" not in cleared["env"]
    assert mcp.public_server(cleared)["has_token"] is False

    with db.get_db() as conn:
        assert mcp.find_server(conn, "github")["env"] == {}


def test_qualified_tool_name_rules() -> None:
    assert (
        mcp.qualified_tool_name("filesystem", "list_directory")
        == "mcp_filesystem_list_directory"
    )
    assert mcp.qualified_tool_name("my server!", "read file") == "mcp_my_server_read_file"
    assert len(mcp.qualified_tool_name("x" * 80, "y" * 80)) <= 64


def test_detect_tool_intent() -> None:
    assert mcp.detect_tool_intent("列出数据目录下的 Markdown 文件") is True
    assert mcp.detect_tool_intent("查询知识库文档总数") is True
    assert mcp.detect_tool_intent("SQL 里 documents 表有多少行") is True
    assert mcp.detect_tool_intent("看看 github 上的 MCP 项目") is True
    assert mcp.detect_tool_intent("RAG 的分块大小是多少？") is False
    assert mcp.detect_tool_intent("手冲咖啡的水温") is False
    assert mcp.detect_tool_intent("") is False


# ---------------------------------------------------------------------------
# 短连接：list / call / allowed_tools / 失败
# ---------------------------------------------------------------------------


def test_client_lists_and_calls_real_stdio_server(
    data_dir: Path, tmp_path: Path
) -> None:
    db.init_db()
    script = fake_server_script(tmp_path)
    write_mcp_servers(
        [custom_server(script, allowed_tools=["add", "list_files", "fail_tool"])]
    )

    tools = asyncio.run(mcp.get_enabled_tools())
    names = {tool["qualified_name"] for tool in tools}
    assert names == {
        "mcp_srv_test_add",
        "mcp_srv_test_list_files",
        "mcp_srv_test_fail_tool",
    }
    assert all(tool["read_only"] is True for tool in tools)
    assert all(tool["server_id"] == "srv_test" for tool in tools)

    result = asyncio.run(
        mcp.call_tool(None, "mcp_srv_test_list_files", {"path": "docs"})
    )
    assert result["ok"] is True
    assert "a.md" in result["text"]
    assert result["structured"]["count"] == 2
    assert "返回 2 项" in mcp.summarize_tool_result(result)

    failed = asyncio.run(mcp.call_tool(None, "mcp_srv_test_fail_tool", {}))
    assert failed["ok"] is False
    assert "模拟工具失败" in str(failed["error"])


def test_client_respects_allowed_tools(data_dir: Path, tmp_path: Path) -> None:
    db.init_db()
    script = fake_server_script(tmp_path)
    write_mcp_servers([custom_server(script, allowed_tools=["list_files"])])

    tools = asyncio.run(mcp.get_enabled_tools())
    assert [tool["tool_name"] for tool in tools] == ["list_files"]

    # 未在允许列表的工具不可调用
    denied = asyncio.run(mcp.call_tool(None, "mcp_srv_test_add", {"a": 1, "b": 2}))
    assert denied["ok"] is False
    assert "未知" in str(denied["error"])


def test_client_disabled_server_exposes_no_tools(
    data_dir: Path, tmp_path: Path
) -> None:
    db.init_db()
    script = fake_server_script(tmp_path)
    write_mcp_servers([custom_server(script, enabled=False)])
    assert asyncio.run(mcp.get_enabled_tools()) == []
    result = asyncio.run(mcp.call_tool(None, "mcp_srv_test_add", {"a": 1, "b": 2}))
    assert result["ok"] is False
    assert mcp.has_enabled_servers() is False


def test_client_timeout_is_readable(data_dir: Path, tmp_path: Path) -> None:
    db.init_db()
    script = fake_server_script(tmp_path)
    server = custom_server(script)
    result = asyncio.run(mcp.list_server_tools(server, timeout=0.05))
    assert result["ok"] is False
    assert "超时" in str(result["error"])


def test_client_bad_command_records_last_error(
    data_dir: Path, tmp_path: Path
) -> None:
    db.init_db()
    script = fake_server_script(tmp_path)
    write_mcp_servers(
        [
            custom_server(
                script,
                server_id="srv_bad",
                command="/nonexistent/synapse-python-xyz",
            ),
            custom_server(script, server_id="srv_ok"),
        ]
    )
    tools = asyncio.run(mcp.get_enabled_tools())
    # 坏 server 被跳过，好 server 的工具仍可用
    assert {tool["server_id"] for tool in tools} == {"srv_ok"}
    with db.get_db() as conn:
        bad = mcp.find_server(conn, "srv_bad")
    assert bad is not None
    assert bad["last_error"]
    assert "失败" in bad["last_error"]


def test_builtin_filesystem_server_real_call(data_dir: Path) -> None:
    db.init_db()
    (data_dir / "hello.md").write_text("# Hello\n测试文件内容。", encoding="utf-8")
    write_mcp_servers([filesystem_server(data_dir)])

    tools = asyncio.run(mcp.get_enabled_tools())
    tool_names = {tool["tool_name"] for tool in tools}
    assert tool_names == {
        "list_directory",
        "read_text_file",
        "search_files",
        "get_file_info",
    }

    result = asyncio.run(
        mcp.call_tool(None, "mcp_filesystem_search_files", {"pattern": "*.md"})
    )
    assert result["ok"] is True
    assert "hello.md" in result["text"]

    escape = asyncio.run(
        mcp.call_tool(None, "mcp_filesystem_read_text_file", {"path": "../etc/passwd"})
    )
    assert escape["ok"] is False
    assert "拒绝访问" in str(escape["error"])


def test_builtin_sqlite_server_real_call(data_dir: Path) -> None:
    db.init_db()
    write_mcp_servers([sqlite_server_fixture(data_dir)])
    result = asyncio.run(
        mcp.call_tool(
            None,
            "mcp_sqlite_query",
            {"sql": "SELECT COUNT(*) AS n FROM documents"},
        )
    )
    assert result["ok"] is True
    assert result["structured"]["row_count"] == 1


def sqlite_server_fixture(data_dir: Path) -> dict:
    from .mcp_helpers import sqlite_server

    return sqlite_server(data_dir / "synapse.db")


def test_config_crud_persists_json(data_dir: Path, tmp_path: Path) -> None:
    db.init_db()
    script = fake_server_script(tmp_path)
    with db.get_db() as conn:
        created = mcp.create_custom_server(
            conn,
            name="自定义工具",
            command="/bin/echo",
            args=["hello"],
            env={"MY_TOKEN": "secret"},
            allowed_tools=["tool_a"],
        )
    assert created["id"].startswith("srv_")
    assert created["builtin"] is None
    assert created["enabled"] is False
    stored = read_mcp_servers()
    assert any(server["id"] == created["id"] for server in stored)
    # 存储层保留 env（子进程需要），但公开视图脱敏
    public = mcp.public_server(created)
    assert "secret" not in json.dumps(public)

    with db.get_db() as conn:
        removed = mcp.delete_custom_server(conn, created["id"])
    assert removed["id"] == created["id"]
    assert all(server["id"] != created["id"] for server in read_mcp_servers())
