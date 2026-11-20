"""LLM 接入（OpenAI 兼容 Chat Completions，流式）+ 离线 extractive 回答。

- 配置优先级：DB settings（UI 保存）> 环境变量 > 空；
- 未配置模型时完全离线：用检索到的片段拼装 extractive 回答；
- LLM 调用失败时抛 ``LLMError``，由 chat 路由发 ``notice`` 事件后降级；
- 测试注入点：``HTTP_TRANSPORT``（httpx MockTransport），测试不访问真实网络。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .. import config, db
from ..utils import collapse_whitespace, split_sentences, truncate
from . import embedding

logger = logging.getLogger("synapse.llm")

#: 测试注入点：httpx 异步传输层（None 表示真实网络）。
HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None
#: 测试注入点：httpx 同步传输层（用于 plan_queries 等同步代码路径）。
HTTP_SYNC_TRANSPORT: httpx.BaseTransport | None = None

DEFAULT_MAX_TOKENS = 1024
STREAM_TIMEOUT_SECONDS = 120.0
TEST_TIMEOUT_SECONDS = 20.0
SOURCE_TRUNCATE_CHARS = 1200
WEB_TRUNCATE_CHARS = 500
HISTORY_MESSAGE_LIMIT = 6
EXTRACTIVE_MAX_SOURCES = 3
EXTRACTIVE_SENTENCE_CHARS = 90
#: 近重复句子判定：词集合 Jaccard 或包含度超过阈值即视为重复
EXTRACTIVE_DEDUPE_JACCARD = 0.35
EXTRACTIVE_DEDUPE_CONTAINMENT = 0.5

SYSTEM_PROMPT = (
    "你是「Synapse 知脉」——一个只基于用户个人知识库回答问题的中文助手。规则：\n"
    "1) 只能使用给出的【资料】作答，禁止编造；\n"
    "2) 每个结论后用 [1][2] 形式标注来源编号；\n"
    "3) 资料不足时明确说「知识库中没有找到足够信息」并指出缺什么；\n"
    "4) 先给结论，再列要点，使用简洁 Markdown；\n"
    "5) 用中文回答。"
)

NO_RESULT_ANSWER = "知识库里暂时没有找到相关内容。试试换种问法，或先导入相关文档。"
EXTRACTIVE_FOOTER = "以上内容来自你导入的文档；配置大模型后可获得更连贯的总结。"


class LLMError(RuntimeError):
    """LLM 调用失败（网络 / 鉴权 / 超时 / 响应异常）。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def _parse_temperature(raw: object, fallback: float = config.DEFAULT_TEMPERATURE) -> float:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return max(0.0, min(2.0, value))


def get_config() -> dict[str, object]:
    """DB settings 覆盖环境变量，返回当前生效的 LLM 配置。"""
    with db.get_db() as conn:
        stored = db.settings_all(conn)
    env = config.llm_env()

    base_url = stored["llm_base_url"] if "llm_base_url" in stored else str(env["base_url"])
    model = stored["llm_model"] if "llm_model" in stored else str(env["model"])
    api_key = stored["llm_api_key"] if "llm_api_key" in stored else str(env["api_key"])
    temperature = (
        _parse_temperature(stored["llm_temperature"])
        if "llm_temperature" in stored
        else _parse_temperature(env["temperature"])
    )
    return {
        "base_url": str(base_url or "").strip().rstrip("/"),
        "api_key": str(api_key or "").strip(),
        "model": str(model or "").strip(),
        "temperature": temperature,
    }


def is_configured(llm_config: dict[str, object] | None = None) -> bool:
    cfg = llm_config or get_config()
    return bool(str(cfg.get("base_url") or "").strip() and str(cfg.get("model") or "").strip())


def public_config(llm_config: dict[str, object] | None = None) -> dict[str, object]:
    """对外暴露的配置（绝不含 key 明文）。"""
    cfg = llm_config or get_config()
    return {
        "configured": is_configured(cfg),
        "model": str(cfg.get("model") or ""),
        "base_url": str(cfg.get("base_url") or ""),
    }


def update_config(updates: dict[str, object]) -> None:
    """写入配置：值为 ``None`` 表示不修改；空串表示清除。"""
    with db.get_db() as conn:
        if updates.get("base_url") is not None:
            db.setting_set(conn, "llm_base_url", str(updates["base_url"]).strip().rstrip("/"))
        if updates.get("model") is not None:
            db.setting_set(conn, "llm_model", str(updates["model"]).strip())
        if updates.get("api_key") is not None:
            db.setting_set(conn, "llm_api_key", str(updates["api_key"]).strip())
        if updates.get("temperature") is not None:
            temperature = _parse_temperature(updates["temperature"])
            db.setting_set(conn, "llm_temperature", str(temperature))


# ---------------------------------------------------------------------------
# Prompt 构建
# ---------------------------------------------------------------------------


WEB_SYSTEM_NOTE = (
    "6) 【外部资料】来自联网搜索、尚未加入用户知识库；如使用外部资料，请用 [W1][W2] 标注，"
    "并在回答中说明来自外部搜索结果；知识库资料仍用 [n] 标注。"
)

MCP_SYSTEM_NOTE = (
    "7) 【外部工具结果】来自 MCP 只读工具（文件系统 / 数据库 / GitHub 等），尚未加入知识库；"
    "如使用工具结果，请用 [T1][T2] 标注并说明数据来自外部工具，不要编造工具未返回的内容。"
)


def build_messages(
    query: str,
    results: list[dict[str, object]],
    history: list[dict[str, object]] | None = None,
    web_sources: list[dict[str, object]] | None = None,
    extra_system: str | None = None,
    source_truncate_chars: int = SOURCE_TRUNCATE_CHARS,
    mcp_context: str | None = None,
) -> list[dict[str, str]]:
    """构造发给 LLM 的消息：system 固定规则 + 最近 6 条历史 + 编号资料。

    ``extra_system`` 追加到 system prompt（引用文档总结等场景）；
    ``source_truncate_chars`` 可调大以塞入文档级全文上下文；
    ``mcp_context`` 为 MCP 工具结果外部上下文，作为额外 ``system`` 消息追加
    （不伪造引用编号，工具结果用 [T1][T2] 标注）。
    """
    sources: list[str] = []
    for index, result in enumerate(results, start=1):
        body = str(result.get("text") or "").strip()
        if len(body) > source_truncate_chars:
            body = body[:source_truncate_chars].rstrip() + "…"
        sources.append(
            f"[{index}] 《{result.get('doc_title', '')}》"
            f"（来源：{result.get('source_type', 'note')}）\n{body}"
        )
    source_block = "\n\n".join(sources) if sources else "（未检索到相关资料）"

    web_block = ""
    if web_sources:
        web_items: list[str] = []
        for index, item in enumerate(web_sources[:8], start=1):
            body = str(item.get("snippet") or "").strip()
            if len(body) > WEB_TRUNCATE_CHARS:
                body = body[:WEB_TRUNCATE_CHARS].rstrip() + "…"
            web_items.append(
                f"[W{index}] {item.get('title', '')}（来源：{item.get('url', '')}）\n{body}"
            )
        web_block = "\n\n【外部资料（联网搜索，未入库）】\n" + "\n\n".join(web_items)

    user_content = f"【资料】\n{source_block}{web_block}\n\n【问题】\n{query.strip()}"

    system_prompt = SYSTEM_PROMPT + ("\n" + WEB_SYSTEM_NOTE if web_sources else "")
    if mcp_context:
        system_prompt += "\n" + MCP_SYSTEM_NOTE
    if extra_system:
        system_prompt += "\n" + extra_system
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    if mcp_context:
        messages.append({"role": "system", "content": mcp_context})
    for item in (history or [])[-HISTORY_MESSAGE_LIMIT:]:
        role = str(item.get("role") or "")
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": truncate(content, 800)})
    messages.append({"role": "user", "content": user_content})
    return messages


# ---------------------------------------------------------------------------
# OpenAI 兼容流式调用
# ---------------------------------------------------------------------------


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _status_message(status_code: int) -> str:
    if status_code == 401:
        return "大模型鉴权失败，请检查 API Key"
    if status_code == 403:
        return "大模型拒绝访问，请检查 API Key 权限"
    if status_code == 404:
        return "未找到模型接口（404）：请检查 Base URL 是否正确，或确认模型服务已启动"
    if status_code == 429:
        return "大模型请求过于频繁，请稍后重试"
    if status_code >= 500:
        return "大模型服务暂时不可用"
    return f"大模型接口返回 HTTP {status_code}"


async def stream_chat(
    llm_config: dict[str, object], messages: list[dict[str, str]]
) -> AsyncIterator[str]:
    """流式调用 Chat Completions，逐段 yield 文本增量；失败抛 ``LLMError``。"""
    base_url = str(llm_config.get("base_url") or "").rstrip("/")
    if not base_url or not llm_config.get("model"):
        raise LLMError("尚未配置大模型")
    url = f"{base_url}/chat/completions"
    payload: dict[str, Any] = {
        "model": llm_config["model"],
        "messages": messages,
        "stream": True,
        "temperature": _parse_temperature(llm_config.get("temperature")),
        "max_tokens": DEFAULT_MAX_TOKENS,
    }
    timeout = httpx.Timeout(STREAM_TIMEOUT_SECONDS, connect=15.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=HTTP_TRANSPORT) as client:
            async with client.stream(
                "POST", url, json=payload, headers=_headers(str(llm_config.get("api_key") or ""))
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise LLMError(_status_message(response.status_code))
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield str(content)
    except LLMError:
        raise
    except httpx.TimeoutException as exc:
        raise LLMError("大模型响应超时") from exc
    except httpx.HTTPError as exc:
        raise LLMError("无法连接模型服务，请确认服务已启动") from exc


async def test_connection(llm_config: dict[str, object] | None = None) -> dict[str, object]:
    """测试连接：始终返回 ``{ok, latency_ms, model, message}``，不抛异常。"""
    cfg = llm_config or get_config()
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    model = str(cfg.get("model") or "")
    if not base_url or not model:
        return {
            "ok": False,
            "latency_ms": 0,
            "model": model,
            "message": "尚未配置模型地址与模型名称",
        }
    url = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
        "temperature": 0,
        "max_tokens": 8,
    }
    timeout = httpx.Timeout(TEST_TIMEOUT_SECONDS, connect=10.0)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=HTTP_TRANSPORT) as client:
            response = await client.post(
                url, json=payload, headers=_headers(str(cfg.get("api_key") or ""))
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            return {
                "ok": False,
                "latency_ms": latency_ms,
                "model": model,
                "message": _status_message(response.status_code),
            }
        try:
            response.json()
        except ValueError:
            return {
                "ok": False,
                "latency_ms": latency_ms,
                "model": model,
                "message": "大模型返回内容无法解析",
            }
        return {"ok": True, "latency_ms": latency_ms, "model": model, "message": "连接成功"}
    except httpx.TimeoutException:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "model": model,
            "message": "连接超时（20 秒），请检查 Base URL 是否可达、模型服务是否已启动",
        }
    except httpx.HTTPError:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "model": model,
            "message": "无法连接模型服务：请确认模型服务已启动、Base URL 是否正确",
        }


# ---------------------------------------------------------------------------
# 非流式 Chat Completions（检索词规划 / 工具调用）
# ---------------------------------------------------------------------------


def _completion_payload(
    llm_config: dict[str, object],
    messages: list[dict[str, str]],
    *,
    tools: list[dict[str, object]] | None = None,
    tool_choice: str | None = None,
    max_tokens: int = 512,
    temperature: float | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": llm_config.get("model"),
        "messages": messages,
        "stream": False,
        "temperature": (
            _parse_temperature(llm_config.get("temperature"))
            if temperature is None
            else max(0.0, min(2.0, float(temperature)))
        ),
        "max_tokens": max(1, min(int(max_tokens), 4096)),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    return payload


def _completion_url(llm_config: dict[str, object]) -> str:
    base_url = str(llm_config.get("base_url") or "").rstrip("/")
    if not base_url or not llm_config.get("model"):
        raise LLMError("尚未配置大模型")
    return f"{base_url}/chat/completions"


def _parse_completion(response: httpx.Response) -> dict[str, object]:
    if response.status_code >= 400:
        raise LLMError(_status_message(response.status_code))
    try:
        payload = response.json()
    except ValueError as exc:
        raise LLMError("大模型返回内容无法解析") from exc
    if not isinstance(payload, dict):
        raise LLMError("大模型返回内容无法解析")
    return payload


def chat_completion(
    llm_config: dict[str, object],
    messages: list[dict[str, str]],
    *,
    tools: list[dict[str, object]] | None = None,
    tool_choice: str | None = None,
    max_tokens: int = 512,
    temperature: float | None = None,
    timeout: float = 60.0,
) -> dict[str, object]:
    """同步非流式调用（用于同步接口中的检索词规划等）。"""
    url = _completion_url(llm_config)
    payload = _completion_payload(
        llm_config,
        messages,
        tools=tools,
        tool_choice=tool_choice,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    timeout_obj = httpx.Timeout(timeout, connect=15.0)
    try:
        with httpx.Client(
            timeout=timeout_obj,
            transport=HTTP_SYNC_TRANSPORT,
            headers=_headers(str(llm_config.get("api_key") or "")),
        ) as client:
            response = client.post(url, json=payload)
        return _parse_completion(response)
    except LLMError:
        raise
    except httpx.TimeoutException as exc:
        raise LLMError("大模型响应超时") from exc
    except httpx.HTTPError as exc:
        raise LLMError("无法连接模型服务，请确认服务已启动") from exc


async def chat_completion_async(
    llm_config: dict[str, object],
    messages: list[dict[str, str]],
    *,
    tools: list[dict[str, object]] | None = None,
    tool_choice: str | None = None,
    max_tokens: int = 512,
    temperature: float | None = None,
    timeout: float = 60.0,
) -> dict[str, object]:
    """异步非流式调用（用于 chat 工具调用阶段）。"""
    url = _completion_url(llm_config)
    payload = _completion_payload(
        llm_config,
        messages,
        tools=tools,
        tool_choice=tool_choice,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    timeout_obj = httpx.Timeout(timeout, connect=15.0)
    try:
        async with httpx.AsyncClient(
            timeout=timeout_obj,
            transport=HTTP_TRANSPORT,
            headers=_headers(str(llm_config.get("api_key") or "")),
        ) as client:
            response = await client.post(url, json=payload)
        return _parse_completion(response)
    except LLMError:
        raise
    except httpx.TimeoutException as exc:
        raise LLMError("大模型响应超时") from exc
    except httpx.HTTPError as exc:
        raise LLMError("无法连接模型服务，请确认服务已启动") from exc


def extract_message_text(payload: dict[str, object]) -> str:
    """从非流式 completion 响应中取出 assistant 文本（可能为空）。"""
    try:
        choices = payload.get("choices") or []
        message = choices[0].get("message") or {}
        content = message.get("content")
        return str(content or "").strip()
    except (AttributeError, IndexError, TypeError):
        return ""


def extract_tool_calls(payload: dict[str, object]) -> list[dict[str, object]]:
    """提取 tool_calls 列表（OpenAI 兼容格式），无则返回空列表。"""
    try:
        choices = payload.get("choices") or []
        message = choices[0].get("message") or {}
        calls = message.get("tool_calls") or []
        return [call for call in calls if isinstance(call, dict)]
    except (AttributeError, IndexError, TypeError):
        return []


# ---------------------------------------------------------------------------
# 离线 extractive 回答
# ---------------------------------------------------------------------------


_MARKDOWN_PREFIX_RE = re.compile(r"^[#>*+\-\s`]+")
_MARKDOWN_INLINE_RE = re.compile(r"[`*_~]+")


def _clean_sentence(sentence: str) -> str:
    cleaned = _MARKDOWN_PREFIX_RE.sub("", sentence.strip())
    cleaned = _MARKDOWN_INLINE_RE.sub("", cleaned)
    return collapse_whitespace(cleaned)


def _sentence_tokens(sentence: str) -> set[str]:
    return set(embedding.tokenize_terms(sentence))


def _jaccard(first: set[str], second: set[str]) -> float:
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def _is_near_duplicate(first: set[str], second: set[str]) -> bool:
    if not first or not second:
        return False
    intersection = len(first & second)
    if intersection == 0:
        return False
    if intersection / len(first | second) >= EXTRACTIVE_DEDUPE_JACCARD:
        return True
    return intersection / min(len(first), len(second)) >= EXTRACTIVE_DEDUPE_CONTAINMENT


def _candidate_sentences(query: str, text: str, limit: int = 5) -> list[str]:
    """按与 query 的词重合度排序，返回最多 ``limit`` 个候选句子。"""
    query_terms = set(embedding.tokenize_terms(query))
    sentences = split_sentences(text) or [text]
    scored: list[tuple[float, int, str]] = []
    seen: set[str] = set()
    for position, sentence in enumerate(sentences):
        compact = _clean_sentence(sentence)
        if len(compact) < 15:  # 标题行 / 列表项过短，不作为要点
            continue
        # 跳过 Markdown 表格行 / 分隔线，避免要点里出现「| 78% | 188 |」
        if compact.startswith("|") or compact.count("|") >= 2:
            continue
        if compact in seen:
            continue
        seen.add(compact)
        tokens = _sentence_tokens(compact)
        overlap = len(query_terms & tokens)
        score = float(overlap) * (1.0 if len(compact) <= 140 else 0.6)
        score += 0.05 if len(compact) >= 30 else 0.0
        score -= position * 0.01
        scored.append((score, position, truncate(compact, EXTRACTIVE_SENTENCE_CHARS)))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [sentence for _score, _position, sentence in scored[:limit]]


def build_extractive_answer(query: str, results: list[dict[str, object]]) -> str:
    """无需模型的高质量摘录式回答（完全离线，要点做近重复去重）。"""
    if not results:
        return NO_RESULT_ANSWER
    bullets: list[str] = []
    chosen_tokens: list[set[str]] = []
    for index, result in enumerate(results[:EXTRACTIVE_MAX_SOURCES], start=1):
        text = str(result.get("text") or "")
        doc_title = _clean_sentence(str(result.get("doc_title") or ""))
        candidates = [
            sentence
            for sentence in _candidate_sentences(query, text)
            # 标题行与 query 的词重合往往最高，但它不是答案；跳过它，
            # 否则「SEA 2026 年市场进入策略」这类问题只会返回一行标题。
            if not sentence.lstrip().startswith("#")
            and _clean_sentence(sentence) != doc_title
        ]
        if not candidates:
            fallback = truncate(_clean_sentence(collapse_whitespace(text)), EXTRACTIVE_SENTENCE_CHARS)
            candidates = [fallback] if fallback else []
        picked = ""
        for sentence in candidates:
            tokens = _sentence_tokens(sentence)
            if any(_is_near_duplicate(tokens, previous) for previous in chosen_tokens):
                continue
            picked = sentence
            chosen_tokens.append(tokens)
            break
        if picked:
            bullets.append(f"- {picked} [{index}]")
    if not bullets:
        return NO_RESULT_ANSWER
    lines = [f"根据你的知识库，我找到了 {len(bullets)} 段相关内容：", "", *bullets, ""]
    lines.append(EXTRACTIVE_FOOTER)
    return "\n".join(lines)
