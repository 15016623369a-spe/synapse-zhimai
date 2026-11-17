"""对话 MCP 集成测试：fake LLM tool call → SSE tool 事件 → done.mcp_results。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.services import llm

from .conftest import chat_events, events_by_name, joined_deltas, seed_corpus
from .mcp_helpers import (
    configure_mock_llm,
    custom_server,
    fake_server_script,
    filesystem_server,
    write_mcp_servers,
)


def _sse_body(text: str) -> bytes:
    event = {"choices": [{"delta": {"content": text}}]}
    return (
        f"data: {json.dumps(event, ensure_ascii=False)}\n\ndata: [DONE]\n\n"
    ).encode("utf-8")


def _mock_stream_answer(request: httpx.Request, state: dict) -> httpx.Response:
    payload = json.loads(request.content)
    state.setdefault("stream", []).append(payload)
    answer = state.get("answer", "已根据工具结果整理。")
    return httpx.Response(
        200,
        content=_sse_body(str(answer)),
        headers={"content-type": "text/event-stream"},
    )


def _completion(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": payload}]})


def test_chat_mcp_tool_loop_and_done_results(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPSE_SEARCH_ENABLED", "0")
    (data_dir / "notes-a.md").write_text("# A\n第一个笔记。", encoding="utf-8")
    (data_dir / "notes-b.md").write_text("# B\n第二个笔记。", encoding="utf-8")
    write_mcp_servers([filesystem_server(data_dir)])
    configure_mock_llm(client)

    state: dict[str, list] = {"non_stream": [], "stream": []}
    state["answer"] = "数据目录下有 notes-a.md 与 notes-b.md。"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("stream"):
            return _mock_stream_answer(request, state)
        state["non_stream"].append(payload)
        messages = payload.get("messages") or []
        has_tool_result = any(item.get("role") == "tool" for item in messages)
        if payload.get("tools") and not has_tool_result:
            return _completion(
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "mcp_filesystem_list_directory",
                                "arguments": json.dumps({"path": "."}),
                            },
                        }
                    ],
                }
            )
        return _completion({"content": ""})

    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    events, _raw = chat_events(client, "列出数据目录下的 Markdown 文件")
    names = [event["event"] for event in events]
    # MCP 成功返回结果 → 不发送 gap 事件、不触发 web search
    assert names[:3] == ["meta", "sources", "tool"]
    assert "gap" not in names
    assert "web_sources" not in names
    assert names[-1] == "done"

    tool_events = events_by_name(events, "tool")
    assert [event["phase"] for event in tool_events] == ["start", "end"]
    start, end = tool_events
    assert start["name"] == "mcp_filesystem_list_directory"
    assert start["args"] == {"path": "."}
    assert end["name"] == "mcp_filesystem_list_directory"
    assert end["ok"] is True
    assert end["summary"]
    assert "notes-a.md" in joined_deltas(events) or "notes-a.md" in str(
        events_by_name(events, "done")[0]["mcp_results"]
    )

    done = events_by_name(events, "done")[0]
    assert done["mode"] == "llm"
    assert done["gap"] is None
    assert len(done["mcp_results"]) == 1
    mcp_result = done["mcp_results"][0]
    assert mcp_result["name"] == "mcp_filesystem_list_directory"
    assert mcp_result["args"] == {"path": "."}
    assert mcp_result["ok"] is True
    assert "notes-a.md" in mcp_result["text"]

    # 第一次 completion 携带 MCP 工具 schema；第二次把工具结果作为 tool 消息回传
    assert len(state["non_stream"]) == 2
    first_payload = state["non_stream"][0]
    assert first_payload["tools"][0]["function"]["name"].startswith("mcp_filesystem_")
    assert first_payload["temperature"] == 0.1
    second_messages = state["non_stream"][1]["messages"]
    assert any(item.get("role") == "tool" for item in second_messages)

    # 最终回答的 messages 里带有工具结果 system 上下文（不伪造引用编号）
    final_messages = state["stream"][0]["messages"]
    system_contents = [
        item["content"] for item in final_messages if item.get("role") == "system"
    ]
    assert any("【外部工具结果" in content for content in system_contents)
    assert any("notes-a.md" in content for content in system_contents)
    assert "tools" not in state["stream"][0]


def test_chat_mcp_caps_tool_calls(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPSE_SEARCH_ENABLED", "0")
    (data_dir / "a.md").write_text("# A", encoding="utf-8")
    (data_dir / "b.md").write_text("# B", encoding="utf-8")
    (data_dir / "c.md").write_text("# C", encoding="utf-8")
    write_mcp_servers([filesystem_server(data_dir)])
    configure_mock_llm(client)

    state: dict[str, list] = {"non_stream": [], "stream": []}
    state["answer"] = "已最多调用三次工具。"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("stream"):
            return _mock_stream_answer(request, state)
        state["non_stream"].append(payload)
        messages = payload.get("messages") or []
        has_tool_result = any(item.get("role") == "tool" for item in messages)
        if payload.get("tools") and not has_tool_result:
            calls = [
                {
                    "id": f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": "mcp_filesystem_get_file_info",
                        "arguments": json.dumps({"path": name}),
                    },
                }
                for index, name in enumerate(["a.md", "b.md", "c.md"])
            ]
            return _completion({"content": "", "tool_calls": calls})
        return _completion({"content": ""})

    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    events, _raw = chat_events(client, "查看数据目录下这些文件的大小信息")
    tool_events = events_by_name(events, "tool")
    assert len(tool_events) == 6  # 3 start + 3 end
    assert "gap" not in [event["event"] for event in events]
    done = events_by_name(events, "done")[0]
    assert done["gap"] is None
    assert len(done["mcp_results"]) == 3
    assert all(item["ok"] for item in done["mcp_results"])


def test_chat_without_mcp_intent_keeps_rag_flow(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPSE_SEARCH_ENABLED", "0")
    seed_corpus(client)
    write_mcp_servers([filesystem_server(data_dir)])
    configure_mock_llm(client)

    state: dict[str, list] = {"non_stream": [], "stream": []}
    state["answer"] = "分块大小为 450 到 650 字，重叠 80 字。"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("stream"):
            return _mock_stream_answer(request, state)
        state["non_stream"].append(payload)
        return _completion({"content": ""})

    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    events, _raw = chat_events(client, "RAG 的分块大小和重叠参数是多少？")
    names = [event["event"] for event in events]
    assert "tool" not in names
    assert "web_sources" not in names
    done = events_by_name(events, "done")[0]
    assert done["mcp_results"] == []
    assert "450 到 650" in joined_deltas(events)
    assert state["non_stream"] == []  # 未命中 intent，不应触发工具规划调用


def test_chat_with_mcp_disabled_does_not_call_tools(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPSE_SEARCH_ENABLED", "0")
    configure_mock_llm(client)
    state: dict[str, list] = {"non_stream": [], "stream": []}
    state["answer"] = "知识库中暂无相关文件信息。"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("stream"):
            return _mock_stream_answer(request, state)
        state["non_stream"].append(payload)
        return _completion({"content": ""})

    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    events, _raw = chat_events(client, "列出数据目录下的 Markdown 文件")
    names = [event["event"] for event in events]
    assert "tool" not in names
    assert events_by_name(events, "done")[0]["mcp_results"] == []
    assert state["non_stream"] == []


def test_chat_mcp_tool_failure_degrades_to_rag(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPSE_SEARCH_ENABLED", "0")
    script = fake_server_script(tmp_path)
    write_mcp_servers(
        [custom_server(script, allowed_tools=["fail_tool"], enabled=True)]
    )
    configure_mock_llm(client)
    state: dict[str, list] = {"non_stream": [], "stream": []}
    state["answer"] = "工具暂时不可用，已根据知识库回答。"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("stream"):
            return _mock_stream_answer(request, state)
        state["non_stream"].append(payload)
        messages = payload.get("messages") or []
        has_tool_result = any(item.get("role") == "tool" for item in messages)
        if payload.get("tools") and not has_tool_result:
            return _completion(
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_fail",
                            "type": "function",
                            "function": {
                                "name": "mcp_srv_test_fail_tool",
                                "arguments": "{}",
                            },
                        }
                    ],
                }
            )
        return _completion({"content": ""})

    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    events, raw = chat_events(client, "读取数据目录里的文件列表")
    assert "event: error" not in raw
    tool_events = events_by_name(events, "tool")
    assert tool_events[-1]["phase"] == "end"
    assert tool_events[-1]["ok"] is False
    assert "失败" in tool_events[-1]["summary"]

    # 工具失败 → 回退常规 gap / RAG 流程，gap 事件与 done.gap 仍保留
    names = [event["event"] for event in events]
    assert "gap" in names
    done = events_by_name(events, "done")[0]
    assert done["gap"] is not None
    assert len(done["mcp_results"]) == 1
    assert done["mcp_results"][0]["ok"] is False
    assert done["mcp_results"][0]["error"]
    assert "已根据知识库回答" in joined_deltas(events)
