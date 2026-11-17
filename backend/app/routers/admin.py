"""管理接口：重置演示数据。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import db

from ..schemas import EnrichIn, ReseedIn
from ..services import enrich, seed

router = APIRouter()


@router.get("/admin/enrich/status")
def enrich_status() -> dict[str, object]:
    with db.get_db() as conn:
        return enrich.enrich_status(conn)


@router.post("/admin/enrich")
def run_enrich(payload: EnrichIn) -> dict[str, object]:
    try:
        return enrich.run_enrich(
            scope=payload.scope,
            doc_ids=payload.doc_ids,
            force=payload.force,
            limit=payload.limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/reseed")
def reseed(payload: ReseedIn | None = None) -> dict[str, object]:
    if not seed.demo_files():
        raise HTTPException(status_code=400, detail="演示文档目录为空，无法重置")
    result = seed.reset_demo()
    return {"ok": True, "imported": result["imported"], "skipped": result["skipped"]}
