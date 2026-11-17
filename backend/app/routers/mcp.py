"""MCP 工具接入 API（V2）。

接口（单用户本地服务，无鉴权；所有响应都不返回 token 明文）：
- ``GET  /api/mcp/servers``            列表 + 已启用 server 的连接状态 / 工具
- ``PATCH /api/mcp/servers/{id}``      启用/停用、allowed_tools、env（GitHub token 只在此传）
- ``POST /api/mcp/servers``            创建自定义 stdio MCP Server（默认 disabled）
- ``DELETE /api/mcp/servers/{id}``     删除自定义 server（内置不可删）
- ``POST /api/mcp/servers/{id}/test``  连接并列出工具（不要求已启用）
- ``POST /api/mcp/suggest``            「AI 帮我配」：LLM 只能从服务端安全目录选 id，
  后端用 catalog 构造 command/args（不写配置、不执行命令、不泄露 token）
- ``GET  /api/mcp/status``             简版状态（enabled_count / tool_count）
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..services import llm, mcp as mcp_service

logger = logging.getLogger("synapse.routers.mcp")

router = APIRouter(tags=["mcp"])


class MCPServerPatchIn(BaseModel):
    """PATCH 入参：只传需要修改的字段；``env`` 中空串表示不修改、null 表示清除。"""

    enabled: bool | None = None
    allowed_tools: list[str] | None = None
    env: dict[str, str | None] | None = None


class MCPServerCreateIn(BaseModel):
    """创建自定义 stdio MCP Server。"""

    name: str = Field(..., description="显示名称")
    command: str = Field(..., description="可执行命令")
    args: list[str] = Field(default_factory=list)
    env: dict[str, str | None] | None = None
    allowed_tools: list[str] | None = None


class MCPSuggestIn(BaseModel):
    """「AI 帮我配」入参：自然语言需求 + 期望建议条数。"""

    need: str = Field(..., description="用户想用 MCP 工具完成什么")
    max_suggestions: int = Field(default=2, description="最多返回建议数（1-3）")


def _server_or_404(conn, server_id: str) -> dict[str, object]:
    server = mcp_service.find_server(conn, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP Server 不存在")
    return server


async def _connect_and_tools(
    server: dict[str, object]
) -> dict[str, object]:
    result = await mcp_service.list_server_tools(server)
    if result["ok"]:
        mcp_service.save_last_error(str(server["id"]), None)
    else:
        mcp_service.save_last_error(
            str(server["id"]), str(result.get("error") or "连接失败")
        )
    return result


@router.get("/mcp/servers")
async def list_servers() -> dict[str, object]:
    with db.get_db() as conn:
        servers = mcp_service.get_servers(conn)
    items: list[dict[str, object]] = []
    enabled_count = 0
    tool_count = 0
    for server in servers:
        tools: list[dict[str, object]] = []
        connected = False
        if server.get("enabled"):
            enabled_count += 1
            result = await _connect_and_tools(server)
            connected = bool(result["ok"])
            tools = list(result.get("tools") or [])
            tool_count += len(tools)
        items.append(
            mcp_service.public_server(server, connected=connected, tools=tools)
        )
    return {"items": items, "enabled_count": enabled_count, "tool_count": tool_count}


@router.patch("/mcp/servers/{server_id}")
async def patch_server(server_id: str, payload: MCPServerPatchIn) -> dict[str, object]:
    updates = payload.model_dump(exclude_unset=True)
    with db.get_db() as conn:
        server = _server_or_404(conn, server_id)
        try:
            candidate = mcp_service.preview_patch(server, updates)
        except mcp_service.MCPConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 启用前必须做一次真实连接校验；失败时保持关闭并记录 last_error
    if candidate.get("enabled"):
        result = await _connect_and_tools(candidate)
        tools = list(result.get("tools") or [])
        if not result["ok"]:
            message = str(result.get("error") or "连接失败")
            candidate["enabled"] = False
            candidate["last_error"] = message
            with db.get_db() as write_conn:
                saved = mcp_service.persist_server(write_conn, candidate)
            logger.info("MCP Server %s 启用失败：%s", server_id, message)
            return {
                "ok": False,
                "error": f"连接失败，已保持关闭：{message}",
                "server": mcp_service.public_server(saved),
            }
        tool_names = {str(tool.get("name")) for tool in tools}
        allowed = candidate.get("allowed_tools")
        if isinstance(allowed, list) and allowed:
            unknown = [name for name in allowed if name not in tool_names]
            if unknown:
                message = f"允许列表包含不存在的工具：{'、'.join(unknown)}"
                candidate["enabled"] = False
                candidate["last_error"] = message
                with db.get_db() as write_conn:
                    saved = mcp_service.persist_server(write_conn, candidate)
                return {
                    "ok": False,
                    "error": message,
                    "server": mcp_service.public_server(saved),
                }
        candidate["last_error"] = None
        with db.get_db() as write_conn:
            saved = mcp_service.persist_server(write_conn, candidate)
        return {
            "ok": True,
            "error": None,
            "server": mcp_service.public_server(
                saved, connected=True, tools=tools
            ),
        }

    with db.get_db() as write_conn:
        saved = mcp_service.persist_server(write_conn, candidate)
    mcp_service.save_last_error(server_id, None)
    return {
        "ok": True,
        "error": None,
        "server": mcp_service.public_server(saved),
    }


@router.post("/mcp/servers")
async def create_server(payload: MCPServerCreateIn) -> dict[str, object]:
    with db.get_db() as conn:
        try:
            server = mcp_service.create_custom_server(
                conn,
                name=payload.name,
                command=payload.command,
                args=payload.args,
                env=payload.env,
                allowed_tools=payload.allowed_tools,
            )
        except mcp_service.MCPConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "error": None,
        "server": mcp_service.public_server(server),
    }


@router.delete("/mcp/servers/{server_id}")
async def delete_server(server_id: str) -> dict[str, object]:
    with db.get_db() as conn:
        server = _server_or_404(conn, server_id)
        if server.get("builtin"):
            raise HTTPException(
                status_code=400, detail="内置 MCP Server 不可删除，可在设置中关闭启用"
            )
        try:
            removed = mcp_service.delete_custom_server(conn, server_id)
        except mcp_service.MCPConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "error": None, "id": str(removed.get("id") or server_id)}


@router.post("/mcp/servers/{server_id}/test")
async def test_server(server_id: str) -> dict[str, object]:
    with db.get_db() as conn:
        server = _server_or_404(conn, server_id)
    result = await _connect_and_tools(server)
    return {
        "ok": bool(result["ok"]),
        "error": result.get("error"),
        "server_id": str(server.get("id") or server_id),
        "name": str(server.get("name") or ""),
        "tools": list(result.get("tools") or []),
        "tool_count": len(result.get("tools") or []),
        "elapsed_ms": int(result.get("elapsed_ms") or 0),
    }


@router.post("/mcp/suggest")
async def suggest(payload: MCPSuggestIn) -> dict[str, object]:
    """「AI 帮我配」：从服务端安全目录推荐 MCP Server（不写配置、不执行命令）。"""
    need = str(payload.need or "").strip()
    if not need:
        raise HTTPException(status_code=400, detail="需求描述不能为空")
    llm_config = llm.get_config()
    if not llm.is_configured(llm_config):
        raise HTTPException(
            status_code=503, detail="请先在设置接入模型，或手动添加"
        )
    try:
        result = await mcp_service.suggest_servers(
            need, payload.max_suggestions, llm_config=llm_config
        )
    except llm.LLMError as exc:
        logger.warning("MCP AI 建议生成失败（LLM）：%s", exc)
        raise HTTPException(
            status_code=502, detail="AI 建议生成失败（模型调用异常），请稍后重试"
        ) from exc
    except mcp_service.MCPConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except mcp_service.MCPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # 建议失败绝不影响 MCP 主流程
        logger.exception("MCP AI 建议生成异常：%s", exc)
        raise HTTPException(
            status_code=502, detail="AI 建议生成失败，请稍后重试"
        ) from exc
    return {"ok": True, **result}


@router.get("/mcp/status")
async def status() -> dict[str, object]:
    with db.get_db() as conn:
        servers = mcp_service.get_servers(conn)
    enabled = [server for server in servers if server.get("enabled")]
    tools = await mcp_service.get_enabled_tools()
    return {
        "enabled_count": len(enabled),
        "tool_count": len(tools),
        "servers_count": len(servers),
        "enabled": bool(enabled),
    }
