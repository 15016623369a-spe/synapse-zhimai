"""会话列表 / 详情 / 删除。"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from .. import db

router = APIRouter()


def _parse_citations(raw: str | None) -> list[dict[str, object]]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


@router.get("/conversations")
def list_conversations() -> dict[str, object]:
    with db.get_db() as conn:
        rows = db.fetch_all(
            conn,
            "SELECT c.id, c.title, c.updated_at, "
            "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
            "FROM conversations c ORDER BY c.updated_at DESC LIMIT 200",
        )
    return {
        "items": [
            {
                "id": str(row["id"]),
                "title": str(row["title"]),
                "updated_at": str(row["updated_at"]),
                "message_count": int(row["message_count"] or 0),
            }
            for row in rows
        ]
    }


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict[str, object]:
    with db.get_db() as conn:
        conversation = db.fetch_one(
            conn,
            "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        rows = db.fetch_all(
            conn,
            "SELECT id, role, content, citations, created_at FROM messages "
            "WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC",
            (conversation_id,),
        )
    return {
        "id": str(conversation["id"]),
        "title": str(conversation["title"]),
        "created_at": str(conversation["created_at"]),
        "updated_at": str(conversation["updated_at"]),
        "messages": [
            {
                "id": str(row["id"]),
                "role": str(row["role"]),
                "content": str(row["content"]),
                "citations": _parse_citations(row["citations"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ],
    }


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str) -> dict[str, object]:
    with db.get_db() as conn:
        row = db.fetch_one(
            conn, "SELECT id FROM conversations WHERE id = ?", (conversation_id,)
        )
        if row is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    return {"ok": True}
