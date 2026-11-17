"""知识巡检 API：状态 / 配置 / 立即执行。

- ``GET  /api/patrol/status``：配置 + 运行状态 + 上次结果 + 知识库计数；
- ``PATCH /api/patrol/config``：部分更新（校验后持久化到 settings），返回状态；
- ``POST /api/patrol/run``：同步执行一次巡检（可能 10–60s）；运行中返回 409。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db
from ..services import patrol as patrol_service

router = APIRouter(tags=["patrol"])


class PatrolConfigIn(BaseModel):
    """巡检配置部分更新；``None`` / 省略表示不修改。"""

    enabled: bool | None = None
    interval_hours: int | None = None
    auto_enrich: bool | None = None
    max_enrich: int | None = None


@router.get("/patrol/status")
def patrol_status() -> dict[str, object]:
    with db.get_db() as conn:
        return patrol_service.status(conn)


@router.patch("/patrol/config")
def patrol_config(payload: PatrolConfigIn) -> dict[str, object]:
    updates = payload.model_dump(exclude_none=True)
    try:
        with db.get_db() as conn:
            patrol_service.update_config(conn, updates)
    except patrol_service.PatrolConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with db.get_db() as conn:
        return patrol_service.status(conn)


@router.post("/patrol/run")
def patrol_run() -> dict[str, object]:
    outcome = patrol_service.run_patrol("manual")
    if outcome.get("running"):
        raise HTTPException(
            status_code=409,
            detail=str(outcome.get("message") or "知识巡检正在运行中，请稍后再试"),
        )
    with db.get_db() as conn:
        current = patrol_service.status(conn)
    return {
        "ok": bool(outcome.get("ok")),
        "result": outcome.get("result"),
        "status": current,
    }
