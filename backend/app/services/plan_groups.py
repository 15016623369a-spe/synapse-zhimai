"""计划分组（Plan Groups）数据层：校验、CRUD 与计划归属。

分组以「计划」为归档单位：``plans.group_id`` 指向 ``plan_groups.id``，NULL = 未分组。
删除分组只解绑计划，绝不删除计划 / 打卡。
"""

from __future__ import annotations

import logging
import re
import sqlite3

from .. import db
from ..utils import new_id, now_iso, truncate

logger = logging.getLogger("synapse.plan_groups")

#: 预设色板（与计划 / 分区共用，按现有分组数循环取色）
GROUP_COLORS: tuple[str, ...] = (
    "#4FD8FF",
    "#A78BFA",
    "#FBBF24",
    "#34D399",
    "#FB7185",
    "#60A5FA",
    "#F472B6",
    "#2DD4BF",
)
UNGROUPED_NAME = "未分组"
UNGROUPED_COLOR = "#5B6F8C"
NAME_MAX = 20
DESCRIPTION_MAX = 120

_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

#: 区分「字段未提供」与「显式传入 null」
UNSET: object = object()


class PlanGroupError(ValueError):
    """分组校验失败（路由层转 400，message 为中文）。"""


class PlanGroupNotFound(PlanGroupError):
    """分组不存在（路由层转 404）。"""


def ungrouped_meta() -> dict[str, str | None]:
    return {"id": None, "name": UNGROUPED_NAME, "color": UNGROUPED_COLOR}


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def validate_name(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise PlanGroupError("分组名称不能为空")
    if len(text) > NAME_MAX:
        raise PlanGroupError(f"分组名称不能超过 {NAME_MAX} 个字符")
    return text


def validate_color(value: object) -> str:
    text = str(value or "").strip()
    if not _COLOR_RE.match(text):
        raise PlanGroupError("颜色格式不合法，应为 #RRGGBB")
    return f"#{text[1:].upper()}"


def validate_description(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > DESCRIPTION_MAX:
        raise PlanGroupError(f"分组描述不能超过 {DESCRIPTION_MAX} 个字符")
    return text


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def _name_exists(conn, name: str, exclude_id: str | None = None) -> bool:
    row = conn.execute(
        "SELECT id FROM plan_groups WHERE lower(name) = lower(?) AND id <> ?",
        (name, exclude_id or ""),
    ).fetchone()
    return row is not None


def _next_sort_order(conn) -> int:
    value = db.fetch_value(
        conn, "SELECT COALESCE(MAX(sort_order), 0) FROM plan_groups", default=0
    )
    return int(value or 0) + 1


def _next_color(conn) -> str:
    count = db.table_count(conn, "plan_groups")
    return GROUP_COLORS[count % len(GROUP_COLORS)]


def get_group_row(conn, group_id: str) -> sqlite3.Row | None:
    return db.fetch_one(conn, "SELECT * FROM plan_groups WHERE id = ?", (group_id,))


def require_group(conn, group_id: str) -> sqlite3.Row:
    row = get_group_row(conn, group_id)
    if row is None:
        raise PlanGroupNotFound("分组不存在")
    return row


def group_exists(conn, group_id: str) -> bool:
    return get_group_row(conn, group_id) is not None


def group_rows(conn) -> list[sqlite3.Row]:
    return db.fetch_all(
        conn,
        "SELECT g.*, "
        "(SELECT COUNT(*) FROM plans p WHERE p.group_id = g.id AND p.archived = 0) "
        "AS plan_count "
        "FROM plan_groups g ORDER BY g.sort_order ASC, g.created_at ASC, g.id ASC",
    )


def serialize_group(row: sqlite3.Row) -> dict[str, object]:
    plan_count = (
        int(row["plan_count"])
        if "plan_count" in row.keys() and row["plan_count"] is not None
        else 0
    )
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "color": str(row["color"]),
        "description": row["description"],
        "sort_order": int(row["sort_order"] or 0),
        "plan_count": plan_count,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def list_groups(conn) -> dict[str, object]:
    ungrouped = int(
        db.fetch_value(
            conn,
            "SELECT COUNT(*) FROM plans WHERE group_id IS NULL AND archived = 0",
            default=0,
        )
        or 0
    )
    return {
        "items": [serialize_group(row) for row in group_rows(conn)],
        "ungrouped_count": ungrouped,
    }


def group_meta_map(conn) -> dict[str, dict[str, str]]:
    """``{group_id: {id, name, color}}``，供计划序列化使用。"""
    return {
        str(row["id"]): {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "color": str(row["color"]),
        }
        for row in group_rows(conn)
    }


def group_count(conn) -> int:
    return db.table_count(conn, "plan_groups")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_group(
    conn,
    *,
    name: object,
    color: object = None,
    description: object = None,
) -> dict[str, object]:
    clean_name = validate_name(name)
    if _name_exists(conn, clean_name):
        raise PlanGroupError("分组名称已存在")
    clean_color = validate_color(color) if color else _next_color(conn)
    clean_description = validate_description(description)
    group_id = new_id("pg")
    now = now_iso()
    conn.execute(
        "INSERT INTO plan_groups(id, name, color, description, sort_order, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (
            group_id,
            clean_name,
            clean_color,
            clean_description,
            _next_sort_order(conn),
            now,
            now,
        ),
    )
    db.add_activity(conn, "plan_group", f"新建分组《{truncate(clean_name, 20)}》")
    logger.info("计划分组已创建：%s（%s）", clean_name, group_id)
    row = next(item for item in group_rows(conn) if str(item["id"]) == group_id)
    return serialize_group(row)


def update_group(
    conn,
    group_id: str,
    *,
    name: object = UNSET,
    color: object = UNSET,
    description: object = UNSET,
    sort_order: object = UNSET,
) -> dict[str, object]:
    row = require_group(conn, group_id)
    updates: dict[str, object] = {}
    display_name = str(row["name"])
    if name is not UNSET and name is not None:
        clean_name = validate_name(name)
        if clean_name.lower() != display_name.lower() and _name_exists(
            conn, clean_name, exclude_id=group_id
        ):
            raise PlanGroupError("分组名称已存在")
        updates["name"] = clean_name
        display_name = clean_name
    if color is not UNSET and color is not None:
        updates["color"] = validate_color(color)
    if description is not UNSET:
        updates["description"] = validate_description(description)
    if sort_order is not UNSET and sort_order is not None:
        try:
            updates["sort_order"] = int(sort_order)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise PlanGroupError("排序值必须是整数") from exc
    if updates:
        updates["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE plan_groups SET {assignments} WHERE id = ?",
            (*updates.values(), group_id),
        )
        db.add_activity(conn, "plan_group", f"更新分组《{truncate(display_name, 20)}》")
    refreshed = next(item for item in group_rows(conn) if str(item["id"]) == group_id)
    return serialize_group(refreshed)


def delete_group(conn, group_id: str) -> int:
    row = require_group(conn, group_id)
    name = str(row["name"])
    detached = int(
        db.fetch_value(
            conn, "SELECT COUNT(*) FROM plans WHERE group_id = ?", (group_id,), 0
        )
        or 0
    )
    now = now_iso()
    conn.execute(
        "UPDATE plans SET group_id = NULL, updated_at = ? WHERE group_id = ?",
        (now, group_id),
    )
    conn.execute("DELETE FROM plan_groups WHERE id = ?", (group_id,))
    db.add_activity(
        conn,
        "plan_group",
        f"删除分组《{truncate(name, 20)}》，{detached} 个计划变为未分组",
    )
    logger.info("计划分组已删除：%s（解绑 %d 个计划）", name, detached)
    return int(
        db.fetch_value(
            conn,
            "SELECT COUNT(*) FROM plans WHERE group_id IS NULL AND archived = 0",
            default=0,
        )
        or 0
    )
