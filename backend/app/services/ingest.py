"""文档解析（md/txt/pdf/docx/html/url/note）与入库。"""

from __future__ import annotations

import io
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import docx
import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from .. import config, db
from ..utils import (
    collapse_whitespace,
    looks_like_filename_title,
    make_summary,
    new_id,
    normalize_tags,
    normalize_text,
    now_iso,
    parse_frontmatter,
    parse_tags_json,
    truncate,
)
from . import chunking, embedding, graph, ocr

logger = logging.getLogger("synapse.ingest")

#: 测试注入点：httpx 传输层（None 表示真实网络）。
HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None


class IngestError(ValueError):
    """解析 / 抓取 / 入库失败，message 为中文可读信息。"""


@dataclass
class ParsedDocument:
    title: str
    content: str
    source_type: str
    tags: list[str] = field(default_factory=list)
    summary: str | None = None
    filename: str | None = None
    source_url: str | None = None
    #: 扫描版 PDF 是否使用了 OCR（不写入 DB schema，仅上传链路可见）
    ocr_used: bool = False
    ocr_pages: int = 0


# ---------------------------------------------------------------------------
# 各格式解析
# ---------------------------------------------------------------------------


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise IngestError("无法解码文本内容，请使用 UTF-8 编码")


def _clean_title(raw: str, fallback: str) -> str:
    title = collapse_whitespace(raw or "")
    if not title:
        title = collapse_whitespace(fallback) or "未命名文档"
    return truncate(title, 120)


def _stem(filename: str) -> str:
    return Path(filename).stem or filename


def _parse_markdown(filename: str, data: bytes) -> ParsedDocument:
    text = _decode_text(data)
    meta, body = parse_frontmatter(text)
    title = _clean_title(str(meta.get("title") or ""), _stem(filename))
    summary = str(meta.get("summary") or "").strip() or None
    return ParsedDocument(
        title=title,
        content=body,
        source_type="md",
        tags=normalize_tags(meta.get("tags") or []),
        summary=summary,
        filename=filename,
    )


def _parse_txt(filename: str, data: bytes) -> ParsedDocument:
    return ParsedDocument(
        title=_clean_title("", _stem(filename)),
        content=_decode_text(data),
        source_type="txt",
        filename=filename,
    )


_PDF_FURNITURE_RE = re.compile(
    r"^(page\s*\d+|\d{1,4}|doi\s*:|https?://doi\.org|arxiv\s*:|"
    r"authorized licensed use|downloaded from|"
    r"ieee transactions|proceedings of|copyright|©|"
    r"received\s|accepted\s|published\s|"
    r"figure\s*\d+|table\s*\d+|index terms|keywords\s*:|"
    r"[ivx]{1,5}$)",
    re.IGNORECASE,
)
_PDF_JUNK_LINE_HINTS = ("authorized licensed", "downloaded from", "all rights reserved")


def _fix_pdf_hyphenation(text: str) -> str:
    """修复 PDF 断词：``train- ing`` → ``training``，``multi- object`` → ``multiobject``。"""
    text = re.sub(r"([A-Za-z])-[\s\n]+([a-z])", r"\1\2", text)
    text = re.sub(r"([a-z])-\n([a-z])", r"\1\2", text)
    return text


def _pdf_line_signature(line: str) -> str:
    return re.sub(r"\d+", "#", line.strip().lower())[:100]


def clean_pdf_pages(pages: list[str]) -> str:
    """跨页识别并移除页眉/页脚/页码噪音，并修复断词。"""
    page_lines: list[list[str]] = []
    for page in pages:
        lines = [line.strip() for line in str(page or "").splitlines()]
        page_lines.append([line for line in lines if line])

    signature_counts: Counter[str] = Counter()
    for lines in page_lines:
        seen = {_pdf_line_signature(line) for line in lines}
        signature_counts.update(seen)
    page_total = max(1, len(page_lines))
    common = {
        signature
        for signature, count in signature_counts.items()
        if count >= max(3, int(page_total * 0.6)) and len(signature) <= 100
    }

    cleaned_pages: list[str] = []
    for lines in page_lines:
        kept: list[str] = []
        for line in lines:
            if _pdf_line_signature(line) in common:
                continue
            if _PDF_FURNITURE_RE.match(line):
                continue
            if any(hint in line.lower() for hint in _PDF_JUNK_LINE_HINTS):
                continue
            kept.append(line)
        if kept:
            cleaned_pages.append("\n".join(kept))
    return _fix_pdf_hyphenation("\n\n".join(cleaned_pages))


def _extract_pdf_title(pages: list[str], metadata_title: str, filename: str) -> str:
    """标题提取：metadata → 首页正文标题 → 文件名 fallback。"""
    candidates: list[str] = []
    if metadata_title:
        candidates.append(metadata_title)
    first_lines: list[str] = []
    if pages:
        for line in pages[0].splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if _PDF_FURNITURE_RE.match(stripped):
                continue
            lowered = stripped.lower()
            if any(
                marker in lowered
                for marker in ("abstract", "摘要", "@", "university", "institute",
                               "department", "school of", "arxiv:", "preprint")
            ):
                break
            first_lines.append(stripped)
            if len(first_lines) >= 3 or sum(len(item) for item in first_lines) > 180:
                break
    if first_lines:
        candidates.append(" ".join(first_lines))
    for candidate in candidates:
        title = collapse_whitespace(candidate)
        if len(title) >= 3 and not looks_like_filename_title(title):
            return title[:180]
    return _stem(filename)


def _parse_pdf(filename: str, data: bytes) -> ParsedDocument:
    try:
        reader = PdfReader(io.BytesIO(data))
        raw_pages: list[str] = []
        for page in reader.pages:
            try:
                raw_pages.append(page.extract_text() or "")
            except Exception:  # pragma: no cover - 个别损坏页
                logger.warning("PDF 页面文本抽取失败，已跳过一页")
    except Exception as exc:
        raise IngestError("PDF 解析失败，文件可能已损坏") from exc
    page_count = len(raw_pages)
    metadata_title = ""
    try:
        metadata_title = str(reader.metadata.title or "") if reader.metadata else ""
    except Exception:  # pragma: no cover - 元数据异常
        metadata_title = ""
    title = _extract_pdf_title(raw_pages, metadata_title, filename)
    content = clean_pdf_pages(raw_pages)
    ocr_used = False
    ocr_pages = 0
    # 文本不足视为扫描件：尝试 OCR（不可用 / 失败时降级，不阻塞原文本）
    if ocr.needs_ocr(content, page_count):
        if ocr.ocr_available():
            result: dict[str, object] | None = None
            try:
                result = ocr.extract_scanned_pdf(data, max_pages=ocr.max_pages())
            except ocr.OCRError as exc:
                logger.warning("扫描 PDF OCR 失败（%s）：%s", filename, exc)
            except Exception:  # pragma: no cover - 兜底，不让 OCR 拖垮上传
                logger.exception("扫描 PDF OCR 异常：%s", filename)
            ocr_text = str(result.get("text") or "") if result is not None else ""
            ocr_blocks = [block for block in ocr_text.split("\n\n") if block.strip()]
            if ocr_blocks:
                ocr_content = clean_pdf_pages(ocr_blocks)
                if not ocr_content.strip():
                    ocr_content = "\n\n".join(ocr_blocks).strip()
                if ocr_content.strip():
                    content = ocr_content
                    ocr_used = True
                    ocr_pages = int(result.get("ocr_pages") or 0) if result else 0
                    title = _extract_pdf_title(ocr_blocks, metadata_title, filename)
        elif not content.strip():
            raise IngestError("未提取到文本（可能为扫描件；OCR 不可用）")
    if not content.strip():
        raise IngestError("未提取到文本（可能为扫描件）")
    return ParsedDocument(
        title=_clean_title(title, _stem(filename)),
        content=content,
        source_type="pdf",
        filename=filename,
        ocr_used=ocr_used,
        ocr_pages=ocr_pages,
    )


def _parse_docx(filename: str, data: bytes) -> ParsedDocument:
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise IngestError("Word 文档解析失败，文件可能已损坏") from exc
    blocks: list[str] = [paragraph.text.strip() for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                blocks.append(" | ".join(cells))
    content = "\n".join(block for block in blocks if block)
    if not content.strip():
        raise IngestError("未提取到文本（Word 文档为空）")
    try:
        core_title = str(document.core_properties.title or "")
    except Exception:  # pragma: no cover - 元数据异常
        core_title = ""
    return ParsedDocument(
        title=_clean_title(core_title, _stem(filename)),
        content=content,
        source_type="docx",
        filename=filename,
    )


def _html_title(soup: BeautifulSoup) -> str:
    if soup.title and soup.title.string:
        return collapse_whitespace(soup.title.string)
    heading = soup.find(["h1", "h2"])
    if heading:
        return collapse_whitespace(heading.get_text(" ", strip=True))
    return ""


def _html_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "iframe", "svg"]):
        tag.decompose()
    title = _html_title(soup)
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    content = soup.get_text("\n")
    content = "\n".join(line.strip() for line in content.split("\n") if line.strip())
    return title, content


def _parse_html(filename: str, data: bytes) -> ParsedDocument:
    title, content = _html_text(_decode_text(data))
    if not content.strip():
        raise IngestError("未提取到正文内容")
    return ParsedDocument(
        title=_clean_title(title, _stem(filename)),
        content=content,
        source_type="html",
        filename=filename,
    )


def _auto_enrich_enabled() -> bool:
    """新上传 PDF 是否自动整理（默认开启，``SYNAPSE_AUTO_ENRICH=0`` 关闭）。"""
    import os

    return os.environ.get("SYNAPSE_AUTO_ENRICH", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def parse_file(filename: str, data: bytes) -> ParsedDocument:
    """按扩展名解析上传文件。"""
    if not filename:
        raise IngestError("缺少文件名")
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix not in config.SUPPORTED_EXTENSIONS:
        supported = "、".join(sorted(config.SUPPORTED_EXTENSIONS))
        raise IngestError(f"不支持的文件类型（支持：{supported}）")
    if len(data) == 0:
        raise IngestError("文件内容为空")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise IngestError("文件超过 25MB 上限")
    if suffix in {"md", "markdown"}:
        return _parse_markdown(filename, data)
    if suffix == "txt":
        return _parse_txt(filename, data)
    if suffix == "pdf":
        return _parse_pdf(filename, data)
    if suffix == "docx":
        return _parse_docx(filename, data)
    return _parse_html(filename, data)


def parse_text_note(title: str | None, content: str, tags: list[str] | None = None) -> ParsedDocument:
    """纯文本笔记。"""
    text = normalize_text(content)
    if not text:
        raise IngestError("笔记内容不能为空")
    fallback_title = ""
    first_line = text.split("\n", 1)[0]
    if first_line:
        fallback_title = first_line.lstrip("#").strip()
    return ParsedDocument(
        title=_clean_title(title or "", truncate(fallback_title, 48) or "未命名笔记"),
        content=text,
        source_type="note",
        tags=normalize_tags(tags or []),
    )


# ---------------------------------------------------------------------------
# URL 抓取
# ---------------------------------------------------------------------------


def _ensure_url_safe(url: str) -> None:
    """复用知识扩展的 SSRF 防护（内网 / 环回 / 非 http(s) 一律拒绝）。

    延迟 import：extend 在模块级 import 了 ingest，这里不能反过来在顶层 import。
    """
    from . import extend as extend_service

    try:
        extend_service.ensure_safe_url(url)
    except extend_service.FetchBlocked as exc:
        raise IngestError(f"抓取被拒绝：{exc}") from exc
    except extend_service.ExtendError as exc:
        raise IngestError(str(exc)) from exc


async def fetch_url_html(url: str) -> tuple[str, str]:
    """抓取网页 HTML，返回 ``(html, 最终 URL)``；限制 5MB / 15s。

    重定向手动跟随（最多 5 跳），每一跳都重新做 SSRF 校验，
    避免「首跳合法、302 到内网」的绕过。
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise IngestError("仅支持 http/https 链接")
    headers = {"User-Agent": config.URL_USER_AGENT, "Accept": "text/html,*/*"}
    timeout = httpx.Timeout(config.URL_TIMEOUT_SECONDS, connect=10.0)
    current = url.strip()
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers=headers,
            transport=HTTP_TRANSPORT,
        ) as client:
            for _ in range(5):
                _ensure_url_safe(current)
                async with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise IngestError("网页重定向缺少目标地址")
                        current = urljoin(str(response.url), location)
                        continue
                    if response.status_code >= 400:
                        raise IngestError(f"网页请求失败（HTTP {response.status_code}）")
                    buffer = bytearray()
                    async for chunk in response.aiter_bytes():
                        buffer.extend(chunk)
                        if len(buffer) > config.MAX_URL_BYTES:
                            raise IngestError("网页内容超过 5MB 限制")
                    encoding = response.charset_encoding or "utf-8"
                    raw = bytes(buffer)
                    try:
                        html = raw.decode(encoding, errors="replace")
                    except LookupError:
                        html = raw.decode("utf-8", errors="replace")
                    return html, str(response.url)
            raise IngestError("网页重定向次数过多")
    except IngestError:
        raise
    except httpx.TimeoutException as exc:
        raise IngestError("网页请求超时（15 秒）") from exc
    except httpx.HTTPError as exc:
        raise IngestError("无法访问该网页，请检查链接是否可访问") from exc


async def parse_url(
    url: str, title: str | None = None, tags: list[str] | None = None
) -> ParsedDocument:
    html, final_url = await fetch_url_html(url)
    page_title, content = _html_text(html)
    if not content.strip():
        raise IngestError("未提取到网页正文内容")
    host = urlparse(final_url).netloc or urlparse(url).netloc
    return ParsedDocument(
        title=_clean_title(title or "", page_title or host),
        content=content,
        source_type="url",
        tags=normalize_tags(tags or []),
        source_url=final_url,
    )


# ---------------------------------------------------------------------------
# 入库
# ---------------------------------------------------------------------------


def serialize_document(row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "title": str(row["title"]),
        "filename": row["filename"],
        "source_type": str(row["source_type"]),
        "source_url": row["source_url"],
        "tags": parse_tags_json(row["tags"]),
        "summary": row["summary"],
        "size": int(row["size"] or 0),
        "chunk_count": int(row["chunk_count"] or 0)
        if "chunk_count" in row.keys()
        else 0,
        "concept_count": int(row["concept_count"] or 0)
        if "concept_count" in row.keys()
        else 0,
        "status": str(row["status"]),
        "error": row["error"],
        "zone_id": row["zone_id"],
        "zone_name": row["zone_name"] if "zone_name" in row.keys() else None,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


_DOCUMENT_SELECT = (
    "SELECT d.*, z.name AS zone_name, "
    "(SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id) AS chunk_count, "
    "(SELECT COUNT(*) FROM doc_concepts dc WHERE dc.doc_id = d.id) AS concept_count "
    "FROM documents d LEFT JOIN zones z ON z.id = d.zone_id "
)


def load_document(conn, doc_id: str):
    return db.fetch_one(conn, _DOCUMENT_SELECT + "WHERE d.id = ?", (doc_id,))


def load_documents(
    conn, where_sql: str = "", params: tuple = (), limit: int = 100, offset: int = 0
) -> list:
    sql = _DOCUMENT_SELECT
    if where_sql:
        sql += f"WHERE {where_sql} "
    sql += "ORDER BY d.created_at DESC, d.rowid DESC LIMIT ? OFFSET ?"
    return db.fetch_all(conn, sql, (*params, limit, offset))


def count_documents(conn, where_sql: str = "", params: tuple = ()) -> int:
    sql = "SELECT COUNT(*) FROM documents d "
    if where_sql:
        sql += f"WHERE {where_sql} "
    return int(db.fetch_value(conn, sql, params, default=0) or 0)


def upsert_doc_concepts(conn, doc_id: str, concepts: list[dict[str, object]], now: str) -> None:
    for concept in concepts:
        norm = str(concept["norm"])
        label = str(concept["label"])
        weight = float(concept["weight"])
        row = db.fetch_one(conn, "SELECT id FROM concepts WHERE norm = ?", (norm,))
        if row is None:
            concept_id = new_id("cv")
            conn.execute(
                "INSERT INTO concepts(id, label, norm, kind, created_at) VALUES(?, ?, ?, 'concept', ?)",
                (concept_id, label, norm, now),
            )
        else:
            concept_id = str(row["id"])
        conn.execute(
            "INSERT OR REPLACE INTO doc_concepts(doc_id, concept_id, weight) VALUES(?, ?, ?)",
            (doc_id, concept_id, weight),
        )


def create_document(
    parsed: ParsedDocument,
    *,
    tags_override: list[str] | None = None,
    rebuild: bool = True,
    record_activity: bool = True,
    zone_id: str | None = None,
) -> dict[str, object]:
    """完整入库：规范化 → 分块 → 向量 → 概念 → 图重建 → 返回 Document。"""
    content = normalize_text(parsed.content)
    if not content:
        raise IngestError("文档内容为空，无法导入")
    tags = normalize_tags([*parsed.tags, *(tags_override or [])])
    summary = collapse_whitespace(parsed.summary or "") or make_summary(content)
    chunks = chunking.chunk_text(content)
    if not chunks:
        raise IngestError("文档内容过短，无法生成有效分块")
    concepts = graph.extract_concepts(content)

    doc_id = new_id("d")
    now = now_iso()
    try:
        vectors = [
            embedding.pack_vector(vector)
            for vector in embedding.embed_texts([str(chunk["text"]) for chunk in chunks])
        ]
    except embedding.EmbeddingError as exc:
        raise IngestError(f"向量化失败：{exc}") from exc

    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO documents(id, title, filename, source_type, source_url, tags, "
            "summary, content, size, status, error, zone_id, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', NULL, ?, ?, ?)",
            (
                doc_id,
                truncate(parsed.title or "未命名文档", 120),
                parsed.filename,
                parsed.source_type,
                parsed.source_url,
                json.dumps(tags, ensure_ascii=False),
                summary,
                content,
                len(content),
                zone_id,
                now,
                now,
            ),
        )
        for chunk, vector in zip(chunks, vectors):
            conn.execute(
                "INSERT INTO chunks(id, doc_id, idx, text, char_start, char_end, "
                "token_count, embedding, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("c"),
                    doc_id,
                    int(chunk["idx"]),
                    str(chunk["text"]),
                    int(chunk["char_start"]),
                    int(chunk["char_end"]),
                    int(chunk["token_count"]),
                    vector,
                    now,
                ),
            )
        upsert_doc_concepts(conn, doc_id, concepts, now)
        if parsed.source_type == "pdf" and _auto_enrich_enabled():
            try:
                from . import enrich as enrich_service  # 延迟导入避免循环依赖

                enrich_service.enrich_document(conn, doc_id)
            except Exception:  # 整理失败不影响入库
                logger.exception("新上传 PDF 自动整理失败：%s", doc_id)
        if rebuild:
            graph.rebuild(conn)
        if record_activity:
            db.add_activity(conn, "upload", f"新增文档《{truncate(parsed.title, 40)}》")
        row = load_document(conn, doc_id)
        assert row is not None
        document = serialize_document(row)
    logger.info("文档已入库：%s（%d 个分块，%d 个概念）", doc_id, len(chunks), len(concepts))
    return document


def delete_document(doc_id: str) -> bool:
    """删除文档（级联 chunks / doc_concepts），并重建图。"""
    with db.get_db() as conn:
        row = db.fetch_one(conn, "SELECT title FROM documents WHERE id = ?", (doc_id,))
        if row is None:
            return False
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        graph.rebuild(conn)
        db.add_activity(conn, "delete", f"删除文档《{truncate(str(row['title']), 40)}》")
    logger.info("文档已删除：%s", doc_id)
    return True
