"""个人计划与打卡测试（SPEC-PLANS 第 5 节）。"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db
from app.services import plans as plans_service

# 固定「今天」为 2026-11-17（周二，所属周 2026-11-16 周一 ~ 2026-11-22 周日）
FROZEN_TODAY = date(2026, 11, 17)
WEEK_START = date(2026, 11, 16)


@pytest.fixture()
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr(plans_service, "_TODAY_OVERRIDE", FROZEN_TODAY)
    return FROZEN_TODAY


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _create_plan(client: TestClient, **payload) -> dict:
    response = client.post("/api/plans", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["plan"]


def _checkin(
    client: TestClient, plan_id: str, day: str | None = None, note: str | None = None
) -> dict:
    body: dict[str, object] = {}
    if day is not None:
        body["date"] = day
    if note is not None:
        body["note"] = note
    response = client.post(f"/api/plans/{plan_id}/checkins", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _plan(client: TestClient, plan_id: str) -> dict:
    response = client.get(f"/api/plans/{plan_id}")
    assert response.status_code == 200, response.text
    return response.json()["plan"]


# ---------------------------------------------------------------------------
# 计划 CRUD 与默认值
# ---------------------------------------------------------------------------


def test_create_plan_defaults(client: TestClient, frozen_today: date) -> None:
    plan = _create_plan(client, title="每日阅读 30 分钟")
    assert plan["frequency"] == "daily"
    assert plan["weekly_target"] == 7
    assert plan["week_target"] == 7
    assert plan["color"] == "#4FD8FF"
    assert plan["start_date"] == frozen_today.isoformat()
    assert plan["archived"] is False
    assert plan["sort_order"] == 1
    assert plan["today_checked"] is False
    assert plan["week_checked"] == 0
    assert plan["current_streak"] == 0
    assert plan["longest_streak"] == 0
    assert plan["total_checkins"] == 0
    assert plan["last_checkin_date"] is None
    assert set(plan) >= {
        "id",
        "title",
        "description",
        "color",
        "frequency",
        "weekly_target",
        "start_date",
        "archived",
        "sort_order",
        "created_at",
        "updated_at",
        "today_checked",
        "week_checked",
        "week_target",
        "current_streak",
        "longest_streak",
        "total_checkins",
        "last_checkin_date",
    }


def test_create_plan_weekly_defaults_and_target(
    client: TestClient, frozen_today: date
) -> None:
    plan = _create_plan(
        client,
        title="每周跑步 3 次",
        description="每次 5km",
        frequency="weekly",
        start_date="2026-11-01",
    )
    assert plan["frequency"] == "weekly"
    assert plan["weekly_target"] == 3
    assert plan["week_target"] == 3
    assert plan["start_date"] == "2026-11-01"

    target_five = _create_plan(
        client, title="每周读书 5 次", frequency="weekly", weekly_target=5
    )
    assert target_five["weekly_target"] == 5
    assert target_five["week_target"] == 5

    # daily 计划强制 weekly_target = 7
    daily = _create_plan(
        client, title="每日写作 200 字", frequency="daily", weekly_target=3
    )
    assert daily["weekly_target"] == 7


def test_plan_colors_cycle(client: TestClient) -> None:
    first = _create_plan(client, title="计划一")
    second = _create_plan(client, title="计划二")
    third = _create_plan(client, title="计划三")
    assert first["color"] == "#4FD8FF"
    assert second["color"] == "#A78BFA"
    assert third["color"] == "#FBBF24"
    explicit = _create_plan(client, title="显式颜色", color="#34d399")
    assert explicit["color"] == "#34D399"


@pytest.mark.parametrize(
    "payload",
    [
        {"title": ""},
        {"title": "   "},
        {"title": "标" * 41},
        {"title": "x", "frequency": "monthly"},
        {"title": "x", "frequency": "weekly", "weekly_target": 0},
        {"title": "x", "frequency": "weekly", "weekly_target": 8},
        {"title": "x", "color": "red"},
        {"title": "x", "color": "#12345"},
        {"title": "x", "start_date": "2026/11/17"},
        {"title": "x", "start_date": "2026-13-40"},
        {"title": "x", "description": "描" * 201},
    ],
)
def test_plan_create_validation(client: TestClient, payload: dict) -> None:
    response = client.post("/api/plans", json=payload)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]


def test_list_plans_and_include_archived(client: TestClient, frozen_today: date) -> None:
    first = _create_plan(client, title="计划一")
    _create_plan(client, title="计划二")
    client.put(f"/api/plans/{first['id']}", json={"archived": True})

    listing = client.get("/api/plans").json()
    assert listing["today"] == frozen_today.isoformat()
    assert listing["week_start"] == WEEK_START.isoformat()
    assert [item["title"] for item in listing["items"]] == ["计划二"]

    with_archived = client.get("/api/plans", params={"include_archived": 1}).json()
    assert len(with_archived["items"]) == 2


def test_plan_update_and_delete(client: TestClient, frozen_today: date) -> None:
    plan = _create_plan(
        client, title="每日阅读", description="旧描述", start_date="2026-11-01"
    )
    response = client.put(
        f"/api/plans/{plan['id']}",
        json={
            "title": "每日阅读 45 分钟",
            "description": "新描述",
            "color": "#a78bfa",
            "frequency": "weekly",
            "weekly_target": 4,
            "sort_order": 9,
        },
    )
    assert response.status_code == 200
    updated = response.json()["plan"]
    assert updated["title"] == "每日阅读 45 分钟"
    assert updated["description"] == "新描述"
    assert updated["color"] == "#A78BFA"
    assert updated["frequency"] == "weekly"
    assert updated["weekly_target"] == 4
    assert updated["week_target"] == 4
    assert updated["sort_order"] == 9

    # weekly → daily 时周目标被强制为 7
    back = client.put(f"/api/plans/{plan['id']}", json={"frequency": "daily"}).json()["plan"]
    assert back["weekly_target"] == 7
    # 显式清空描述
    cleared = client.put(f"/api/plans/{plan['id']}", json={"description": None}).json()["plan"]
    assert cleared["description"] is None

    assert client.get("/api/plans/p_missing").status_code == 404
    assert client.put("/api/plans/p_missing", json={"title": "x"}).status_code == 404

    _checkin(client, plan["id"], "2026-11-16")
    deleted = client.delete(f"/api/plans/{plan['id']}")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True, "deleted_checkins": 1}
    assert client.get(f"/api/plans/{plan['id']}").status_code == 404
    assert client.delete(f"/api/plans/{plan['id']}").status_code == 404


# ---------------------------------------------------------------------------
# 打卡
# ---------------------------------------------------------------------------


def test_checkin_duplicate_future_and_before_start(
    client: TestClient, frozen_today: date
) -> None:
    plan = _create_plan(client, title="每日阅读", start_date="2026-11-01")

    created = _checkin(client, plan["id"], "2026-11-17", "读完 20 页")
    assert created["ok"] is True
    assert created["checkin"]["plan_id"] == plan["id"]
    assert created["checkin"]["date"] == "2026-11-17"
    assert created["checkin"]["note"] == "读完 20 页"
    assert created["checkin"]["id"].startswith("ck_")
    assert created["plan"]["today_checked"] is True
    assert created["plan"]["week_checked"] == 1
    assert created["plan"]["current_streak"] == 1
    assert created["plan"]["total_checkins"] == 1
    assert created["plan"]["last_checkin_date"] == "2026-11-17"

    duplicate = client.post(f"/api/plans/{plan['id']}/checkins", json={"date": "2026-11-17"})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "这一天已经打过卡了"

    future = client.post(f"/api/plans/{plan['id']}/checkins", json={"date": "2026-11-18"})
    assert future.status_code == 400
    assert future.json()["detail"] == "不能给未来日期打卡"

    before_start = client.post(
        f"/api/plans/{plan['id']}/checkins", json={"date": "2026-10-31"}
    )
    assert before_start.status_code == 400
    assert "开始日期" in before_start.json()["detail"]

    invalid = client.post(
        f"/api/plans/{plan['id']}/checkins", json={"date": "2026-11-1"}
    )
    assert invalid.status_code == 400

    long_note = client.post(
        f"/api/plans/{plan['id']}/checkins",
        json={"date": "2026-11-16", "note": "备" * 121},
    )
    assert long_note.status_code == 400

    missing_plan = client.post(
        "/api/plans/p_missing/checkins", json={"date": "2026-11-17"}
    )
    assert missing_plan.status_code == 404


def test_checkin_default_today_and_empty_note(
    client: TestClient, frozen_today: date
) -> None:
    plan = _create_plan(client, title="每日复盘", start_date="2026-11-01")
    response = client.post(f"/api/plans/{plan['id']}/checkins", json={})
    assert response.status_code == 200
    payload = response.json()
    assert payload["checkin"]["date"] == frozen_today.isoformat()
    assert payload["checkin"]["note"] is None

    blank_note = client.post(
        f"/api/plans/{plan['id']}/checkins",
        json={"date": "2026-11-16", "note": "   "},
    ).json()
    assert blank_note["checkin"]["note"] is None


def test_undo_checkin(client: TestClient) -> None:
    plan = _create_plan(client, title="每日阅读", start_date="2026-11-01")
    _checkin(client, plan["id"], "2026-11-16")
    response = client.delete(f"/api/plans/{plan['id']}/checkins/2026-11-16")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["plan"]["total_checkins"] == 0
    assert payload["plan"]["today_checked"] is False

    missing = client.delete(f"/api/plans/{plan['id']}/checkins/2026-11-16")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "打卡记录不存在"

    missing_plan = client.delete("/api/plans/p_missing/checkins/2026-11-16")
    assert missing_plan.status_code == 404
    assert missing_plan.json()["detail"] == "计划不存在"


def test_checkin_list_range(client: TestClient, frozen_today: date) -> None:
    plan = _create_plan(client, title="每日阅读", start_date="2026-01-01")
    for day in ("2026-11-10", "2026-11-15", "2026-11-17"):
        _checkin(client, plan["id"], day)

    default = client.get(f"/api/plans/{plan['id']}/checkins").json()["items"]
    assert [item["date"] for item in default] == ["2026-11-10", "2026-11-15", "2026-11-17"]

    ranged = client.get(
        f"/api/plans/{plan['id']}/checkins",
        params={"start": "2026-11-11", "end": "2026-11-16"},
    ).json()["items"]
    assert [item["date"] for item in ranged] == ["2026-11-15"]

    reversed_range = client.get(
        f"/api/plans/{plan['id']}/checkins",
        params={"start": "2026-11-17", "end": "2026-11-10"},
    )
    assert reversed_range.status_code == 400
    assert "结束日期" in reversed_range.json()["detail"]

    too_wide = client.get(
        f"/api/plans/{plan['id']}/checkins",
        params={"start": "2025-01-01", "end": "2026-11-17"},
    )
    assert too_wide.status_code == 400
    assert "366" in too_wide.json()["detail"]

    missing_plan = client.get("/api/plans/p_missing/checkins")
    assert missing_plan.status_code == 404


# ---------------------------------------------------------------------------
# 连续统计
# ---------------------------------------------------------------------------


def test_daily_streak_current_and_longest(
    client: TestClient, frozen_today: date
) -> None:
    plan = _create_plan(client, title="每日阅读", start_date="2026-10-01")
    for day in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"):
        _checkin(client, plan["id"], day)
    for offset in range(8):  # 11-10 ~ 11-17 连续 8 天
        _checkin(client, plan["id"], (date(2026, 11, 10) + timedelta(days=offset)).isoformat())

    stats = _plan(client, plan["id"])
    assert stats["current_streak"] == 8
    assert stats["longest_streak"] == 8
    assert stats["total_checkins"] == 13
    assert stats["last_checkin_date"] == "2026-11-17"

    # 今天未打卡 → 从昨天开始数
    client.delete(f"/api/plans/{plan['id']}/checkins/2026-11-17")
    assert _plan(client, plan["id"])["current_streak"] == 7

    # 最近一次打卡早于昨天 → 0
    client.delete(f"/api/plans/{plan['id']}/checkins/2026-11-16")
    stats = _plan(client, plan["id"])
    assert stats["current_streak"] == 0
    assert stats["longest_streak"] == 6


def test_weekly_streak(client: TestClient, frozen_today: date) -> None:
    plan = _create_plan(
        client,
        title="每周跑步 2 次",
        frequency="weekly",
        weekly_target=2,
        start_date="2026-10-01",
    )
    # 11-02 周、11-09 周各达标 2 次；当前周（11-16）仅 1 次未达标
    for day in ("2026-11-02", "2026-11-03", "2026-11-09", "2026-11-10", "2026-11-16"):
        _checkin(client, plan["id"], day)

    stats = _plan(client, plan["id"])
    assert stats["current_streak"] == 2
    assert stats["longest_streak"] == 2
    assert stats["week_checked"] == 1
    assert stats["week_target"] == 2

    # 当前周补到达标 → 连续周数 +1
    _checkin(client, plan["id"], "2026-11-17")
    stats = _plan(client, plan["id"])
    assert stats["current_streak"] == 3
    assert stats["longest_streak"] == 3
    assert stats["week_checked"] == 2


def test_weekly_progress_fields(client: TestClient, frozen_today: date) -> None:
    plan = _create_plan(
        client,
        title="每周读书 3 次",
        frequency="weekly",
        weekly_target=3,
        start_date="2026-11-01",
    )
    _checkin(client, plan["id"], "2026-11-16")
    _checkin(client, plan["id"], "2026-11-17")
    stats = _plan(client, plan["id"])
    assert stats["week_checked"] == 2
    assert stats["week_target"] == 3
    assert stats["today_checked"] is True


# ---------------------------------------------------------------------------
# 单日行程（frequency=once）
# ---------------------------------------------------------------------------


def test_create_once_schedule(client: TestClient, frozen_today: date) -> None:
    plan = _create_plan(
        client,
        title="瑜伽课",
        description="带上瑜伽垫",
        frequency="once",
        plan_date="2026-11-17",
        start_time="19:00",
        end_time="20:00",
        location="社区健身房",
    )
    assert plan["frequency"] == "once"
    assert plan["plan_date"] == "2026-11-17"
    assert plan["start_date"] == "2026-11-17"  # 自动等于行程日期
    assert plan["start_time"] == "19:00"
    assert plan["end_time"] == "20:00"
    assert plan["location"] == "社区健身房"
    assert plan["weekly_target"] == 0
    assert plan["week_target"] == 0
    assert plan["checked"] is False
    assert plan["today_checked"] is False
    assert plan["current_streak"] == 0
    assert plan["total_checkins"] == 0

    listed = client.get("/api/plans").json()["items"]
    assert listed[0]["plan_date"] == "2026-11-17"
    assert listed[0]["frequency"] == "once"


def test_create_once_validation(client: TestClient, frozen_today: date) -> None:
    invalid_payloads = [
        {"title": "x", "frequency": "once"},  # 缺 plan_date
        {"title": "x", "frequency": "once", "plan_date": "2026/11/17"},
        {"title": "x", "frequency": "once", "plan_date": "2026-11-17", "start_time": "25:00"},
        {"title": "x", "frequency": "once", "plan_date": "2026-11-17", "end_time": "9:5"},
        {
            "title": "x",
            "frequency": "once",
            "plan_date": "2026-11-17",
            "start_time": "20:00",
            "end_time": "19:00",
        },
        {
            "title": "x",
            "frequency": "once",
            "plan_date": "2026-11-17",
            "location": "地" * 61,
        },
        {"title": "x", "frequency": "once", "plan_date": "2029-11-17"},  # 超出 ±2 年
    ]
    for payload in invalid_payloads:
        response = client.post("/api/plans", json=payload)
        assert response.status_code == 400, (payload, response.text)

    # 非 once 计划传入行程字段会被忽略（存 NULL）
    recurring = _create_plan(
        client,
        title="每日阅读",
        frequency="daily",
        plan_date="2026-11-17",
        start_time="09:00",
        location="图书馆",
    )
    assert recurring["plan_date"] is None
    assert recurring["start_time"] is None
    assert recurring["end_time"] is None
    assert recurring["location"] is None


def test_once_checkin_rules(client: TestClient, frozen_today: date) -> None:
    today_plan = _create_plan(
        client,
        title="瑜伽课",
        frequency="once",
        plan_date="2026-11-17",
        start_time="19:00",
        end_time="20:00",
        location="社区健身房",
    )
    future_plan = _create_plan(
        client, title="体检", frequency="once", plan_date="2026-11-20", start_time="09:00"
    )

    early = client.post(f"/api/plans/{future_plan['id']}/checkins")
    assert early.status_code == 400
    assert early.json()["detail"] == "行程还没到，不能提前完成"

    wrong_date = client.post(
        f"/api/plans/{today_plan['id']}/checkins", json={"date": "2026-11-16"}
    )
    assert wrong_date.status_code == 400
    assert wrong_date.json()["detail"] == "该行程只能在行程日期完成"

    # 省略 date 自动取 plan_date
    created = client.post(
        f"/api/plans/{today_plan['id']}/checkins", json={"note": "练完拉伸"}
    )
    assert created.status_code == 200
    payload = created.json()
    assert payload["checkin"]["date"] == "2026-11-17"
    assert payload["checkin"]["note"] == "练完拉伸"
    plan = payload["plan"]
    assert plan["checked"] is True
    assert plan["today_checked"] is True
    assert plan["current_streak"] == 0
    assert plan["longest_streak"] == 0
    assert plan["week_checked"] == 0
    assert plan["week_target"] == 0
    assert plan["total_checkins"] == 1

    duplicate = client.post(f"/api/plans/{today_plan['id']}/checkins")
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "这一天已经打过卡了"

    wrong_undo = client.delete(f"/api/plans/{today_plan['id']}/checkins/2026-11-16")
    assert wrong_undo.status_code == 400
    assert wrong_undo.json()["detail"] == "该行程只能在行程日期完成"

    undone = client.delete(f"/api/plans/{today_plan['id']}/checkins/2026-11-17")
    assert undone.status_code == 200
    assert undone.json()["plan"]["checked"] is False
    assert undone.json()["plan"]["today_checked"] is False

    missing = client.delete(f"/api/plans/{today_plan['id']}/checkins/2026-11-17")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "打卡记录不存在"


def test_once_past_can_be_completed_later(
    client: TestClient, frozen_today: date
) -> None:
    plan = _create_plan(
        client, title="体检", frequency="once", plan_date="2026-11-15", start_time="09:00"
    )
    created = client.post(
        f"/api/plans/{plan['id']}/checkins", json={"note": "补记：体检完成"}
    )
    assert created.status_code == 200
    payload = created.json()
    assert payload["checkin"]["date"] == "2026-11-15"  # 自动回到行程日期
    assert payload["plan"]["checked"] is True
    assert payload["plan"]["today_checked"] is False  # 不是今天

    overview = client.get("/api/plans/overview", params={"days": 30}).json()
    heatmap = {item["date"]: item["count"] for item in overview["heatmap"]}
    assert heatmap["2026-11-15"] == 1
    assert overview["stats"]["today_schedule_total"] == 0
    assert overview["stats"]["upcoming_schedule_total"] == 0


def test_plan_type_switch(client: TestClient, frozen_today: date) -> None:
    recurring = _create_plan(
        client, title="每日阅读", frequency="daily", start_date="2026-11-01"
    )
    missing_date = client.put(f"/api/plans/{recurring['id']}", json={"frequency": "once"})
    assert missing_date.status_code == 400
    assert "日期" in missing_date.json()["detail"]

    switched = client.put(
        f"/api/plans/{recurring['id']}",
        json={
            "frequency": "once",
            "plan_date": "2026-11-17",
            "start_time": "09:00",
            "end_time": "10:00",
            "location": "图书馆",
        },
    ).json()["plan"]
    assert switched["frequency"] == "once"
    assert switched["plan_date"] == "2026-11-17"
    assert switched["start_date"] == "2026-11-17"
    assert switched["start_time"] == "09:00"
    assert switched["end_time"] == "10:00"
    assert switched["location"] == "图书馆"
    assert switched["weekly_target"] == 0

    # once → weekly：清理行程字段，周目标取默认 3
    back = client.put(
        f"/api/plans/{recurring['id']}", json={"frequency": "weekly"}
    ).json()["plan"]
    assert back["frequency"] == "weekly"
    assert back["weekly_target"] == 3
    assert back["plan_date"] is None
    assert back["start_time"] is None
    assert back["end_time"] is None
    assert back["location"] is None
    assert back["checked"] is False

    daily = client.put(
        f"/api/plans/{recurring['id']}", json={"frequency": "daily"}
    ).json()["plan"]
    assert daily["weekly_target"] == 7


def test_plan_kind_filter(client: TestClient, frozen_today: date) -> None:
    _create_plan(client, title="每日阅读", frequency="daily")
    _create_plan(client, title="每周跑步", frequency="weekly")
    _create_plan(client, title="瑜伽课", frequency="once", plan_date="2026-11-17")

    assert len(client.get("/api/plans").json()["items"]) == 3
    recurring = client.get("/api/plans", params={"kind": "recurring"}).json()
    once = client.get("/api/plans", params={"kind": "once"}).json()
    assert len(recurring["items"]) == 2
    assert all(item["frequency"] != "once" for item in recurring["items"])
    assert len(once["items"]) == 1
    assert once["items"][0]["frequency"] == "once"
    assert recurring["kind"] == "recurring"

    invalid = client.get("/api/plans", params={"kind": "bogus"})
    assert invalid.status_code == 400
    assert "kind" in invalid.json()["detail"]


def test_overview_schedule_stats(client: TestClient, frozen_today: date) -> None:
    group = client.post("/api/plan-groups", json={"name": "日常习惯"}).json()["group"]
    daily = _create_plan(
        client, title="每日阅读", group_id=group["id"], start_date="2026-11-01"
    )
    _create_plan(client, title="每周跑步", frequency="weekly", start_date="2026-11-01")
    today_schedule = _create_plan(
        client, title="瑜伽课", frequency="once", plan_date="2026-11-17",
        start_time="19:00", group_id=group["id"],
    )
    _create_plan(
        client, title="体检", frequency="once", plan_date="2026-11-20", start_time="09:00"
    )
    past_schedule = _create_plan(
        client, title="牙医", frequency="once", plan_date="2026-11-15"
    )
    _create_plan(client, title="太远的行程", frequency="once", plan_date="2027-03-01")

    client.post(f"/api/plans/{daily['id']}/checkins", json={"date": "2026-11-17"})
    client.post(f"/api/plans/{past_schedule['id']}/checkins")

    stats = client.get("/api/plans/overview").json()["stats"]
    # 既有字段只统计循环计划
    assert stats["active_plans"] == 2
    assert stats["today_total"] == 2
    assert stats["today_checked"] == 1
    assert stats["recurring_today_total"] == 2
    assert stats["recurring_today_checked"] == 1
    assert stats["best_current_streak"] == 1
    # 新增统计
    assert stats["group_count"] == 1
    assert stats["today_schedule_total"] == 1
    assert stats["today_schedule_checked"] == 0
    assert stats["upcoming_schedule_total"] == 1  # 11-20 体检（2027-03 超出 30 天）
    assert stats["total_checkins"] == 2  # 循环 1 + 过去行程 1

    client.post(f"/api/plans/{today_schedule['id']}/checkins")
    stats = client.get("/api/plans/overview").json()["stats"]
    assert stats["today_schedule_checked"] == 1
    assert stats["total_checkins"] == 3


# ---------------------------------------------------------------------------
# 总览 / 统计
# ---------------------------------------------------------------------------


def _seed_overview(client: TestClient) -> tuple[dict, dict]:
    daily = _create_plan(
        client, title="每日阅读 30 分钟", start_date="2026-11-01"
    )
    weekly = _create_plan(
        client,
        title="每周跑步 3 次",
        frequency="weekly",
        weekly_target=3,
        start_date="2026-11-01",
    )
    for day in ("2026-11-15", "2026-11-16", "2026-11-17"):
        _checkin(client, daily["id"], day)
    for day in ("2026-11-10", "2026-11-12", "2026-11-16"):
        _checkin(client, weekly["id"], day)
    return daily, weekly


def test_overview_stats_heatmap_recent(
    client: TestClient, frozen_today: date
) -> None:
    _seed_overview(client)
    payload = client.get("/api/plans/overview", params={"days": 30}).json()

    assert payload["today"] == "2026-11-17"
    assert payload["week_start"] == "2026-11-16"
    assert payload["week_end"] == "2026-11-22"
    stats = payload["stats"]
    assert stats["active_plans"] == 2
    assert stats["today_total"] == 2
    assert stats["today_checked"] == 1
    assert stats["week_checked_total"] == 3  # daily 2 + weekly 1
    assert stats["week_target_total"] == 10  # 7 + 3
    assert stats["best_current_streak"] == 3
    assert stats["best_plan_title"] == "每日阅读 30 分钟"
    assert stats["total_checkins"] == 6

    heatmap = payload["heatmap"]
    assert len(heatmap) == 30
    assert heatmap[0]["date"] == "2026-10-19"
    assert heatmap[-1]["date"] == "2026-11-17"
    counts = {item["date"]: item["count"] for item in heatmap}
    assert counts["2026-11-17"] == 1
    assert counts["2026-11-16"] == 2
    assert counts["2026-11-15"] == 1
    assert counts["2026-11-14"] == 0
    dates = [item["date"] for item in heatmap]
    assert dates == sorted(dates)
    assert (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days == 29

    recent = payload["recent"]
    assert len(recent) == 6
    assert recent[0]["date"] == "2026-11-17"
    assert recent[0]["plan_title"] == "每日阅读 30 分钟"
    assert recent[0]["color"] == "#4FD8FF"
    assert recent[0]["note"] is None
    assert {"plan_id", "plan_title", "color", "date", "note", "created_at"} <= set(
        recent[0]
    )


def test_overview_empty_and_days_validation(client: TestClient) -> None:
    empty = client.get("/api/plans/overview").json()
    assert empty["stats"]["active_plans"] == 0
    assert empty["stats"]["today_checked"] == 0
    assert empty["stats"]["best_plan_title"] is None
    assert empty["stats"]["total_checkins"] == 0
    assert len(empty["heatmap"]) == 120
    assert empty["recent"] == []

    assert client.get("/api/plans/overview", params={"days": 29}).status_code == 422
    assert client.get("/api/plans/overview", params={"days": 367}).status_code == 422
    wide = client.get("/api/plans/overview", params={"days": 366}).json()
    assert len(wide["heatmap"]) == 366


def test_overview_excludes_archived_from_active_stats(
    client: TestClient, frozen_today: date
) -> None:
    plan = _create_plan(client, title="已归档计划", start_date="2026-11-01")
    _checkin(client, plan["id"], "2026-11-17")
    before = client.get("/api/plans/overview").json()["stats"]
    client.put(f"/api/plans/{plan['id']}", json={"archived": True})
    after = client.get("/api/plans/overview").json()["stats"]
    assert before["active_plans"] == 1
    assert after["active_plans"] == 0
    assert after["today_checked"] == 0
    # 热力图 / 累计打卡保留历史记录
    assert after["total_checkins"] == 1


def test_plan_delete_cascades_checkins(
    client: TestClient, frozen_today: date
) -> None:
    plan = _create_plan(client, title="每日写作", start_date="2026-11-01")
    for day in ("2026-11-15", "2026-11-16", "2026-11-17"):
        _checkin(client, plan["id"], day)
    assert client.get("/api/plans/overview").json()["stats"]["total_checkins"] == 3

    response = client.delete(f"/api/plans/{plan['id']}")
    assert response.json()["deleted_checkins"] == 3
    assert client.get("/api/plans").json()["items"] == []
    overview = client.get("/api/plans/overview").json()
    assert overview["stats"]["total_checkins"] == 0
    assert all(item["count"] == 0 for item in overview["heatmap"])


def test_stats_plans_summary_and_activity(
    client: TestClient, frozen_today: date
) -> None:
    plan = _create_plan(client, title="每日阅读", start_date="2026-11-01")
    _checkin(client, plan["id"], "2026-11-17")

    stats = client.get("/api/stats").json()
    assert stats["plans"] == {"total": 1, "today_checked": 1, "groups": 0}
    activity = client.get("/api/stats").json()["recent"]
    assert any(
        entry["kind"] == "plan" and "新建计划" in entry["detail"] for entry in activity
    )
    assert any(
        entry["kind"] == "plan" and "完成打卡" in entry["detail"] for entry in activity
    )


# ---------------------------------------------------------------------------
# 迁移（schema_version 2 → 3）
# ---------------------------------------------------------------------------

_LEGACY_V2_SCHEMA = """
CREATE TABLE documents(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT, source_type TEXT NOT NULL,
  source_url TEXT, tags TEXT NOT NULL DEFAULT '[]',
  summary TEXT, content TEXT, size INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ready',
  error TEXT, zone_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE zones(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL, description TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE conversations(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE messages(
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL, content TEXT NOT NULL, citations TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def test_migration_v2_to_v3_is_idempotent_and_preserves_data(tmp_path) -> None:
    legacy_path = tmp_path / "legacy_v2.db"
    conn = sqlite3.connect(legacy_path)
    conn.executescript(_LEGACY_V2_SCHEMA)
    conn.execute(
        "INSERT INTO documents(id, title, source_type, tags, content, size, created_at, updated_at) "
        "VALUES('d_legacy', '历史文档', 'md', '[\"AI\"]', '正文', 2, "
        "'2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO zones(id, name, color, sort_order, created_at, updated_at) "
        "VALUES('z_legacy', '历史分区', '#4FD8FF', 1, "
        "'2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
    )
    conn.execute("UPDATE documents SET zone_id='z_legacy' WHERE id='d_legacy'")
    conn.execute(
        "INSERT INTO conversations(id, title, created_at, updated_at) "
        "VALUES('conv_legacy', '历史会话', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO messages(id, conversation_id, role, content, created_at) "
        "VALUES('m_legacy', 'conv_legacy', 'user', '历史消息', '2026-11-01T00:00:00Z')"
    )
    conn.executemany(
        "INSERT INTO settings(key, value) VALUES(?, ?)",
        [
            ("schema_version", "2"),
            ("llm_base_url", "https://legacy.example/v1"),
            ("llm_api_key", "sk-legacy-secret"),
            ("llm_model", "legacy-model"),
        ],
    )
    conn.commit()
    conn.close()

    db.init_db(legacy_path)
    db.init_db(legacy_path)  # 幂等

    conn = db.connect(legacy_path)  # 启用外键，验证级联删除
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"plans", "plan_checkins"} <= tables
        indexes = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {"idx_checkins_plan_date", "idx_checkins_date"} <= indexes
        assert conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0] == "6"
        # 既有数据全部保留
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        document = conn.execute("SELECT zone_id FROM documents WHERE id='d_legacy'").fetchone()
        assert document["zone_id"] == "z_legacy"
        settings = {
            row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")
        }
        assert settings["llm_base_url"] == "https://legacy.example/v1"
        assert settings["llm_api_key"] == "sk-legacy-secret"
        assert settings["llm_model"] == "legacy-model"
        # 新表可正常写入并级联删除
        conn.execute(
            "INSERT INTO plans(id, title, color, frequency, weekly_target, start_date, "
            "archived, sort_order, created_at, updated_at) "
            "VALUES('p_legacy', '旧库新计划', '#4FD8FF', 'daily', 7, '2026-11-01', 0, 1, "
            "'2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO plan_checkins(id, plan_id, checkin_date, note, created_at) "
            "VALUES('ck_legacy', 'p_legacy', '2026-11-01', NULL, '2026-11-01T00:00:00Z')"
        )
        conn.execute("DELETE FROM plans WHERE id='p_legacy'")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM plan_checkins").fetchone()[0] == 0
    finally:
        conn.close()


_LEGACY_V3_SCHEMA = """
CREATE TABLE documents(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT, source_type TEXT NOT NULL,
  source_url TEXT, tags TEXT NOT NULL DEFAULT '[]',
  summary TEXT, content TEXT, size INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ready',
  error TEXT, zone_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE zones(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL, description TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE plans(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT, color TEXT NOT NULL,
  frequency TEXT NOT NULL, weekly_target INTEGER NOT NULL, start_date TEXT NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE plan_checkins(
  id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  checkin_date TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL,
  UNIQUE(plan_id, checkin_date)
);
CREATE TABLE conversations(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE messages(
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL, content TEXT NOT NULL, citations TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def test_migration_v3_to_v4_preserves_existing_plans_and_checkins(tmp_path) -> None:
    legacy_path = tmp_path / "legacy_v3.db"
    conn = sqlite3.connect(legacy_path)
    conn.executescript(_LEGACY_V3_SCHEMA)
    conn.execute(
        "INSERT INTO documents(id, title, source_type, tags, content, size, created_at, updated_at) "
        "VALUES('d_legacy', '历史文档', 'md', '[]', '正文', 2, "
        "'2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO zones(id, name, color, sort_order, created_at, updated_at) "
        "VALUES('z_legacy', '历史分区', '#4FD8FF', 1, "
        "'2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
    )
    conn.execute("UPDATE documents SET zone_id='z_legacy' WHERE id='d_legacy'")
    conn.execute(
        "INSERT INTO conversations(id, title, created_at, updated_at) "
        "VALUES('conv_legacy', '历史会话', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO messages(id, conversation_id, role, content, created_at) "
        "VALUES('m_legacy', 'conv_legacy', 'user', '历史消息', '2026-11-01T00:00:00Z')"
    )
    conn.executemany(
        "INSERT INTO plans(id, title, color, frequency, weekly_target, start_date, "
        "archived, sort_order, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        [
            ("p_daily", "每日阅读", "#4FD8FF", "daily", 7, "2026-11-01", 1,
             "2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z"),
            ("p_weekly", "每周跑步", "#A78BFA", "weekly", 3, "2026-11-01", 2,
             "2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z"),
        ],
    )
    conn.executemany(
        "INSERT INTO plan_checkins(id, plan_id, checkin_date, note, created_at) "
        "VALUES(?, ?, ?, ?, ?)",
        [
            ("ck_1", "p_daily", "2026-11-15", None, "2026-11-15T00:00:00Z"),
            ("ck_2", "p_daily", "2026-11-16", "读完 20 页", "2026-11-16T00:00:00Z"),
            ("ck_3", "p_weekly", "2026-11-16", None, "2026-11-16T00:00:00Z"),
        ],
    )
    conn.executemany(
        "INSERT INTO settings(key, value) VALUES(?, ?)",
        [
            ("schema_version", "3"),
            ("llm_base_url", "https://legacy.example/v1"),
            ("llm_api_key", "sk-legacy-secret"),
            ("llm_model", "legacy-model"),
        ],
    )
    conn.commit()
    conn.close()

    db.init_db(legacy_path)
    db.init_db(legacy_path)  # 幂等

    conn = db.connect(legacy_path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        schema_version = conn.execute(
            "SELECT value FROM settings WHERE key='schema_version'"
        ).fetchone()[0]
        assert schema_version == "6"
        plan_columns = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
        assert {"group_id", "plan_date", "start_time", "end_time", "location"} <= plan_columns
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "plan_groups" in tables
        indexes = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {"idx_plans_group", "idx_plans_plan_date"} <= indexes
        # 既有数据全部保留
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM plan_checkins").fetchone()[0] == 3
        plans_rows = {
            row["id"]: row
            for row in conn.execute(
                "SELECT * FROM plans ORDER BY id"
            ).fetchall()
        }
        assert plans_rows["p_daily"]["frequency"] == "daily"
        assert plans_rows["p_daily"]["weekly_target"] == 7
        assert plans_rows["p_weekly"]["frequency"] == "weekly"
        assert plans_rows["p_weekly"]["weekly_target"] == 3
        assert plans_rows["p_daily"]["group_id"] is None
        assert plans_rows["p_daily"]["plan_date"] is None
        settings = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM settings")
        }
        assert settings["llm_base_url"] == "https://legacy.example/v1"
        assert settings["llm_api_key"] == "sk-legacy-secret"
        assert settings["llm_model"] == "legacy-model"
    finally:
        conn.close()


def test_once_without_times_and_update_time_validation(
    client: TestClient, frozen_today: date
) -> None:
    plan = _create_plan(client, title="牙医", frequency="once", plan_date="2026-11-17")
    assert plan["start_time"] is None
    assert plan["end_time"] is None
    assert plan["location"] is None

    bad_range = client.put(
        f"/api/plans/{plan['id']}",
        json={"start_time": "18:00", "end_time": "18:00"},
    )
    assert bad_range.status_code == 400
    assert "结束时间" in bad_range.json()["detail"]

    updated = client.put(
        f"/api/plans/{plan['id']}",
        json={"start_time": "18:00", "end_time": "18:30", "location": "牙科诊所"},
    ).json()["plan"]
    assert updated["start_time"] == "18:00"
    assert updated["end_time"] == "18:30"
    assert updated["location"] == "牙科诊所"

    cleared = client.put(
        f"/api/plans/{plan['id']}",
        json={"start_time": None, "end_time": None, "location": None},
    ).json()["plan"]
    assert cleared["start_time"] is None
    assert cleared["end_time"] is None
    assert cleared["location"] is None


def test_once_plan_respects_archived_and_kind_filter(
    client: TestClient, frozen_today: date
) -> None:
    plan = _create_plan(
        client, title="旧行程", frequency="once", plan_date="2026-11-10"
    )
    client.put(f"/api/plans/{plan['id']}", json={"archived": True})
    assert client.get("/api/plans").json()["items"] == []
    archived = client.get("/api/plans", params={"include_archived": 1}).json()["items"]
    assert len(archived) == 1
    assert archived[0]["frequency"] == "once"
    assert client.get("/api/plans", params={"kind": "once"}).json()["items"] == []
