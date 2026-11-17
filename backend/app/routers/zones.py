"""分区（Zones）CRUD。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import db
from ..schemas import ZoneCreateIn, ZoneUpdateIn
from ..services import zones

router = APIRouter()


@router.get("/zones")
def list_zones() -> dict[str, object]:
    with db.get_db() as conn:
        return {
            "items": zones.list_zones(conn),
            "unassigned_count": zones.unassigned_count(conn),
        }


@router.post("/zones")
def create_zone(payload: ZoneCreateIn) -> dict[str, object]:
    try:
        with db.get_db() as conn:
            zone = zones.create_zone(
                conn,
                name=payload.name,
                color=payload.color,
                description=payload.description,
            )
    except zones.ZoneError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"zone": zone}


@router.put("/zones/{zone_id}")
def update_zone(zone_id: str, payload: ZoneUpdateIn) -> dict[str, object]:
    provided = payload.model_fields_set
    try:
        with db.get_db() as conn:
            zone = zones.update_zone(
                conn,
                zone_id,
                name=payload.name if "name" in provided else zones.UNSET,
                color=payload.color if "color" in provided else zones.UNSET,
                description=payload.description
                if "description" in provided
                else zones.UNSET,
            )
    except zones.ZoneNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except zones.ZoneError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"zone": zone}


@router.delete("/zones/{zone_id}")
def delete_zone(zone_id: str) -> dict[str, object]:
    try:
        with db.get_db() as conn:
            remaining = zones.delete_zone(conn, zone_id)
    except zones.ZoneNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "unassigned_count": remaining}
