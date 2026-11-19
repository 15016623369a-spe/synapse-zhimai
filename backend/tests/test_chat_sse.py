"""聊天 SSE 测试（SPEC 6.3–6.4 / 7.4 / 10.1）：

覆盖本地 extractive 模式、无结果引导、多轮会话、Mock LLM 流式、LLM 失败降级。
"""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from app.services import llm

from .conftest import chat_events, events_by_name, joined_deltas, seed_corpus

CITATION_KEYS = {
    "index",
    "doc_id",
    "doc_title",
    "chunk_id",
    "source_type",
    "snippet",
    "score",
    "rank",
    "relevance",
    "kind",
}

LONG_MESSAGE = "请问在我的 RAG 检索增强生成实践笔记里，分块大小和重叠参数具体是多少？"


def test_local_extractive_chat_stream(client: TestClient) -> None:
    seed_corpus(client)
    events, raw = chat_events(client, LONG_MESSAGE)
    names = [event["event"] for event in events]
    assert names[0] == "meta"
    assert names[1] == "sources"
    assert names[-1] == "done"
    assert set(names) <= {"meta", "sources", "gap", "delta", "notice", "done", "error"}

    meta = events_by_name(events, "meta")[0]
    assert meta["mode"] == "extractive"
    assert meta["conversation_id"].startswith("conv_")
    assert meta["user_message_id"].startswith("m_")

    sources = events_by_name(events, "sources")[0]
    citations = sources["citations"]
    assert citations
    for citation in citations:
        assert set(citation) == CITATION_KEYS
        assert citation["chunk_id"].startswith("c_")
        assert citation["doc_id"].startswith("d_")
        assert citation["doc_title"]
        assert citation["snippet"]
        assert citation["score"] is None or 0 < citation["score"] <= 1
        assert citation["rank"] == citation["index"]
        assert citation["relevance"] in {"high", "medium", "low", "document"}
        assert citation["kind"] in {"chunk", "document"}
    assert [citation["index"] for citation in citations] == list(
        range(1, len(citations) + 1)
    )

    answer = joined_deltas(events)
    assert "根据你的知识库，我找到了" in answer
    assert "[1]" in answer
    assert "以上内容来自你导入的文档" in answer

    done = events_by_name(events, "done")[0]
    assert done["mode"] == "extractive"
    assert done["conversation_id"] == meta["conversation_id"]
    assert done["message_id"].startswith("m_")
    # 只展示回答里真正引用过的来源（[n] 裁剪后）
    done_citations = done["citations"]
    assert done_citations
    assert {item["index"] for item in done_citations} <= {
        item["index"] for item in citations
    }
    for item in done_citations:
        assert f"[{item['index']}]" in answer
    assert done["title"] == LONG_MESSAGE[:24]

    conversation = client.get(f"/api/conversations/{meta['conversation_id']}").json()
    assert conversation["id"] == meta["conversation_id"]
    assert [message["role"] for message in conversation["messages"]] == [
        "user",
        "assistant",
    ]
    assistant = conversation["messages"][1]
    assert assistant["content"] == answer
    assert assistant["citations"] == done_citations

    conversations = client.get("/api/conversations").json()["items"]
    assert conversations[0]["id"] == meta["conversation_id"]
    assert conversations[0]["message_count"] == 2
    assert conversations[0]["title"] == LONG_MESSAGE[:24]

    headers = client.post("/api/chat", json={"message": "一句话回答"})
    assert headers.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert headers.headers["cache-control"] == "no-cache"
    assert headers.headers["x-accel-buffering"] == "no"


def test_local_chat_without_results_shows_guidance(client: TestClient) -> None:
    events, _ = chat_events(client, "知识库里有什么关于量子计算的内容？")
    answer = joined_deltas(events)
    assert "知识库里暂时没有找到相关内容" in answer
    assert events_by_name(events, "done")[0]["citations"] == []


def test_chat_multi_turn_and_history(client: TestClient) -> None:
    seed_corpus(client)
    first_events, _ = chat_events(client, "RAG 的分块参数是多少？")
    conversation_id = events_by_name(first_events, "meta")[0]["conversation_id"]
    first_title = events_by_name(first_events, "done")[0]["title"]

    second_events, _ = chat_events(
        client, "那向量检索用的是什么方法？", conversation_id=conversation_id
    )
    second_meta = events_by_name(second_events, "meta")[0]
    assert second_meta["conversation_id"] == conversation_id
    assert second_meta["mode"] == "extractive"
    done = events_by_name(second_events, "done")[0]
    assert done["conversation_id"] == conversation_id
    assert done["title"] == first_title

    conversation = client.get(f"/api/conversations/{conversation_id}").json()
    assert len(conversation["messages"]) == 4
    assert [message["role"] for message in conversation["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_extractive_answer_dedupes_near_duplicate_sentences() -> None:
    results = [
        {
            "text": "RAG 的分块大小是 450 到 650 字，块间重叠 80 字。这段补充引用编号的规范。",
            "doc_title": "A",
            "source_type": "md",
            "chunk_id": "c_a",
            "doc_id": "d_a",
            "snippet": "",
            "score": 0.98,
        },
        {
            "text": "RAG 的分块大小是 450 到 650 字，块间重叠 80 字。这篇还提了本地检索兜底。",
            "doc_title": "B",
            "source_type": "md",
            "chunk_id": "c_b",
            "doc_id": "d_b",
            "snippet": "",
            "score": 0.9,
        },
        {
            "text": "手冲咖啡的水温是 92 度，研磨度用中细。",
            "doc_title": "C",
            "source_type": "md",
            "chunk_id": "c_c",
            "doc_id": "d_c",
            "snippet": "",
            "score": 0.8,
        },
    ]
    answer = llm.build_extractive_answer("RAG 的分块大小是多少？", results)
    bullets = [line for line in answer.splitlines() if line.startswith("- ")]
    texts = [bullet.rsplit(" [", 1)[0] for bullet in bullets]
    assert len(texts) == len(set(texts)), f"要点出现重复：{texts}"
    assert sum("RAG 的分块大小是 450 到 650 字" in text for text in texts) == 1


def test_chat_validates_input(client: TestClient) -> None:
    response = client.post("/api/chat", json={"message": "   "})
    assert response.status_code == 400
    assert response.json()["detail"] == "消息不能为空"

    missing = client.post(
        "/api/chat", json={"message": "你好", "conversation_id": "conv_missing"}
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "会话不存在"


def _configure_mock_llm(client: TestClient, base_url: str = "https://mock.api/v1/") -> dict:
    response = client.put(
        "/api/settings",
        json={
            "llm": {
                "base_url": base_url,
                "model": "mock-chat",
                "api_key": "sk-test-secret-123",
                "temperature": 0.2,
            }
        },
    )
    assert response.status_code == 200
    return response.json()


def test_llm_streaming_mode_with_mocked_transport(
    client: TestClient, monkeypatch
) -> None:
    seed_corpus(client)
    _configure_mock_llm(client)

    chunks = ["根据资料 [1]，", "分块大小为 450 到 650 字，", "块间重叠约 80 字。"]
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer sk-test-secret-123"
        payload = json.loads(request.content)
        captured["payload"] = payload
        lines = []
        for piece in chunks:
            event = {"choices": [{"delta": {"content": piece}}]}
            lines.append(f"data: {json.dumps(event, ensure_ascii=False)}\n\n")
        lines.append("data: [DONE]\n\n")
        return httpx.Response(
            200,
            content="".join(lines).encode("utf-8"),
            headers={"content-type": "text/event-stream; charset=utf-8"},
        )

    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    events, _ = chat_events(client, "分块大小是多少？")

    meta = events_by_name(events, "meta")[0]
    assert meta["mode"] == "llm"
    assert events_by_name(events, "notice") == []
    assert joined_deltas(events) == "".join(chunks)
    done = events_by_name(events, "done")[0]
    assert done["mode"] == "llm"
    assert done["citations"]

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["stream"] is True
    assert payload["model"] == "mock-chat"
    assert payload["max_tokens"] == 1024
    assert payload["messages"][0]["role"] == "system"
    assert "只能使用给出的【资料】" in payload["messages"][0]["content"]
    assert "【资料】" in payload["messages"][-1]["content"]


def test_llm_failure_falls_back_to_extractive(client: TestClient, monkeypatch) -> None:
    seed_corpus(client)
    _configure_mock_llm(client, base_url="http://127.0.0.1:9/v1")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    events, raw = chat_events(client, "RAG 的分块参数是多少？")
    assert "event: error" not in raw

    notices = events_by_name(events, "notice")
    assert len(notices) == 1
    assert notices[0]["mode"] == "extractive"
    assert notices[0]["message"] == "大模型连接失败，已切换本地检索模式"

    done = events_by_name(events, "done")[0]
    assert done["mode"] == "extractive"
    answer = joined_deltas(events)
    assert "根据你的知识库" in answer
    assert "以上内容来自你导入的文档" in answer
