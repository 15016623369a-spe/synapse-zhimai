"""V2：反馈驱动 Reranker + 定时知识巡检测试（SPEC-V2-RERANK-PATROL）。

覆盖：
- rerank 偏好画像（host 平滑偏好 / URL / term）、summary API、无反馈不改变排序趋势、
  accepted host 加分 / rejected host 降权、精确 URL ±1、/extend/search 集成字段；
- patrol 默认配置与 PATCH 校验、run_patrol 自动整理 + issues + 结果持久化、
  auto_enrich 关闭、单篇失败不中断、重复/过时/孤立/过短/无标签检测、
  运行中 409、调度 due 逻辑与 tick 触发（不启动真实 task）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db
from app.services import patrol, rerank
from app.utils import new_id

from .conftest import add_note

#: 短文档（<800 字）与长文档（>800 字），用于过短检测
SHORT_CONTENT = "知识巡检短文档。" * 40  # 320 字
LONG_CONTENT = "知识巡检长文档正文内容。" * 100  # 1200 字


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """搜索用 mock、不访问真实网络；调度器不自动启动。"""
    monkeypatch.setenv("SYNAPSE_SEARCH_PROVIDER", "mock")
    monkeypatch.setenv("SYNAPSE_SEARCH_ENABLED", "1")
    monkeypatch.delenv("SYNAPSE_SEARCH_MOCK_RESULTS", raising=False)


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _add_feedback(rows: list[tuple[str, str, str, str | None]]) -> None:
    """直接写入 knowledge_feedback（url, title, decision, query）。"""
    with db.get_db() as conn:
        for url, title, decision, query in rows:
            conn.execute(
                "INSERT INTO knowledge_feedback(id, url, title, query, decision, doc_id, created_at) "
                "VALUES(?, ?, ?, ?, ?, NULL, ?)",
                (new_id("kf"), url, title, query, decision, "2026-11-17T00:00:00Z"),
            )


def _feedback_count() -> int:
    with db.get_db() as conn:
        return db.table_count(conn, "knowledge_feedback")


def _candidate(
    url: str,
    *,
    title: str = "RAG 评估资料",
    snippet: str = "RAG 评估指标与检索实践",
    relevance: float = 0.8,
    authority: str = "unknown",
    authority_score: float = 0.5,
) -> dict[str, object]:
    return {
        "url": url,
        "title": title,
        "snippet": snippet,
        "source": "mock",
        "relevance": relevance,
        "authority": authority,
        "authority_category": authority,
        "authority_score": authority_score,
    }


def _rerank(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    with db.get_db() as conn:
        return rerank.rerank_candidates(conn, candidates, "RAG 评估指标")


def _find(items: list[dict[str, object]], url: str) -> dict[str, object]:
    for item in items:
        if item["url"] == url:
            return item
    raise AssertionError(f"候选不存在：{url}")


# ---------------------------------------------------------------------------
# Reranker：偏好画像 / 摘要
# ---------------------------------------------------------------------------


def test_load_profile_domain_preference(client: TestClient) -> None:
    _add_feedback(
        [
            ("https://zhihu.com/a", "RAG 实践笔记", "accepted", "RAG 评估"),
            ("https://www.zhihu.com/b", "RAG 实践复盘", "accepted", None),
            ("https://zhihu.com/c", "无关内容", "rejected", None),
            ("https://csdn.net/x", "分块技巧", "rejected", None),
            ("https://csdn.net/y", "向量技巧", "rejected", None),
        ]
    )
    with db.get_db() as conn:
        profile = rerank.load_profile(conn)

    assert profile["accepted"] == 2
    assert profile["rejected"] == 3
    assert profile["total"] == 5
    # www 变体归并到同一 host
    assert rerank.host_key("https://www.zhihu.com/b") == "zhihu.com"
    assert "https://www.zhihu.com/b" in profile["accepted_urls"]
    assert "https://csdn.net/x" in profile["rejected_urls"]
    domains = profile["domains"]
    assert domains[0]["host"] == "zhihu.com"
    assert domains[0]["accepted"] == 2
    assert domains[0]["rejected"] == 1
    # 平滑偏好 (2+1)/(2+1+2) = 0.6
    assert domains[0]["score"] == 0.6
    assert [item["host"] for item in domains] == ["zhihu.com", "csdn.net"]
    # term 集合来自 title/query token
    assert "rag" in profile["accepted_terms"]
    assert "实践" in profile["accepted_terms"]
    assert "无关" in profile["rejected_terms"]


def test_reranker_summary_api_and_feedback_preserved(client: TestClient) -> None:
    _add_feedback(
        [
            ("https://zhihu.com/a", "RAG 实践笔记", "accepted", None),
            ("https://zhihu.com/b", "RAG 实践复盘", "accepted", None),
            ("https://zhihu.com/c", "无关内容", "rejected", None),
            ("https://csdn.net/x", "分块技巧", "rejected", None),
        ]
    )
    before = _feedback_count()
    payload = client.get("/api/reranker/summary").json()

    assert payload["accepted"] == 2
    assert payload["rejected"] == 2
    assert payload["description"] == "你忽略的域名会在后续外部资料中降权；采纳的来源会加分。"
    assert len(payload["domains"]) == 2
    top = payload["domains"][0]
    assert set(top) == {"host", "accepted", "rejected", "score"}
    assert top == {"host": "zhihu.com", "accepted": 2, "rejected": 1, "score": 0.6}

    # 只读接口：不删除、不重置用户反馈
    assert _feedback_count() == before

    # 空状态
    with db.get_db() as conn:
        conn.execute("DELETE FROM knowledge_feedback")
    empty = client.get("/api/reranker/summary").json()
    assert empty["accepted"] == 0
    assert empty["rejected"] == 0
    assert empty["domains"] == []
    assert empty["description"]


# ---------------------------------------------------------------------------
# Reranker：候选排序
# ---------------------------------------------------------------------------


def test_rerank_accepted_host_boosts_same_host(client: TestClient) -> None:
    _add_feedback([("https://good.com/anything", "RAG 指南", "accepted", None)])
    candidates = [
        _candidate("https://other.com/a"),
        _candidate("https://good.com/b"),
    ]
    ranked = _rerank(candidates)
    assert ranked[0]["url"] == "https://good.com/b"
    assert ranked[0]["feedback_score"] > 0
    assert ranked[0]["feedback_label"] == "你采纳过类似来源"
    # 其它 host 只有文本重叠带来的轻微加分，明显低于同 host 偏好
    assert _find(ranked, "https://other.com/a")["feedback_score"] < ranked[0]["feedback_score"]


def test_rerank_rejected_host_demoted(client: TestClient) -> None:
    _add_feedback(
        [
            ("https://bad.com/1", "无关内容甲", "rejected", None),
            ("https://bad.com/2", "无关内容乙", "rejected", None),
        ]
    )
    candidates = [
        _candidate("https://bad.com/3"),
        _candidate("https://neutral.com/4"),
    ]
    ranked = _rerank(candidates)
    assert ranked[0]["url"] == "https://neutral.com/4"
    assert ranked[-1]["url"] == "https://bad.com/3"
    assert ranked[-1]["feedback_score"] < 0
    assert ranked[-1]["feedback_label"] == "你曾忽略过类似来源"


def test_rerank_exact_url_signals(client: TestClient) -> None:
    _add_feedback(
        [
            ("https://good.com/keep", "RAG 指南", "accepted", None),
            ("https://bad.com/skip", "RAG 指南", "rejected", None),
        ]
    )
    candidates = [
        _candidate("https://other.com/mid"),
        _candidate("https://good.com/keep/"),  # 规范化后命中原 URL
        _candidate("https://bad.com/skip"),
    ]
    ranked = _rerank(candidates)
    assert ranked[0]["url"] == "https://good.com/keep/"
    assert ranked[0]["feedback_score"] == 1.0
    assert ranked[0]["feedback_label"] == "你采纳过该来源"
    assert ranked[-1]["url"] == "https://bad.com/skip"
    assert ranked[-1]["feedback_score"] == -1.0
    assert ranked[-1]["feedback_label"] == "你曾忽略该来源"


def test_rerank_without_feedback_keeps_order(client: TestClient) -> None:
    candidates = [
        _candidate("https://a.com/1", relevance=0.9),
        _candidate("https://a.com/2", relevance=0.7),
        _candidate("https://a.com/3", relevance=0.5),
    ]
    ranked = _rerank(candidates)
    assert [item["url"] for item in ranked] == [
        "https://a.com/1",
        "https://a.com/2",
        "https://a.com/3",
    ]
    for item in ranked:
        assert item["feedback_score"] == 0
        assert item["feedback_label"] == ""
        # 无反馈时反馈项恒为 0.5，不改变原有排序趋势
        assert item["rerank_score"] > 0


def test_extend_search_adds_feedback_fields(client: TestClient) -> None:
    rejected_url = "https://example.com/vector-search-basics"
    client.post(
        "/api/extend/reject",
        json={"items": [{"url": rejected_url, "title": "无关向量资料"}]},
    )
    payload = client.post(
        "/api/extend/search", json={"question": "RAG 的评估指标有哪些？"}
    ).json()
    candidates = payload["candidates"]
    assert candidates
    for candidate in candidates:
        assert "feedback_score" in candidate
        assert "feedback_label" in candidate
        assert -1 <= float(candidate["feedback_score"]) <= 1
    # 精确 rejected URL 仍被现有逻辑排除
    assert rejected_url not in [item["url"] for item in candidates]
    # 其它反馈偏好字段不破坏既有契约
    assert {"url", "title", "snippet", "relevance", "authority_label"} <= set(
        candidates[0]
    )


def test_extend_search_rerank_uses_accepted_host(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "SYNAPSE_SEARCH_MOCK_RESULTS",
        json.dumps(
            [
                {
                    "url": "https://platform.openai.com/docs/rag-eval",
                    "title": "RAG 评估指标指南",
                    "snippet": "RAG 评估指标包括忠实度与召回率。",
                },
                {
                    "url": "https://api-docs.deepseek.com/rag-eval",
                    "title": "RAG 评估指标说明",
                    "snippet": "RAG 评估指标包括忠实度与召回率。",
                },
            ],
            ensure_ascii=False,
        ),
    )
    _add_feedback(
        [("https://api-docs.deepseek.com/anything", "RAG 评估指标说明", "accepted", None)]
    )
    payload = client.post(
        "/api/extend/search", json={"question": "RAG 的评估指标有哪些？"}
    ).json()
    urls = [item["url"] for item in payload["candidates"]]
    assert urls, payload
    assert urls[0] == "https://api-docs.deepseek.com/rag-eval"
    accepted = payload["candidates"][0]
    assert accepted["feedback_score"] > 0
    assert accepted["feedback_label"] in {"你采纳过类似来源", "你采纳过该来源"}


# ---------------------------------------------------------------------------
# Patrol：配置 / 状态
# ---------------------------------------------------------------------------


def test_patrol_status_defaults_and_patch(client: TestClient) -> None:
    status = client.get("/api/patrol/status").json()
    assert set(status) == {
        "enabled",
        "interval_hours",
        "auto_enrich",
        "max_enrich",
        "running",
        "last_run_at",
        "next_run_at",
        "last_result",
        "counts",
    }
    assert status["enabled"] is False
    assert status["interval_hours"] == 24
    assert status["auto_enrich"] is True
    assert status["max_enrich"] == 3
    assert status["running"] is False
    assert status["last_run_at"] is None
    assert status["next_run_at"] is None
    assert status["last_result"] is None
    assert status["counts"] == {
        "pending_docs": 0,
        "total_documents": 0,
        "enriched_documents": 0,
    }

    updated = client.patch(
        "/api/patrol/config",
        json={
            "enabled": True,
            "interval_hours": 6,
            "auto_enrich": False,
            "max_enrich": 5,
        },
    ).json()
    assert updated["enabled"] is True
    assert updated["interval_hours"] == 6
    assert updated["auto_enrich"] is False
    assert updated["max_enrich"] == 5
    # false → true 且 next_run 为空：立即设为当前时间（下一次 tick 即巡检）
    assert updated["next_run_at"] is not None

    persisted = client.get("/api/patrol/status").json()
    assert persisted["interval_hours"] == 6
    assert persisted["max_enrich"] == 5
    assert persisted["enabled"] is True

    # 校验：非法值 400，且不生效（事务回滚）
    assert client.patch("/api/patrol/config", json={"interval_hours": 0}).status_code == 400
    assert client.patch("/api/patrol/config", json={"interval_hours": 169}).status_code == 400
    assert client.patch("/api/patrol/config", json={"max_enrich": 0}).status_code == 400
    assert client.patch("/api/patrol/config", json={"max_enrich": 13}).status_code == 400
    still = client.get("/api/patrol/status").json()
    assert still["interval_hours"] == 6
    assert still["max_enrich"] == 5


# ---------------------------------------------------------------------------
# Patrol：执行 / 自动整理 / issues
# ---------------------------------------------------------------------------


def test_run_patrol_auto_enrich_and_result_persisted(client: TestClient) -> None:
    for index in range(4):
        add_note(client, f"巡检待整理文档 {index}", SHORT_CONTENT, ["测试"])
    before = client.get("/api/patrol/status").json()["counts"]
    assert before["pending_docs"] == 4
    assert before["enriched_documents"] == 0

    response = client.post("/api/patrol/run")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    result = payload["result"]
    assert {"started_at", "finished_at", "duration_ms", "trigger", "enriched", "issues", "summary"} <= set(
        result
    )
    assert result["trigger"] == "manual"
    assert result["ok"] is True
    assert isinstance(result["duration_ms"], int)
    assert result["graph_rebuilt"] is True
    # 默认 max_enrich=3：最多整理 3 篇
    assert len(result["enriched"]) == 3
    assert all(item["ok"] is True for item in result["enriched"])
    # issues 结构完整
    assert set(result["issues"]) == {
        "pending_docs",
        "duplicate_docs",
        "stale_docs",
        "orphan_docs",
        "small_docs",
        "missing_tags",
    }
    assert result["issues"]["pending_docs"]["count"] == 1
    assert isinstance(result["issues"]["pending_docs"]["sample"], list)
    assert result["summary"]

    status = payload["status"]
    assert status["last_run_at"] == result["started_at"]
    assert status["next_run_at"] is not None
    assert status["last_result"]["summary"] == result["summary"]
    assert status["counts"]["pending_docs"] == 1
    assert status["counts"]["enriched_documents"] == 3
    assert status["counts"]["total_documents"] == 4

    # 已整理文档 enrich_version > 0
    with db.get_db() as conn:
        enriched_count = db.fetch_value(
            conn, "SELECT COUNT(*) FROM documents WHERE enrich_version > 0", default=0
        )
        kinds = [
            str(row["kind"])
            for row in db.fetch_all(
                conn, "SELECT kind FROM activity ORDER BY id DESC LIMIT 20"
            )
        ]
    assert enriched_count == 3
    assert "patrol" in kinds


def test_run_patrol_auto_enrich_disabled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    add_note(client, "巡检不整理文档", SHORT_CONTENT, ["测试"])
    client.patch("/api/patrol/config", json={"auto_enrich": False})

    calls: list[str] = []

    def fake_enrich(*args, **kwargs):  # pragma: no cover - 不应被调用
        calls.append("called")
        raise AssertionError("auto_enrich=false 时不应调用 enrich")

    monkeypatch.setattr(patrol.enrich, "enrich_document", fake_enrich)
    payload = client.post("/api/patrol/run").json()
    assert payload["ok"] is True
    assert payload["result"]["enriched"] == []
    assert calls == []
    assert payload["result"]["issues"]["pending_docs"]["count"] == 1
    assert payload["status"]["counts"]["pending_docs"] == 1


def test_run_patrol_single_enrich_failure_does_not_abort(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = add_note(client, "巡检第一篇", SHORT_CONTENT, ["测试"])
    second = add_note(client, "巡检第二篇", SHORT_CONTENT, ["测试"])
    attempts: list[str] = []

    def fake_enrich(conn, doc_id, *, force=False, llm_config=None):
        attempts.append(doc_id)
        if doc_id == first["id"]:
            raise RuntimeError("模拟整理失败")
        return {"id": doc_id, "skipped": False, "title": "整理后的标题", "mode": "local"}

    monkeypatch.setattr(patrol.enrich, "enrich_document", fake_enrich)
    payload = client.post("/api/patrol/run").json()
    result = payload["result"]
    assert result["ok"] is True
    assert len(attempts) == 2
    assert len(result["enriched"]) == 2
    by_id = {item["id"]: item for item in result["enriched"]}
    assert by_id[first["id"]]["ok"] is False
    assert "模拟整理失败" in by_id[first["id"]]["error"]
    assert by_id[second["id"]]["ok"] is True
    assert by_id[second["id"]]["title"] == "整理后的标题"
    assert "失败 1 篇" in result["summary"]
    assert result["graph_rebuilt"] is True


def test_patrol_issue_detection(client: TestClient) -> None:
    zone = client.post("/api/zones", json={"name": "巡检测试分区"}).json()["zone"]

    first = add_note(client, "知识管理笔记整理", SHORT_CONTENT, ["知识管理"])
    second = add_note(client, "知识管理笔记整理！", SHORT_CONTENT, ["知识管理"])
    orphan = add_note(client, "咖啡参数记录", LONG_CONTENT)  # tags=[] 且无分区
    stale = add_note(client, "过时文档", LONG_CONTENT, ["旧"])
    client.put(
        "/api/documents/zone",
        json={"doc_ids": [first["id"], second["id"], stale["id"]], "zone_id": zone["id"]},
    )
    old_time = (datetime.now(timezone.utc) - timedelta(days=60)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE documents SET updated_at = ? WHERE id = ?", (old_time, stale["id"])
        )

    client.patch("/api/patrol/config", json={"auto_enrich": False})
    result = client.post("/api/patrol/run").json()["result"]
    issues = result["issues"]

    assert issues["pending_docs"]["count"] == 4
    assert issues["duplicate_docs"]["count"] == 1
    pair = issues["duplicate_docs"]["pairs"][0]
    assert {pair["a"]["id"], pair["b"]["id"]} == {first["id"], second["id"]}
    assert pair["similarity"] >= 0.9

    assert issues["stale_docs"]["count"] == 1
    assert issues["stale_docs"]["sample"][0]["id"] == stale["id"]

    assert issues["orphan_docs"]["count"] == 1
    assert issues["orphan_docs"]["sample"][0]["id"] == orphan["id"]

    # first/second 内容 320 字 <800；orphan/stale 为 1200 字
    assert issues["small_docs"]["count"] == 2
    assert {item["id"] for item in issues["small_docs"]["sample"]} == {
        first["id"],
        second["id"],
    }

    # 只有 orphan 文档无标签
    assert issues["missing_tags"]["count"] == 1
    assert issues["missing_tags"]["sample"][0]["id"] == orphan["id"]

    assert "重复标题 1" in result["summary"]
    assert "过时 1" in result["summary"]
    assert "未分区 1" in result["summary"]
    assert "无标签 1" in result["summary"]


def test_patrol_run_returns_409_when_running(client: TestClient) -> None:
    assert patrol._RUN_LOCK.acquire(blocking=False)
    try:
        response = client.post("/api/patrol/run")
        assert response.status_code == 409
        assert "运行" in response.json()["detail"]
        # 只读状态接口仍可用
        assert client.get("/api/patrol/status").json()["running"] is True
    finally:
        patrol._RUN_LOCK.release()


# ---------------------------------------------------------------------------
# Patrol：调度器（不启动真实 task）
# ---------------------------------------------------------------------------


def test_patrol_scheduler_due_logic(client: TestClient) -> None:
    now = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert patrol.is_due(now, None) is True
    assert patrol.is_due(now, "2026-11-17T11:59:00Z") is True
    assert patrol.is_due(now, "2026-11-17T12:00:00Z") is True
    assert patrol.is_due(now, "2026-11-17T12:01:00Z") is False
    assert patrol.is_due(now, "不是时间") is True


def test_patrol_scheduler_autostart_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNAPSE_PATROL_AUTOSTART", "0")
    assert patrol.autostart_disabled() is True

    async def run_and_stop() -> None:
        assert await patrol.start_scheduler() is False
        await patrol.stop_scheduler()

    asyncio.run(run_and_stop())


def test_patrol_scheduler_tick_triggers_when_due(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_run(trigger: str = "manual") -> dict[str, object]:
        calls.append(trigger)
        return {"ok": True, "running": False, "result": {}, "summary": "fake", "next_run_at": "x"}

    monkeypatch.setattr(patrol, "run_patrol", fake_run)

    async def scenario() -> tuple[bool, bool, bool]:
        assert await patrol._tick() is False  # 默认 enabled=false
        with db.get_db() as conn:
            db.setting_set(conn, patrol.SETTING_ENABLED, "true")
            db.setting_set(conn, patrol.SETTING_NEXT_RUN, "2999-01-01T00:00:00Z")
        not_due = await patrol._tick()  # 未到期
        with db.get_db() as conn:
            db.setting_set(conn, patrol.SETTING_NEXT_RUN, "2000-01-01T00:00:00Z")
        due = await patrol._tick()  # 到期 → 触发
        return False, not_due, due

    try:
        _, not_due, due = asyncio.run(scenario())
    finally:
        monkeypatch.setattr(patrol, "_scheduler_lock", None)
        monkeypatch.setattr(patrol, "_scheduler_task", None)
    assert not_due is False
    assert due is True
    assert calls == ["scheduled"]
