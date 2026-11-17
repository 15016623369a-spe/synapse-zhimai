"""A tiny OpenAI-compatible mock LLM server for Synapse end-to-end tests.

Usage:
    python scripts/fake_llm.py [port]   # default 7899

Endpoints:
    GET  /v1/models
    POST /v1/chat/completions   (supports stream=true SSE)
"""
from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7899

CANNED = (
    "### 结论\n\n"
    "根据你的知识库，这个问题可以从三个方面理解：检索质量、上下文组织与持续复盘。[1][2]\n\n"
    "- **检索质量**：混合检索（BM25 + 向量）比单一检索更稳，中文场景建议加入分词与短语加权。[1]\n"
    "- **上下文组织**：把资料按主题分块，并在回答中标注来源编号，能显著降低幻觉。[2]\n"
    "- **持续复盘**：每次回答后回看引用是否真的支撑结论，把缺口补回知识库。\n\n"
    "以上内容仅基于你导入的文档整理，可继续追问细节。"
)


def _answer(messages: list[dict]) -> str:
    user = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user = str(msg.get("content", ""))
            break
    short = user.strip().splitlines()[-1][:40] if user.strip() else "你的问题"
    return f"关于「{short}」，{CANNED}"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401 - silence noisy logs
        sys.stderr.write("[fake-llm] " + (fmt % args) + "\n")

    def _send_json(self, body: dict, status: int = 200) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._send_json({"object": "list", "data": [{"id": "synapse-mock", "object": "model"}]})
        elif self.path.rstrip("/").endswith("/health"):
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send_json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "bad json"}, 400)
            return

        messages = payload.get("messages") or []
        content = _answer(messages)
        model = payload.get("model") or "synapse-mock"

        if not payload.get("stream"):
            self._send_json(
                {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 120, "completion_tokens": len(content), "total_tokens": 220},
                }
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(payload: dict) -> None:
            raw = b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n"
            self.wfile.write(f"{len(raw):x}\r\n".encode("ascii") + raw + b"\r\n")
            self.wfile.flush()

        first = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        }
        chunk(first)
        # Deliver in small pieces so the UI streaming path is genuinely exercised.
        step = 6
        for i in range(0, len(content), step):
            piece = content[i : i + step]
            chunk(
                {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                }
            )
            time.sleep(0.004)
        chunk(
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        raw = b"data: [DONE]\n\n"
        self.wfile.write(f"{len(raw):x}\r\n".encode("ascii") + raw + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[fake-llm] listening on http://127.0.0.1:{PORT}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
