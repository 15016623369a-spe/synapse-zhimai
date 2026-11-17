"""计划分组 API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import db
from ..schemas import PlanGroupCreateIn, PlanGroupUpdateIn
from ..services import plan_groups

router = APIRouter()


def _raise_http(exc: plan_groups.PlanGroupError) -> None:
    if isinstance(exc, plan_groups.PlanGroupNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/plan-groups")
def list_plan_groups() -> dict[str, object]:
    with db.get_db() as conn:
        return plan_groups.list_groups(conn)


@router.post("/plan-groups")
def create_plan_group(payload: PlanGroupCreateIn) -> dict[str, object]:
    try:
        with db.get_db() as conn:
            group = plan_groups.create_group(
                conn,
                name=payload.name,
                color=payload.color,
                description=payload.description,
            )
    except plan_groups.PlanGroupError as exc:
        _raise_http(exc)
        raise  # pragma: no cover - _raise_http 总是抛异常
    return {"group": group}


@router.put("/plan-groups/{group_id}")
def update_plan_group(group_id: str, payload: PlanGroupUpdateIn) -> dict[str, object]:
    provided = payload.model_fields_set
    try:
        with db.get_db() as conn:
            group = plan_groups.update_group(
                conn,
                group_id,
                name=payload.name if "name" in provided else plan_groups.UNSET,
                color=payload.color if "color" in provided else plan_groups.UNSET,
                description=payload.description
                if "description" in provided
                else plan_groups.UNSET,
                sort_order=payload.sort_order
                if "sort_order" in provided
                else plan_groups.UNSET,
            )
    except plan_groups.PlanGroupError as exc:
        _raise_http(exc)
        raise  # pragma: no cover
    return {"group": group}


@router.delete("/plan-groups/{group_id}")
def delete_plan_group(group_id: str) -> dict[str, object]:
    try:
        with db.get_db() as conn:
            ungrouped = plan_groups.delete_group(conn, group_id)
    except plan_groups.PlanGroupError as exc:
        _raise_http(exc)
        raise  # pragma: no cover
    return {"ok": True, "ungrouped_count": ungrouped}
