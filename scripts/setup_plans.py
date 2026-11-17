"""Seed three demo plans (with recent check-in history) for a populated Plans page.

Idempotent-ish: plans are created only if a plan with the same title does not exist;
demo check-ins are only added for plans created by this run.

Usage:
    backend/.venv/bin/python scripts/setup_plans.py [base_url]
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


def d(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


def checkin(plan_id: str, day: str, note: str | None = None) -> None:
    body: dict = {"date": day}
    if note:
        body["note"] = note
    call("POST", f"/api/plans/{plan_id}/checkins", body)


def main() -> int:
    existing = {p["title"]: p for p in call("GET", "/api/plans").get("items", [])}
    created: list[dict] = []

    if "每日阅读 30 分钟" not in existing:
        plan = call(
            "POST",
            "/api/plans",
            {
                "title": "每日阅读 30 分钟",
                "description": "睡前读非虚构类，手机放客厅，读完记一句",
                "color": "#4FD8FF",
                "frequency": "daily",
                "start_date": d(-16),
            },
        )["plan"]
        # 12 consecutive days ending yesterday → current streak 12, today still open
        for offset in range(-12, 0):
            note = "睡前读完了 20 页非虚构" if offset == -1 else None
            checkin(plan["id"], d(offset), note)
        created.append(plan)
        print(f"+ 每日阅读 30 分钟 ({plan['id']})，补 12 天记录，今天留空")

    if "每周跑步 3 次" not in existing:
        plan = call(
            "POST",
            "/api/plans",
            {
                "title": "每周跑步 3 次",
                "description": "每次 5km，轻松跑，周末可换成徒步",
                "color": "#A78BFA",
                "frequency": "weekly",
                "weekly_target": 3,
                "start_date": d(-28),
            },
        )["plan"]
        monday = TODAY - timedelta(days=TODAY.weekday())
        for week in (3, 2, 1):
            base = monday - timedelta(days=7 * week)
            for offset, note in ((0, "晨跑 5km，配速 6'10\""), (2, None), (4, "夜跑 5km，放松跑")):
                checkin(plan["id"], (base + timedelta(days=offset)).isoformat(), note)
        checkin(plan["id"], TODAY.isoformat(), "傍晚轻松跑 5km")
        created.append(plan)
        print(f"+ 每周跑步 3 次 ({plan['id']})，补 3 个达标周 + 今天打卡")

    if "每日写作 200 字" not in existing:
        plan = call(
            "POST",
            "/api/plans",
            {
                "title": "每日写作 200 字",
                "description": "记录当天的一个想法，200 字即可，不发出去也没关系",
                "color": "#FBBF24",
                "frequency": "daily",
                "start_date": d(-9),
            },
        )["plan"]
        for offset in range(-8, 1):
            note = "写了一段本周复盘" if offset == -1 else ("记录了关于 RAG 的一个想法" if offset == -4 else None)
            checkin(plan["id"], d(offset), note)
        created.append(plan)
        print(f"+ 每日写作 200 字 ({plan['id']})，补 9 天记录（含今天）")

    overview = call("GET", "/api/plans/overview?days=120")
    plans = call("GET", "/api/plans")["items"]
    print("\n=== seeded plans ===")
    for p in plans:
        print(
            f"  {p['title']}: 今日{'✓' if p['today_checked'] else '—'} "
            f"本周 {p['week_checked']}/{p['week_target']} 连续 {p['current_streak']} 累计 {p['total_checkins']}"
        )
    print("\noverview stats:", overview["stats"])
    print("created this run:", len(created))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
