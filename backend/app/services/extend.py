"""联网知识扩展：搜索候选、人工审核入库、反馈闭环。

- 候选只作为「待用户确认」的外部资料展示，**不会自动入库**；
- 入库一律新建 ``source_type='web'`` 的 document（tags 含「外部扩展」、source_url 为规范化 URL），
  原文档 / 分块 / ID 完全不变；
- ``knowledge_feedback`` 记录采纳 / 忽略；被忽略 URL 后续搜索不再推荐。
"""

from __future__ import annotations

import io
import ipaddress
import logging
import os
import re
import socket
import time
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

import httpx
from pypdf import PdfReader

from .. import db
from ..utils import new_id, normalize_text, now_iso, truncate
from . import context as context_service, doc_ref, graph, ingest, rerank, search, zones
from .ingest import ParsedDocument

logger = logging.getLogger("synapse.extend")

#: 测试注入点：httpx 同步传输层（None 表示真实网络）。
HTTP_TRANSPORT: httpx.BaseTransport | None = None

FETCH_TIMEOUT = 15.0
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_PDF_TEXT_CHARS = 20000
MIN_FULLTEXT_CHARS = 200
MIN_SNIPPET_CHARS = 30
EXTERNAL_TAG = "外部扩展"
ACADEMIC_TAG = "学术"
MAX_QUERIES = 4
MAX_CANDIDATES = 12
MAX_TOOL_CANDIDATES = 8
FEEDBACK_ACCEPTED = "accepted"
FEEDBACK_REJECTED = "rejected"
FETCH_USER_AGENT = "SynapseBot/1.0 (+knowledge-extension)"


class ExtendError(ValueError):
    """知识扩展校验 / 抓取失败（路由层转 400，message 为中文）。"""


class FetchBlocked(ExtendError):
    """目标地址被安全策略拦截（内网 / 非 http(s) / 解析失败）。"""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _allow_private_fetch() -> bool:
    """测试 / 内网自建场景可显式放行（默认严格拦截内网地址）。"""
    return _env("SYNAPSE_ALLOW_PRIVATE_FETCH", "0").lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# URL 规范化与安全校验
# ---------------------------------------------------------------------------


def normalize_url(url: object) -> str:
    """规范化 URL：scheme/host 小写、去 fragment、去尾斜杠、去默认端口。"""
    text = str(url or "").strip()
    if not text:
        raise ExtendError("链接不能为空")
    parsed = urlparse(text)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ExtendError("仅支持 http/https 链接")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ExtendError("链接缺少域名")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ExtendError("链接端口不合法") from exc
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = parsed.path
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    if path == "/":
        path = ""
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def _resolve_ips(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchBlocked("无法解析该域名") from exc
    return list({str(info[4][0]) for info in infos})


def _is_private_ip(raw_ip: str) -> bool:
    try:
        address = ipaddress.ip_address(raw_ip)
    except ValueError:
        return True
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _ensure_host_safe(host: str) -> None:
    if not host:
        raise FetchBlocked("链接缺少域名")
    lowered = host.lower()
    if lowered in {"localhost", "127.0.0.1", "::1"} or lowered.endswith(
        (".local", ".internal", ".localhost")
    ):
        raise FetchBlocked("暂不支持抓取内网地址")
    for raw_ip in _resolve_ips(host):
        if _is_private_ip(raw_ip):
            raise FetchBlocked("暂不支持抓取内网地址")


def ensure_safe_url(url: object) -> str:
    """规范化并做 SSRF 防护；返回可安全请求的 URL。"""
    normalized = normalize_url(url)
    if not _allow_private_fetch():
        _ensure_host_safe(urlparse(normalized).hostname or "")
    return normalized


def _strip_bing_redirect(url: str) -> str:
    """Bing 偶尔返回 /ck/a?u=a1<base64> 跳转链接，尽量还原真实地址。"""
    parsed = urlparse(url)
    if parsed.netloc.endswith("bing.com") and parsed.path.startswith("/ck/a"):
        value = (parse_qs(parsed.query).get("u") or [""])[0]
        if value.startswith("a1"):
            import base64

            body = value[2:]
            try:
                decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
                candidate = decoded.decode("utf-8", errors="ignore")
            except (ValueError, TypeError):
                return ""
            if candidate.startswith(("http://", "https://")):
                return candidate
        return ""
    return url


# ---------------------------------------------------------------------------
# 抓取与正文抽取
# ---------------------------------------------------------------------------


MAX_REDIRECTS = 5


def _stream_with_guard(
    client: httpx.Client,
    url: object,
    headers: dict[str, str],
    max_bytes: int,
    label: str,
) -> tuple[str, str | None, bytes]:
    """手动跟随重定向，每一跳都重新做 SSRF 校验。

    ``follow_redirects=True`` 会在校验前先把请求发到 302 目标，
    「首跳合法、二跳到内网」就能绕过防护；这里改成逐跳校验。
    """
    current = ensure_safe_url(url)
    for _ in range(MAX_REDIRECTS):
        ensure_safe_url(current)
        with client.stream("GET", current, headers=headers) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ExtendError("重定向缺少目标地址")
                current = urljoin(str(response.url), location)
                continue
            if response.status_code >= 400:
                raise ExtendError(f"{label}请求失败（HTTP {response.status_code}）")
            buffer = bytearray()
            for chunk in response.iter_bytes():
                buffer.extend(chunk)
                if len(buffer) > max_bytes:
                    raise ExtendError(f"{label}内容超过 {max_bytes // (1024 * 1024)}MB 限制")
            return str(response.url), response.charset_encoding, bytes(buffer)
    raise ExtendError("重定向次数过多")


def fetch_page(url: object) -> tuple[str, str]:
    """抓取网页，返回 ``(最终 URL, HTML)``；15s / 5MB / 仅 http(s) / 拦截内网。"""
    headers = {
        "User-Agent": FETCH_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    }
    try:
        with httpx.Client(
            timeout=httpx.Timeout(FETCH_TIMEOUT, connect=10.0),
            follow_redirects=False,
            transport=HTTP_TRANSPORT,
        ) as client:
            final_url, charset, raw = _stream_with_guard(
                client, url, headers, MAX_PAGE_BYTES, "网页"
            )
        encoding = charset or "utf-8"
        try:
            html = raw.decode(encoding, errors="replace")
        except LookupError:
            html = raw.decode("utf-8", errors="replace")
        return final_url, html
    except ExtendError:
        raise
    except httpx.TimeoutException as exc:
        raise ExtendError("网页抓取超时（15 秒）") from exc
    except httpx.HTTPError as exc:
        raise ExtendError("网页抓取失败，请检查链接是否可访问") from exc


def extract_article(html: str) -> tuple[str, str]:
    """抽取网页标题与正文（复用入库模块的清洗逻辑）。"""
    title, content = ingest._html_text(html)  # noqa: SLF001 - 复用既有清洗实现
    return title, normalize_text(content)


_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)


def _arxiv_pdf_url(url: object) -> str | None:
    match = _ARXIV_URL_RE.search(str(url or ""))
    if not match:
        return None
    return search.ARXIV_PDF_TEMPLATE.format(arxiv_id=match.group(1))


def fetch_arxiv_pdf_text(pdf_url: object) -> tuple[str, str]:
    """抓取 arXiv PDF 并用 pypdf 抽取全文（≤15MB / ≤20000 字）。"""
    headers = {
        "User-Agent": FETCH_USER_AGENT,
        "Accept": "application/pdf",
    }
    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            transport=HTTP_TRANSPORT,
        ) as client:
            _final_url, _charset, buffer = _stream_with_guard(
                client, pdf_url, headers, MAX_PDF_BYTES, "PDF"
            )
        try:
            reader = PdfReader(io.BytesIO(buffer))
            title = ""
            try:
                if reader.metadata and reader.metadata.title:
                    title = str(reader.metadata.title).strip()
            except Exception:  # pragma: no cover - 个别 PDF 元数据异常
                title = ""
            pages: list[str] = []
            for page in reader.pages:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:  # pragma: no cover - 损坏页
                    continue
            content = normalize_text("\n\n".join(pages))[:MAX_PDF_TEXT_CHARS]
            return title, content
        except ExtendError:
            raise
        except Exception as exc:
            raise ExtendError("PDF 解析失败") from exc
    except ExtendError:
        raise
    except httpx.TimeoutException as exc:
        raise ExtendError("PDF 抓取超时") from exc
    except httpx.HTTPError as exc:
        raise ExtendError("PDF 抓取失败") from exc


# ---------------------------------------------------------------------------
# 反馈与知识库状态
# ---------------------------------------------------------------------------


def _normalize_optional(url: object) -> str | None:
    try:
        return normalize_url(url)
    except ExtendError:
        return None


def kb_url_map(conn) -> dict[str, str]:
    """规范化 URL → 文档标题（仅 ``source_url`` 非空的文档）。"""
    mapping: dict[str, str] = {}
    rows = db.fetch_all(
        conn,
        "SELECT source_url, title FROM documents WHERE source_url IS NOT NULL AND source_url <> ''",
    )
    for row in rows:
        normalized = _normalize_optional(row["source_url"])
        if normalized:
            mapping.setdefault(normalized, str(row["title"]))
    return mapping


def rejected_urls(conn) -> set[str]:
    rows = db.fetch_all(
        conn,
        "SELECT url FROM knowledge_feedback WHERE decision = ?",
        (FEEDBACK_REJECTED,),
    )
    return {str(row["url"]) for row in rows}


def feedback_counts(conn) -> tuple[int, int]:
    accepted = int(
        db.fetch_value(
            conn,
            "SELECT COUNT(*) FROM knowledge_feedback WHERE decision = ?",
            (FEEDBACK_ACCEPTED,),
            default=0,
        )
        or 0
    )
    rejected = int(
        db.fetch_value(
            conn,
            "SELECT COUNT(*) FROM knowledge_feedback WHERE decision = ?",
            (FEEDBACK_REJECTED,),
            default=0,
        )
        or 0
    )
    return accepted, rejected


def _record_feedback(
    conn,
    *,
    url: str,
    title: str | None,
    query: str | None,
    decision: str,
    doc_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO knowledge_feedback(id, url, title, query, decision, doc_id, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (new_id("kf"), url, title, query, decision, doc_id, now_iso()),
    )


def status_info(conn) -> dict[str, object]:
    accepted, rejected = feedback_counts(conn)
    return {
        "enabled": search.is_enabled(),
        "provider": search.provider_name(),
        "label": search.provider_label(),
        "requires_approval": True,
        "accepted_count": accepted,
        "rejected_count": rejected,
        "can_search": search.is_enabled(),
    }


# ---------------------------------------------------------------------------
# 搜索候选
# ---------------------------------------------------------------------------


def _candidate_payload(
    result: dict[str, object], kb_map: dict[str, str]
) -> dict[str, object] | None:
    url = _strip_bing_redirect(str(result.get("url") or ""))
    normalized = _normalize_optional(url)
    if not normalized:
        return None
    payload: dict[str, object] = {
        "url": normalized,
        "title": truncate(str(result.get("title") or normalized), 120, suffix=""),
        "snippet": truncate(str(result.get("snippet") or ""), 300, suffix=""),
        "source": str(result.get("source") or search.provider_name()),
        "already_in_kb": normalized in kb_map,
        "rejected_before": False,
        "pdf_url": result.get("pdf_url"),
        "relevance": float(result.get("relevance") or 0.0),
        "authority": str(result.get("authority") or ""),
    }
    if result.get("arxiv_id"):
        payload["arxiv_id"] = str(result["arxiv_id"])
    if result.get("doi"):
        payload["doi"] = str(result["doi"])
    return payload


def filter_candidates(
    conn,
    candidates: list[dict[str, object]],
    *,
    limit: int = MAX_TOOL_CANDIDATES,
) -> list[dict[str, object]]:
    """过滤被忽略 URL、去重、补充 already_in_kb 标记（用于 SSE / 面板）。"""
    rejected = rejected_urls(conn)
    kb_map = kb_url_map(conn)
    output: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in candidates:
        payload = _candidate_payload(
            {
                "url": str(item.get("url") or ""),
                "title": str(item.get("title") or ""),
                "snippet": str(item.get("snippet") or ""),
                "source": str(item.get("source") or search.provider_name()),
            },
            kb_map,
        )
        if payload is None:
            continue
        url = str(payload["url"])
        if url in seen:
            continue
        seen.add(url)
        if not payload["already_in_kb"] and url in rejected:
            continue
        output.append(payload)
        if len(output) >= limit:
            break
    return output


def _academic_raw_results(
    question: str,
    academic_context: dict[str, object],
    queries: list[str],
    per_query_limit: int,
    errors: list[str],
    context_follow_up: bool = False,
) -> list[dict[str, object]]:
    """学术意图：arXiv（按 ID / 标题）+ OpenAlex（DOI / 标题搜索）+ Bing 学术白名单。

    ``context_follow_up`` 为多轮追问场景：先用上下文英文术语检索 OpenAlex（快且相关），
    已拿到候选时跳过较慢的 Bing 学术白名单检索。
    """
    raw: list[dict[str, object]] = []
    arxiv_ids = [str(item) for item in academic_context.get("arxiv_ids") or []][:2]
    dois = [str(item) for item in academic_context.get("dois") or []][:2]
    title_terms = [str(item) for item in academic_context.get("title_terms") or []]

    for arxiv_id in arxiv_ids:
        try:
            raw.extend(item.to_dict() for item in search.arxiv_by_id(arxiv_id))
        except search.SearchUnavailable as exc:
            errors.append(str(exc))
    for doi in dois:
        try:
            raw.extend(item.to_dict() for item in search.openalex_by_doi(doi))
        except search.SearchUnavailable as exc:
            errors.append(str(exc))
    search_terms = [
        str(item) for item in academic_context.get("search_terms") or []
    ][:8]

    # 多轮追问 / 引用文档：没有 arXiv ID / DOI 时，先用上下文英文术语精确检索
    # OpenAlex（快、相关），避免只靠网页搜索拿到百科 / 词典等无关结果。
    if not raw and context_follow_up and search_terms:
        try:
            raw.extend(
                item.to_dict()
                for item in search.openalex_search(
                    " ".join(search_terms), max_results=per_query_limit
                )
            )
        except search.SearchUnavailable as exc:
            errors.append(str(exc))

    if not raw and title_terms:
        try:
            raw.extend(
                item.to_dict()
                for item in search.arxiv_search(" ".join(title_terms[:6]))
            )
        except search.SearchUnavailable as exc:
            errors.append(str(exc))

    # 普通学术查询：OpenAlex 标题搜索兜底
    if not raw and not context_follow_up and search_terms:
        try:
            raw.extend(
                item.to_dict()
                for item in search.openalex_search(
                    " ".join(search_terms), max_results=per_query_limit
                )
            )
        except search.SearchUnavailable as exc:
            errors.append(str(exc))

    # Bing：学术意图下只保留学术白名单域名；追问场景已有学术结果时跳过（慢）
    if raw and context_follow_up:
        return raw
    for query in queries:
        try:
            results = search.search_web(query, per_query_limit)
        except search.SearchUnavailable as exc:
            errors.append(str(exc))
            continue
        raw.extend(
            item.to_dict()
            for item in results
            if search.authority_category(item.url) == "academic"
            or search.authority_category(item.url) == "official"
        )
    return raw


def collect_candidates(
    conn,
    question: str,
    queries: list[str] | None = None,
    max_results: int | None = None,
    llm_config: dict[str, object] | None = None,
    conversation_id: str | None = None,
) -> dict[str, object]:
    """执行检索（学术意图走 arXiv/OpenAlex），返回带相关度/权威度打分的候选。

    传入 ``conversation_id`` 时解析多轮上下文：追问会继承上文引用文档、用
    「上文问题 + 当前问题」做检索 / 学术意图判断，响应附带
    ``context_used`` / ``context_summary``。
    """
    started = time.perf_counter()
    clean_question = str(question or "").strip()
    context_info = (
        context_service.resolve_conversation_context(
            conn, conversation_id, clean_question
        )
        if conversation_id and clean_question
        else None
    )
    reference_doc = (
        doc_ref.detect_referenced_document(conn, clean_question) if clean_question else None
    )
    if reference_doc is None and context_info is not None:
        reference_doc = context_info.get("reference_doc")  # type: ignore[assignment]
    search_question = clean_question
    if context_info is not None:
        search_question = str(context_info.get("search_text") or clean_question)
    academic_context = search.build_academic_context(search_question, reference_doc)
    if context_info is not None:
        # 用紧凑上下文卡片的 topic_terms 补充英文学术检索词（不塞入上一轮回答 / 全文）
        merged_terms = list(
            dict.fromkeys(
                [
                    *(academic_context.get("search_terms") or []),
                    *context_service.topic_search_terms(context_info),
                ]
            )
        )
        academic_context["search_terms"] = merged_terms[:10]
    rank_question = search_question
    if context_info is not None:
        topic_terms = [str(term) for term in context_info.get("topic_terms") or []]
        if topic_terms:
            rank_question = f"{search_question} {' '.join(topic_terms)}"
    academic_intent = bool(academic_context["intent"])
    intent = "academic" if academic_intent else "general"
    plan_context = {"reference_doc": reference_doc or {}}

    requested = [
        str(query).strip() for query in (queries or []) if str(query).strip()
    ][:MAX_QUERIES]
    if not requested and context_info is not None:
        # 追问：优先用确定性上下文检索词（论文标题 / arXiv ID），避开 novelty 泛词
        requested = context_service.build_search_queries(context_info, clean_question)
        if not requested:
            requested = search.deterministic_queries(search_question, plan_context)
    if not requested:
        requested = search.plan_queries(search_question, llm_config, plan_context)
    if not requested and reference_doc:
        requested = search.deterministic_queries(search_question, plan_context)
    if not requested:
        raise ExtendError("请输入要检索的问题或关键词")

    used_queries: list[str] = []
    seen_queries: set[str] = set()
    for query in requested:
        key = query.lower()
        if key in seen_queries:
            continue
        seen_queries.add(key)
        used_queries.append(query)
        if len(used_queries) >= MAX_QUERIES:
            break

    kb_map = kb_url_map(conn)
    rejected = rejected_urls(conn)
    per_query_limit = max(1, min(int(max_results or search._max_results()), 12))  # noqa: SLF001
    errors: list[str] = []

    if academic_intent:
        raw_results = _academic_raw_results(
            search_question,
            academic_context,
            used_queries,
            per_query_limit,
            errors,
            context_follow_up=context_info is not None,
        )
    else:
        raw_results = []
        for query in used_queries:
            try:
                results = search.search_web(query, per_query_limit)
            except search.SearchUnavailable as exc:
                errors.append(str(exc))
                continue
            raw_results.extend(result.to_dict() for result in results)

    raw_count = len(raw_results)
    candidates: list[dict[str, object]] = []
    seen_urls: set[str] = set()
    for result in raw_results:
        payload = _candidate_payload(result, kb_map)
        if payload is None:
            continue
        url = str(payload["url"])
        if url in seen_urls:
            continue
        seen_urls.add(url)
        if not payload["already_in_kb"] and url in rejected:
            continue  # 被忽略的 URL 不再推荐
        candidates.append(payload)

    if raw_count == 0:
        if errors:
            raise search.SearchUnavailable(errors[0])
        raise search.SearchUnavailable("联网搜索暂未找到相关结果")

    ranked, filtered_count = search.rank_and_filter_candidates(
        rank_question, reference_doc, candidates, academic_intent
    )
    # 反馈驱动的规则化重排（relevance/authority 主权重，反馈项做轻量个性化）
    ranked = rerank.rerank_candidates(conn, ranked, rank_question, reference_doc)
    filtered_count += max(0, raw_count - len(candidates))
    provider = search.provider_name()
    if academic_intent:
        academic_sources = {
            str(item.get("source") or "") for item in ranked
        } & {"arxiv", "openalex"}
        if academic_sources:
            provider = sorted(academic_sources)[0]
    took_ms = int((time.perf_counter() - started) * 1000)
    return {
        "ok": True,
        "provider": provider,
        "intent": intent,
        "queries": used_queries,
        "candidates": ranked[:MAX_CANDIDATES],
        "filtered_count": filtered_count,
        "took_ms": took_ms,
        "context_used": context_info is not None,
        "context_summary": (
            str(context_info.get("context_summary") or "") if context_info else None
        ),
    }


def prepare_tool_candidates(
    conn,
    question: str,
    reference_doc: dict[str, object] | None,
    candidates: list[dict[str, object]],
) -> tuple[list[dict[str, object]], str]:
    """工具调用阶段的候选处理：过滤 ignored/已入库 + 相关性/来源打分。"""
    prepared = filter_candidates(conn, candidates, limit=MAX_CANDIDATES)
    intent = (
        "academic"
        if search.detect_academic_intent(question, reference_doc)
        else "general"
    )
    ranked, _filtered = search.rank_and_filter_candidates(
        question, reference_doc, prepared, intent == "academic"
    )
    ranked = rerank.rerank_candidates(conn, ranked, question, reference_doc)
    return ranked, intent


def _snippet_document(
    title_hint: str, snippet: str, normalized_url: str, *, academic: bool = False
) -> ParsedDocument:
    title = title_hint or urlparse(normalized_url).netloc
    content = (
        f"{title}\n\n{snippet}\n\n来源链接：{normalized_url}\n\n"
        "（以上内容来自联网搜索摘要，尚未抓取到完整正文。）"
    )
    tags = [EXTERNAL_TAG, ACADEMIC_TAG] if academic else [EXTERNAL_TAG]
    return ParsedDocument(
        title=title,
        content=content,
        source_type="web",
        tags=tags,
        source_url=normalized_url,
    )


def commit_candidates(
    conn,
    items: list[dict[str, object]],
    zone_id: str | None = None,
    query: str | None = None,
) -> dict[str, object]:
    """把用户勾选的外部资料入库；全部为新增文档，原文档不动。"""
    if not items:
        raise ExtendError("请选择要加入知识库的资料")
    if zone_id:
        zones.get_zone_or_404(conn, zone_id)  # 分区不存在 → 404
    kb_map = kb_url_map(conn)
    added: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    seen: set[str] = set()

    for item in items:
        raw_url = str(item.get("url") or "")
        title_hint = str(item.get("title") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        item_query = str(item.get("query") or query or "").strip() or None
        try:
            normalized = normalize_url(raw_url)
        except ExtendError as exc:
            skipped.append({"url": raw_url, "reason": str(exc)})
            continue
        if normalized in kb_map:
            skipped.append({"url": normalized, "reason": "已在知识库"})
            continue
        if normalized in seen:
            skipped.append({"url": normalized, "reason": "重复的链接"})
            continue
        seen.add(normalized)

        parsed: ParsedDocument | None = None
        mode = ""
        academic_url = _arxiv_pdf_url(normalized) or _arxiv_pdf_url(
            str(item.get("pdf_url") or "")
        )
        tags = [EXTERNAL_TAG, ACADEMIC_TAG] if academic_url else [EXTERNAL_TAG]
        try:
            if academic_url:
                page_title, content = fetch_arxiv_pdf_text(academic_url)
                if len(content) < MIN_FULLTEXT_CHARS:
                    raise ExtendError("PDF 正文过短")
                doc_url = normalized
            else:
                final_url, html = fetch_page(normalized)
                page_title, content = extract_article(html)
                if len(content) < MIN_FULLTEXT_CHARS:
                    raise ExtendError("页面正文过短")
                doc_url = normalize_url(final_url)
            if doc_url in kb_map:
                skipped.append({"url": normalized, "reason": "已在知识库"})
                continue
            parsed = ParsedDocument(
                title=page_title or title_hint or urlparse(doc_url).netloc,
                content=content,
                source_type="web",
                tags=tags,
                source_url=doc_url,
            )
            mode = "fulltext"
        except FetchBlocked as exc:
            # 安全拦截的地址不允许用摘要兜底入库
            skipped.append({"url": normalized, "reason": f"抓取被拒绝：{exc}"})
            continue
        except ExtendError as exc:
            if len(snippet) >= MIN_SNIPPET_CHARS:
                parsed = _snippet_document(
                    title_hint, snippet, normalized, academic=bool(academic_url)
                )
                mode = "snippet"
            else:
                skipped.append({"url": normalized, "reason": f"抓取失败：{exc}"})
                continue

        try:
            # 注意：ingest.create_document 使用独立连接（含自己的事务与图谱重建开关），
            # 不能在调用方打开的写事务里嵌套执行，否则 SQLite 会 database is locked。
            document = ingest.create_document(
                parsed,
                tags_override=tags,
                rebuild=False,
                zone_id=zone_id,
            )
        except ingest.IngestError as exc:
            skipped.append({"url": normalized, "reason": f"入库失败：{exc}"})
            continue
        document["mode"] = mode
        doc_url = str(document.get("source_url") or normalized)
        with db.get_db() as feedback_conn:
            _record_feedback(
                feedback_conn,
                url=doc_url,
                title=str(document.get("title") or ""),
                query=item_query,
                decision=FEEDBACK_ACCEPTED,
                doc_id=str(document["id"]),
            )
        kb_map[doc_url] = str(document.get("title") or "")
        added.append(document)

    if added:
        with db.get_db() as graph_conn:
            graph.rebuild(graph_conn)
            db.add_activity(
                graph_conn,
                "extend",
                f"从联网搜索新增 {len(added)} 篇外部知识（原文档未修改）",
            )
        logger.info("联网知识扩展入库：新增 %d 篇", len(added))

    message = (
        f"已新增 {len(added)} 篇外部知识（原文档未修改）"
        if added
        else "没有新增外部知识"
    )
    return {"ok": True, "added": added, "skipped": skipped, "message": message}


# ---------------------------------------------------------------------------
# 忽略反馈
# ---------------------------------------------------------------------------


def reject_candidates(
    conn, items: list[dict[str, object]], query: str | None = None
) -> dict[str, object]:
    if not items:
        raise ExtendError("请选择要忽略的资料")
    clean_query = str(query or "").strip() or None
    remembered = 0
    seen: set[str] = set()
    for item in items:
        raw_url = str(item.get("url") or "")
        try:
            normalized = normalize_url(raw_url)
        except ExtendError:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        existing = db.fetch_one(
            conn,
            "SELECT id FROM knowledge_feedback WHERE url = ? AND decision = ?",
            (normalized, FEEDBACK_REJECTED),
        )
        if existing is not None:
            continue
        _record_feedback(
            conn,
            url=normalized,
            title=str(item.get("title") or "").strip() or None,
            query=clean_query,
            decision=FEEDBACK_REJECTED,
        )
        remembered += 1
    if remembered:
        db.add_activity(conn, "extend", f"忽略 {remembered} 条外部资料推荐")
    return {"ok": True, "remembered": remembered}
