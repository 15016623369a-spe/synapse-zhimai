"""Synapse · 知脉 FastAPI 入口。

- 所有业务 API 挂在 ``/api``；
- 生产模式托管 ``frontend/dist``，未知路径回退 SPA ``index.html``；
- 启动时初始化 SQLite，并在文档为空时自动载入 ``data/demo_docs/*.md``。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, db
from .routers import (
    admin,
    chat,
    conversations,
    documents,
    extend,
    graph,
    health,
    mcp,
    embedding,
    patrol,
    reranker,
    plan_groups,
    plans,
    search,
    settings,
    stats,
    zones,
)
from .services import patrol as patrol_service, seed

logger = logging.getLogger("synapse")

#: frontend/dist 未提供 favicon 时的内置品牌图标（神经节点），避免浏览器请求 404
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#05070D"/>'
    '<g stroke="#4FD8FF" stroke-width="2" opacity="0.75">'
    '<line x1="32" y1="18" x2="16" y2="40"/><line x1="32" y1="18" x2="48" y2="40"/>'
    '<line x1="16" y1="40" x2="48" y2="40"/></g>'
    '<circle cx="32" cy="18" r="6" fill="#4FD8FF"/>'
    '<circle cx="16" cy="40" r="5" fill="#A78BFA"/>'
    '<circle cx="48" cy="40" r="5" fill="#FBBF24"/>'
    "</svg>"
)


def _configure_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _configure_logging()
    logger.info("Synapse 后端启动中（数据目录：%s）", config.data_dir())
    db.init_db()
    try:
        outcome = seed.seed_if_empty()
        if outcome is not None:
            logger.info("启动自动载入演示知识库：%s", outcome)
    except Exception:  # 种子失败不能阻塞服务启动
        logger.exception("自动载入演示知识库失败，服务继续启动")
    try:
        if seed.ensure_concept_index():
            logger.info("概念抽取规则已升级，概念与图谱已按当前版本重建")
    except Exception:  # 迁移失败不能阻塞服务启动
        logger.exception("概念索引迁移失败，服务继续启动")
    with db.get_db() as conn:
        documents = db.table_count(conn, "documents")
        chunks = db.table_count(conn, "chunks")
        concepts = db.table_count(conn, "concepts")
    logger.info("Synapse 就绪：%d 篇文档 / %d 个分块 / %d 个概念", documents, chunks, concepts)
    try:
        await patrol_service.start_scheduler()
    except Exception:  # 调度器失败不能阻塞服务启动
        logger.exception("知识巡检调度器启动失败，服务继续运行")
    yield
    try:
        await patrol_service.stop_scheduler()
    except Exception:
        logger.exception("知识巡检调度器停止失败")
    logger.info("Synapse 后端停止")


def _register_routers(app: FastAPI) -> None:
    for module in (
        health,
        mcp,
        embedding,
        patrol,
        reranker,
        plan_groups,
        plans,
        documents,
        extend,
        search,
        conversations,
        chat,
        graph,
        stats,
        settings,
        admin,
        zones,
    ):
        app.include_router(module.router, prefix="/api")


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(_request, _exc) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "请求参数不合法，请检查后重试"})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(_request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if detail == "Not Found":
            detail = "接口不存在"
        elif detail == "Method Not Allowed":
            detail = "请求方法不被支持"
        return JSONResponse(status_code=exc.status_code, content={"detail": detail})


def _register_frontend(app: FastAPI) -> None:
    """静态托管 frontend/dist；不存在时返回友好 JSON（不 500）。"""

    @app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
    def favicon():
        dist_favicon = config.frontend_dist_dir() / "favicon.ico"
        if dist_favicon.is_file():
            return FileResponse(dist_favicon)
        return Response(
            content=FAVICON_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa_fallback(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="接口不存在")
        dist_dir = config.frontend_dist_dir()
        index = dist_dir / "index.html"
        if not index.is_file():
            return JSONResponse(
                status_code=200,
                content={
                    "status": "ok",
                    "message": "Synapse 后端已启动；前端尚未构建（frontend/dist 不存在）。",
                    "api": "/api/health",
                    "version": config.VERSION,
                },
            )
        if full_path:
            try:
                candidate = (dist_dir / full_path).resolve()
                candidate.relative_to(dist_dir.resolve())
            except (ValueError, OSError):
                raise HTTPException(status_code=404, detail="资源不存在") from None
            if candidate.is_file():
                return FileResponse(candidate)
            if Path(full_path).suffix:
                raise HTTPException(status_code=404, detail="资源不存在")
        return FileResponse(index)


def create_app() -> FastAPI:
    _configure_logging()
    app = FastAPI(
        title="Synapse · 知脉 API",
        version=config.VERSION,
        description="把个人文档变成可对话、可漫游的第二大脑。",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    _register_routers(app)
    _register_error_handlers(app)
    _register_frontend(app)
    return app


app = create_app()
