"""Seed demo plan groups and one-off schedule plans for the Plans page.

Idempotent by plan title / group name. Existing plans get assigned to groups if ungrouped.

Usage:
    backend/.venv/bin/python scripts/setup_plan_groups.py [base_url]
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, timedelta

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7800").rstrip("/")
TODAY = date.today()


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - localhost
        raw = resp.read()
    return json.loads(raw) if raw else {}


def ensure_group(name: str, color: str, description: str) -> dict:
    groups = call("GET", "/api/plan-groups")["items"]
    found = next((g for g in groups if g["name"] == name), None)
    if found:
        return found
    group = call(
        "POST", "/api/plan-groups", {"name": name, "color": color, "description": description}
    )["group"]
    print(f"+ 分组《{name}》({group['id']})")
    return group


def ensure_schedule(
    title: str,
    plan_date: str,
    color: str,
    group_id: str,
    *,
    start_time: str | None = None,
    end_time: str | None = None,
    location: str | None = None,
    description: str | None = None,
) -> dict:
    plans = call("GET", "/api/plans?kind=once")["items"]
    found = next((p for p in plans if p["title"] == title), None)
    if found:
        return found
    plan = call(
        "POST",
        "/api/plans",
        {
            "title": title,
            "description": description,
            "color": color,
            "frequency": "once",
            "plan_date": plan_date,
            "start_time": start_time,
            "end_time": end_time,
            "location": location,
            "group_id": group_id,
        },
    )["plan"]
    print(f"+ 单日行程《{title}》{plan_date} ({plan['id']})")
    return plan


def main() -> int:
    habit_group = ensure_group("日常习惯", "#4FD8FF", "每天要做的小事")
    schedule_group = ensure_group("生活行程", "#34D399", "出行与健康安排")

    plans = call("GET", "/api/plans")["items"]
    for plan in plans:
        if plan["title"] in ("每日阅读 30 分钟", "每周跑步 3 次", "每日写作 200 字"):
            if plan.get("group_id") != habit_group["id"]:
                call("PUT", f"/api/plans/{plan['id']}", {"group_id": habit_group["id"]})
                print(f"→ 将《{plan['title']}》移入分组《日常习惯》")

    ensure_schedule(
        "瑜伽课",
        TODAY.isoformat(),
        "#FB7185",
        schedule_group["id"],
        start_time="19:00",
        end_time="20:00",
        location="社区健身房",
        description="带上瑜伽垫，练完拉伸 10 分钟",
    )
    ensure_schedule(
        "体检",
        (TODAY + timedelta(days=3)).isoformat(),
        "#34D399",
        schedule_group["id"],
        start_time="09:00",
        location="市一医院",
        description="空腹，带医保卡和上次报告",
    )
    ensure_schedule(
        "交水电费",
        (TODAY - timedelta(days=1)).isoformat(),
        "#60A5FA",
        schedule_group["id"],
        description="物业 App 缴费",
    )

    groups = call("GET", "/api/plan-groups")
    plans = call("GET", "/api/plans")["items"]
    overview = call("GET", "/api/plans/overview?days=120")
    print("\n=== plan groups ===")
    for g in groups["items"]:
        print(f"  {g['name']} ({g['color']}): {g['plan_count']} 个计划")
    print("ungrouped plans:", groups["ungrouped_count"])
    print("\n=== plans ===")
    for p in plans:
        extra = ""
        if p["frequency"] == "once":
            extra = f" | {p['plan_date']} {p['start_time'] or '全天'}{'–' + p['end_time'] if p['end_time'] else ''} | {p['location'] or ''} | checked={p['checked']}"
        print(f"  [{p.get('group_name')}] {p['title']} ({p['frequency']}){extra}")
    print("\noverview stats:", overview["stats"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
