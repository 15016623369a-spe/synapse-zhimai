"""Provision a curated set of knowledge zones for the current Synapse library.

Idempotent: existing zones (matched by name) are reused; documents are (re)assigned
by title pattern. Unmatched documents stay unassigned.

Usage:
    backend/.venv/bin/python scripts/setup_zones.py [base_url]
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7800").rstrip("/")

ZONES: list[dict] = [
    {
        "name": "AI 与大模型",
        "color": "#4FD8FF",
        "description": "大模型、RAG、Agent、评测与面试准备笔记",
        "patterns": [
            "00-总纲",
            "01-RAG",
            "02-Agent",
            "03-记忆与上下文",
            "04-工程化与评测安全",
            "05-模型基础与系统设计",
            "06-面经原题",
            "RAG 检索增强生成实践笔记",
            "向量检索与混合检索调参记录",
            "提示词工程踩坑笔记",
            "Agent 与工作流",
        ],
    },
    {
        "name": "知识管理与学习",
        "color": "#A78BFA",
        "description": "卡片盒、间隔重复、费曼与深度工作",
        "patterns": ["卡片盒笔记法", "间隔重复与费曼学习法", "深度工作"],
    },
    {
        "name": "产品与写作",
        "color": "#FBBF24",
        "description": "用户访谈、MVP 验证与技术写作",
        "patterns": ["MVP 不是缩水版", "用户访谈", "技术写作与金字塔原理"],
    },
    {
        "name": "健康与生活",
        "color": "#34D399",
        "description": "跑步、睡眠恢复与旅行计划",
        "patterns": ["跑步心率区间与周末徒步", "睡眠恢复", "京都旅行计划"],
    },
    {
        "name": "理财与决策",
        "color": "#FB7185",
        "description": "资产配置、应急金与决策清单",
        "patterns": ["资产配置", "应急金该存多少", "我的决策清单"],
    },
]


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - localhost
        raw = resp.read()
    return json.loads(raw) if raw else {}


def main() -> int:
    docs = call("GET", "/api/documents")["items"]
    existing = {z["name"]: z for z in call("GET", "/api/zones")["items"]}
    assigned = 0
    summary = []
    for spec in ZONES:
        zone = existing.get(spec["name"])
        if zone is None:
            created = call(
                "POST",
                "/api/zones",
                {"name": spec["name"], "color": spec["color"], "description": spec["description"]},
            )
            zone = created["zone"]
            print(f"+ 新建分区《{spec['name']}》({zone['id']})")
        else:
            print(f"= 复用分区《{spec['name']}》({zone['id']})")
        doc_ids = []
        for doc in docs:
            title = doc.get("title", "")
            if any(pattern in title for pattern in spec["patterns"]):
                doc_ids.append(doc["id"])
        if doc_ids:
            result = call("PUT", "/api/documents/zone", {"doc_ids": doc_ids, "zone_id": zone["id"]})
            assigned += int(result.get("updated", 0))
            summary.append((spec["name"], len(doc_ids)))
        else:
            summary.append((spec["name"], 0))
    print("\n=== zones summary ===")
    for name, count in summary:
        print(f"  {name}: {count} 篇")
    print(f"assigned total: {assigned} / {len(docs)} docs")
    zones = call("GET", "/api/zones")
    print(f"zones: {len(zones['items'])}, unassigned docs: {zones['unassigned_count']}")
    unmatched = [d["title"] for d in docs if not any(
        any(p in d["title"] for p in spec["patterns"]) for spec in ZONES
    )]
    if unmatched:
        print("未匹配（保持未分区）:", unmatched)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
