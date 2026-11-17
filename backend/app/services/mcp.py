"""MCP 客户端 / 配置管理（V2）。

职责：
- 在 ``settings`` 表 ``mcp_servers`` 中保存 MCP Server 配置，首次读取时写入 3 个内置
  preset（filesystem / sqlite / github，默认 disabled）；
- 每次操作 **短连接**：``stdio_client`` → ``ClientSession.initialize()`` → 操作 → 关闭，
  避免常驻子进程的生命周期问题；
- 提供 ``get_enabled_tools`` / ``call_tool`` / ``detect_tool_intent`` 给对话集成使用；
- 提供「AI 帮我配」安全目录 ``MCP_SUGGEST_CATALOG`` + ``suggest_servers``：
  LLM 只能从服务端目录中选 id，命令/参数全部来自 catalog，**不写入配置、不执行命令**；
- 配置读取接口一律脱敏（不返回 API token 明文，只返回 ``has_token``）；
- 所有错误信息为中文、可读；任何失败只记录 ``last_error``，不影响主服务。

安全：内置 server 全部只读；自定义 server 只有显式 ``allowed_tools`` 才允许被调用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from .. import config, db
from ..utils import collapse_whitespace, new_id, truncate
from . import llm

logger = logging.getLogger("synapse.mcp")

SETTINGS_KEY = "mcp_servers"

#: 内置 server 标识
BUILTIN_IDS = ("filesystem", "sqlite", "github")
BUILTIN_DEFAULTS: dict[str, dict[str, object]] = {
    "filesystem": {
        "name": "文件系统",
        "module": "app.mcp_servers.filesystem_server",
    },
    "sqlite": {
        "name": "知识库数据库",
        "module": "app.mcp_servers.sqlite_server",
    },
    "github": {
        "name": "GitHub",
        "module": "app.mcp_servers.github_server",
    },
}

# ---------------------------------------------------------------------------
# 「AI 帮我配」安全目录（catalog）
#
# 设计原则：LLM 只能从本目录选择 ``id`` + 给出理由，**命令 / 参数模板全部来自服务端**，
# 模型输出中的 command/args/env 一律忽略；建议只用于前端预填，不写入配置、不执行命令。
# ---------------------------------------------------------------------------

#: AI 建议：单次最多返回条数 / need 最大长度
SUGGEST_MAX_ITEMS = 3
SUGGEST_NEED_MAX_CHARS = 500
SUGGEST_TIMEOUT_SECONDS = 45.0
SUGGEST_MAX_TOKENS = 700

#: 常见 stdio MCP Server 安全目录
MCP_SUGGEST_CATALOG: dict[str, dict[str, object]] = {
    # ---- 内置（UI 直接启用已有 preset，不创建重复 server）----
    "filesystem": {
        "kind": "builtin",
        "server_id": "filesystem",
        "name": "文件系统",
        "description": "只读浏览 / 读取 / 搜索知识库数据目录内的文件（内置，路径受限）",
        "keywords": ["文件", "目录", "本地文件", "读取文件", "markdown", "文本", "文件夹"],
        "env_keys": [],
        "requires_token": False,
        "test_note": "内置只读工具；在 MCP 工具卡中启用「文件系统」即可。",
    },
    "sqlite": {
        "kind": "builtin",
        "server_id": "sqlite",
        "name": "知识库数据库",
        "description": "只读 SQL 查询当前知识库 SQLite 数据库（内置，仅 SELECT/WITH/EXPLAIN）",
        "keywords": ["数据库", "sql", "表", "查询", "统计", "文档数量", "知识库数据"],
        "env_keys": [],
        "requires_token": False,
        "test_note": "内置只读工具；在 MCP 工具卡中启用「知识库数据库」即可。",
    },
    "github": {
        "kind": "builtin",
        "server_id": "github",
        "name": "GitHub",
        "description": "只读搜索 GitHub 仓库 / issue / 文件内容（内置，可选 Token 提高限额）",
        "keywords": ["github", "仓库", "repo", "issue", "开源", "代码", "pull request"],
        "env_keys": ["GITHUB_TOKEN"],
        "requires_token": False,
        "test_note": "内置只读工具；未配置 Token 时匿名 API 限流，可在 MCP 工具卡填入 Token 后测试。",
    },
    # ---- 自定义（command/args 只能来自本目录模板）----
    "fetch": {
        "kind": "custom",
        "server_id": None,
        "name": "网页抓取",
        "description": "抓取网页并转为 Markdown 文本（mcp-server-fetch，只读网络请求）",
        "keywords": ["网页", "抓取", "网址", "url", "网页内容", "爬取", "网页搜索"],
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "params": {},
        "env_keys": [],
        "requires_token": False,
        "test_note": "需要本机已安装 uv/uvx（首次运行会下载依赖）；启用后点「测试连接」确认。",
    },
    "git": {
        "kind": "custom",
        "server_id": None,
        "name": "Git 仓库",
        "description": "只读查看本地 Git 仓库状态 / 日志 / diff（mcp-server-git）",
        "keywords": ["git", "仓库", "提交", "commit", "diff", "分支", "本地代码"],
        "command": "uvx",
        "args": ["mcp-server-git", "--repository", "{repository_path}"],
        "params": {
            "repository_path": {
                "label": "本地 Git 仓库路径",
                "kind": "path",
                "placeholder": "<仓库路径>",
            }
        },
        "env_keys": [],
        "requires_token": False,
        "test_note": "把 --repository 参数改成项目内本地 Git 仓库路径；uvx 首次运行会下载 mcp-server-git。",
    },
    "time": {
        "kind": "custom",
        "server_id": None,
        "name": "时间工具",
        "description": "获取当前时间 / 时区换算（mcp-server-time，无网络无写入）",
        "keywords": ["时间", "时区", "日期", "几点", "utc", "今天"],
        "command": "uvx",
        "args": ["mcp-server-time"],
        "params": {},
        "env_keys": [],
        "requires_token": False,
        "test_note": "需要本机已安装 uv/uvx；启用后点「测试连接」确认。",
    },
    "memory": {
        "kind": "custom",
        "server_id": None,
        "name": "知识记忆",
        "description": "跨会话保存实体 / 关系 / 观察（@modelcontextprotocol/server-memory）",
        "keywords": ["记忆", "记住", "实体", "关系网", "长期记忆", "memory"],
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "params": {},
        "env_keys": ["MEMORY_FILE_PATH"],
        "requires_token": False,
        "test_note": "需要本机 Node/npx；可选设置 MEMORY_FILE_PATH 持久化记忆文件；该工具含写入操作，请按需授权 allowed_tools。",
    },
}

#: 工具列表上限（qualified tools）
MAX_TOOLS = 20

#: 单次工具输出文本截断
MAX_RESULT_CHARS = 8000
#: 连接（initialize + list_tools）超时
CONNECT_TIMEOUT_SECONDS = 20.0
#: 单次工具调用超时
CALL_TIMEOUT_SECONDS = 30.0
#: 允许的工具名 / server id 字符
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
#: 单个 server 允许的工具数上限
MAX_TOOLS_PER_SERVER = 50
#: 自定义 server 最多数量
MAX_CUSTOM_SERVERS = 20
_QUALIFIED_PREFIX = "mcp_"

#: qualified name → {server_id, tool_name} 映射（进程内缓存，get_enabled_tools 时刷新）
_QUALIFIED_INDEX: dict[str, dict[str, str]] = {}

#: 对话意图关键词（中文子串 + 英文单词）
INTENT_KEYWORDS: tuple[str, ...] = (
    "github",
    "仓库",
    "repo",
    "repository",
    "issue",
    "pull request",
    "文件系统",
    "文件",
    "文件夹",
    "目录",
    "读取",
    "打开",
    "列出",
    "列表",
    "数据库",
    "数据表",
    "表结构",
    "sql",
    "查询",
    "统计数据",
    "文档总数",
    "文件列表",
    "文件内容",
    "磁盘",
    "多少篇",
    "多少条",
    "记录数",
)
_INTENT_EN_RE = re.compile(
    r"(?i)\b(github|repo|repository|issue|sql|table|database|file|files|directory|dir|folder)\b"
)


class MCPError(RuntimeError):
    """MCP 客户端错误（中文消息）。"""


class MCPConfigError(ValueError):
    """MCP 配置错误（中文消息，API 层转 400）。"""


# ---------------------------------------------------------------------------
# 默认配置 / 读写
# ---------------------------------------------------------------------------


def _python_command() -> str:
    executable = str(sys.executable or "").strip()
    return executable or "python3"


def default_servers() -> list[dict[str, object]]:
    """3 个内置 preset（默认 disabled，不含任何 token 明文）。"""
    python = _python_command()
    data_dir = str(config.data_dir())
    db_file = str(config.db_path())
    return [
        {
            "id": "filesystem",
            "name": "文件系统",
            "builtin": "filesystem",
            "transport": "stdio",
            "command": python,
            "args": [
                "-m",
                "app.mcp_servers.filesystem_server",
                "--root",
                data_dir,
            ],
            "env": {},
            "enabled": False,
            "allowed_tools": None,
            "last_error": None,
        },
        {
            "id": "sqlite",
            "name": "知识库数据库",
            "builtin": "sqlite",
            "transport": "stdio",
            "command": python,
            "args": [
                "-m",
                "app.mcp_servers.sqlite_server",
                "--db",
                db_file,
            ],
            "env": {},
            "enabled": False,
            "allowed_tools": None,
            "last_error": None,
        },
        {
            "id": "github",
            "name": "GitHub",
            "builtin": "github",
            "transport": "stdio",
            "command": python,
            "args": ["-m", "app.mcp_servers.github_server"],
            "env": {"GITHUB_TOKEN": ""},
            "enabled": False,
            "allowed_tools": None,
            "last_error": None,
        },
    ]


def _normalize_server(raw: object, index: int = 0) -> dict[str, object]:
    """把存储中的一条配置规范化为完整结构（容错读取）。"""
    item = raw if isinstance(raw, dict) else {}
    builtin_raw = str(item.get("builtin") or "").strip() or None
    builtin = builtin_raw if builtin_raw in BUILTIN_IDS else None
    server_id = str(item.get("id") or "").strip() or str(builtin or f"srv_{index}")
    env = item.get("env")
    env_dict: dict[str, object] = {}
    if isinstance(env, dict):
        for key, value in env.items():
            if value is None:
                env_dict[str(key)] = None
            elif str(value).strip() != "":
                env_dict[str(key)] = str(value).strip()
    raw_allowed = item.get("allowed_tools")
    if isinstance(raw_allowed, list):
        allowed: list[str] | None = [
            str(tool).strip() for tool in raw_allowed if str(tool).strip()
        ]
    elif raw_allowed is None:
        allowed = None
    else:
        allowed = None
    name = str(item.get("name") or "").strip()
    if not name and builtin:
        name = str(BUILTIN_DEFAULTS[builtin]["name"])
    command = str(item.get("command") or "").strip()
    args_raw = item.get("args")
    args: list[str] = []
    if isinstance(args_raw, list):
        args = [str(arg) for arg in args_raw]
    if builtin and not command:
        command = _python_command()
        preset = next(
            (preset for preset in default_servers() if preset["id"] == builtin), None
        )
        if preset is not None and not args:
            args = [str(arg) for arg in preset["args"]]
    return {
        "id": server_id,
        "name": name or server_id,
        "builtin": builtin,
        "transport": "stdio",
        "command": command,
        "args": args,
        "env": env_dict,
        "enabled": bool(item.get("enabled")),
        "allowed_tools": allowed,
        "last_error": (
            str(item.get("last_error")) if item.get("last_error") else None
        ),
    }


def _save_servers(conn, servers: list[dict[str, object]]) -> None:
    db.setting_set(
        conn,
        SETTINGS_KEY,
        json.dumps(servers, ensure_ascii=False, separators=(",", ":")),
    )


def get_servers(conn) -> list[dict[str, object]]:
    """读取（必要时初始化）MCP Server 配置列表。"""
    raw = db.setting_get(conn, SETTINGS_KEY)
    if raw is None:
        servers = default_servers()
        _save_servers(conn, servers)
        return servers
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("settings.mcp_servers 不是合法 JSON，回退到默认 preset")
        return default_servers()
    if not isinstance(parsed, list):
        return default_servers()
    servers = [_normalize_server(item, index) for index, item in enumerate(parsed)]
    # 去重（同 id 只保留第一条），并保持内置顺序在前
    unique: list[dict[str, object]] = []
    seen: set[str] = set()
    for server in servers:
        server_id = str(server["id"])
        if server_id in seen:
            continue
        seen.add(server_id)
        unique.append(server)
    unique.sort(
        key=lambda server: (
            0 if server.get("builtin") else 1,
            (
                BUILTIN_IDS.index(str(server.get("builtin")))
                if server.get("builtin") in BUILTIN_IDS
                else len(BUILTIN_IDS)
            ),
            str(server.get("name") or ""),
        )
    )
    return unique


def find_server(conn, server_id: str) -> dict[str, object] | None:
    target = str(server_id or "").strip()
    for server in get_servers(conn):
        if str(server.get("id")) == target:
            return server
    return None


def _load_servers() -> list[dict[str, object]]:
    with db.get_db() as conn:
        return get_servers(conn)


def save_last_error(server_id: str, error: str | None) -> None:
    """记录 / 清除某个 server 的 ``last_error``（失败不影响主流程）。"""
    target = str(server_id or "").strip()
    if not target:
        return
    try:
        with db.get_db() as conn:
            servers = get_servers(conn)
            changed = False
            for server in servers:
                if str(server.get("id")) != target:
                    continue
                normalized = str(error).strip() if error else None
                if server.get("last_error") == normalized:
                    return
                server["last_error"] = normalized
                changed = True
                break
            if changed:
                _save_servers(conn, servers)
    except Exception:  # 记录错误失败不影响主流程
        logger.debug("写入 MCP last_error 失败：%s", target, exc_info=True)


# ---------------------------------------------------------------------------
# 对外公开结构 / token 脱敏
# ---------------------------------------------------------------------------


def _configured_env(server: dict[str, object]) -> dict[str, object]:
    env = server.get("env")
    return env if isinstance(env, dict) else {}


def effective_env(server: dict[str, object]) -> dict[str, str]:
    """合并进程环境与配置 env（配置中的非空值优先；``None`` 表示清除）。"""
    merged: dict[str, str] = {
        str(key): str(value) for key, value in os.environ.items()
    }
    for key, value in _configured_env(server).items():
        if value is None:
            merged.pop(str(key), None)
            continue
        text = str(value)
        if text == "":
            continue
        merged[str(key)] = text
    return merged


def has_github_token(server: dict[str, object]) -> bool:
    """``GITHUB_TOKEN`` 是否已配置（配置值或进程环境变量）。"""
    configured = _configured_env(server).get("GITHUB_TOKEN")
    if isinstance(configured, str) and configured.strip():
        return True
    return bool(os.environ.get("GITHUB_TOKEN", "").strip())


def public_server(
    server: dict[str, object],
    *,
    connected: bool = False,
    tools: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """脱敏后的 server 视图（绝不返回 env 值 / token 明文）。"""
    return {
        "id": str(server.get("id") or ""),
        "name": str(server.get("name") or ""),
        "builtin": server.get("builtin"),
        "enabled": bool(server.get("enabled")),
        "transport": "stdio",
        "command": str(server.get("command") or ""),
        "args": [str(arg) for arg in server.get("args") or []],
        "allowed_tools": server.get("allowed_tools"),
        "has_token": has_github_token(server),
        "connected": bool(connected),
        "tools": tools or [],
        "last_error": server.get("last_error"),
    }


# ---------------------------------------------------------------------------
# 配置变更（供 API 调用；错误抛 MCPConfigError）
# ---------------------------------------------------------------------------


def _validate_env_updates(updates: object) -> dict[str, object]:
    if not isinstance(updates, dict):
        raise MCPConfigError("env 必须是对象（键值对）")
    cleaned: dict[str, object] = {}
    for key, value in updates.items():
        name = str(key).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise MCPConfigError(f"环境变量名不合法：{key}")
        if value is None:
            cleaned[name] = None
        elif isinstance(value, (str, int, float, bool)):
            cleaned[name] = str(value).strip()
        else:
            raise MCPConfigError(f"环境变量 {name} 的值必须是字符串")
    return cleaned


def _apply_env_update(
    current: dict[str, object], updates: dict[str, object]
) -> dict[str, object]:
    """空串表示不修改；``None`` 表示清除；非空值覆盖。"""
    merged = dict(current)
    for key, value in updates.items():
        if value is None:
            merged.pop(key, None)
        elif str(value) == "":
            continue
        else:
            merged[key] = str(value)
    return merged


def _validate_allowed_tools(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise MCPConfigError("allowed_tools 必须是字符串数组或 null")
    cleaned: list[str] = []
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        if text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > MAX_TOOLS_PER_SERVER:
        raise MCPConfigError(f"allowed_tools 最多 {MAX_TOOLS_PER_SERVER} 个")
    return cleaned


def _validate_command(command: object) -> str:
    text = str(command or "").strip()
    if not text:
        raise MCPConfigError("启动命令不能为空")
    return text


def _validate_args(args: object) -> list[str]:
    if args is None:
        return []
    if not isinstance(args, list):
        raise MCPConfigError("args 必须是字符串数组")
    return [str(arg) for arg in args]


def preview_patch(server: dict[str, object], updates: dict[str, object]) -> dict[str, object]:
    """在内存中应用 PATCH 更新做校验，返回候选配置（不落库）。"""
    candidate = dict(server)
    if "enabled" in updates and updates["enabled"] is not None:
        candidate["enabled"] = bool(updates["enabled"])
    if "allowed_tools" in updates:
        candidate["allowed_tools"] = _validate_allowed_tools(updates["allowed_tools"])
    if "env" in updates:
        env_updates = _validate_env_updates(updates["env"])
        candidate["env"] = _apply_env_update(_configured_env(candidate), env_updates)
    if candidate.get("enabled"):
        validate_enable(candidate)
    return candidate


def persist_server(conn, server: dict[str, object]) -> dict[str, object]:
    """按 id 覆盖写入一条 server 配置（不存在则追加）。"""
    servers = get_servers(conn)
    target = str(server.get("id") or "").strip()
    replaced = False
    for position, item in enumerate(servers):
        if str(item.get("id")) == target:
            servers[position] = server
            replaced = True
            break
    if not replaced:
        servers.append(server)
    _save_servers(conn, servers)
    return server


def apply_patch(conn, server_id: str, updates: dict[str, object]) -> dict[str, object]:
    """把 PATCH 更新写入配置并返回更新后的 server（不校验连接）。"""
    server = find_server(conn, server_id)
    if server is None:
        raise MCPConfigError("MCP Server 不存在")
    candidate = preview_patch(server, updates)
    return persist_server(conn, candidate)


def create_custom_server(
    conn,
    *,
    name: object,
    command: object,
    args: object = None,
    env: object = None,
    allowed_tools: object = None,
) -> dict[str, object]:
    """创建自定义 stdio MCP Server（默认 disabled）。"""
    servers = get_servers(conn)
    custom_count = sum(1 for server in servers if not server.get("builtin"))
    if custom_count >= MAX_CUSTOM_SERVERS:
        raise MCPConfigError(f"自定义 MCP Server 最多 {MAX_CUSTOM_SERVERS} 个")
    server_name = str(name or "").strip()
    if not server_name:
        raise MCPConfigError("名称不能为空")
    if len(server_name) > 40:
        raise MCPConfigError("名称最多 40 个字符")
    existing_ids = {str(server["id"]) for server in servers}
    server_id = new_id("srv")[:16]
    while server_id in existing_ids:  # pragma: no cover - 极小概率
        server_id = new_id("srv")[:16]
    server = {
        "id": server_id,
        "name": server_name,
        "builtin": None,
        "transport": "stdio",
        "command": _validate_command(command),
        "args": _validate_args(args),
        "env": (
            _apply_env_update({}, _validate_env_updates(env))
            if env is not None
            else {}
        ),
        "enabled": False,
        "allowed_tools": _validate_allowed_tools(allowed_tools),
        "last_error": None,
    }
    servers.append(server)
    _save_servers(conn, servers)
    return server


def delete_custom_server(conn, server_id: str) -> dict[str, object]:
    """删除自定义 MCP Server（内置不可删除）。"""
    servers = get_servers(conn)
    target = str(server_id or "").strip()
    server = next((item for item in servers if item["id"] == target), None)
    if server is None:
        raise MCPConfigError("MCP Server 不存在")
    if server.get("builtin"):
        raise MCPConfigError("内置 MCP Server 不可删除，可在设置中关闭启用")
    remaining = [item for item in servers if item["id"] != target]
    _save_servers(conn, remaining)
    return server


def validate_enable(server: dict[str, object]) -> None:
    """启用前的静态校验（连接校验由调用方异步完成）。"""
    if not str(server.get("command") or "").strip():
        raise MCPConfigError("MCP Server 未配置启动命令，无法启用")
    if not server.get("builtin"):
        allowed = server.get("allowed_tools")
        if not isinstance(allowed, list) or not allowed:
            raise MCPConfigError(
                "自定义 MCP Server 必须显式配置 allowed_tools（非空）后才能启用"
            )


# ---------------------------------------------------------------------------
# 短连接 / 工具列表
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _session(server: dict[str, object]) -> AsyncIterator[ClientSession]:
    """建立一次 stdio MCP 会话（退出上下文即关闭子进程）。"""
    command = str(server.get("command") or "").strip()
    if not command:
        raise MCPError("MCP Server 未配置启动命令")
    args = [str(arg) for arg in server.get("args") or []]
    params = StdioServerParameters(
        command=command,
        args=args,
        env=effective_env(server),
        cwd=str(config.BASE_DIR),
    )
    try:
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
    except MCPError:
        raise


def _flatten_exception(exc: BaseException) -> str:
    """从异常（可能是 ExceptionGroup）里提取可读消息。"""
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            message = _flatten_exception(sub)
            if message:
                return message
        return ""
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _friendly_error(exc: BaseException, action: str = "连接") -> str:
    message = _flatten_exception(exc)
    lowered = message.lower()
    if isinstance(exc, FileNotFoundError) or "no such file or directory" in lowered:
        return f"MCP Server {action}失败：找不到启动命令，请检查 command 配置"
    if "permission denied" in lowered:
        return f"MCP Server {action}失败：没有执行权限，请检查 command 配置"
    if "timeout" in lowered or isinstance(exc, TimeoutError):
        return f"MCP Server {action}超时（{int(CONNECT_TIMEOUT_SECONDS)} 秒），请检查命令是否可正常运行"
    if "modulenotfound" in lowered or "no module named" in lowered:
        return f"MCP Server {action}失败：Python 模块不存在，请检查 args 配置（{message}）"
    if message:
        return f"MCP Server {action}失败：{truncate(message, 300)}"
    return f"MCP Server {action}失败，请检查配置"


async def _run_with_session(
    server: dict[str, object],
    callback: Callable[[ClientSession], Awaitable[Any]],
    timeout: float,
) -> Any:
    async def _runner() -> Any:
        async with _session(server) as session:
            return await callback(session)

    task = asyncio.ensure_future(_runner())
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
    except BaseException:
        task.cancel()
        raise
    if not done:
        # 超时：取消后等待清理（stdio_client 关闭子进程时的异常直接忽略）
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        raise MCPError(
            f"MCP Server 操作超时（{int(timeout)} 秒），请检查 Server 是否正常"
        )
    return task.result()


def _tool_read_only(server: dict[str, object], tool: types.Tool) -> bool:
    if server.get("builtin"):
        return True
    annotations = getattr(tool, "annotations", None)
    return bool(annotations is not None and getattr(annotations, "readOnlyHint", None))


def _normalize_tool(server: dict[str, object], tool: types.Tool) -> dict[str, object]:
    schema = getattr(tool, "inputSchema", None)
    if not isinstance(schema, dict) or not schema:
        schema = {"type": "object", "properties": {}}
    return {
        "name": str(tool.name),
        "description": str(getattr(tool, "description", None) or ""),
        "input_schema": schema,
        "read_only": _tool_read_only(server, tool),
    }


async def list_server_tools(
    server: dict[str, object], timeout: float = CONNECT_TIMEOUT_SECONDS
) -> dict[str, object]:
    """连接 server 并列出工具；返回 ``{ok, tools, error, elapsed_ms}``，不抛异常。"""
    started = time.perf_counter()
    try:
        result = await _run_with_session(
            server, lambda session: session.list_tools(), timeout
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # 连接失败不抛出，记录错误
        message = _friendly_error(exc)
        logger.info("MCP Server %s 连接失败：%s", server.get("id"), message)
        return {
            "ok": False,
            "tools": [],
            "error": message,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
    tools = [_normalize_tool(server, tool) for tool in result.tools]
    return {
        "ok": True,
        "tools": tools,
        "error": None,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


def qualified_tool_name(server_id: str, tool_name: str) -> str:
    def _sanitize(value: str) -> str:
        cleaned = _SAFE_NAME_RE.sub("_", str(value)).strip("_")
        return cleaned or "tool"

    return f"{_QUALIFIED_PREFIX}{_sanitize(server_id)}_{_sanitize(tool_name)}"[:64]


async def get_enabled_tools(conn=None) -> list[dict[str, object]]:
    """返回所有已启用 server 的可调用只读工具（最多 20 个）。"""
    servers = get_servers(conn) if conn is not None else _load_servers()
    enabled = [server for server in servers if server.get("enabled")]
    collected: list[dict[str, object]] = []
    for server in enabled:
        allowed = server.get("allowed_tools")
        if isinstance(allowed, list) and not allowed:
            continue
        result = await list_server_tools(server)
        if not result["ok"]:
            save_last_error(str(server["id"]), str(result.get("error") or "连接失败"))
            continue
        server_id = str(server["id"])
        for tool in result["tools"]:
            if tool.get("read_only") is False and not server.get("builtin"):
                continue
            if isinstance(allowed, list) and tool["name"] not in allowed:
                continue
            collected.append(
                {
                    "qualified_name": qualified_tool_name(server_id, str(tool["name"])),
                    "server_id": server_id,
                    "server_name": str(server.get("name") or server_id),
                    "tool_name": str(tool["name"]),
                    "description": str(tool.get("description") or ""),
                    "input_schema": tool.get("input_schema")
                    or {"type": "object", "properties": {}},
                    "read_only": bool(tool.get("read_only")),
                }
            )
            if len(collected) >= MAX_TOOLS:
                break
        if len(collected) >= MAX_TOOLS:
            break
    _QUALIFIED_INDEX.clear()
    for tool in collected:
        _QUALIFIED_INDEX[str(tool["qualified_name"])] = {
            "server_id": str(tool["server_id"]),
            "tool_name": str(tool["tool_name"]),
        }
    return collected


async def _rebuild_index(conn=None) -> None:
    await get_enabled_tools(conn)


def _resolve_qualified(qualified_name: str) -> dict[str, str] | None:
    return _QUALIFIED_INDEX.get(str(qualified_name or "").strip())


def _extract_result_text(result: types.CallToolResult) -> str:
    chunks: list[str] = []
    for item in result.content or []:
        if isinstance(item, types.TextContent):
            chunks.append(item.text)
        elif isinstance(item, types.ResourceLink):
            chunks.append(f"[资源] {item.uri}")
        elif isinstance(item, types.EmbeddedResource):
            resource = item.resource
            text = getattr(resource, "text", None)
            if text:
                chunks.append(str(text))
    if not chunks and result.structuredContent is not None:
        return json.dumps(result.structuredContent, ensure_ascii=False)
    return "\n".join(chunks).strip()


_EXEC_ERROR_PREFIX_RE = re.compile(r"^(?:Error executing tool [^:]+:\s*)", re.IGNORECASE)


def _clean_tool_error(text: str, fallback: str = "工具调用失败") -> str:
    cleaned = _EXEC_ERROR_PREFIX_RE.sub("", str(text or "").strip())
    if not cleaned:
        return fallback
    return truncate(cleaned, 500)


async def call_tool(
    conn,
    qualified_name: str,
    arguments: dict[str, object] | None = None,
) -> dict[str, object]:
    """调用一个 MCP 工具；返回 ``{ok, text, structured?, error?}``，不抛异常。"""
    name = str(qualified_name or "").strip()
    if not name:
        return {"ok": False, "text": "", "error": "MCP 工具名不能为空"}
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return {"ok": False, "text": "", "error": "工具参数必须是 JSON 对象"}
    entry = _resolve_qualified(name)
    if entry is None:
        try:
            await _rebuild_index(conn)
        except Exception:  # 重建失败按未知工具处理
            logger.debug("重建 MCP 工具索引失败", exc_info=True)
        entry = _resolve_qualified(name)
    if entry is None:
        return {
            "ok": False,
            "text": "",
            "error": f"未知的 MCP 工具：{name}（可能已禁用或未启用）",
        }
    servers = get_servers(conn) if conn is not None else _load_servers()
    server = next(
        (
            item
            for item in servers
            if str(item.get("id")) == entry.get("server_id")
        ),
        None,
    )
    if server is None:
        return {"ok": False, "text": "", "error": f"MCP Server 不存在：{entry.get('server_id')}"}
    if not server.get("enabled"):
        return {
            "ok": False,
            "text": "",
            "error": f"MCP Server 未启用：{server.get('name') or server.get('id')}",
        }
    server_id = str(server.get("id"))
    tool_name = str(entry.get("tool_name"))
    allowed = server.get("allowed_tools")
    if isinstance(allowed, list) and tool_name not in allowed:
        return {
            "ok": False,
            "text": "",
            "error": f"工具 {tool_name} 不在允许列表（allowed_tools）中",
        }
    try:
        result = await _run_with_session(
            server,
            lambda session: session.call_tool(tool_name, arguments),
            CALL_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = _friendly_error(exc, action="调用")
        save_last_error(server_id, message)
        return {"ok": False, "text": "", "error": message}
    text = _extract_result_text(result)[:MAX_RESULT_CHARS]
    structured = result.structuredContent
    if result.isError:
        save_last_error(server_id, _clean_tool_error(text))
        return {
            "ok": False,
            "text": text,
            "structured": structured,
            "error": _clean_tool_error(text),
        }
    if server.get("last_error"):
        save_last_error(server_id, None)
    return {"ok": True, "text": text, "structured": structured, "error": None}


def summarize_tool_result(outcome: dict[str, object]) -> str:
    """生成客户端的工具结果摘要（SSE tool end 事件使用）。"""
    if not outcome.get("ok"):
        return f"失败：{truncate(str(outcome.get('error') or '未知错误'), 160)}"
    structured = outcome.get("structured")
    if isinstance(structured, dict):
        data = structured
        for key, unit in (("row_count", "行"), ("count", "项")):
            value = data.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return f"返回 {value} {unit}"
        total = data.get("total_count")
        if isinstance(total, int) and not isinstance(total, bool):
            return f"共 {total} 条匹配"
        for key in ("items", "rows", "tables", "files", "entries"):
            value = data.get(key)
            if isinstance(value, list):
                return f"返回 {len(value)} 项"
        content = data.get("content")
        if isinstance(content, str):
            return f"读取 {len(content)} 字符"
    text = str(outcome.get("text") or "").strip()
    if not text:
        return "已返回空结果"
    compact = collapse_whitespace(text)
    return truncate(compact, 160) or "已返回结果"


def detect_tool_intent(question: str) -> bool:
    """关键词启发式判断问题是否可能需要 MCP 外部工具。"""
    text = str(question or "").strip().lower()
    if not text:
        return False
    if any(keyword.lower() in text for keyword in INTENT_KEYWORDS):
        return True
    return bool(_INTENT_EN_RE.search(text))


def has_enabled_servers(conn=None) -> bool:
    servers = get_servers(conn) if conn is not None else _load_servers()
    return any(server.get("enabled") for server in servers)


# ---------------------------------------------------------------------------
# 「AI 帮我配」：LLM 只选 catalog id，命令 / 参数由服务端目录构造
# ---------------------------------------------------------------------------

SUGGEST_SYSTEM_PROMPT = (
    "你是「Synapse 知脉」的 MCP 工具推荐助手。用户想通过 AI 配置（只读 / 低风险）MCP 工具。\n"
    "硬性规则：\n"
    "1) 只能从下面的「安全目录」中选择 id，禁止发明目录外的工具；\n"
    "2) 禁止输出任何命令、包安装步骤、环境变量值或工具参数以外的内容；"
    "后端会使用目录中的固定命令与参数模板；\n"
    "3) 最多选择 {max_items} 个最匹配的 id，按相关度排序；都不匹配时 suggestions 返回空数组；\n"
    "4) rationale 用一句话中文说明理由（≤40 字）；\n"
    "5) 只输出严格 JSON（不要 Markdown 代码块、不要额外解释）。\n\n"
    "安全目录（id / 类型 / 名称 / 说明）：\n{catalog}\n\n"
    "输出格式：\n"
    '{{"suggestions":[{{"catalog_id":"<目录 id>","rationale":"<中文理由>","params":{{}}}}],'
    '"notes":"<可选补充说明>"}}\n'
    "params 仅 git 支持 repository_path（用户需求里明确给出本地路径时才填，否则留空由用户在界面填写）。"
)


def _catalog_prompt_text() -> str:
    lines: list[str] = []
    for catalog_id, entry in MCP_SUGGEST_CATALOG.items():
        kind = "内置（启用现有 preset）" if entry.get("kind") == "builtin" else "自定义 stdio"
        line = f"- {catalog_id}｜{kind}｜{entry.get('name')}：{entry.get('description')}"
        params = entry.get("params") or {}
        if isinstance(params, dict) and params:
            names = "、".join(str(name) for name in params)
            line += f"（可填参数：{names}）"
        env_keys = entry.get("env_keys") or []
        if env_keys:
            line += f"（环境变量：{'、'.join(str(key) for key in env_keys)}）"
        lines.append(line)
    return "\n".join(lines)


def _extract_json_object(text: str) -> dict[str, object]:
    """从 LLM 文本中提取第一个 JSON 对象（容忍代码块 / 前后解释）。"""
    raw = str(text or "").strip()
    if not raw:
        raise MCPError("AI 未返回推荐内容，请稍后重试")
    if raw.startswith("```"):
        lines = [
            line
            for line in raw.splitlines()
            if not line.strip().startswith("```")
        ]
        raw = "\n".join(lines).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise MCPError("AI 返回的推荐结果不是有效 JSON，请稍后重试")
    try:
        parsed = json.loads(raw[start : end + 1])
    except (TypeError, ValueError) as exc:
        raise MCPError("AI 返回的推荐结果不是有效 JSON，请稍后重试") from exc
    if not isinstance(parsed, dict):
        raise MCPError("AI 返回的推荐结果格式不正确，请稍后重试")
    return parsed


def _validate_path_param(value: object) -> str | None:
    """校验路径类参数：必须是项目目录 / data 目录内的绝对路径，且无 shell 元字符。"""
    text = str(value or "").strip()
    if not text or len(text) > 300:
        return None
    if any(char in text for char in ";&|`$<>\n\r\"'\\*?[]{}"):
        return None
    if text.startswith("~") or ".." in Path(text).parts:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        return None
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    allowed_roots = (config.data_dir().resolve(), config.PROJECT_ROOT.resolve())
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        return None
    return str(resolved)


def _render_custom_args(
    entry: dict[str, object], params: dict[str, object]
) -> list[str]:
    """用 catalog 参数模板渲染 args；非法 / 缺失参数保留占位符（由用户在 UI 填写）。"""
    specs = entry.get("params")
    spec_map = specs if isinstance(specs, dict) else {}
    values: dict[str, str] = {}
    for key, spec in spec_map.items():
        if not isinstance(spec, dict):
            continue
        raw_value = params.get(str(key))
        if raw_value is None:
            continue
        kind = str(spec.get("kind") or "text")
        if kind == "path":
            validated = _validate_path_param(raw_value)
        else:
            text = str(raw_value).strip()
            validated = (
                text
                if text and len(text) <= 200 and not any(
                    char in text for char in ";&|`$<>\n\r\"'\\"
                )
                else None
            )
        if validated is not None:
            values[str(key)] = validated
    rendered: list[str] = []
    for template in entry.get("args") or []:
        text = str(template)
        for key, spec in spec_map.items():
            if not isinstance(spec, dict):
                continue
            placeholder = str(spec.get("placeholder") or f"<{key}>")
            text = text.replace("{" + str(key) + "}", values.get(str(key), placeholder))
        rendered.append(text)
    return rendered


def _build_catalog_suggestion(
    catalog_id: str,
    entry: dict[str, object],
    rationale: str,
    params: dict[str, object],
) -> dict[str, object]:
    """按 catalog 构造一条建议（builtin 只给 server_id；custom 给目录内 command/args）。"""
    suggestion: dict[str, object] = {
        "catalog_id": catalog_id,
        "kind": str(entry.get("kind") or "custom"),
        "name": str(entry.get("name") or catalog_id),
        "description": str(entry.get("description") or ""),
        "rationale": rationale,
    }
    if entry.get("kind") == "builtin":
        suggestion["server_id"] = str(entry.get("server_id") or catalog_id)
    else:
        suggestion["command"] = str(entry.get("command") or "")
        suggestion["args"] = _render_custom_args(entry, params)
    suggestion["env_keys"] = [str(key) for key in entry.get("env_keys") or []]
    suggestion["requires_token"] = bool(entry.get("requires_token"))
    suggestion["test_note"] = str(entry.get("test_note") or "")
    return suggestion


async def suggest_servers(
    need: str,
    max_suggestions: int = 2,
    llm_config: dict[str, object] | None = None,
) -> dict[str, object]:
    """用 LLM 从安全目录中挑选 MCP Server 建议。

    - LLM 只返回 catalog id / rationale / 少量参数，命令与参数模板全部来自服务端 catalog；
    - 未知 id、重复 id、非法参数一律过滤；
    - **不写入配置、不执行任何命令**。
    """
    text = str(need or "").strip()
    if not text:
        raise MCPConfigError("需求描述不能为空")
    if len(text) > SUGGEST_NEED_MAX_CHARS:
        text = text[:SUGGEST_NEED_MAX_CHARS]
    try:
        limit = max(1, min(int(max_suggestions), SUGGEST_MAX_ITEMS))
    except (TypeError, ValueError):
        limit = 2
    cfg = llm_config if llm_config is not None else llm.get_config()
    if not llm.is_configured(cfg):
        raise MCPConfigError("请先在设置接入模型，或手动添加")

    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": SUGGEST_SYSTEM_PROMPT.format(
                max_items=limit, catalog=_catalog_prompt_text()
            ),
        },
        {"role": "user", "content": f"用户需求：{text}"},
    ]
    payload = await llm.chat_completion_async(
        cfg,
        messages,
        max_tokens=SUGGEST_MAX_TOKENS,
        temperature=0.1,
        timeout=SUGGEST_TIMEOUT_SECONDS,
    )
    data = _extract_json_object(llm.extract_message_text(payload))
    raw_items = data.get("suggestions")
    if not isinstance(raw_items, list):
        raise MCPError("AI 返回的推荐结果格式不正确，请稍后重试")

    suggestions: list[dict[str, object]] = []
    seen: set[str] = set()
    unknown: list[str] = []
    duplicated = 0
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        catalog_id = str(item.get("catalog_id") or item.get("id") or "").strip()
        if not catalog_id:
            continue
        entry = MCP_SUGGEST_CATALOG.get(catalog_id)
        if entry is None:
            unknown.append(catalog_id[:40])
            continue
        if catalog_id in seen:
            duplicated += 1
            continue
        seen.add(catalog_id)
        if len(suggestions) >= limit:
            continue
        raw_params = item.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        rationale = str(item.get("rationale") or "").strip()
        rationale = truncate(collapse_whitespace(rationale), 120)
        if not rationale:
            rationale = f"匹配你的需求：{entry.get('name')}"
        suggestions.append(
            _build_catalog_suggestion(catalog_id, entry, rationale, params)
        )

    notes_parts: list[str] = []
    model_notes = str(data.get("notes") or "").strip()
    if model_notes:
        notes_parts.append(truncate(collapse_whitespace(model_notes), 200))
    if unknown:
        notes_parts.append(f"已忽略 {len(unknown)} 条不在安全目录中的建议")
    if duplicated:
        notes_parts.append(f"已去重 {duplicated} 条重复建议")
    if not suggestions:
        notes_parts.append("AI 未给出匹配建议，可换个描述，或手动添加自定义 MCP Server")
    notes_parts.append("以上建议仅用于预填，未执行任何命令、未修改配置")
    return {
        "need": text,
        "suggestions": suggestions,
        "notes": "；".join(notes_parts),
    }
