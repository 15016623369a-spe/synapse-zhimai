"""分区（Zones）数据层：色板、校验、CRUD 与文档归档。

分区以「文档」为归档单位：``documents.zone_id`` 指向 ``zones.id``，NULL 表示未分区。
删除分区只解绑文档，绝不删除文档 / 会话 / 设置。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Sequence

from .. import db
from ..utils import new_id, now_iso, truncate

logger = logging.getLogger("synapse.zones")

#: 预设色板（按现有区域数循环取色）
ZONE_COLORS: tuple[str, ...] = (
    "#4FD8FF",
    "#A78BFA",
    "#FBBF24",
    "#34D399",
    "#FB7185",
    "#60A5FA",
    "#F472B6",
    "#2DD4BF",
)
UNASSIGNED_NAME = "未分区"
UNASSIGNED_COLOR = "#5B6F8C"
ZONE_NAME_MAX = 30
ZONE_DESCRIPTION_MAX = 200
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

#: 用于区分「字段未提供」与「显式传入 null」
UNSET: object = object()


class ZoneError(ValueError):
    """分区校验失败（路由层转 400，message 为中文）。"""


class ZoneNotFound(ZoneError):
    """分区不存在（路由层转 404）。"""


def zone_key(zone_id: str | None) -> str:
    """分区在节点 id 中的后缀片段（未分区为 ``unassigned``）。"""
    return zone_id or "unassigned"


def zone_suffix(zone_id: str | None) -> str:
    """节点 id 的区域后缀，如 ``@z_ab12cd`` / ``@unassigned``。"""
    return f"@{zone_key(zone_id)}"


def unassigned_zone_meta() -> dict[str, str | None]:
    return {"id": None, "name": UNASSIGNED_NAME, "color": UNASSIGNED_COLOR}


# ---------------------------------------------------------------------------
# 校验与读取
# ---------------------------------------------------------------------------


def validate_name(name: object) -> str:
    text = str(name or "").strip()
    if not text:
        raise ZoneError("分区名称不能为空")
    if len(text) > ZONE_NAME_MAX:
        raise ZoneError(f"分区名称不能超过 {ZONE_NAME_MAX} 个字符")
    return text


def validate_color(color: object) -> str:
    text = str(color or "").strip()
    if not _COLOR_RE.match(text):
        raise ZoneError("颜色格式不合法，应为 #RRGGBB")
    return f"#{text[1:].upper()}"


def _clean_description(description: object) -> str | None:
    text = str(description or "").strip()
    if not text:
        return None
    if len(text) > ZONE_DESCRIPTION_MAX:
        raise ZoneError(f"分区描述不能超过 {ZONE_DESCRIPTION_MAX} 个字符")
    return text


def _name_exists(conn, name: str, exclude_id: str | None = None) -> bool:
    row = conn.execute(
        "SELECT id FROM zones WHERE lower(name) = lower(?) AND id <> ?",
        (name, exclude_id or ""),
    ).fetchone()
    return row is not None


def _next_sort_order(conn) -> int:
    value = db.fetch_value(conn, "SELECT COALESCE(MAX(sort_order), 0) FROM zones", default=0)
    return int(value or 0) + 1


def next_color(conn) -> str:
    count = db.table_count(conn, "zones")
    return ZONE_COLORS[count % len(ZONE_COLORS)]


def zone_exists(conn, zone_id: str) -> bool:
    return get_zone_row(conn, zone_id) is not None


def get_zone_row(conn, zone_id: str) -> sqlite3.Row | None:
    return db.fetch_one(conn, "SELECT * FROM zones WHERE id = ?", (zone_id,))


def get_zone_or_404(conn, zone_id: str) -> sqlite3.Row:
    row = get_zone_row(conn, zone_id)
    if row is None:
        raise ZoneNotFound("分区不存在")
    return row


def list_zone_rows(conn) -> list[sqlite3.Row]:
    return db.fetch_all(
        conn,
        "SELECT z.*, "
        "(SELECT COUNT(*) FROM documents d WHERE d.zone_id = z.id) AS document_count "
        "FROM zones z ORDER BY z.sort_order ASC, z.created_at ASC, z.id ASC",
    )


def list_zones(conn) -> list[dict[str, object]]:
    return [serialize_zone(row) for row in list_zone_rows(conn)]


def unassigned_count(conn) -> int:
    value = db.fetch_value(
        conn, "SELECT COUNT(*) FROM documents WHERE zone_id IS NULL", default=0
    )
    return int(value or 0)


def serialize_zone(row: sqlite3.Row) -> dict[str, object]:
    document_count = (
        int(row["document_count"])
        if "document_count" in row.keys() and row["document_count"] is not None
        else 0
    )
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "color": str(row["color"]),
        "description": row["description"],
        "sort_order": int(row["sort_order"] or 0),
        "document_count": document_count,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def zone_brief(conn, zone_id: str) -> dict[str, object]:
    row = get_zone_or_404(conn, zone_id)
    return {"id": str(row["id"]), "name": str(row["name"])}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_zone(
    conn,
    *,
    name: object,
    color: object = None,
    description: object = None,
) -> dict[str, object]:
    clean_name = validate_name(name)
    if _name_exists(conn, clean_name):
        raise ZoneError("分区名称已存在")
    clean_color = validate_color(color) if color else next_color(conn)
    clean_description = _clean_description(description)
    zone_id = new_id("z")
    now = now_iso()
    conn.execute(
        "INSERT INTO zones(id, name, color, description, sort_order, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (
            zone_id,
            clean_name,
            clean_color,
            clean_description,
            _next_sort_order(conn),
            now,
            now,
        ),
    )
    db.add_activity(conn, "zone", f"创建分区《{truncate(clean_name, 30)}》")
    logger.info("分区已创建：%s（%s）", clean_name, zone_id)
    row = list_zone_rows(conn)
    zone = next(item for item in row if str(item["id"]) == zone_id)
    return serialize_zone(zone)


def update_zone(
    conn,
    zone_id: str,
    *,
    name: object = UNSET,
    color: object = UNSET,
    description: object = UNSET,
) -> dict[str, object]:
    row = get_zone_or_404(conn, zone_id)
    updates: dict[str, object] = {}
    display_name = str(row["name"])
    if name is not UNSET and name is not None:
        clean_name = validate_name(name)
        if clean_name.lower() != display_name.lower() and _name_exists(
            conn, clean_name, exclude_id=zone_id
        ):
            raise ZoneError("分区名称已存在")
        updates["name"] = clean_name
        display_name = clean_name
    if color is not UNSET and color is not None:
        updates["color"] = validate_color(color)
    if description is not UNSET:
        updates["description"] = _clean_description(description)
    if updates:
        updates["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE zones SET {assignments} WHERE id = ?",
            (*updates.values(), zone_id),
        )
        db.add_activity(conn, "zone", f"更新分区《{truncate(display_name, 30)}》")
    refreshed = next(
        item for item in list_zone_rows(conn) if str(item["id"]) == zone_id
    )
    return serialize_zone(refreshed)


def delete_zone(conn, zone_id: str) -> int:
    row = get_zone_or_404(conn, zone_id)
    name = str(row["name"])
    detached = int(
        db.fetch_value(
            conn, "SELECT COUNT(*) FROM documents WHERE zone_id = ?", (zone_id,), 0
        )
        or 0
    )
    now = now_iso()
    conn.execute(
        "UPDATE documents SET zone_id = NULL, updated_at = ? WHERE zone_id = ?",
        (now, zone_id),
    )
    conn.execute("DELETE FROM zones WHERE id = ?", (zone_id,))
    db.add_activity(
        conn, "zone", f"删除分区《{truncate(name, 30)}》，{detached} 篇文档移入未分区"
    )
    logger.info("分区已删除：%s（解绑 %d 篇文档）", name, detached)
    return unassigned_count(conn)


# ---------------------------------------------------------------------------
# 文档归档
# ---------------------------------------------------------------------------


def assign_documents(
    conn, doc_ids: Sequence[str], zone_id: str | None
) -> dict[str, object]:
    """把文档批量移入分区（``zone_id=None`` 表示移出到未分区）。

    未知文档 id 会被忽略，并通过 ``skipped`` 计数返回。
    """
    zone: dict[str, object] | None = None
    if zone_id is not None:
        row = get_zone_or_404(conn, zone_id)
        zone = {"id": str(row["id"]), "name": str(row["name"])}

    unique_ids = list(dict.fromkeys(str(doc_id) for doc_id in doc_ids if str(doc_id)))
    existing: set[str] = set()
    if unique_ids:
        placeholders = ",".join("?" for _ in unique_ids)
        existing = {
            str(row["id"])
            for row in db.fetch_all(
                conn,
                f"SELECT id FROM documents WHERE id IN ({placeholders})",
                unique_ids,
            )
        }
    now = now_iso()
    updated = 0
    if existing:
        conn.executemany(
            "UPDATE documents SET zone_id = ?, updated_at = ? WHERE id = ?",
            [(zone_id, now, doc_id) for doc_id in existing],
        )
        updated = len(existing)
        detail = (
            f"将 {updated} 篇文档移入分区《{truncate(str(zone['name']), 30)}》"
            if zone is not None
            else f"将 {updated} 篇文档移入未分区"
        )
        db.add_activity(conn, "zone", detail)
        logger.info("文档归档：%d 篇 → %s", updated, zone_id or "未分区")
    return {
        "updated": updated,
        "skipped": len(unique_ids) - updated,
        "zone": zone,
    }


# ---------------------------------------------------------------------------
# 分组辅助（供分区图谱使用）
# ---------------------------------------------------------------------------


def zone_meta_map(conn) -> dict[str, dict[str, object]]:
    """``{zone_id: {id, name, color}}``（按 sort_order 有序的 dict）。"""
    result: dict[str, dict[str, object]] = {}
    for row in list_zone_rows(conn):
        result[str(row["id"])] = {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "color": str(row["color"]),
        }
    return result
