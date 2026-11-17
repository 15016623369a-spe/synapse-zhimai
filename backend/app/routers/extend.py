"""联网知识扩展 API：状态 / 搜索候选 / 审核入库 / 忽略反馈。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import db
from ..schemas import ExtendCommitIn, ExtendRejectIn, ExtendSearchIn
from ..services import extend, llm, search, zones

router = APIRouter()


@router.get("/extend/status")
def extend_status() -> dict[str, object]:
    with db.get_db() as conn:
        return extend.status_info(conn)


@router.post("/extend/search")
def extend_search(payload: ExtendSearchIn) -> dict[str, object]:
    try:
        with db.get_db() as conn:
            return extend.collect_candidates(
                conn,
                payload.question,
                queries=payload.queries,
                max_results=payload.max_results,
                llm_config=llm.get_config(),
                conversation_id=payload.conversation_id,
            )
    except search.SearchUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except extend.ExtendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/extend/commit")
def extend_commit(payload: ExtendCommitIn) -> dict[str, object]:
    items = [item.model_dump() for item in payload.items]
    try:
        with db.get_db() as conn:
            return extend.commit_candidates(conn, items, zone_id=payload.zone_id)
    except zones.ZoneNotFound as exc:
        raise HTTPException(status_code=404, detail="分区不存在") from exc
    except extend.ExtendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/extend/reject")
def extend_reject(payload: ExtendRejectIn) -> dict[str, object]:
    items = [item.model_dump() for item in payload.items]
    try:
        with db.get_db() as conn:
            return extend.reject_candidates(conn, items, query=payload.query)
    except extend.ExtendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
