"""知识图谱：全局图谱 / 分区图谱 / 节点详情。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .. import db
from ..services import graph, zones

router = APIRouter()


@router.get("/graph")
def get_graph(
    mode: str = Query("global", description="global | zones"),
    max_concepts: int = Query(120, ge=1, le=500),
    min_link_weight: float = Query(0.15, ge=0.0, le=10.0),
    min_concept_degree: int = Query(0, ge=0, le=50),
) -> dict[str, object]:
    normalized_mode = (mode or "global").strip().lower()
    if normalized_mode not in {"global", "zones"}:
        raise HTTPException(
            status_code=400, detail="不支持的模式：仅支持 global 或 zones"
        )
    with db.get_db() as conn:
        if normalized_mode == "zones":
            return graph.build_zones_graph(
                conn, max_concepts, min_link_weight, min_concept_degree
            )
        return graph.build_graph(conn, max_concepts, min_link_weight, min_concept_degree)


@router.get("/graph/node")
def get_graph_node(
    id: str = Query(..., description="节点 id，如 concept:cv_xxx 或 concept:cv_xxx@z_xxx"),
    zone_id: str | None = Query(None, description="分区作用域：z_xxx 或 unassigned"),
) -> dict[str, object]:
    with db.get_db() as conn:
        try:
            detail = graph.get_node(conn, id, zone_id)
        except zones.ZoneNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="节点不存在")
    return detail
