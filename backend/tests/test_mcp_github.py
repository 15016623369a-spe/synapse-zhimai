"""内置 GitHub MCP Server 单元测试（monkeypatch httpx，不发真实请求）。"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.mcp_servers import github_server as github


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/search/repositories":
        return httpx.Response(
            200,
            json={
                "total_count": 2,
                "items": [
                    {
                        "full_name": "modelcontextprotocol/python-sdk",
                        "description": "MCP Python SDK",
                        "stargazers_count": 1200,
                        "language": "Python",
                        "html_url": "https://github.com/modelcontextprotocol/python-sdk",
                        "updated_at": "2026-11-01T00:00:00Z",
                    },
                    {
                        "full_name": "other/repo",
                        "description": None,
                        "stargazers_count": 3,
                        "language": None,
                        "html_url": "https://github.com/other/repo",
                        "updated_at": "2026-10-01T00:00:00Z",
                    },
                ],
            },
        )
    if path == "/repos/owner/repo":
        return httpx.Response(
            200,
            json={
                "full_name": "owner/repo",
                "description": "示例仓库",
                "stargazers_count": 42,
                "forks_count": 7,
                "open_issues_count": 3,
                "language": "Python",
                "html_url": "https://github.com/owner/repo",
                "default_branch": "main",
                "updated_at": "2026-11-01T00:00:00Z",
                "topics": ["mcp", "tools"],
            },
        )
    if path == "/repos/owner/repo/issues":
        return httpx.Response(
            200,
            json=[
                {
                    "number": 1,
                    "title": "工具调用失败",
                    "state": "open",
                    "user": {"login": "alice"},
                    "created_at": "2026-11-01T00:00:00Z",
                    "html_url": "https://github.com/owner/repo/issues/1",
                    "labels": [{"name": "bug"}],
                    "body": "详细描述",
                },
                {
                    "number": 2,
                    "title": "PR 不是 issue",
                    "state": "open",
                    "user": {"login": "bob"},
                    "pull_request": {"url": "https://api.github.com/pulls/2"},
                    "html_url": "https://github.com/owner/repo/pull/2",
                    "labels": [],
                },
            ],
        )
    if path == "/repos/owner/repo/contents/README.md":
        content = "# Demo\n" + "x" * 13000
        return httpx.Response(
            200,
            json={
                "type": "file",
                "path": "README.md",
                "sha": "abc123",
                "size": len(content),
                "encoding": "base64",
                "content": base64.b64encode(content.encode()).decode(),
            },
        )
    if path == "/repos/owner/repo/contents/docs":
        return httpx.Response(
            200,
            json=[
                {"name": "a.md", "path": "docs/a.md", "type": "file", "size": 10},
                {"name": "b.md", "path": "docs/b.md", "type": "file", "size": 20},
            ],
        )
    if path == "/repos/owner/limited":
        return httpx.Response(403, json={"message": "API rate limit exceeded"})
    if path == "/repos/owner/unauthorized":
        return httpx.Response(401, json={"message": "Bad credentials"})
    return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture(autouse=True)
def mock_github_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "HTTP_TRANSPORT", httpx.MockTransport(_handler))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def test_github_search_and_repository() -> None:
    import asyncio

    search = asyncio.run(github.search_repositories("mcp python", 5))
    assert search["ok"] is True
    assert search["total_count"] == 2
    assert search["count"] == 2
    assert search["items"][0]["full_name"] == "modelcontextprotocol/python-sdk"
    assert search["items"][0]["stars"] == 1200
    assert search["anonymous"] is True

    repo = asyncio.run(github.get_repository("owner/repo"))
    assert repo["ok"] is True
    assert repo["stars"] == 42
    assert repo["default_branch"] == "main"
    assert repo["topics"] == ["mcp", "tools"]

    with pytest.raises(github.GitHubError, match="owner/name"):
        asyncio.run(github.get_repository("not a repo"))


def test_github_list_issues_filters_pull_requests() -> None:
    import asyncio

    issues = asyncio.run(github.list_issues("owner/repo", "open", 5))
    assert issues["ok"] is True
    assert issues["count"] == 1
    assert issues["items"][0]["title"] == "工具调用失败"
    assert issues["items"][0]["labels"] == ["bug"]

    with pytest.raises(github.GitHubError, match="state"):
        asyncio.run(github.list_issues("owner/repo", "bogus"))


def test_github_get_file_contents_decodes_and_truncates() -> None:
    import asyncio

    file_payload = asyncio.run(github.get_file_contents("owner/repo", "README.md"))
    assert file_payload["ok"] is True
    assert file_payload["path"] == "README.md"
    assert file_payload["content"].startswith("# Demo")
    assert file_payload["truncated"] is True
    assert len(file_payload["content"]) <= 12000

    directory = asyncio.run(github.get_file_contents("owner/repo", "docs"))
    assert directory["type"] == "directory"
    assert directory["count"] == 2

    with pytest.raises(github.GitHubError, match="不能为空"):
        asyncio.run(github.get_file_contents("owner/repo", "  "))


def test_github_readable_errors_and_token_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    with pytest.raises(github.GitHubError, match="限流.*GITHUB_TOKEN"):
        asyncio.run(github.get_repository("owner/limited"))

    with pytest.raises(github.GitHubError, match="Token 无效"):
        asyncio.run(github.get_repository("owner/unauthorized"))

    with pytest.raises(github.GitHubError, match="不存在"):
        asyncio.run(github.get_repository("owner/missing"))

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"full_name": "owner/repo"})

    monkeypatch.setattr(github, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
    payload = asyncio.run(github.get_repository("owner/repo"))
    assert payload["ok"] is True
    assert captured["auth"] == "Bearer ghp_test_token"


def test_github_server_tool_names() -> None:
    import asyncio

    async def _list_tools() -> list[str]:
        tools = await github.mcp.list_tools()
        return sorted(tool.name for tool in tools)

    names = asyncio.run(_list_tools())
    assert names == [
        "get_file_contents",
        "get_repository",
        "list_issues",
        "search_repositories",
    ]


def test_github_search_empty_query() -> None:
    import asyncio

    with pytest.raises(github.GitHubError, match="不能为空"):
        asyncio.run(github.search_repositories("   "))


def test_github_response_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    monkeypatch.setattr(github, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(github.GitHubError, match="无法解析"):
        asyncio.run(github.get_repository("owner/repo"))


def test_github_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout", request=request)

    monkeypatch.setattr(github, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(github.GitHubError, match="超时"):
        asyncio.run(github.get_repository("owner/repo"))
