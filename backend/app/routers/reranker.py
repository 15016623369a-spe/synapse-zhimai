"""反馈偏好（规则化 Reranker）API：偏好摘要，不修改/不删除用户反馈。"""

from __future__ import annotations

from fastapi import APIRouter

from .. import db
from ..services import rerank

router = APIRouter(tags=["reranker"])


@router.get("/reranker/summary")
def reranker_summary() -> dict[str, object]:
    """反馈偏好摘要：accepted/rejected 计数、top domains、规则说明。"""
    with db.get_db() as conn:
        return rerank.summary(conn)
