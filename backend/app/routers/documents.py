"""文档：列表 / 上传 / 笔记 / 网页 / 详情 / 分块 / 删除。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from .. import db
from ..schemas import DocumentTextIn, DocumentUrlIn, DocumentZoneIn
from ..services import graph, ingest, zones
from ..utils import normalize_tag, normalize_tags

logger = logging.getLogger("synapse.documents")

router = APIRouter()


@router.get("/documents")
def list_documents(
    q: str | None = Query(None),
    tag: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    where: list[str] = []
    params: list[object] = []
    keyword = (q or "").strip()
    if keyword:
        like = f"%{keyword}%"
        where.append("(d.title LIKE ? OR d.summary LIKE ? OR d.content LIKE ?)")
        params.extend([like, like, like])
    tag_name = normalize_tag(tag or "")
    if tag_name:
        where.append("d.tags LIKE ?")
        params.append(f'%"{tag_name}"%')
    where_sql = " AND ".join(where)
    with db.get_db() as conn:
        rows = ingest.load_documents(conn, where_sql, tuple(params), limit, offset)
        total = ingest.count_documents(conn, where_sql, tuple(params))
    return {"items": [ingest.serialize_document(row) for row in rows], "total": total}


@router.put("/documents/zone")
def assign_documents_zone(payload: DocumentZoneIn) -> dict[str, object]:
    """批量把文档移入分区；``zone_id=null`` 表示移出到未分区。"""
    with db.get_db() as conn:
        try:
            result = zones.assign_documents(conn, payload.doc_ids, payload.zone_id)
        except zones.ZoneNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except zones.ZoneError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "updated": result["updated"],
        "skipped": result["skipped"],
        "zone": result["zone"],
    }


@router.post("/documents/upload")
async def upload_documents(
    files: list[UploadFile] = File(...),
    tags: str | None = Form(None),
) -> dict[str, object]:
    if not files:
        raise HTTPException(status_code=400, detail="请选择要上传的文件")
    tag_override = normalize_tags(tags.split(",")) if tags else []
    items: list[dict[str, object]] = []
    created_any = False
    for upload in files:
        filename = upload.filename or "未命名文件"
        try:
            data = await upload.read()
            parsed = ingest.parse_file(filename, data)
            document = ingest.create_document(
                parsed, tags_override=tag_override, rebuild=False
            )
            created_any = True
            items.append({"ok": True, "document": document})
        except ingest.IngestError as exc:
            items.append({"ok": False, "filename": filename, "error": str(exc)})
        except Exception:  # 单文件失败不影响其它文件
            logger.exception("上传文件处理失败：%s", filename)
            items.append({"ok": False, "filename": filename, "error": "文件导入失败，请稍后重试"})
        finally:
            await upload.close()
    if created_any:
        with db.get_db() as conn:
            graph.rebuild(conn)
    return {"items": items}


@router.post("/documents/text")
def create_text_document(payload: DocumentTextIn) -> dict[str, object]:
    tags = normalize_tags(payload.tags)
    try:
        parsed = ingest.parse_text_note(payload.title, payload.content, tags)
        if payload.source_type in {"note", "text"}:
            parsed.source_type = payload.source_type
        document = ingest.create_document(parsed)
    except ingest.IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "document": document}


@router.post("/documents/url")
async def create_url_document(payload: DocumentUrlIn) -> dict[str, object]:
    tags = normalize_tags(payload.tags)
    try:
        parsed = await ingest.parse_url(payload.url, payload.title, tags)
        document = ingest.create_document(parsed)
    except ingest.IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "document": document}


@router.get("/documents/{doc_id}")
def get_document(doc_id: str) -> dict[str, object]:
    with db.get_db() as conn:
        row = ingest.load_document(conn, doc_id)
    if row is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return ingest.serialize_document(row)


@router.get("/documents/{doc_id}/chunks")
def get_document_chunks(
    doc_id: str, limit: int = Query(50, ge=1, le=200)
) -> dict[str, object]:
    with db.get_db() as conn:
        exists = db.fetch_one(conn, "SELECT id FROM documents WHERE id = ?", (doc_id,))
        if exists is None:
            raise HTTPException(status_code=404, detail="文档不存在")
        rows = db.fetch_all(
            conn,
            "SELECT id, doc_id, idx, text, char_start, char_end FROM chunks "
            "WHERE doc_id = ? ORDER BY idx LIMIT ?",
            (doc_id, limit),
        )
    return {
        "items": [
            {
                "id": str(row["id"]),
                "doc_id": str(row["doc_id"]),
                "idx": int(row["idx"]),
                "text": str(row["text"]),
                "char_start": int(row["char_start"] or 0),
                "char_end": int(row["char_end"] or 0),
            }
            for row in rows
        ]
    }


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: str) -> dict[str, object]:
    if not ingest.delete_document(doc_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"ok": True}
