"""联网知识扩展测试（SPEC-EXTEND 第 5 节）。

覆盖：gap 检测、搜索 provider 解析/失败、extend search/reject、commit
（全文/摘要 fallback、去重、原文档不变、分区、feedback、图谱重建）、
tool loop（mock LLM）、迁移 v4→v5。
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.services import agent, extend, search

from .conftest import add_note

FULLTEXT_HTML = (
    "<html><head><title>RAG 评估指南</title></head><body><article>"
    "<h1>RAG 评估</h1><p>"
    + "RAG 系统的评估指标包括忠实度 faithfulness、答案相关性 answer relevance、"
    "上下文召回率 context recall 与引用准确率。工程上建议固定问题集做回归测试，"
    "并结合人工抽检与线上反馈持续迭代。"
    * 6
    + "</p></article></body></html>"
)


@pytest.fixture(autouse=True)
def search_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认使用 mock 搜索 provider，并放行测试抓取（不访问真实网络）。"""
    monkeypatch.setenv("SYNAPSE_SEARCH_PROVIDER", "mock")
    monkeypatch.setenv("SYNAPSE_SEARCH_ENABLED", "1")
    monkeypatch.setenv("SYNAPSE_ALLOW_PRIVATE_FETCH", "1")
    for key in (
        "SYNAPSE_SEARCH_MOCK_RESULTS",
        "SYNAPSE_SEARXNG_URL",
        "SYNAPSE_SEARCH_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


def _fetch_transport(pages: dict[str, str] | None = None, *, fail: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("connection refused", request=request)
        url = str(request.url)
        for key, html in (pages or {}).items():
            if key in url:
                return httpx.Response(
                    200, text=html, headers={"content-type": "text/html; charset=utf-8"}
                )
        return httpx.Response(404, text="not found", request=request)

    return httpx.MockTransport(handler)


def _llm_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# gap 检测
# ---------------------------------------------------------------------------


def _patch_corpus_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    df = Counter(
        {
            "rag": 20,
            "评估": 12,
            "指标": 8,
            "分块": 6,
            "长度": 4,
            "重叠": 4,
            "咖啡": 5,
            "水温": 3,
        }
    )
    monkeypatch.setattr(agent, "_corpus_stats", lambda: (df, 100))


def test_gap_detection_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_corpus_stats(monkeypatch)

    # 1) 检索结果为空 → 缺口
    no_result = agent.detect_gap("RAG 评估指标有哪些？", [])
    assert no_result is not None
    assert no_result["needs_more"] is True
    assert no_result["reason"]
    assert 1 <= len(no_result["suggested_queries"]) <= 3

    # 2) 仅 1 条弱结果 → 缺口
    weak = agent.detect_gap(
        "RAG 评估指标有哪些？",
        [{"text": "手冲咖啡的水温是 92 度，研磨度中细。", "score": 0.3}],
    )
    assert weak is not None and weak["needs_more"] is True

    # 3) 覆盖充分 → 不误报
    covered = agent.detect_gap(
        "RAG 评估指标有哪些？",
        [
            {
                "text": "RAG 评估指标包括忠实度、答案相关性、上下文召回率与引用准确率。",
                "score": 0.9,
            }
        ],
    )
    assert covered is None

    # 4) 语料中完全未登录的关键词（明显超出知识库）→ 缺口
    out_of_kb = agent.detect_gap(
        "量子纠缠的最新实验进展",
        [
            {
                "text": "RAG 评估指标与分块长度、重叠参数的实践经验。",
                "score": 0.9,
            }
        ],
    )
    assert out_of_kb is not None and out_of_kb["needs_more"] is True

    # 5) 语料外问题但部分通用词命中 → 仍判缺口（未登录词主导）
    f1 = agent.detect_gap(
        "2026 年 F1 轮胎规则是什么？",
        [
            {
                "text": "2026 年的计划与规则记录，包括每周复盘与预算。",
                "score": 0.9,
            }
        ],
    )
    assert f1 is not None and f1["needs_more"] is True

    # 6) 无语料统计时退化为普通覆盖率
    monkeypatch.setattr(agent, "_corpus_stats", lambda: (Counter(), 0))
    fallback_gap = agent.detect_gap(
        "RAG 评估指标有哪些？",
        [{"text": "手冲咖啡水温 92 度。", "score": 0.5}],
    )
    assert fallback_gap is not None
    fallback_covered = agent.detect_gap(
        "RAG 评估指标有哪些？",
        [{"text": "RAG 评估指标包含忠实度和召回率。", "score": 0.9}],
    )
    assert fallback_covered is None


# ---------------------------------------------------------------------------
# 搜索 provider
# ---------------------------------------------------------------------------


def test_bing_html_parsing_fixture() -> None:
    html = """
    <html><body><ol>
      <li class="b_algo"><h2><a href="https://example.com/a">RAG 评估指标</a></h2>
        <div class="b_caption"><p>介绍 RAG 的评估方法与指标。</p></div></li>
      <li class="b_algo"><h2><a href="https://example.com/b">向量检索实践</a></h2>
        <div class="b_caption"><p>BM25 与向量混合检索。</p></div></li>
      <li class="b_algo"><h2><a href="javascript:void(0)">无效链接</a></h2></li>
    </ol></body></html>
    """
    results = search._parse_bing_html(html, 5)  # noqa: SLF001
    assert [item.url for item in results] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert results[0].title == "RAG 评估指标"
    assert results[0].source == "bing"
    assert "评估方法" in results[0].snippet


def test_bing_provider_search_with_mocked_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNAPSE_SEARCH_PROVIDER", "bing")
    html = """
    <html><body><ol>
      <li class="b_algo"><h2><a href="https://example.com/rag">RAG 指南</a></h2>
        <div class="b_caption"><p>外部资料摘要。</p></div></li>
    </ol></body></html>
    """
    monkeypatch.setattr(
        search,
        "HTTP_TRANSPORT",
        httpx.MockTransport(
            lambda request: httpx.Response(
                200, text=html, headers={"content-type": "text/html"}
            )
        ),
    )
    results = search.search_web("RAG 评估", max_results=3)
    assert len(results) == 1
    assert results[0].url == "https://example.com/rag"
    assert results[0].source == "bing"


def test_mock_provider_and_plan_queries() -> None:
    results = search.search_web("随便什么关键词", max_results=2)
    assert len(results) == 2
    assert results[0].source == "mock"
    assert all(item.url.startswith("http") for item in results)

    queries = search.plan_queries("RAG 的评估指标有哪些？", llm_config=None)
    assert 1 <= len(queries) <= 3
    assert any("RAG" in query for query in queries)


def test_search_unavailable_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPSE_SEARCH_PROVIDER", "bing")

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down", request=request)

    monkeypatch.setattr(search, "HTTP_TRANSPORT", httpx.MockTransport(fail))
    response = client.post(
        "/api/extend/search", json={"question": "RAG 评估指标", "queries": ["RAG 评估"]}
    )
    assert response.status_code == 503
    assert "联网搜索暂不可用" in response.json()["detail"]


# ---------------------------------------------------------------------------
# extend status / search / reject
# ---------------------------------------------------------------------------


def test_extend_status(client: TestClient) -> None:
    payload = client.get("/api/extend/status").json()
    assert set(payload) == {
        "enabled",
        "provider",
        "label",
        "requires_approval",
        "accepted_count",
        "rejected_count",
        "can_search",
    }
    assert payload["provider"] == "mock"
    assert payload["enabled"] is True
    assert payload["requires_approval"] is True
    assert payload["accepted_count"] == 0
    assert payload["rejected_count"] == 0


def test_extend_search_candidates_and_reject_flow(client: TestClient) -> None:
    response = client.post(
        "/api/extend/search", json={"question": "RAG 的评估指标有哪些？"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["provider"] == "mock"
    assert 1 <= len(payload["queries"]) <= 4
    candidates = payload["candidates"]
    assert candidates
    for candidate in candidates:
        assert {
            "url",
            "title",
            "snippet",
            "source",
            "already_in_kb",
            "rejected_before",
            "relevance",
            "authority_label",
        } <= set(candidate)
        assert candidate["already_in_kb"] is False
        assert isinstance(payload["took_ms"], int)

    first = candidates[0]
    rejected = client.post(
        "/api/extend/reject",
        json={"items": [{"url": first["url"], "title": first["title"]}], "query": "RAG 评估"},
    )
    assert rejected.json() == {"ok": True, "remembered": 1}
    status = client.get("/api/extend/status").json()
    assert status["rejected_count"] == 1

    again = client.post(
        "/api/extend/search", json={"question": "RAG 的评估指标有哪些？"}
    ).json()
    assert first["url"] not in [item["url"] for item in again["candidates"]]

    # 重复忽略同一条不会再增加计数
    client.post("/api/extend/reject", json={"items": [{"url": first["url"]}]})
    assert client.get("/api/extend/status").json()["rejected_count"] == 1


def test_extend_search_marks_already_in_kb(client: TestClient) -> None:
    document = add_note(client, "已有文档", "RAG 评估指标包括忠实度与召回率。")
    # 手动把该文档 source_url 设为候选 URL，模拟已入库
    with db.get_db() as conn:
        conn.execute(
            "UPDATE documents SET source_url = ? WHERE id = ?",
            ("https://example.com/rag-evaluation-guide", document["id"]),
        )
    payload = client.post(
        "/api/extend/search", json={"question": "RAG 评估指标", "queries": ["RAG 评估指标"]}
    ).json()
    kb = {
        item["url"]: item["already_in_kb"] for item in payload["candidates"]
    }
    assert kb.get("https://example.com/rag-evaluation-guide") is True


# ---------------------------------------------------------------------------
# 审核入库
# ---------------------------------------------------------------------------


def test_commit_fulltext_creates_new_document_without_touching_original(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = add_note(client, "我的 RAG 笔记", "RAG 分块 450 到 650 字，重叠 80 字。", ["AI"])
    originals_before = client.get("/api/documents").json()
    chunks_before = client.get(f"/api/documents/{original['id']}/chunks").json()["items"]

    monkeypatch.setattr(
        extend,
        "HTTP_TRANSPORT",
        _fetch_transport({"example.com/rag-eval": FULLTEXT_HTML}),
    )
    response = client.post(
        "/api/extend/commit",
        json={
            "items": [
                {
                    "url": "https://Example.com/rag-eval/#section",
                    "title": "候选标题",
                    "snippet": "候选摘要",
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert len(payload["added"]) == 1
    document = payload["added"][0]
    assert document["source_type"] == "web"
    assert document["source_url"] == "https://example.com/rag-eval"
    assert "外部扩展" in document["tags"]
    assert document["mode"] == "fulltext"
    assert payload["skipped"] == []
    assert "原文档未修改" in payload["message"]

    # 原文档与分块完全不变
    originals_after = client.get("/api/documents").json()
    assert originals_after["total"] == originals_before["total"] + 1
    original_after = client.get(f"/api/documents/{original['id']}").json()
    assert original_after["size"] == original["size"]
    assert original_after["updated_at"] == original["updated_at"]
    chunks_after = client.get(f"/api/documents/{original['id']}/chunks").json()["items"]
    assert chunks_after == chunks_before

    # 图谱重建包含新文档，检索也能命中外部知识
    graph = client.get("/api/graph").json()
    assert f"doc:{document['id']}" in {node["id"] for node in graph["nodes"]}
    results = client.post("/api/search", json={"query": "忠实度 评估指标"}).json()["results"]
    assert any(result["doc_id"] == document["id"] for result in results)

    status = client.get("/api/extend/status").json()
    assert status["accepted_count"] == 1


def test_commit_dedupes_normalized_urls(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        extend, "HTTP_TRANSPORT", _fetch_transport({"example.com/rag-eval": FULLTEXT_HTML})
    )
    first = client.post(
        "/api/extend/commit",
        json={"items": [{"url": "https://example.com/rag-eval"}]},
    ).json()
    assert len(first["added"]) == 1

    duplicates = client.post(
        "/api/extend/commit",
        json={
            "items": [
                {"url": "https://EXAMPLE.com/rag-eval/"},
                {"url": "https://example.com/rag-eval#x"},
            ]
        },
    ).json()
    assert duplicates["added"] == []
    assert all(item["reason"] == "已在知识库" for item in duplicates["skipped"])


def test_commit_snippet_fallback_when_fetch_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extend, "HTTP_TRANSPORT", _fetch_transport(fail=True))
    payload = client.post(
        "/api/extend/commit",
        json={
            "items": [
                {
                    "url": "https://example.com/snippet-only",
                    "title": "外部摘要资料",
                    "snippet": "这是一段足够长的外部搜索摘要，用于验证摘要入库的回退逻辑是否正常工作，内容长度超过三十字。",
                },
                {
                    "url": "https://example.com/no-content",
                    "title": "没有摘要",
                    "snippet": "太短",
                },
            ]
        },
    ).json()
    assert len(payload["added"]) == 1
    added = payload["added"][0]
    assert added["mode"] == "snippet"
    assert added["source_type"] == "web"
    assert "外部搜索摘要" in (added["summary"] or "")
    assert len(payload["skipped"]) == 1
    assert "抓取失败" in payload["skipped"][0]["reason"]


def test_commit_with_zone_and_unknown_zone(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    zone = client.post("/api/zones", json={"name": "外部资料区"}).json()["zone"]
    monkeypatch.setattr(
        extend, "HTTP_TRANSPORT", _fetch_transport({"example.com/rag-eval": FULLTEXT_HTML})
    )
    payload = client.post(
        "/api/extend/commit",
        json={"items": [{"url": "https://example.com/rag-eval"}], "zone_id": zone["id"]},
    ).json()
    assert payload["added"][0]["zone_id"] == zone["id"]

    unknown = client.post(
        "/api/extend/commit",
        json={"items": [{"url": "https://example.com/other"}], "zone_id": "z_missing"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "分区不存在"


def test_commit_skips_invalid_and_private_urls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SYNAPSE_ALLOW_PRIVATE_FETCH", raising=False)
    monkeypatch.setattr(extend, "_resolve_ips", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(
        extend, "HTTP_TRANSPORT", _fetch_transport({"example.com/rag-eval": FULLTEXT_HTML})
    )
    payload = client.post(
        "/api/extend/commit",
        json={
            "items": [
                {"url": "ftp://example.com/a", "snippet": "s" * 40},
                {"url": "http://127.0.0.1/admin", "snippet": "s" * 40},
                {"url": "http://localhost:8000/a", "snippet": "s" * 40},
            ]
        },
    ).json()
    assert payload["added"] == []
    reasons = {item["reason"] for item in payload["skipped"]}
    assert any("http/https" in reason for reason in reasons)
    assert any("内网" in reason for reason in reasons)


# ---------------------------------------------------------------------------
# 工具调用（Chat）
# ---------------------------------------------------------------------------

TOOL_ANSWER = "外部资料 [W1] 显示可以参考外部来源。"


def _enable_mock_llm(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={"llm": {"base_url": "https://mock.api/v1", "model": "mock", "api_key": "sk-test"}},
    )
    assert response.status_code == 200


def _llm_tool_handler(state: dict[str, object]):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("stream") is False:
            state["rounds"] = int(state.get("rounds", 0)) + 1
            if state["rounds"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": "call_1",
                                            "type": "function",
                                            "function": {
                                                "name": "search_web",
                                                "arguments": json.dumps({"query": "RAG 评估指标"}),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "无需更多搜索"}}]}
            )
        body = (
            'data: {"choices":[{"delta":{"content":"'
            + TOOL_ANSWER
            + '"}}]}\n\ndata: [DONE]\n\n'
        )
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    return handler


def test_chat_gap_and_tool_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import llm

    _enable_mock_llm(client)
    state: dict[str, object] = {}
    monkeypatch.setattr(
        llm, "HTTP_TRANSPORT", httpx.MockTransport(_llm_tool_handler(state))
    )
    response = client.post("/api/chat", json={"message": "RAG 的评估指标有哪些？"})
    assert response.status_code == 200
    raw = response.text
    events = [
        block.splitlines()[0][len("event:") :].strip()
        for block in raw.split("\n\n")
        if block.strip().startswith("event:")
    ]
    assert events[:3] == ["meta", "sources", "gap"]
    assert events.count("tool") == 2
    assert "web_sources" in events
    assert events[-1] == "done"
    assert "delta" in events

    # web_sources 候选来自 mock provider
    for block in raw.split("\n\n"):
        if block.startswith("event: web_sources"):
            data = json.loads(block.splitlines()[1][len("data:") :].strip())
            assert data["provider"] == "mock"
            assert data["candidates"]
            assert data["candidates"][0]["url"].startswith("http")
        if block.startswith("event: done"):
            done = json.loads(block.splitlines()[1][len("data:") :].strip())
            assert done["gap"]["needs_more"] is True
            assert done["web_sources"]
            assert done["mode"] == "llm"


def test_chat_tool_phase_failure_degrades_gracefully(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import llm

    _enable_mock_llm(client)

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(fail))
    response = client.post("/api/chat", json={"message": "RAG 的评估指标有哪些？"})
    assert response.status_code == 200  # 绝不 500
    raw = response.text
    events = [
        block.splitlines()[0][len("event:") :].strip()
        for block in raw.split("\n\n")
        if block.strip().startswith("event:")
    ]
    assert "gap" in events
    assert "notice" in events
    assert events[-1] == "done"
    assert "web_sources" not in events


def test_chat_local_mode_emits_gap_without_search(client: TestClient) -> None:
    raw = client.post("/api/chat", json={"message": "量子纠缠通信原理是什么？"}).text
    events = [
        block.splitlines()[0][len("event:") :].strip()
        for block in raw.split("\n\n")
        if block.strip().startswith("event:")
    ]
    assert events[:3] == ["meta", "sources", "gap"]
    assert "tool" not in events
    assert "web_sources" not in events
    answer = "".join(
        json.loads(block.splitlines()[1][len("data:") :].strip())["text"]
        for block in raw.split("\n\n")
        if block.startswith("event: delta")
    )
    assert "联网检索补充资料" in answer


# ---------------------------------------------------------------------------
# 迁移 / health
# ---------------------------------------------------------------------------

_LEGACY_V4_SCHEMA = """
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
  group_id TEXT, plan_date TEXT, start_time TEXT, end_time TEXT, location TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE plan_checkins(
  id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  checkin_date TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL,
  UNIQUE(plan_id, checkin_date)
);
CREATE TABLE plan_groups(
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


def test_migration_v4_to_v5_is_idempotent_and_preserves_data(tmp_path) -> None:
    legacy_path = tmp_path / "legacy_v4.db"
    conn = sqlite3.connect(legacy_path)
    conn.executescript(_LEGACY_V4_SCHEMA)
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
        "INSERT INTO plan_groups(id, name, color, sort_order, created_at, updated_at) "
        "VALUES('pg_legacy', '历史分组', '#34D399', 1, "
        "'2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO plans(id, title, color, frequency, weekly_target, start_date, "
        "archived, sort_order, group_id, created_at, updated_at) "
        "VALUES('p_legacy', '每日阅读', '#4FD8FF', 'daily', 7, '2026-11-01', 0, 1, "
        "'pg_legacy', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO plan_checkins(id, plan_id, checkin_date, note, created_at) "
        "VALUES('ck_legacy', 'p_legacy', '2026-11-15', NULL, '2026-11-15T00:00:00Z')"
    )
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
            ("schema_version", "4"),
            ("llm_base_url", "https://legacy.example/v1"),
            ("llm_api_key", "sk-legacy-secret"),
            ("llm_model", "legacy-model"),
        ],
    )
    conn.commit()
    conn.close()

    db.init_db(legacy_path)
    db.init_db(legacy_path)

    conn = db.connect(legacy_path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        schema_version = conn.execute(
            "SELECT value FROM settings WHERE key='schema_version'"
        ).fetchone()[0]
        assert schema_version == "6"
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "knowledge_feedback" in tables
        indexes = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {"idx_feedback_url", "idx_feedback_decision"} <= indexes
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM plan_groups").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM plan_checkins").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        settings = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM settings")
        }
        assert settings["llm_base_url"] == "https://legacy.example/v1"
        assert settings["llm_api_key"] == "sk-legacy-secret"
    finally:
        conn.close()


def test_health_includes_search_status(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    assert payload["search"] == {"provider": "mock", "enabled": True}


def test_url_normalization() -> None:
    assert extend.normalize_url("HTTPS://Example.COM/a/b/#frag") == "https://example.com/a/b"
    assert extend.normalize_url("https://example.com/") == "https://example.com"
    assert extend.normalize_url("https://example.com:443/a") == "https://example.com/a"
    with pytest.raises(extend.ExtendError):
        extend.normalize_url("ftp://example.com/a")
    with pytest.raises(extend.ExtendError):
        extend.normalize_url("not-a-url")
