"""「AI 帮我配」MCP 推荐 API 测试（catalog + POST /api/mcp/suggest）。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.services import llm, mcp as mcp_service

from .mcp_helpers import configure_mock_llm

SUGGEST_KEYS = {"ok", "need", "suggestions", "notes"}
BUILTIN_SUGGESTION_KEYS = {
    "catalog_id",
    "kind",
    "name",
    "description",
    "rationale",
    "server_id",
    "env_keys",
    "requires_token",
    "test_note",
}
CUSTOM_SUGGESTION_KEYS = {
    "catalog_id",
    "kind",
    "name",
    "description",
    "rationale",
    "command",
    "args",
    "env_keys",
    "requires_token",
    "test_note",
}


def _mock_suggest_llm(
    monkeypatch: pytest.MonkeyPatch,
    content: str | dict,
    captured: dict | None = None,
) -> None:
    """注入 mock LLM：suggest 调用为非流式 completion，直接返回 content。"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if captured is not None:
            captured.setdefault("payloads", []).append(payload)
        text = json.dumps(content, ensure_ascii=False) if isinstance(content, dict) else content
        return httpx.Response(
            200, json={"choices": [{"message": {"content": text}}]}
        )

    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(handler))


def _mcp_settings_raw() -> str | None:
    with db.get_db() as conn:
        return db.setting_get(conn, "mcp_servers")


# ---------------------------------------------------------------------------
# 安全目录
# ---------------------------------------------------------------------------


def test_catalog_covers_builtin_and_custom() -> None:
    catalog = mcp_service.MCP_SUGGEST_CATALOG
    assert {"filesystem", "sqlite", "github"} <= set(catalog)
    for builtin_id in ("filesystem", "sqlite", "github"):
        entry = catalog[builtin_id]
        assert entry["kind"] == "builtin"
        assert entry["server_id"] == builtin_id
        assert entry["name"] and entry["description"] and entry["test_note"]
        assert isinstance(entry["env_keys"], list)
        assert "requires_token" in entry

    custom_ids = {"fetch", "git", "time", "memory"}
    assert custom_ids <= set(catalog)
    for custom_id in custom_ids:
        entry = catalog[custom_id]
        assert entry["kind"] == "custom"
        assert entry["server_id"] is None
        assert str(entry["command"]).strip()
        assert isinstance(entry["args"], list) and entry["args"]
        assert entry["name"] and entry["description"] and entry["test_note"]
        assert isinstance(entry["env_keys"], list)
        # 命令只能来自服务端目录的固定模板
        assert "{" in " ".join(str(arg) for arg in entry["args"]) or entry["params"] == {}

    # 目录中不包含任何 token 明文
    serialized = json.dumps(catalog, ensure_ascii=False)
    assert "sk-" not in serialized and "ghp_" not in serialized


# ---------------------------------------------------------------------------
# 503 / 502 / 400
# ---------------------------------------------------------------------------


def test_suggest_requires_configured_llm(client: TestClient) -> None:
    response = client.post("/api/mcp/suggest", json={"need": "读取本地文件"})
    assert response.status_code == 503
    assert response.json()["detail"] == "请先在设置接入模型，或手动添加"


def test_suggest_llm_error_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_mock_llm(client)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    response = client.post("/api/mcp/suggest", json={"need": "网页抓取"})
    assert response.status_code == 502
    assert "AI 建议生成失败" in response.json()["detail"]


def test_suggest_invalid_json_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_mock_llm(client)
    _mock_suggest_llm(monkeypatch, "抱歉，我无法推荐（不是 JSON）")
    response = client.post("/api/mcp/suggest", json={"need": "网页抓取"})
    assert response.status_code == 502
    assert "JSON" in response.json()["detail"]


def test_suggest_empty_need_returns_400(client: TestClient) -> None:
    response = client.post("/api/mcp/suggest", json={"need": "   "})
    assert response.status_code == 400
    assert response.json()["detail"] == "需求描述不能为空"


# ---------------------------------------------------------------------------
# 正常建议：builtin / custom
# ---------------------------------------------------------------------------


def test_suggest_builtin_github(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    configure_mock_llm(client)
    captured: dict = {}
    _mock_suggest_llm(
        monkeypatch,
        {
            "suggestions": [
                {
                    "catalog_id": "github",
                    "rationale": "用户要查 GitHub 仓库与 issue",
                }
            ],
            "notes": "推荐启用内置 GitHub 工具",
        },
        captured,
    )
    before = _mcp_settings_raw()
    response = client.post(
        "/api/mcp/suggest", json={"need": "我想查 GitHub 仓库的 issue"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == SUGGEST_KEYS
    assert body["ok"] is True
    assert body["need"] == "我想查 GitHub 仓库的 issue"
    assert body["notes"]
    assert len(body["suggestions"]) == 1
    suggestion = body["suggestions"][0]
    assert set(suggestion) == BUILTIN_SUGGESTION_KEYS
    assert suggestion["catalog_id"] == "github"
    assert suggestion["kind"] == "builtin"
    assert suggestion["server_id"] == "github"
    assert suggestion["requires_token"] is False
    assert suggestion["env_keys"] == ["GITHUB_TOKEN"]
    # 不泄露 token / 不创建 server / 不写入配置
    assert "ghp_should_not_leak" not in response.text
    assert _mcp_settings_raw() == before

    # prompt 中只允许从目录选 id，并禁止模型输出命令
    payload = captured["payloads"][0]
    system_prompt = payload["messages"][0]["content"]
    for catalog_id in mcp_service.MCP_SUGGEST_CATALOG:
        assert catalog_id in system_prompt
    assert "禁止输出任何命令" in system_prompt
    assert payload["temperature"] == 0.1
    assert payload["stream"] is False


def test_suggest_custom_fetch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_mock_llm(client)
    _mock_suggest_llm(
        monkeypatch,
        {
            "suggestions": [
                {"catalog_id": "fetch", "rationale": "需要抓取网页正文"}
            ]
        },
    )
    body = client.post("/api/mcp/suggest", json={"need": "我需要网页抓取"}).json()
    suggestion = body["suggestions"][0]
    assert set(suggestion) == CUSTOM_SUGGESTION_KEYS
    assert suggestion["kind"] == "custom"
    assert suggestion["catalog_id"] == "fetch"
    assert suggestion["command"] == "uvx"
    assert suggestion["args"] == ["mcp-server-fetch"]
    assert "server_id" not in suggestion
    assert suggestion["requires_token"] is False


def test_suggest_filters_unknown_duplicates_and_caps(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_mock_llm(client)
    _mock_suggest_llm(
        monkeypatch,
        {
            "suggestions": [
                {"catalog_id": "evil-server", "rationale": "未知目录"},
                {"catalog_id": "fetch", "rationale": "抓网页"},
                {"catalog_id": "fetch", "rationale": "重复"},
                {"catalog_id": "github", "rationale": "查仓库"},
                {"catalog_id": "time", "rationale": "时间"},
            ]
        },
    )
    body = client.post(
        "/api/mcp/suggest", json={"need": "网页+GitHub", "max_suggestions": 2}
    ).json()
    assert [item["catalog_id"] for item in body["suggestions"]] == ["fetch", "github"]
    assert "evil-server" not in json.dumps(body, ensure_ascii=False)
    assert "忽略" in body["notes"] and "去重" in body["notes"]


def test_suggest_empty_result_is_ok(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_mock_llm(client)
    _mock_suggest_llm(
        monkeypatch, {"suggestions": [], "notes": "没有合适的工具"}
    )
    response = client.post("/api/mcp/suggest", json={"need": "和工具无关的问题"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["suggestions"] == []
    assert "手动添加" in body["notes"]


# ---------------------------------------------------------------------------
# 参数与安全：路径参数校验、不执行命令、不写配置
# ---------------------------------------------------------------------------


def test_suggest_git_path_param_validation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_mock_llm(client)
    # 1) 非法路径（root 之外 / shell 元字符）→ 保留占位符
    _mock_suggest_llm(
        monkeypatch,
        {
            "suggestions": [
                {
                    "catalog_id": "git",
                    "rationale": "查看本地仓库",
                    "params": {"repository_path": "/etc/passwd; rm -rf /"},
                }
            ]
        },
    )
    body = client.post("/api/mcp/suggest", json={"need": "查看本地 git 仓库"}).json()
    args = body["suggestions"][0]["args"]
    assert args[:3] == ["mcp-server-git", "--repository", "<仓库路径>"]

    # 2) 项目内合法路径 → 使用校验后的绝对路径
    allowed_path = str(config.PROJECT_ROOT)
    _mock_suggest_llm(
        monkeypatch,
        {
            "suggestions": [
                {
                    "catalog_id": "git",
                    "rationale": "查看项目仓库",
                    "params": {"repository_path": allowed_path},
                }
            ]
        },
    )
    body = client.post("/api/mcp/suggest", json={"need": "查看本地 git 仓库"}).json()
    args = body["suggestions"][0]["args"]
    assert args[0] == "mcp-server-git"
    assert args[2] == str(Path(allowed_path).resolve())
    assert "<仓库路径>" not in " ".join(args)


def test_suggest_never_executes_or_writes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_mock_llm(client)
    _mock_suggest_llm(
        monkeypatch,
        {"suggestions": [{"catalog_id": "time", "rationale": "获取当前时间"}]},
    )

    def _forbidden(*args, **kwargs):
        raise AssertionError("suggest 不允许执行 MCP 子进程或写配置")

    # 任何连接 / 写入路径被调用都算失败
    monkeypatch.setattr(mcp_service, "list_server_tools", _forbidden)
    monkeypatch.setattr(mcp_service, "get_enabled_tools", _forbidden)
    monkeypatch.setattr(mcp_service, "_session", _forbidden)
    monkeypatch.setattr(mcp_service, "create_custom_server", _forbidden)
    monkeypatch.setattr(mcp_service, "apply_patch", _forbidden)
    monkeypatch.setattr(mcp_service, "persist_server", _forbidden)

    before = _mcp_settings_raw()
    response = client.post("/api/mcp/suggest", json={"need": "现在几点了"})
    assert response.status_code == 200, response.text
    assert response.json()["suggestions"][0]["catalog_id"] == "time"
    assert _mcp_settings_raw() == before
    # 不创建任何 server：GET servers 只应看到默认 3 个内置 preset
    listing = client.get("/api/mcp/servers").json()
    assert {item["id"] for item in listing["items"]} == {
        "filesystem",
        "sqlite",
        "github",
    }


def test_suggest_prompt_and_response_schema(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_mock_llm(client)
    captured: dict = {}
    _mock_suggest_llm(
        monkeypatch,
        {"suggestions": [{"catalog_id": "sqlite", "rationale": "查询知识库统计"}]},
        captured,
    )
    response = client.post(
        "/api/mcp/suggest",
        json={"need": "查询知识库文档数量", "max_suggestions": 1},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == SUGGEST_KEYS
    suggestion = body["suggestions"][0]
    assert suggestion["catalog_id"] == "sqlite"
    assert suggestion["kind"] == "builtin"
    assert suggestion["server_id"] == "sqlite"
    assert 0 < len(suggestion["rationale"]) <= 120
    payload = captured["payloads"][0]
    assert "最多选择 1 个" in payload["messages"][0]["content"]
    assert payload["messages"][-1]["content"] == "用户需求：查询知识库文档数量"
    assert "max_tokens" in payload
