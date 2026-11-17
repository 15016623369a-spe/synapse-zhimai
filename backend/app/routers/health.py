"""健康检查与元信息。"""

from __future__ import annotations

from fastapi import APIRouter

from .. import config, db
from ..services import embedding, llm, ocr, search

router = APIRouter()


@router.get("/health")
def health() -> dict[str, object]:
    with db.get_db() as conn:
        documents = db.table_count(conn, "documents")
        chunks = db.table_count(conn, "chunks")
        concepts = db.table_count(conn, "concepts")
    search_status = search.search_status()
    return {
        "status": "ok",
        "version": config.VERSION,
        "documents": documents,
        "chunks": chunks,
        "concepts": concepts,
        "llm": llm.public_config(),
        "embedding": embedding.provider_info(),
        "ocr": ocr.ocr_engine_info(),
        "search": {
            "provider": search_status["provider"],
            "enabled": search_status["enabled"],
        },
    }
