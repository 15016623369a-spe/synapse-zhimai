"""个人计划与打卡 API（支持分组与单日行程）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .. import db
from ..schemas import CheckinIn, PlanCreateIn, PlanUpdateIn
from ..services import plan_groups, plans

router = APIRouter()


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, plan_groups.PlanGroupNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, plan_groups.PlanGroupError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    assert isinstance(exc, plans.PlanError)
    if isinstance(exc, (plans.PlanNotFound, plans.CheckinNotFound)):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, plans.DuplicateCheckin):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/plans")
def list_plans(
    include_archived: int = Query(0, ge=0, le=1),
    kind: str = Query("all", description="all | recurring | once"),
) -> dict[str, object]:
    try:
        with db.get_db() as conn:
            return plans.list_plans(
                conn, include_archived=bool(include_archived), kind=kind
            )
    except plans.PlanError as exc:
        _raise_http(exc)
        raise  # pragma: no cover


@router.post("/plans")
def create_plan(payload: PlanCreateIn) -> dict[str, object]:
    try:
        with db.get_db() as conn:
            return plans.create_plan(conn, payload.model_dump())
    except (plans.PlanError, plan_groups.PlanGroupError) as exc:
        _raise_http(exc)
        raise  # pragma: no cover - _raise_http 总是抛异常


@router.get("/plans/overview")
def plans_overview(
    days: int = Query(120, ge=30, le=366),
) -> dict[str, object]:
    with db.get_db() as conn:
        return plans.overview(conn, days=days)


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str) -> dict[str, object]:
    try:
        with db.get_db() as conn:
            return plans.get_plan(conn, plan_id)
    except plans.PlanError as exc:
        _raise_http(exc)
        raise  # pragma: no cover


@router.put("/plans/{plan_id}")
def update_plan(plan_id: str, payload: PlanUpdateIn) -> dict[str, object]:
    try:
        with db.get_db() as conn:
            return plans.update_plan(
                conn, plan_id, payload.model_dump(), set(payload.model_fields_set)
            )
    except (plans.PlanError, plan_groups.PlanGroupError) as exc:
        _raise_http(exc)
        raise  # pragma: no cover


@router.delete("/plans/{plan_id}")
def delete_plan(plan_id: str) -> dict[str, object]:
    try:
        with db.get_db() as conn:
            deleted = plans.delete_plan(conn, plan_id)
    except plans.PlanError as exc:
        _raise_http(exc)
        raise  # pragma: no cover
    return {"ok": True, "deleted_checkins": deleted}


@router.post("/plans/{plan_id}/checkins")
def create_checkin(plan_id: str, payload: CheckinIn | None = None) -> dict[str, object]:
    date_value = payload.date if payload is not None else None
    note = payload.note if payload is not None else None
    try:
        with db.get_db() as conn:
            return plans.create_checkin(conn, plan_id, date_value, note)
    except plans.PlanError as exc:
        _raise_http(exc)
        raise  # pragma: no cover


@router.delete("/plans/{plan_id}/checkins/{date}")
def undo_checkin(plan_id: str, date: str) -> dict[str, object]:
    try:
        with db.get_db() as conn:
            return plans.undo_checkin(conn, plan_id, date)
    except plans.PlanError as exc:
        _raise_http(exc)
        raise  # pragma: no cover


@router.get("/plans/{plan_id}/checkins")
def list_checkins(
    plan_id: str,
    start: str | None = Query(None, description="起始日期 YYYY-MM-DD"),
    end: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
) -> dict[str, object]:
    try:
        with db.get_db() as conn:
            return plans.list_checkins(conn, plan_id, start, end)
    except plans.PlanError as exc:
        _raise_http(exc)
        raise  # pragma: no cover
