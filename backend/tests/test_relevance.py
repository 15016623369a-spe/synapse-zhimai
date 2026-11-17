"""知识扩展相关性修复测试（SPEC-RELEVANCE 第 6 节）。

覆盖：引用文档识别（arXiv ID / 书名号 / 拼写差异）、文档级总结聊天、
短文档 gap、学术意图检索（arXiv/OpenAlex）、查询规划校验、候选相关度过滤、
arxiv commit PDF、health 等。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.services import doc_ref, extend, llm, search
from app.services.search import SearchResult

from .conftest import add_note

ARXIV_ID = "2410.06294"
PAPER_FILENAME = f"Wei_Liang_Meyer_2024_NEBPplus_FUSION2024_arXiv{ARXIV_ID}.pdf"
PAPER_TITLE = f"Wei_Liang_Meyer_2024_NEBPplus_FUSION2024_arXiv{ARXIV_ID}"
PAPER_CONTENT = (
    "NEBPplus: Neural Enhanced Background Prediction for FUSION 2024.\n\n"
    "Abstract: We propose NEBPplus, a neural enhanced background prediction method "
    "for the FUSION 2024 challenge. It improves object tracking robustness under "
    "clutter by combining transformer features with background modeling.\n\n"
    "Method: our pipeline first extracts multi-scale features, then applies a "
    "background-aware attention module, and finally fuses motion cues with "
    "appearance embeddings.\n\n"
    "Experiments: on the FUSION 2024 benchmark, NEBPplus improves MOTA by 3.2 "
    "points over the baseline while keeping latency under 20 ms.\n\n"
    "Conclusion: NEBPplus demonstrates that neural background prediction is an "
    "effective and efficient enhancement for multi-object tracking.\n"
    * 3
)


def _insert_paper(
    content: str = PAPER_CONTENT,
    filename: str = PAPER_FILENAME,
    title: str = PAPER_TITLE,
) -> str:
    doc_id = "d_paper_relevance_test"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO documents(id, title, filename, source_type, tags, summary, "
            "content, size, status, error, zone_id, created_at, updated_at) "
            "VALUES(?, ?, ?, 'pdf', '[]', ?, ?, ?, 'ready', NULL, NULL, ?, ?)",
            (doc_id, title, filename, "论文", content, len(content), now, now),
        )
        conn.execute(
            "INSERT INTO chunks(id, doc_id, idx, text, char_start, char_end, "
            "token_count, embedding, created_at) VALUES(?, ?, 0, ?, 0, ?, 10, NULL, ?)",
            (f"c_{doc_id}", doc_id, content[:600], min(600, len(content)), now),
        )
    return doc_id


def _sse_events(raw: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in raw.split("\n\n"):
        if not block.strip().startswith("event:"):
            continue
        lines = block.splitlines()
        name = lines[0][len("event:") :].strip()
        data = {}
        for line in lines[1:]:
            if line.startswith("data:"):
                data = json.loads(line[len("data:") :].strip())
        events.append((name, data))
    return events


# ---------------------------------------------------------------------------
# 引用文档识别
# ---------------------------------------------------------------------------


def test_extract_ids() -> None:
    ids = doc_ref.extract_ids(
        "请总结 arXiv:2410.06294v2 与 doi:10.1234/abc.def 的内容"
    )
    assert ids["arxiv_ids"] == ["2410.06294"]
    assert ids["dois"] == ["10.1234/abc.def"]


def test_detect_referenced_document_by_arxiv_id_with_spelling_variant(
    client: TestClient,
) -> None:
    doc_id = _insert_paper()
    with db.get_db() as conn:
        # 用户拼写 NEBBplus（文档是 NEBPplus），带书名号/长文件名
        detected = doc_ref.detect_referenced_document(
            conn,
            "请总结《Wei_Liang_Meyer_2024_NEBBplus_FUSION2024_arXiv2410.06294》的核心内容。",
        )
        assert detected is not None
        assert detected["doc_id"] == doc_id
        assert detected["arxiv_id"] == ARXIV_ID

        detected2 = doc_ref.detect_referenced_document(
            conn, "总结一下 arXiv 2410.06294 这篇论文"
        )
        assert detected2 is not None and detected2["doc_id"] == doc_id


def test_detect_referenced_document_by_quoted_title(client: TestClient) -> None:
    _insert_paper()
    with db.get_db() as conn:
        detected = doc_ref.detect_referenced_document(
            conn, f"《{PAPER_TITLE}》讲了什么？"
        )
        assert detected is not None
        assert detected["title"] == PAPER_TITLE


def test_detect_referenced_document_quoted_title_beats_shared_generic_terms(
    client: TestClient,
) -> None:
    """书名号精确标题优先，不能因为别的文档共享 Transformer/multi-object 等词判为歧义。"""
    paper_id = _insert_paper(
        title="RadarMOTR Multi-Object Tracking with Transformers on Range-Doppler Maps",
        filename="RadarMOTR Multi-Object Tracking with Transformers on Range-Doppler Maps.pdf",
        content=(
            "RadarMOTR: Multi-Object Tracking with Transformers on Range-Doppler Maps.\n\n"
            "Abstract: We propose RadarMOTR for radar-based multi-object tracking and "
            "evaluate it on a new Range-Doppler dataset. Experiments show 78.9% HOTA.\n"
            * 10
        ),
    )
    noise_id = "d_noise_ref_test"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO documents(id, title, filename, source_type, tags, summary, "
            "content, size, status, error, zone_id, created_at, updated_at) "
            "VALUES(?, ?, ?, 'pdf', '[]', ?, ?, ?, 'ready', NULL, NULL, ?, ?)",
            (
                noise_id,
                "Transformer Multi-Object Tracking 研究框架",
                "Transformer_MOT.pdf",
                "另一篇 Transformer 多目标跟踪论文",
                "Transformer Multi-Object Tracking 综述",
                40,
                now,
                now,
            ),
        )
    with db.get_db() as conn:
        detected = doc_ref.detect_referenced_document(
            conn,
            "请总结《RadarMOTR Multi-Object Tracking with Transformers on "
            "Range-Doppler Maps》的主要贡献和实验结果。",
        )
    assert detected is not None
    assert detected["doc_id"] == paper_id
    assert detected["match_score"] >= 0.9


def test_detect_referenced_document_ignores_normal_question(client: TestClient) -> None:
    _insert_paper()
    with db.get_db() as conn:
        assert (
            doc_ref.detect_referenced_document(conn, "RAG 的分块大小和重叠是多少？")
            is None
        )


# ---------------------------------------------------------------------------
# 文档级总结聊天
# ---------------------------------------------------------------------------


def _mock_stream_llm(monkeypatch: pytest.MonkeyPatch, captured: list[dict]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.append(payload)
        body = 'data: {"choices":[{"delta":{"content":"论文总结了 NEBPplus 方法。"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(handler))


def test_chat_reference_summary_uses_full_document(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id = _insert_paper()
    client.put(
        "/api/settings",
        json={"llm": {"base_url": "https://mock.api/v1", "model": "mock", "api_key": "sk-test"}},
    )
    captured: list[dict] = []
    _mock_stream_llm(monkeypatch, captured)

    raw = client.post(
        "/api/chat",
        json={"message": "请总结《Wei_Liang_Meyer_2024_NEBBplus_FUSION2024_arXiv2410.06294》的核心内容。"},
    ).text
    events = _sse_events(raw)
    names = [name for name, _data in events]
    assert names[0] == "meta"
    assert names[1] == "sources"
    assert "gap" not in names
    assert "tool" not in names
    assert "web_sources" not in names
    meta = next(data for name, data in events if name == "meta")
    assert meta["reference"]["doc_id"] == doc_id
    sources = next(data for name, data in events if name == "sources")
    assert len(sources["citations"]) == 1
    assert sources["citations"][0]["doc_id"] == doc_id

    # LLM 收到的 prompt 中有文档全文片段与「直接总结」指令
    assert captured, "LLM 应被调用"
    messages = captured[-1]["messages"]
    assert any("NEBPplus" in str(message.get("content")) for message in messages)
    assert "直接基于" in str(messages[0]["content"]) or "总结" in str(messages[0]["content"])
    assert len(str(messages[-1]["content"])) > 2000


def test_chat_short_reference_document_still_triggers_gap(
    client: TestClient,
) -> None:
    _insert_paper(content="短文档：" + "NEBPplus 只是一个短语。" * 10)
    raw = client.post(
        "/api/chat",
        json={"message": "总结 arXiv 2410.06294 这篇论文"},
    ).text
    events = dict(_sse_events(raw))
    assert "meta" in events
    assert events["meta"]["reference"]["doc_id"] == "d_paper_relevance_test"
    assert "gap" in events
    suggested = " ".join(events["gap"]["suggested_queries"])
    assert "2410.06294" in suggested or "NEBP" in suggested


# ---------------------------------------------------------------------------
# 学术意图 / provider / 查询规划
# ---------------------------------------------------------------------------


def test_detect_academic_intent() -> None:
    assert search.detect_academic_intent("总结 arXiv 2005.11401 的核心内容")
    assert search.detect_academic_intent("这篇 DOI 10.1234/abc 论文的方法")
    assert search.detect_academic_intent("RAG 评估指标有哪些？") is False
    assert (
        search.detect_academic_intent(
            "帮我看看这篇论文",
            {"title": "X arXiv2410.06294", "filename": "x.pdf", "source_type": "pdf"},
        )
        is True
    )


def test_plan_queries_for_academic_reference() -> None:
    context = {
        "reference_doc": {
            "title": "Wei Liang Meyer 2024 NEBPplus FUSION2024 arXiv2410.06294",
            "filename": PAPER_FILENAME,
            "arxiv_id": ARXIV_ID,
        }
    }
    queries = search.plan_queries("请总结这篇论文的核心内容", None, context)
    assert queries
    joined = " ".join(queries)
    assert ARXIV_ID in joined
    for forbidden in ("总结", "请", "核心内容", "资料"):
        assert forbidden not in joined


def test_invalid_queries_are_rejected_and_fallback_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert search.validate_query("总结 核心内容") is False
    assert search.validate_query("请帮我总结一下") is False
    assert search.validate_query(ARXIV_ID) is True
    assert search.validate_query("RAG 评估指标") is True

    monkeypatch.setattr(llm, "is_configured", lambda _cfg=None: True)
    monkeypatch.setattr(search, "_llm_plan_queries", lambda *args, **kwargs: [])
    queries = search.plan_queries("RAG 的评估指标有哪些？", {"base_url": "x", "model": "m"})
    assert queries
    assert not any("总结" in query or "核心内容" in query for query in queries)


_ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2410.06294v1</id>
    <title>NEBPplus: Neural Enhanced Background Prediction</title>
    <summary>We propose NEBPplus for the FUSION 2024 challenge.</summary>
    <link href="http://arxiv.org/abs/2410.06294v1" rel="alternate"/>
    <link href="http://arxiv.org/pdf/2410.06294v1" title="pdf"/>
  </entry>
</feed>
"""

_OPENALEX_JSON = {
    "title": "NEBPplus for Multi Object Tracking",
    "abstract_inverted_index": {"Neural": [0], "tracking": [1], "method": [2]},
}


def test_arxiv_provider_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "export.arxiv.org" in str(request.url)
        return httpx.Response(
            200, text=_ARXIV_XML, headers={"content-type": "application/atom+xml"}
        )

    monkeypatch.setattr(search, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    results = search.arxiv_by_id(ARXIV_ID)
    assert len(results) == 1
    result = results[0]
    assert result.source == "arxiv"
    assert result.authority == "academic"
    assert result.url == f"https://arxiv.org/abs/{ARXIV_ID}"
    assert result.pdf_url == f"https://arxiv.org/pdf/{ARXIV_ID}"
    assert "NEBPplus" in result.title


def test_openalex_provider_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "api.openalex.org" in str(request.url)
        return httpx.Response(200, json=_OPENALEX_JSON)

    monkeypatch.setattr(search, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    results = search.openalex_by_doi("10.1234/abc.def")
    assert len(results) == 1
    assert results[0].source == "openalex"
    assert results[0].authority == "academic"
    assert "Neural tracking method" in results[0].snippet


# ---------------------------------------------------------------------------
# 相关度 / 权威度过滤
# ---------------------------------------------------------------------------


def test_rank_and_filter_academic_intent_drops_community() -> None:
    candidates = [
        {
            "url": f"https://arxiv.org/abs/{ARXIV_ID}",
            "title": "NEBPplus Neural Enhanced Background Prediction",
            "snippet": "object tracking FUSION 2024 arXiv paper",
            "source": "arxiv",
        },
        {
            "url": "https://zhuanlan.zhihu.com/p/123",
            "title": "知乎：聊天软件怎么用",
            "snippet": "这是一篇与论文完全无关的知乎文章。",
            "source": "bing",
        },
    ]
    ranked, filtered = search.rank_and_filter_candidates(
        "总结 arXiv 2410.06294 论文的核心内容",
        {"title": "NEBPplus FUSION2024 arXiv2410.06294", "content": ""},
        candidates,
        True,
    )
    assert filtered == 1
    assert [item["url"] for item in ranked] == [f"https://arxiv.org/abs/{ARXIV_ID}"]
    assert ranked[0]["authority_label"] == "学术"
    assert 0 < ranked[0]["relevance"] <= 1


def test_rank_and_filter_general_drops_no_entity_overlap() -> None:
    candidates = [
        {
            "url": "https://baike.baidu.com/item/2026",
            "title": "2026 年_百度百科",
            "snippet": "2026 年日历与节假日安排。",
            "source": "bing",
        },
        {
            "url": "https://www.rili.com.cn/wannianli/2026",
            "title": "万年历 2026 年日历表",
            "snippet": "2026 年农历查询。",
            "source": "bing",
        },
    ]
    ranked, filtered = search.rank_and_filter_candidates(
        "2026 年 F1 轮胎规则是什么？", None, candidates, False
    )
    assert ranked == []
    assert filtered == 2


def test_rank_and_filter_general_intent_removes_irrelevant() -> None:
    candidates = [
        {
            "url": "https://example.com/coffee",
            "title": "手冲咖啡水温与研磨度参数",
            "snippet": "水温 92 度，研磨度中细，粉水比 1:15。",
            "source": "bing",
        },
        {
            "url": "https://baijiahao.baidu.com/x",
            "title": "今天聊聊娱乐圈八卦",
            "snippet": "完全无关的娱乐八卦内容。",
            "source": "bing",
        },
    ]
    ranked, filtered = search.rank_and_filter_candidates(
        "我的手冲咖啡水温是多少？", None, candidates, False
    )
    assert filtered >= 1
    assert all("咖啡" in item["title"] or item["relevance"] > 0.12 for item in ranked)


# ---------------------------------------------------------------------------
# /extend/search 学术链路
# ---------------------------------------------------------------------------


def test_extend_search_academic_returns_arxiv_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_arxiv(arxiv_id: str, max_results: int = 3) -> list[SearchResult]:
        return [
            SearchResult(
                url=f"https://arxiv.org/abs/{arxiv_id}",
                title="NEBPplus Neural Enhanced Background Prediction",
                snippet="object tracking FUSION 2024 paper method experiments",
                source="arxiv",
                authority="academic",
                relevance=0.9,
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
                extra={"arxiv_id": arxiv_id},
            )
        ]

    def fake_search_web(query: str, max_results: int | None = None) -> list[SearchResult]:
        return [
            SearchResult(
                url="https://zhuanlan.zhihu.com/p/1",
                title="知乎：无关内容",
                snippet="聊天软件怎么用",
                source="bing",
                authority="community",
            ),
            SearchResult(
                url="https://arxiv.org/abs/2410.06294",
                title="NEBPplus",
                snippet="paper",
                source="bing",
                authority="academic",
            ),
        ]

    monkeypatch.setattr(search, "arxiv_by_id", fake_arxiv)
    monkeypatch.setattr(search, "search_web", fake_search_web)
    payload = client.post(
        "/api/extend/search",
        json={"question": "总结 arXiv 2410.06294 的核心内容"},
    ).json()
    assert payload["intent"] == "academic"
    assert payload["provider"] == "arxiv"
    assert payload["candidates"]
    assert all(candidate["authority_label"] == "学术" for candidate in payload["candidates"])
    # arXiv ID 精确命中的目标论文必须高相关（前端默认勾选 relevance>=0.5）
    assert payload["candidates"][0]["relevance"] >= 0.9
    # 契约：authority 为类别字符串
    assert payload["candidates"][0]["authority"] == "academic"
    assert all("zhihu" not in candidate["url"] for candidate in payload["candidates"])
    assert payload["filtered_count"] >= 1


# ---------------------------------------------------------------------------
# commit arXiv PDF
# ---------------------------------------------------------------------------


def test_commit_arxiv_pdf_creates_academic_document(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = add_note(client, "原有笔记", "原有知识库内容，不能被修改。", ["AI"])
    before = client.get("/api/documents").json()["total"]

    def fake_pdf_text(pdf_url: object) -> tuple[str, str]:
        assert "arxiv.org/pdf/" in str(pdf_url)
        return "NEBPplus", "NEBPplus 论文全文：" + "方法、实验与结论。" * 200

    monkeypatch.setattr(extend, "fetch_arxiv_pdf_text", fake_pdf_text)
    response = client.post(
        "/api/extend/commit",
        json={
            "items": [
                {
                    "url": "https://arxiv.org/abs/2410.06294",
                    "title": "NEBPplus FUSION2024",
                    "snippet": "论文摘要",
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["added"]) == 1
    document = payload["added"][0]
    assert document["source_type"] == "web"
    assert "外部扩展" in document["tags"] and "学术" in document["tags"]
    assert document["mode"] == "fulltext"
    assert document["source_url"] == "https://arxiv.org/abs/2410.06294"
    assert client.get("/api/documents").json()["total"] == before + 1
    original_after = client.get(f"/api/documents/{original['id']}").json()
    assert original_after["size"] == original["size"]


def test_health_includes_search_provider(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    assert set(payload["search"]) == {"provider", "enabled"}
