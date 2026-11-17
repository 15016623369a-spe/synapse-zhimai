"""个人计划与打卡服务。

- 计划：daily（每天，周目标固定 7）/ weekly（每周任意天打卡，周目标 1–7）；
- 打卡：每计划每天最多一次（唯一约束），可补记 ≥ 开始日期且 ≤ 今天；
- 连续统计：daily 按自然日连续；weekly 按「达标完整周」连续；
- 热力图 / 总览按服务器本地时区（Asia/Shanghai）自然日计算，一周从周一开始。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta

from .. import db
from ..utils import new_id, now_iso, truncate
from . import plan_groups as plan_groups_service

logger = logging.getLogger("synapse.plans")

#: 预设色板（与分区共用，按现有计划数循环取色）
PLAN_COLORS: tuple[str, ...] = (
    "#4FD8FF",
    "#A78BFA",
    "#FBBF24",
    "#34D399",
    "#FB7185",
    "#60A5FA",
    "#F472B6",
    "#2DD4BF",
)
FREQUENCIES = ("daily", "weekly", "once")
ONCE = "once"
KINDS = ("all", "recurring", "once")
TITLE_MAX = 40
DESCRIPTION_MAX = 200
NOTE_MAX = 120
LOCATION_MAX = 60
PLAN_DATE_RANGE_DAYS = 730  # 单日行程日期允许今天前后约 2 年
DEFAULT_WEEKLY_TARGET = 3
CHECKIN_DEFAULT_DAYS = 90
CHECKIN_MAX_RANGE_DAYS = 366
HEATMAP_MIN_DAYS = 30
HEATMAP_MAX_DAYS = 366
HEATMAP_DEFAULT_DAYS = 120
RECENT_LIMIT = 10

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

#: 测试注入点：固定「今天」，避免依赖真实系统时间
_TODAY_OVERRIDE: date | None = None


class PlanError(ValueError):
    """计划 / 打卡校验失败（路由层转 400，message 为中文）。"""


class PlanNotFound(PlanError):
    """计划不存在（404）。"""


class CheckinNotFound(PlanError):
    """打卡记录不存在（404）。"""


class DuplicateCheckin(PlanError):
    """同一计划同一天重复打卡（409）。"""


# ---------------------------------------------------------------------------
# 日期工具（本地时区自然日，周一开始）
# ---------------------------------------------------------------------------


def today() -> date:
    """服务器本地「今天」；测试可通过 ``_TODAY_OVERRIDE`` 固定。"""
    if _TODAY_OVERRIDE is not None:
        return _TODAY_OVERRIDE
    return datetime.now().date()


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_end(day: date) -> date:
    return week_start(day) + timedelta(days=6)


def iso(day: date) -> str:
    return day.isoformat()


def parse_date(value: object, *, default: date | None = None) -> date:
    """解析 ``YYYY-MM-DD``；空值取默认值，非法格式抛中文错误。"""
    text = str(value or "").strip()
    if not text:
        if default is None:
            raise PlanError("日期不能为空")
        return default
    if not _DATE_RE.match(text):
        raise PlanError("日期格式不合法，应为 YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise PlanError("日期不合法") from exc


def _stored_date(value: object) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:  # pragma: no cover - 数据库内日期应始终合法
        return today()


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def validate_title(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise PlanError("计划标题不能为空")
    if len(text) > TITLE_MAX:
        raise PlanError(f"计划标题不能超过 {TITLE_MAX} 个字符")
    return text


def validate_description(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > DESCRIPTION_MAX:
        raise PlanError(f"计划描述不能超过 {DESCRIPTION_MAX} 个字符")
    return text


def validate_note(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > NOTE_MAX:
        raise PlanError(f"备注不能超过 {NOTE_MAX} 个字符")
    return text


def validate_color(value: object) -> str:
    text = str(value or "").strip()
    if not _COLOR_RE.match(text):
        raise PlanError("颜色格式不合法，应为 #RRGGBB")
    return f"#{text[1:].upper()}"


def validate_frequency(value: object) -> str:
    text = str(value or "daily").strip().lower()
    if text not in FREQUENCIES:
        raise PlanError("频率只能是 daily 或 weekly")
    return text


def validate_weekly_target(frequency: str, value: object) -> int:
    """daily 固定 7；weekly 取 1–7（缺省 3）。"""
    if frequency == "daily":
        return 7
    if value is None or str(value).strip() == "":
        return DEFAULT_WEEKLY_TARGET
    try:
        target = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise PlanError("每周目标次数必须是 1 到 7 的整数") from exc
    if not 1 <= target <= 7:
        raise PlanError("每周目标次数必须在 1 到 7 之间")
    return target


def validate_plan_date(value: object, *, today_date: date | None = None) -> date:
    """单日行程日期：合法 ``YYYY-MM-DD``，允许今天前后约 2 年。"""
    target = parse_date(value)
    anchor = today_date or today()
    if abs((target - anchor).days) > PLAN_DATE_RANGE_DAYS:
        raise PlanError("行程日期需在今天前后 2 年内")
    return target


def validate_time(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not _TIME_RE.match(text):
        raise PlanError("时间格式不合法，应为 HH:MM")
    return text


def validate_time_range(
    start_value: object, end_value: object
) -> tuple[str | None, str | None]:
    start_time = validate_time(start_value)
    end_time = validate_time(end_value)
    if start_time and end_time and end_time <= start_time:
        raise PlanError("结束时间必须晚于开始时间")
    return start_time, end_time


def validate_location(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > LOCATION_MAX:
        raise PlanError(f"地点不能超过 {LOCATION_MAX} 个字符")
    return text


def validate_group_id(conn, value: object) -> str | None:
    """可空分组 id；非空时必须存在（否则抛 PlanGroupNotFound → 404）。"""
    text = str(value or "").strip()
    if not text:
        return None
    if not plan_groups_service.group_exists(conn, text):
        raise plan_groups_service.PlanGroupNotFound("分组不存在")
    return text


def _next_sort_order(conn) -> int:
    value = db.fetch_value(conn, "SELECT COALESCE(MAX(sort_order), 0) FROM plans", default=0)
    return int(value or 0) + 1


def _next_color(conn) -> str:
    count = db.table_count(conn, "plans")
    return PLAN_COLORS[count % len(PLAN_COLORS)]


# ---------------------------------------------------------------------------
# 连续统计
# ---------------------------------------------------------------------------


def _daily_streaks(dates: list[date], today_date: date) -> tuple[int, int]:
    """返回 (当前连续天数, 最长连续天数)。"""
    date_set = set(dates)
    cursor = today_date if today_date in date_set else today_date - timedelta(days=1)
    current = 0
    while cursor in date_set:
        current += 1
        cursor -= timedelta(days=1)

    longest = 0
    run = 0
    previous: date | None = None
    for day in sorted(date_set):
        if previous is not None and day == previous + timedelta(days=1):
            run += 1
        else:
            run = 1
        longest = max(longest, run)
        previous = day
    return current, longest


def _weekly_streaks(
    dates: list[date], weekly_target: int, today_date: date
) -> tuple[int, int]:
    """返回 (当前连续达标周数, 最长连续达标周数)。"""
    target = max(1, int(weekly_target))
    week_counts = Counter(week_start(day) for day in dates)
    current_week = week_start(today_date)
    if week_counts.get(current_week, 0) >= target:
        cursor = current_week
    else:
        cursor = current_week - timedelta(days=7)
    current = 0
    while week_counts.get(cursor, 0) >= target:
        current += 1
        cursor -= timedelta(days=7)

    longest = 0
    run = 0
    previous: date | None = None
    for week in sorted(week_counts):
        if week_counts[week] >= target:
            if previous is not None and week == previous + timedelta(days=7):
                run += 1
            else:
                run = 1
            longest = max(longest, run)
        else:
            run = 0
        previous = week
    return current, longest


def compute_stats(
    plan_row: sqlite3.Row, dates: list[date], today_date: date
) -> dict[str, object]:
    """计算 PlanWithStats 的派生字段。"""
    frequency = str(plan_row["frequency"])
    weekly_target = int(plan_row["weekly_target"] or 7)
    date_set = set(dates)
    week_checked = sum(1 for day in dates if week_start(day) == week_start(today_date))
    if frequency == "daily":
        current_streak, longest_streak = _daily_streaks(dates, today_date)
        week_target = 7
    else:
        current_streak, longest_streak = _weekly_streaks(dates, weekly_target, today_date)
        week_target = weekly_target
    last_checkin = max(dates) if dates else None
    return {
        "today_checked": today_date in date_set,
        "week_checked": week_checked,
        "week_target": week_target,
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "total_checkins": len(dates),
        "last_checkin_date": iso(last_checkin) if last_checkin else None,
    }


# ---------------------------------------------------------------------------
# 读取辅助
# ---------------------------------------------------------------------------


def serialize_checkin(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "plan_id": str(row["plan_id"]),
        "date": str(row["checkin_date"]),
        "note": row["note"],
        "created_at": str(row["created_at"]),
    }


def _plan_rows(
    conn, include_archived: bool = False, kind: str = "all"
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    if not include_archived:
        clauses.append("archived = 0")
    if kind == "once":
        clauses.append("frequency = 'once'")
    elif kind == "recurring":
        clauses.append("frequency <> 'once'")
    sql = "SELECT * FROM plans"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY sort_order ASC, created_at ASC, id ASC"
    return db.fetch_all(conn, sql)


def _plan_row(conn, plan_id: str) -> sqlite3.Row | None:
    return db.fetch_one(conn, "SELECT * FROM plans WHERE id = ?", (plan_id,))


def require_plan(conn, plan_id: str) -> sqlite3.Row:
    row = _plan_row(conn, plan_id)
    if row is None:
        raise PlanNotFound("计划不存在")
    return row


def _dates_for_plan(conn, plan_id: str) -> list[date]:
    rows = db.fetch_all(
        conn,
        "SELECT checkin_date FROM plan_checkins WHERE plan_id = ? ORDER BY checkin_date ASC",
        (plan_id,),
    )
    return [_stored_date(row["checkin_date"]) for row in rows]


def _dates_by_plan(conn) -> dict[str, list[date]]:
    rows = db.fetch_all(
        conn, "SELECT plan_id, checkin_date FROM plan_checkins ORDER BY checkin_date ASC"
    )
    result: dict[str, list[date]] = {}
    for row in rows:
        result.setdefault(str(row["plan_id"]), []).append(_stored_date(row["checkin_date"]))
    return result


def _row_group_id(plan_row: sqlite3.Row) -> str | None:
    if "group_id" not in plan_row.keys():
        return None
    value = plan_row["group_id"]
    return str(value) if value else None


def _row_field(plan_row: sqlite3.Row, key: str) -> object:
    return plan_row[key] if key in plan_row.keys() else None


def plan_with_stats(
    plan_row: sqlite3.Row,
    dates: list[date],
    today_date: date,
    group_meta: dict[str, dict[str, str]] | None = None,
) -> dict[str, object]:
    frequency = str(plan_row["frequency"])
    is_once = frequency == ONCE
    group_id = _row_group_id(plan_row)
    meta = (group_meta or {}).get(group_id) if group_id else None
    payload: dict[str, object] = {
        "id": str(plan_row["id"]),
        "title": str(plan_row["title"]),
        "description": plan_row["description"],
        "color": str(plan_row["color"]),
        "frequency": frequency,
        "weekly_target": int(plan_row["weekly_target"]),
        "start_date": str(plan_row["start_date"]),
        "archived": bool(int(plan_row["archived"] or 0)),
        "sort_order": int(plan_row["sort_order"] or 0),
        "group_id": group_id,
        "group_name": meta["name"] if meta else plan_groups_service.UNGROUPED_NAME,
        "group_color": meta["color"] if meta else plan_groups_service.UNGROUPED_COLOR,
        "plan_date": _row_field(plan_row, "plan_date") if is_once else None,
        "start_time": _row_field(plan_row, "start_time") if is_once else None,
        "end_time": _row_field(plan_row, "end_time") if is_once else None,
        "location": _row_field(plan_row, "location") if is_once else None,
        "created_at": str(plan_row["created_at"]),
        "updated_at": str(plan_row["updated_at"]),
    }
    if is_once:
        checked = bool(dates)
        plan_date = payload["plan_date"]
        last_checkin = max(dates) if dates else None
        payload.update(
            {
                "checked": checked,
                "today_checked": bool(checked and plan_date == iso(today_date)),
                "week_checked": 0,
                "week_target": 0,
                "current_streak": 0,
                "longest_streak": 0,
                "total_checkins": len(dates),
                "last_checkin_date": iso(last_checkin) if last_checkin else None,
            }
        )
    else:
        payload.update(compute_stats(plan_row, dates, today_date))
        payload["checked"] = False
    return payload


def _plan_payload(conn, plan_id: str) -> dict[str, object]:
    row = require_plan(conn, plan_id)
    return plan_with_stats(
        row,
        _dates_for_plan(conn, plan_id),
        today(),
        plan_groups_service.group_meta_map(conn),
    )


# ---------------------------------------------------------------------------
# 计划 CRUD
# ---------------------------------------------------------------------------


def list_plans(
    conn, include_archived: bool = False, kind: str = "all"
) -> dict[str, object]:
    normalized_kind = str(kind or "all").strip().lower()
    if normalized_kind not in KINDS:
        raise PlanError("kind 只能是 all、recurring 或 once")
    today_date = today()
    rows = _plan_rows(conn, include_archived=include_archived, kind=normalized_kind)
    dates_map = _dates_by_plan(conn)
    group_meta = plan_groups_service.group_meta_map(conn)
    return {
        "items": [
            plan_with_stats(
                row, dates_map.get(str(row["id"]), []), today_date, group_meta
            )
            for row in rows
        ],
        "today": iso(today_date),
        "week_start": iso(week_start(today_date)),
        "kind": normalized_kind,
    }


def get_plan(conn, plan_id: str) -> dict[str, object]:
    return {"plan": _plan_payload(conn, plan_id)}


def _once_fields(
    payload: dict[str, object],
) -> tuple[str, str | None, str | None, str | None]:
    """校验单日行程字段，返回 (plan_date, start_time, end_time, location)。"""
    plan_date = validate_plan_date(payload.get("plan_date"))
    start_time, end_time = validate_time_range(
        payload.get("start_time"), payload.get("end_time")
    )
    location = validate_location(payload.get("location"))
    return iso(plan_date), start_time, end_time, location


def create_plan(conn, payload: dict[str, object]) -> dict[str, object]:
    title = validate_title(payload.get("title"))
    frequency = validate_frequency(payload.get("frequency") or "daily")
    group_id = validate_group_id(conn, payload.get("group_id"))
    description = validate_description(payload.get("description"))
    color = (
        validate_color(payload["color"])
        if str(payload.get("color") or "").strip()
        else _next_color(conn)
    )

    if frequency == ONCE:
        plan_date, start_time, end_time, location = _once_fields(payload)
        weekly_target = 0
        start_date = plan_date
    else:
        weekly_target = validate_weekly_target(frequency, payload.get("weekly_target"))
        start = parse_date(payload.get("start_date"), default=today())
        start_date = iso(start)
        plan_date = start_time = end_time = location = None

    plan_id = new_id("p")
    now = now_iso()
    conn.execute(
        "INSERT INTO plans(id, title, description, color, frequency, weekly_target, "
        "start_date, archived, sort_order, group_id, plan_date, start_time, end_time, "
        "location, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            plan_id,
            title,
            description,
            color,
            frequency,
            weekly_target,
            start_date,
            _next_sort_order(conn),
            group_id,
            plan_date,
            start_time,
            end_time,
            location,
            now,
            now,
        ),
    )
    db.add_activity(conn, "plan", f"新建计划《{truncate(title, 30)}》")
    logger.info("计划已创建：%s（%s，%s）", title, plan_id, frequency)
    return {"plan": _plan_payload(conn, plan_id)}


def update_plan(
    conn, plan_id: str, payload: dict[str, object], provided: set[str]
) -> dict[str, object]:
    row = require_plan(conn, plan_id)
    updates: dict[str, object] = {}
    display_title = str(row["title"])

    if "title" in provided and payload.get("title") is not None:
        title = validate_title(payload.get("title"))
        updates["title"] = title
        display_title = title
    if "description" in provided:
        updates["description"] = validate_description(payload.get("description"))
    if "color" in provided and payload.get("color") is not None:
        updates["color"] = validate_color(payload.get("color"))
    if "group_id" in provided:
        updates["group_id"] = validate_group_id(conn, payload.get("group_id"))
    if "frequency" in provided and payload.get("frequency") is not None:
        updates["frequency"] = validate_frequency(payload.get("frequency"))
    if "archived" in provided and payload.get("archived") is not None:
        updates["archived"] = 1 if payload.get("archived") else 0
    if "sort_order" in provided and payload.get("sort_order") is not None:
        updates["sort_order"] = int(payload["sort_order"])  # type: ignore[arg-type]

    new_frequency = str(updates.get("frequency", row["frequency"]))
    frequency_changed = new_frequency != str(row["frequency"])
    was_once = str(row["frequency"]) == ONCE

    if new_frequency == ONCE:
        if "plan_date" in provided and payload.get("plan_date") is not None:
            plan_date = validate_plan_date(payload.get("plan_date"))
        elif was_once and _row_field(row, "plan_date"):
            plan_date = _stored_date(_row_field(row, "plan_date"))
        else:
            raise PlanError("单日行程必须选择日期")
        start_value = (
            payload.get("start_time") if "start_time" in provided else _row_field(row, "start_time")
        )
        end_value = (
            payload.get("end_time") if "end_time" in provided else _row_field(row, "end_time")
        )
        start_time, end_time = validate_time_range(start_value, end_value)
        if "location" in provided:
            location = validate_location(payload.get("location"))
        else:
            location = validate_location(_row_field(row, "location")) if was_once else None
        updates.update(
            {
                "plan_date": iso(plan_date),
                "start_date": iso(plan_date),
                "weekly_target": 0,
                "start_time": start_time,
                "end_time": end_time,
                "location": location,
            }
        )
    else:
        # 切回循环计划：清理行程专属字段
        updates.update(
            {
                "plan_date": None,
                "start_time": None,
                "end_time": None,
                "location": None,
            }
        )
        if "start_date" in provided and payload.get("start_date") is not None:
            updates["start_date"] = iso(parse_date(payload.get("start_date")))
        elif was_once:
            updates["start_date"] = iso(today())
        if "weekly_target" in provided and payload.get("weekly_target") is not None:
            updates["weekly_target"] = validate_weekly_target(
                new_frequency, payload.get("weekly_target")
            )
        elif new_frequency == "daily":
            updates["weekly_target"] = 7
        elif frequency_changed:
            updates["weekly_target"] = DEFAULT_WEEKLY_TARGET

    if updates:
        updates["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE plans SET {assignments} WHERE id = ?",
            (*updates.values(), plan_id),
        )
        db.add_activity(conn, "plan", f"更新计划《{truncate(display_title, 30)}》")
    return {"plan": _plan_payload(conn, plan_id)}


def delete_plan(conn, plan_id: str) -> int:
    row = require_plan(conn, plan_id)
    deleted_checkins = int(
        db.fetch_value(
            conn,
            "SELECT COUNT(*) FROM plan_checkins WHERE plan_id = ?",
            (plan_id,),
            0,
        )
        or 0
    )
    conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    db.add_activity(
        conn, "plan", f"删除计划《{truncate(str(row['title']), 30)}》"
    )
    logger.info("计划已删除：%s（级联 %d 条打卡）", plan_id, deleted_checkins)
    return deleted_checkins


# ---------------------------------------------------------------------------
# 打卡
# ---------------------------------------------------------------------------


def create_checkin(
    conn, plan_id: str, date_value: object = None, note: object = None
) -> dict[str, object]:
    row = require_plan(conn, plan_id)
    today_date = today()
    if str(row["frequency"]) == ONCE:
        plan_date = _stored_date(_row_field(row, "plan_date"))
        if plan_date > today_date:
            raise PlanError("行程还没到，不能提前完成")
        target = parse_date(date_value, default=plan_date)
        if target != plan_date:
            raise PlanError("该行程只能在行程日期完成")
    else:
        target = parse_date(date_value, default=today_date)
        if target > today_date:
            raise PlanError("不能给未来日期打卡")
        start = _stored_date(row["start_date"])
        if target < start:
            raise PlanError("不能早于计划开始日期打卡")
    clean_note = validate_note(note)
    existing = db.fetch_one(
        conn,
        "SELECT id FROM plan_checkins WHERE plan_id = ? AND checkin_date = ?",
        (plan_id, iso(target)),
    )
    if existing is not None:
        raise DuplicateCheckin("这一天已经打过卡了")
    checkin_id = new_id("ck")
    now = now_iso()
    conn.execute(
        "INSERT INTO plan_checkins(id, plan_id, checkin_date, note, created_at) "
        "VALUES(?, ?, ?, ?, ?)",
        (checkin_id, plan_id, iso(target), clean_note, now),
    )
    db.add_activity(
        conn, "plan", f"完成打卡《{truncate(str(row['title']), 30)}》"
    )
    checkin_row = db.fetch_one(
        conn, "SELECT * FROM plan_checkins WHERE id = ?", (checkin_id,)
    )
    assert checkin_row is not None
    return {
        "ok": True,
        "checkin": serialize_checkin(checkin_row),
        "plan": _plan_payload(conn, plan_id),
    }


def undo_checkin(conn, plan_id: str, date_value: object) -> dict[str, object]:
    row = require_plan(conn, plan_id)
    target = parse_date(date_value)
    if str(row["frequency"]) == ONCE:
        plan_date = _stored_date(_row_field(row, "plan_date"))
        if target != plan_date:
            raise PlanError("该行程只能在行程日期完成")
    existing = db.fetch_one(
        conn,
        "SELECT id FROM plan_checkins WHERE plan_id = ? AND checkin_date = ?",
        (plan_id, iso(target)),
    )
    if existing is None:
        raise CheckinNotFound("打卡记录不存在")
    conn.execute(
        "DELETE FROM plan_checkins WHERE plan_id = ? AND checkin_date = ?",
        (plan_id, iso(target)),
    )
    db.add_activity(
        conn, "plan", f"撤销打卡《{truncate(str(row['title']), 30)}》"
    )
    return {"ok": True, "plan": _plan_payload(conn, plan_id)}


def list_checkins(
    conn,
    plan_id: str,
    start_value: object = None,
    end_value: object = None,
) -> dict[str, object]:
    require_plan(conn, plan_id)
    today_date = today()
    end_date = parse_date(end_value, default=today_date)
    start_date = parse_date(
        start_value, default=end_date - timedelta(days=CHECKIN_DEFAULT_DAYS - 1)
    )
    if start_date > end_date:
        raise PlanError("结束日期不能早于开始日期")
    if (end_date - start_date).days + 1 > CHECKIN_MAX_RANGE_DAYS:
        raise PlanError(f"查询区间不能超过 {CHECKIN_MAX_RANGE_DAYS} 天")
    rows = db.fetch_all(
        conn,
        "SELECT * FROM plan_checkins WHERE plan_id = ? AND checkin_date BETWEEN ? AND ? "
        "ORDER BY checkin_date ASC",
        (plan_id, iso(start_date), iso(end_date)),
    )
    return {"items": [serialize_checkin(row) for row in rows]}


# ---------------------------------------------------------------------------
# 总览 / 统计摘要
# ---------------------------------------------------------------------------


def overview(conn, days: int = HEATMAP_DEFAULT_DAYS) -> dict[str, object]:
    today_date = today()
    current_week_start = week_start(today_date)
    current_week_end = week_end(today_date)
    today_text = iso(today_date)
    upcoming_limit = iso(today_date + timedelta(days=30))
    days = max(HEATMAP_MIN_DAYS, min(int(days), HEATMAP_MAX_DAYS))

    active_rows = _plan_rows(conn, include_archived=False)
    recurring_rows = [row for row in active_rows if str(row["frequency"]) != ONCE]
    schedule_rows = [row for row in active_rows if str(row["frequency"]) == ONCE]
    dates_map = _dates_by_plan(conn)

    def is_checked(row: sqlite3.Row) -> bool:
        return bool(dates_map.get(str(row["id"])))

    stats_rows = [
        (row, compute_stats(row, dates_map.get(str(row["id"]), []), today_date))
        for row in recurring_rows
    ]

    best_streak = 0
    best_title: str | None = None
    for row, stats in stats_rows:
        streak = int(stats["current_streak"])
        if streak > best_streak:
            best_streak = streak
            best_title = str(row["title"])

    today_schedule = [
        row for row in schedule_rows if str(_row_field(row, "plan_date")) == today_text
    ]
    today_schedule_checked = sum(1 for row in today_schedule if is_checked(row))
    upcoming_schedule = [
        row
        for row in schedule_rows
        if _row_field(row, "plan_date") is not None
        and today_text < str(_row_field(row, "plan_date")) <= upcoming_limit
        and not is_checked(row)
    ]
    recurring_today_checked = sum(
        1 for _row, stats in stats_rows if stats["today_checked"]
    )

    total_checkins = sum(len(dates) for dates in dates_map.values())
    heat_start = today_date - timedelta(days=days - 1)
    heat_counts = Counter(
        iso(day)
        for dates in dates_map.values()
        for day in dates
        if heat_start <= day <= today_date
    )
    heatmap = [
        {
            "date": iso(heat_start + timedelta(days=offset)),
            "count": heat_counts.get(iso(heat_start + timedelta(days=offset)), 0),
        }
        for offset in range(days)
    ]

    recent_rows = db.fetch_all(
        conn,
        "SELECT ck.plan_id, ck.checkin_date, ck.note, ck.created_at, "
        "p.title AS plan_title, p.color AS plan_color "
        "FROM plan_checkins ck JOIN plans p ON p.id = ck.plan_id "
        "ORDER BY ck.checkin_date DESC, ck.created_at DESC, ck.rowid DESC LIMIT ?",
        (RECENT_LIMIT,),
    )

    return {
        "today": today_text,
        "week_start": iso(current_week_start),
        "week_end": iso(current_week_end),
        "stats": {
            # 既有字段：语义仍只统计循环计划
            "active_plans": len(recurring_rows),
            "today_total": len(recurring_rows),
            "today_checked": recurring_today_checked,
            "week_checked_total": sum(
                int(stats["week_checked"]) for _row, stats in stats_rows
            ),
            "week_target_total": sum(
                int(stats["week_target"]) for _row, stats in stats_rows
            ),
            "best_current_streak": best_streak,
            "best_plan_title": best_title,
            "total_checkins": total_checkins,
            # 分组 / 单日行程扩展统计
            "group_count": plan_groups_service.group_count(conn),
            "today_schedule_total": len(today_schedule),
            "today_schedule_checked": today_schedule_checked,
            "upcoming_schedule_total": len(upcoming_schedule),
            "recurring_today_total": len(recurring_rows),
            "recurring_today_checked": recurring_today_checked,
        },
        "heatmap": heatmap,
        "recent": [
            {
                "plan_id": str(row["plan_id"]),
                "plan_title": str(row["plan_title"]),
                "color": str(row["plan_color"]),
                "date": str(row["checkin_date"]),
                "note": row["note"],
                "created_at": str(row["created_at"]),
            }
            for row in recent_rows
        ],
    }


def stats_summary(conn) -> dict[str, object]:
    """``GET /api/stats`` 的计划摘要。"""
    today_date = today()
    total = db.table_count(conn, "plans")
    today_checked = int(
        db.fetch_value(
            conn,
            "SELECT COUNT(DISTINCT plan_id) FROM plan_checkins WHERE checkin_date = ?",
            (iso(today_date),),
            0,
        )
        or 0
    )
    return {
        "total": total,
        "today_checked": today_checked,
        "groups": plan_groups_service.group_count(conn),
    }
