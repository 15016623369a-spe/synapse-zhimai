"""MCP 设置 API 测试（GET/PATCH/POST/DELETE/test/status）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app import db

from .mcp_helpers import (
    custom_server,
    fake_server_script,
    filesystem_server,
    github_server,
    read_mcp_servers,
    write_mcp_servers,
)

EXPECTED_FIELDS = {
    "id",
    "name",
    "builtin",
    "enabled",
    "transport",
    "command",
    "args",
    "allowed_tools",
    "has_token",
    "connected",
    "tools",
    "last_error",
}


def _items(response) -> list[dict]:
    return response.json()["items"]


def test_default_mcp_servers_api(client: TestClient) -> None:
    response = client.get("/api/mcp/servers")
    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled_count"] == 0
    assert payload["tool_count"] == 0
    items = payload["items"]
    assert [item["id"] for item in items] == ["filesystem", "sqlite", "github"]
    for item in items:
        assert set(item) == EXPECTED_FIELDS
        assert item["enabled"] is False
        assert item["connected"] is False
        assert item["tools"] == []
        assert item["has_token"] is False
        assert item["transport"] == "stdio"
    assert "GITHUB_TOKEN" not in response.text


def test_patch_enable_and_disable_filesystem(
    client: TestClient, data_dir: Path
) -> None:
    (data_dir / "note.md").write_text("# Note\n内容", encoding="utf-8")
    write_mcp_servers([filesystem_server(data_dir, enabled=False)])

    response = client.patch("/api/mcp/servers/filesystem", json={"enabled": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["error"] is None
    server = body["server"]
    assert server["enabled"] is True
    assert server["connected"] is True
    tool_names = {tool["name"] for tool in server["tools"]}
    assert tool_names == {
        "list_directory",
        "read_text_file",
        "search_files",
        "get_file_info",
    }

    listing = client.get("/api/mcp/servers").json()
    assert listing["enabled_count"] == 1
    assert listing["tool_count"] == 4
    filesystem = next(item for item in listing["items"] if item["id"] == "filesystem")
    assert filesystem["connected"] is True
    assert filesystem["last_error"] is None

    disabled = client.patch(
        "/api/mcp/servers/filesystem", json={"enabled": False}
    ).json()
    assert disabled["ok"] is True
    assert disabled["server"]["enabled"] is False
    assert client.get("/api/mcp/status").json()["enabled_count"] == 0


def test_patch_github_token_redaction(client: TestClient) -> None:
    response = client.patch(
        "/api/mcp/servers/github", json={"env": {"GITHUB_TOKEN": "ghp_super_secret"}}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["server"]["has_token"] is True
    assert "ghp_super_secret" not in response.text

    listing = client.get("/api/mcp/servers")
    assert "ghp_super_secret" not in listing.text
    github = next(item for item in _items(listing) if item["id"] == "github")
    assert github["has_token"] is True

    # 空字符串表示不修改
    keep = client.patch(
        "/api/mcp/servers/github", json={"env": {"GITHUB_TOKEN": ""}}
    ).json()
    assert keep["server"]["has_token"] is True

    # null 表示清除
    cleared = client.patch(
        "/api/mcp/servers/github", json={"env": {"GITHUB_TOKEN": None}}
    ).json()
    assert cleared["server"]["has_token"] is False
    stored = {server["id"]: server for server in read_mcp_servers()}
    assert "GITHUB_TOKEN" not in stored["github"]["env"]


def test_test_endpoint_works_for_disabled_server(client: TestClient) -> None:
    response = client.post("/api/mcp/servers/filesystem/test")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["server_id"] == "filesystem"
    assert payload["tool_count"] == 4
    assert {tool["name"] for tool in payload["tools"]} == {
        "list_directory",
        "read_text_file",
        "search_files",
        "get_file_info",
    }
    assert payload["elapsed_ms"] >= 0
    # 测试不要求已启用
    assert client.get("/api/mcp/status").json()["enabled_count"] == 0

    github = client.post("/api/mcp/servers/github/test").json()
    assert github["ok"] is True  # 列工具不需要网络 / token
    assert {tool["name"] for tool in github["tools"]} == {
        "search_repositories",
        "get_repository",
        "list_issues",
        "get_file_contents",
    }


def test_test_endpoint_unknown_server_404(client: TestClient) -> None:
    response = client.post("/api/mcp/servers/not-exist/test")
    assert response.status_code == 404
    assert response.json()["detail"] == "MCP Server 不存在"


def test_custom_server_crud(client: TestClient, tmp_path: Path) -> None:
    script = fake_server_script(tmp_path)
    created = client.post(
        "/api/mcp/servers",
        json={
            "name": "自定义只读工具",
            "command": sys.executable,
            "args": [str(script)],
            "env": {"DEMO_TOKEN": "abc"},
            "allowed_tools": ["add", "list_files"],
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["ok"] is True
    server = body["server"]
    assert server["builtin"] is None
    assert server["enabled"] is False
    assert server["allowed_tools"] == ["add", "list_files"]
    assert "abc" not in created.text
    server_id = server["id"]
    assert server_id.startswith("srv_")

    # 启用自定义 server（真实连接临时 server）
    enabled = client.patch(
        f"/api/mcp/servers/{server_id}", json={"enabled": True}
    ).json()
    assert enabled["ok"] is True, enabled
    assert enabled["server"]["enabled"] is True
    assert enabled["server"]["connected"] is True
    assert {tool["name"] for tool in enabled["server"]["tools"]} == {
        "add",
        "list_files",
        "fail_tool",
    }

    # 测试连接
    tested = client.post(f"/api/mcp/servers/{server_id}/test").json()
    assert tested["ok"] is True
    assert tested["tool_count"] == 3

    # 删除
    deleted = client.delete(f"/api/mcp/servers/{server_id}")
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True
    assert deleted.json()["id"] == server_id
    assert all(item["id"] != server_id for item in _items(client.get("/api/mcp/servers")))
    assert client.post(f"/api/mcp/servers/{server_id}/test").status_code == 404


def test_custom_server_enable_requires_allowed_tools(
    client: TestClient, tmp_path: Path
) -> None:
    created = client.post(
        "/api/mcp/servers",
        json={"name": "无允许列表", "command": sys.executable, "args": ["-c", "pass"]},
    )
    assert created.status_code == 200
    server_id = created.json()["server"]["id"]

    response = client.patch(
        f"/api/mcp/servers/{server_id}", json={"enabled": True}
    )
    assert response.status_code == 400
    assert "allowed_tools" in response.json()["detail"]
    stored = {server["id"]: server for server in read_mcp_servers()}
    assert stored[server_id]["enabled"] is False


def test_enable_bad_command_keeps_disabled(
    client: TestClient, tmp_path: Path
) -> None:
    script = fake_server_script(tmp_path)
    write_mcp_servers(
        [
            custom_server(
                script,
                server_id="srv_bad",
                command="/nonexistent/synapse-python-xyz",
                allowed_tools=["add"],
                enabled=False,
            )
        ]
    )
    response = client.patch("/api/mcp/servers/srv_bad", json={"enabled": True})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["server"]["enabled"] is False
    assert body["server"]["last_error"]
    assert "失败" in body["error"] or "超时" in body["error"]
    status = client.get("/api/mcp/status").json()
    assert status["enabled_count"] == 0


def test_delete_builtin_rejected_and_unknown_404(client: TestClient) -> None:
    builtin = client.delete("/api/mcp/servers/filesystem")
    assert builtin.status_code == 400
    assert "内置" in builtin.json()["detail"]

    unknown = client.delete("/api/mcp/servers/not-exist")
    assert unknown.status_code == 404


def test_patch_unknown_server_404(client: TestClient) -> None:
    response = client.patch("/api/mcp/servers/not-exist", json={"enabled": True})
    assert response.status_code == 404
    assert response.json()["detail"] == "MCP Server 不存在"


def test_patch_allowed_tools_unknown_tool_rejected(
    client: TestClient, data_dir: Path
) -> None:
    write_mcp_servers(
        [filesystem_server(data_dir, enabled=False)]
    )
    response = client.patch(
        "/api/mcp/servers/filesystem",
        json={"enabled": True, "allowed_tools": ["not_a_real_tool"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "不存在" in body["error"]
    assert body["server"]["enabled"] is False


def test_status_endpoint_with_enabled_server(
    client: TestClient, data_dir: Path
) -> None:
    write_mcp_servers([filesystem_server(data_dir, enabled=True)])
    status = client.get("/api/mcp/status").json()
    assert status["enabled_count"] == 1
    assert status["tool_count"] == 4
    assert status["servers_count"] == 1
    assert status["enabled"] is True

    # 非 enabled server 不连接
    write_mcp_servers([github_server(enabled=False)])
    idle = client.get("/api/mcp/servers").json()
    assert idle["items"][0]["connected"] is False
    assert idle["items"][0]["tools"] == []


def test_patch_invalid_env_name(client: TestClient) -> None:
    response = client.patch(
        "/api/mcp/servers/github", json={"env": {"BAD-NAME": "x"}}
    )
    assert response.status_code == 400
    assert "环境变量名不合法" in response.json()["detail"]
