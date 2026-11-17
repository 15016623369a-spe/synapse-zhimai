"""内置只读 GitHub MCP Server（FastMCP + stdio）。

启动方式::

    python -m app.mcp_servers.github_server

- 只调用 GitHub REST 只读接口，不提供任何写操作；
- Token 从环境变量 ``GITHUB_TOKEN`` 读取（可选）；未配置时使用匿名公开 API，
  匿名调用每小时限流 60 次，触发限流时会返回中文提示；
- 所有错误都转成中文可读信息。
"""

from __future__ import annotations

import base64
import logging
import os
import re
import urllib.parse

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

logger = logging.getLogger("synapse.mcp.github")

API_BASE = "https://api.github.com"
TIMEOUT_SECONDS = 20.0
MAX_FILE_CHARS = 12000
MAX_SEARCH_RESULTS = 10
_USER_AGENT = "Synapse-MCP/1.0"

#: 测试注入点：httpx 异步传输层（None 表示真实网络）。
HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ISSUE_STATES = frozenset({"open", "closed", "all"})


class GitHubError(RuntimeError):
    """GitHub 工具错误（消息为中文，可直接展示）。"""


def _token() -> str:
    return os.environ.get("GITHUB_TOKEN", "").strip()


def _status_message(status_code: int) -> str:
    if status_code == 401:
        return "GitHub Token 无效或已过期，请在 MCP 设置中更新 Token"
    if status_code == 403:
        if not _token():
            return (
                "GitHub 匿名访问已触发限流（匿名每小时 60 次），"
                "请在 MCP 设置中配置 GITHUB_TOKEN 后重试"
            )
        return "GitHub API 限流或权限不足，请稍后重试并检查 Token 权限"
    if status_code == 404:
        return "GitHub 资源不存在（请检查仓库名 / 路径 / 分支是否正确）"
    if status_code == 422:
        return "GitHub 拒绝了请求参数（请检查查询语句是否合法）"
    if status_code >= 500:
        return f"GitHub 服务暂时不可用（HTTP {status_code}），请稍后重试"
    return f"GitHub API 返回 HTTP {status_code}"


async def _request(path: str, params: dict[str, object] | None = None) -> object:
    """GET 只读 GitHub API，返回解析后的 JSON。"""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = API_BASE + path
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=10.0),
            transport=HTTP_TRANSPORT,
            headers=headers,
        ) as client:
            response = await client.get(url, params=params or {})
    except httpx.TimeoutException as exc:
        raise GitHubError("GitHub API 请求超时（20 秒），请稍后重试") from exc
    except httpx.HTTPError as exc:
        raise GitHubError("无法连接 GitHub API，请检查网络后重试") from exc
    if response.status_code >= 400:
        raise GitHubError(_status_message(response.status_code))
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubError("GitHub 返回内容无法解析") from exc


def _clamp_results(raw: object, default: int = 5) -> int:
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, MAX_SEARCH_RESULTS))


def _validate_repo(repo: object) -> str:
    text = str(repo or "").strip()
    if not text:
        raise GitHubError("仓库名不能为空，格式应为 owner/name")
    if not _REPO_RE.match(text):
        raise GitHubError(f"仓库名格式不合法：{text}，应形如 owner/name")
    return text


def _quote_path(path: str) -> str:
    return urllib.parse.quote(str(path).lstrip("/"), safe="/")


async def search_repositories(
    query: str, max_results: int = 5
) -> dict[str, object]:
    """搜索 GitHub 仓库（只读）。"""
    keyword = str(query or "").strip()
    if not keyword:
        raise GitHubError("搜索关键词不能为空")
    limit = _clamp_results(max_results)
    payload = await _request(
        "/search/repositories", {"q": keyword, "per_page": limit, "sort": "stars"}
    )
    data = payload if isinstance(payload, dict) else {}
    raw_items = data.get("items") or []
    items = [
        {
            "full_name": str(item.get("full_name") or ""),
            "description": str(item.get("description") or ""),
            "stars": int(item.get("stargazers_count") or 0),
            "language": str(item.get("language") or ""),
            "url": str(item.get("html_url") or ""),
            "updated_at": str(item.get("updated_at") or ""),
        }
        for item in raw_items[:limit]
        if isinstance(item, dict)
    ]
    return {
        "ok": True,
        "query": keyword,
        "total_count": int(data.get("total_count") or 0),
        "count": len(items),
        "items": items,
        "anonymous": not _token(),
    }


async def get_repository(repo: str) -> dict[str, object]:
    """获取仓库基本信息（只读）。"""
    name = _validate_repo(repo)
    payload = await _request(f"/repos/{name}")
    data = payload if isinstance(payload, dict) else {}
    return {
        "ok": True,
        "full_name": str(data.get("full_name") or name),
        "description": str(data.get("description") or ""),
        "stars": int(data.get("stargazers_count") or 0),
        "forks": int(data.get("forks_count") or 0),
        "open_issues": int(data.get("open_issues_count") or 0),
        "language": str(data.get("language") or ""),
        "url": str(data.get("html_url") or ""),
        "default_branch": str(data.get("default_branch") or ""),
        "updated_at": str(data.get("updated_at") or ""),
        "topics": [str(topic) for topic in data.get("topics") or []][:10],
    }


async def list_issues(
    repo: str, state: str = "open", max_results: int = 5
) -> dict[str, object]:
    """列出仓库 Issues（只读，不含 Pull Request）。"""
    name = _validate_repo(repo)
    issue_state = str(state or "open").strip().lower()
    if issue_state not in _ISSUE_STATES:
        raise GitHubError("state 只支持 open / closed / all")
    limit = _clamp_results(max_results)
    payload = await _request(
        f"/repos/{name}/issues",
        {"state": issue_state, "per_page": limit, "sort": "created", "direction": "desc"},
    )
    raw_items = payload if isinstance(payload, list) else []
    items: list[dict[str, object]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        if item.get("pull_request") is not None:
            continue
        user = item.get("user") or {}
        items.append(
            {
                "number": int(item.get("number") or 0),
                "title": str(item.get("title") or ""),
                "state": str(item.get("state") or ""),
                "user": str(user.get("login") or "") if isinstance(user, dict) else "",
                "created_at": str(item.get("created_at") or ""),
                "url": str(item.get("html_url") or ""),
                "labels": [
                    str(label.get("name") or "")
                    for label in item.get("labels") or []
                    if isinstance(label, dict)
                ],
                "body_snippet": str(item.get("body") or "")[:300],
            }
        )
        if len(items) >= limit:
            break
    return {
        "ok": True,
        "repo": name,
        "state": issue_state,
        "count": len(items),
        "items": items,
    }


async def get_file_contents(
    repo: str, path: str, ref: str = ""
) -> dict[str, object]:
    """读取仓库中某个文件的内容（只读，base64 解码，最多 12000 字符）。"""
    name = _validate_repo(repo)
    file_path = str(path or "").strip()
    if not file_path:
        raise GitHubError("文件路径不能为空")
    if ".." in file_path.split("/"):
        raise GitHubError("文件路径不合法：不允许 ..")
    params: dict[str, object] = {}
    branch = str(ref or "").strip()
    if branch:
        params["ref"] = branch
    payload = await _request(f"/repos/{name}/contents/{_quote_path(file_path)}", params)
    if isinstance(payload, list):
        entries = [
            {
                "name": str(item.get("name") or ""),
                "path": str(item.get("path") or ""),
                "type": str(item.get("type") or ""),
                "size": int(item.get("size") or 0),
            }
            for item in payload
            if isinstance(item, dict)
        ]
        return {
            "ok": True,
            "repo": name,
            "path": file_path,
            "type": "directory",
            "count": len(entries),
            "entries": entries[:100],
        }
    if not isinstance(payload, dict):
        raise GitHubError("GitHub 返回内容无法解析")
    if str(payload.get("type") or "file") != "file":
        raise GitHubError(f"该路径不是文件（类型：{payload.get('type')}）")
    encoded = payload.get("content")
    if not encoded:
        return {
            "ok": True,
            "repo": name,
            "path": str(payload.get("path") or file_path),
            "type": "file",
            "size": int(payload.get("size") or 0),
            "content": "",
            "truncated": False,
            "note": "文件为空或内容过大（GitHub 未返回内容）",
        }
    try:
        decoded = base64.b64decode(str(encoded), validate=False)
    except (ValueError, TypeError) as exc:
        raise GitHubError("GitHub 文件内容解码失败") from exc
    text = decoded.decode("utf-8", errors="replace")
    truncated = len(text) > MAX_FILE_CHARS
    return {
        "ok": True,
        "repo": name,
        "path": str(payload.get("path") or file_path),
        "sha": str(payload.get("sha") or ""),
        "type": "file",
        "size": int(payload.get("size") or len(decoded)),
        "truncated": truncated,
        "content": text[:MAX_FILE_CHARS],
    }


mcp = FastMCP("synapse-github")


@mcp.tool(name="search_repositories", annotations=ToolAnnotations(readOnlyHint=True))
async def search_repositories_tool(query: str, max_results: int = 5) -> dict[str, object]:
    """搜索 GitHub 仓库（只读），返回仓库名 / 描述 / star 数 / 链接。"""
    return await search_repositories(query, max_results)


@mcp.tool(name="get_repository", annotations=ToolAnnotations(readOnlyHint=True))
async def get_repository_tool(repo: str) -> dict[str, object]:
    """获取 GitHub 仓库信息（只读），repo 格式为 owner/name。"""
    return await get_repository(repo)


@mcp.tool(name="list_issues", annotations=ToolAnnotations(readOnlyHint=True))
async def list_issues_tool(
    repo: str, state: str = "open", max_results: int = 5
) -> dict[str, object]:
    """列出 GitHub 仓库 Issues（只读），state 可选 open / closed / all。"""
    return await list_issues(repo, state, max_results)


@mcp.tool(name="get_file_contents", annotations=ToolAnnotations(readOnlyHint=True))
async def get_file_contents_tool(
    repo: str, path: str, ref: str = ""
) -> dict[str, object]:
    """读取 GitHub 仓库文件内容（只读），ref 为可选分支 / tag / commit。"""
    return await get_file_contents(repo, path, ref)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
