"""MCP 测试公共工具：临时 FastMCP server、配置构造、mock LLM 配置。

不放进 ``conftest.py``，避免与并行开发的其他 V2 任务测试互相干扰。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app import db

#: 临时 MCP server：两个只读工具 + 一个必然失败的工具
FAKE_SERVER_SOURCE = '''"""测试用 FastMCP stdio server。"""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("fake-tools")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def add(a: int, b: int) -> dict[str, object]:
    """Add two integers."""
    return {"ok": True, "sum": a + b}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_files(path: str = ".") -> dict[str, object]:
    """List fake files in a path."""
    return {"ok": True, "path": path, "files": ["a.md", "b.md"], "count": 2}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def fail_tool() -> dict[str, object]:
    """Always raise a readable error."""
    raise ValueError("模拟工具失败")


if __name__ == "__main__":
    mcp.run(transport="stdio")
'''


def fake_server_script(tmp_path: Path, name: str = "fake_mcp_server.py") -> Path:
    """在临时目录写入一个可运行的 FastMCP stdio server 脚本。"""
    path = tmp_path / name
    path.write_text(FAKE_SERVER_SOURCE, encoding="utf-8")
    return path


def write_mcp_servers(servers: list[dict]) -> None:
    """把 MCP server 配置写入当前测试库的 settings。"""
    with db.get_db() as conn:
        db.setting_set(
            conn, "mcp_servers", json.dumps(servers, ensure_ascii=False)
        )


def read_mcp_servers() -> list[dict]:
    with db.get_db() as conn:
        raw = db.setting_get(conn, "mcp_servers")
    return json.loads(raw) if raw else []


def custom_server(
    script: Path,
    *,
    server_id: str = "srv_test",
    name: str = "测试工具",
    allowed_tools: list[str] | None = None,
    enabled: bool = True,
    env: dict[str, object] | None = None,
    command: str | None = None,
) -> dict:
    """自定义 stdio server 配置。"""
    return {
        "id": server_id,
        "name": name,
        "builtin": None,
        "transport": "stdio",
        "command": command or sys.executable,
        "args": [str(script)],
        "env": env or {},
        "enabled": enabled,
        "allowed_tools": (
            allowed_tools if allowed_tools is not None else ["add", "list_files"]
        ),
        "last_error": None,
    }


def filesystem_server(
    root: Path,
    *,
    enabled: bool = True,
    allowed_tools: list[str] | None = None,
) -> dict:
    """内置文件系统 server 配置（root 指向临时数据目录）。"""
    return {
        "id": "filesystem",
        "name": "文件系统",
        "builtin": "filesystem",
        "transport": "stdio",
        "command": sys.executable,
        "args": [
            "-m",
            "app.mcp_servers.filesystem_server",
            "--root",
            str(root),
        ],
        "env": {},
        "enabled": enabled,
        "allowed_tools": allowed_tools,
        "last_error": None,
    }


def sqlite_server(
    db_file: Path,
    *,
    enabled: bool = True,
    allowed_tools: list[str] | None = None,
) -> dict:
    """内置 SQLite server 配置。"""
    return {
        "id": "sqlite",
        "name": "知识库数据库",
        "builtin": "sqlite",
        "transport": "stdio",
        "command": sys.executable,
        "args": [
            "-m",
            "app.mcp_servers.sqlite_server",
            "--db",
            str(db_file),
        ],
        "env": {},
        "enabled": enabled,
        "allowed_tools": allowed_tools,
        "last_error": None,
    }


def github_server(
    *,
    token: str = "",
    enabled: bool = False,
    allowed_tools: list[str] | None = None,
) -> dict:
    """内置 GitHub server 配置。"""
    return {
        "id": "github",
        "name": "GitHub",
        "builtin": "github",
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-m", "app.mcp_servers.github_server"],
        "env": {"GITHUB_TOKEN": token},
        "enabled": enabled,
        "allowed_tools": allowed_tools,
        "last_error": None,
    }


def configure_mock_llm(
    client: TestClient, base_url: str = "https://mock.api/v1/"
) -> dict:
    """通过 API 写入 mock LLM 配置（测试不访问真实模型）。"""
    response = client.put(
        "/api/settings",
        json={
            "llm": {
                "base_url": base_url,
                "model": "mock-chat",
                "api_key": "sk-test-secret-123",
                "temperature": 0.2,
            }
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def sse_events(raw: str) -> list[str]:
    """按顺序返回 SSE 事件名列表。"""
    return [
        block.splitlines()[0][len("event:") :].strip()
        for block in raw.split("\n\n")
        if block.strip().startswith("event:")
    ]
