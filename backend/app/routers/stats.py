"""统计信息。"""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter

from .. import db
from ..services import llm
from ..services import plans as plans_service
from ..utils import parse_tags_json

router = APIRouter()


@router.get("/stats")
def stats() -> dict[str, object]:
    with db.get_db() as conn:
        documents = db.table_count(conn, "documents")
        chunks = db.table_count(conn, "chunks")
        concepts = db.table_count(conn, "concepts")
        links = db.table_count(conn, "links")
        zones_count = db.table_count(conn, "zones")
        rows = db.fetch_all(conn, "SELECT tags FROM documents")
        recent = db.recent_activity(conn, 8)
        plans_summary = plans_service.stats_summary(conn)
    tag_counter: Counter[str] = Counter()
    for row in rows:
        for tag in parse_tags_json(row["tags"]):
            tag_counter[tag] += 1
    tags = [
        {"name": name, "count": count}
        for name, count in sorted(tag_counter.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {
        "documents": documents,
        "chunks": chunks,
        "concepts": concepts,
        "links": links,
        "zones": zones_count,
        "tags": tags,
        "llm_configured": llm.is_configured(),
        "plans": plans_summary,
        "recent": recent,
    }
