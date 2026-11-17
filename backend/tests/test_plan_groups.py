"""计划分组 + 单日行程（计划侧）测试（SPEC-PLAN-GROUPS 第 2 / 3 节）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

GROUP_KEYS = {
    "id",
    "name",
    "color",
    "description",
    "sort_order",
    "plan_count",
    "created_at",
    "updated_at",
}


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _create_group(client: TestClient, name: str, **kwargs) -> dict:
    response = client.post("/api/plan-groups", json={"name": name, **kwargs})
    assert response.status_code == 200, response.text
    group = response.json()["group"]
    assert set(group) == GROUP_KEYS
    return group


def _create_plan(client: TestClient, **payload) -> dict:
    response = client.post("/api/plans", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["plan"]


def _plan(client: TestClient, plan_id: str) -> dict:
    response = client.get(f"/api/plans/{plan_id}")
    assert response.status_code == 200, response.text
    return response.json()["plan"]


# ---------------------------------------------------------------------------
# 分组 CRUD
# ---------------------------------------------------------------------------


def test_groups_empty_then_create_defaults(client: TestClient) -> None:
    assert client.get("/api/plan-groups").json() == {
        "items": [],
        "ungrouped_count": 0,
    }

    first = _create_group(client, "日常习惯")
    assert first["id"].startswith("pg_")
    assert first["name"] == "日常习惯"
    assert first["color"] == "#4FD8FF"
    assert first["description"] is None
    assert first["sort_order"] == 1
    assert first["plan_count"] == 0

    second = _create_group(client, "生活行程", description="出行与健康")
    assert second["color"] == "#A78BFA"
    assert second["sort_order"] == 2
    assert second["description"] == "出行与健康"

    # 未分组计划计数
    _create_plan(client, title="未分组计划一")
    _create_plan(client, title="未分组计划二")
    payload = client.get("/api/plan-groups").json()
    assert [item["name"] for item in payload["items"]] == ["日常习惯", "生活行程"]
    assert payload["ungrouped_count"] == 2


def test_group_create_validation(client: TestClient) -> None:
    _create_group(client, "日常习惯")

    duplicate = client.post("/api/plan-groups", json={"name": " 日常习惯 "})
    assert duplicate.status_code == 400
    assert duplicate.json()["detail"] == "分组名称已存在"

    assert client.post("/api/plan-groups", json={"name": "  "}).status_code == 400
    assert client.post("/api/plan-groups", json={"name": "组" * 21}).status_code == 400
    for bad_color in ("red", "#12345", "#GGGGGG"):
        assert (
            client.post("/api/plan-groups", json={"name": "颜色", "color": bad_color}).status_code
            == 400
        )
    assert (
        client.post(
            "/api/plan-groups", json={"name": "长描述", "description": "描" * 121}
        ).status_code
        == 400
    )
    explicit = client.post(
        "/api/plan-groups", json={"name": "显式颜色", "color": "#a78bfa"}
    ).json()["group"]
    assert explicit["color"] == "#A78BFA"


def test_group_update_and_delete(client: TestClient) -> None:
    group = _create_group(client, "日常习惯", description="旧描述")
    updated = client.put(
        f"/api/plan-groups/{group['id']}",
        json={
            "name": "日常习惯（新）",
            "color": "#34d399",
            "description": "新描述",
            "sort_order": 9,
        },
    )
    assert updated.status_code == 200
    body = updated.json()["group"]
    assert body["name"] == "日常习惯（新）"
    assert body["color"] == "#34D399"
    assert body["description"] == "新描述"
    assert body["sort_order"] == 9

    cleared = client.put(
        f"/api/plan-groups/{group['id']}", json={"description": None}
    ).json()["group"]
    assert cleared["description"] is None

    blank = client.put(f"/api/plan-groups/{group['id']}", json={"name": "  "})
    assert blank.status_code == 400
    assert client.put("/api/plan-groups/pg_missing", json={"name": "x"}).status_code == 404

    # 删除分组：计划与打卡保留，仅变未分组
    plan = _create_plan(
        client,
        title="每日阅读",
        group_id=group["id"],
        start_date="2026-11-01",
    )
    client.post(f"/api/plans/{plan['id']}/checkins", json={"date": "2026-11-16"})
    deleted = client.delete(f"/api/plan-groups/{group['id']}")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True, "ungrouped_count": 1}

    assert client.get("/api/plan-groups").json()["items"] == []
    refreshed = _plan(client, plan["id"])
    assert refreshed["group_id"] is None
    assert refreshed["group_name"] == "未分组"
    assert refreshed["group_color"] == "#5B6F8C"
    assert refreshed["total_checkins"] == 1
    checkins = client.get(f"/api/plans/{plan['id']}/checkins").json()["items"]
    assert len(checkins) == 1
    assert client.delete(f"/api/plan-groups/{group['id']}").status_code == 404


def test_plan_group_assignment_fields(client: TestClient) -> None:
    group = _create_group(client, "日常习惯")
    grouped = _create_plan(client, title="每日阅读", group_id=group["id"])
    assert grouped["group_id"] == group["id"]
    assert grouped["group_name"] == "日常习惯"
    assert grouped["group_color"] == "#4FD8FF"

    ungrouped = _create_plan(client, title="无分组计划")
    assert ungrouped["group_id"] is None
    assert ungrouped["group_name"] == "未分组"
    assert ungrouped["group_color"] == "#5B6F8C"

    moved = client.put(
        f"/api/plans/{ungrouped['id']}", json={"group_id": group["id"]}
    ).json()["plan"]
    assert moved["group_id"] == group["id"]
    assert moved["group_name"] == "日常习惯"

    cleared = client.put(f"/api/plans/{ungrouped['id']}", json={"group_id": None}).json()[
        "plan"
    ]
    assert cleared["group_id"] is None

    unknown = client.post(
        "/api/plans", json={"title": "坏分组", "group_id": "pg_missing"}
    )
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "分组不存在"
    unknown_put = client.put(
        f"/api/plans/{ungrouped['id']}", json={"group_id": "pg_missing"}
    )
    assert unknown_put.status_code == 404
    assert unknown_put.json()["detail"] == "分组不存在"


def test_group_plan_count_and_ungrouped_count(client: TestClient) -> None:
    group = _create_group(client, "日常习惯")
    plan_one = _create_plan(client, title="计划一", group_id=group["id"])
    _create_plan(client, title="计划二", group_id=group["id"])
    _create_plan(client, title="计划三")

    payload = client.get("/api/plan-groups").json()
    assert payload["items"][0]["plan_count"] == 2
    assert payload["ungrouped_count"] == 1

    # 归档计划不计入分组 plan_count / 未分组计数
    client.put(f"/api/plans/{plan_one['id']}", json={"archived": True})
    payload = client.get("/api/plan-groups").json()
    assert payload["items"][0]["plan_count"] == 1
    assert payload["ungrouped_count"] == 1

    # 编辑分组名称不影响计划归属
    client.put(f"/api/plan-groups/{group['id']}", json={"name": "新分组名"})
    listed = client.get("/api/plans").json()["items"]
    grouped = next(item for item in listed if item["group_id"] == group["id"])
    assert grouped["group_name"] == "新分组名"


def test_plan_list_includes_group_fields(client: TestClient) -> None:
    group = _create_group(client, "学习成长")
    _create_plan(client, title="每日阅读", group_id=group["id"])
    payload = client.get("/api/plans").json()
    item = payload["items"][0]
    for key in (
        "group_id",
        "group_name",
        "group_color",
        "plan_date",
        "start_time",
        "end_time",
        "location",
        "checked",
    ):
        assert key in item
    assert payload["kind"] == "all"


def test_group_sort_order_and_color_cycle(client: TestClient) -> None:
    first = _create_group(client, "第一组")
    second = _create_group(client, "第二组")
    third = _create_group(client, "第三组")
    assert [first["color"], second["color"], third["color"]] == [
        "#4FD8FF",
        "#A78BFA",
        "#FBBF24",
    ]
    assert [first["sort_order"], second["sort_order"], third["sort_order"]] == [1, 2, 3]

    client.put(f"/api/plan-groups/{third['id']}", json={"sort_order": 0})
    names = [item["name"] for item in client.get("/api/plan-groups").json()["items"]]
    assert names[0] == "第三组"


def test_delete_group_with_multiple_plans_keeps_checkins(client: TestClient) -> None:
    group = _create_group(client, "日常习惯")
    first = _create_plan(
        client, title="每日阅读", group_id=group["id"], start_date="2026-11-01"
    )
    second = _create_plan(
        client, title="每日写作", group_id=group["id"], start_date="2026-11-01"
    )
    for plan in (first, second):
        client.post(f"/api/plans/{plan['id']}/checkins", json={"date": "2026-11-16"})

    deleted = client.delete(f"/api/plan-groups/{group['id']}")
    assert deleted.json() == {"ok": True, "ungrouped_count": 2}

    plans = client.get("/api/plans").json()["items"]
    assert len(plans) == 2
    assert all(item["group_id"] is None for item in plans)
    for plan in plans:
        checkins = client.get(f"/api/plans/{plan['id']}/checkins").json()["items"]
        assert len(checkins) == 1


def test_group_activity_recorded(client: TestClient) -> None:
    group = _create_group(client, "日常习惯")
    client.delete(f"/api/plan-groups/{group['id']}")
    activity = client.get("/api/stats").json()["recent"]
    assert any(
        entry["kind"] == "plan_group" and "新建分组" in entry["detail"]
        for entry in activity
    )
    assert any(
        entry["kind"] == "plan_group" and "删除分组" in entry["detail"]
        for entry in activity
    )
