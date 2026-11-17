"""检索。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..schemas import SearchIn
from ..services import retrieval

router = APIRouter()


@router.post("/search")
def search(payload: SearchIn) -> dict[str, object]:
    query = (payload.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="请输入查询内容")
    doc_ids = [str(doc_id) for doc_id in payload.doc_ids] if payload.doc_ids else None
    return retrieval.search(query, payload.top_k, doc_ids)
