"""扫描版 PDF OCR 测试（SPEC-V2-OCR-EMBED 功能 A）。

覆盖：needs_ocr 判定、RapidOCR 渲染识别（mock 引擎）、页数上限/截断、
中文错误、文本型 PDF 不触发 OCR、OCR 不可用 / 失败降级、health.ocr。
"""

from __future__ import annotations

import io
import os

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from app import db
from app.services import ingest, ocr

# ---------------------------------------------------------------------------
# 扫描 PDF fixture：先把文字渲染成图片，再把图片插入新 PDF（无文本层）
# ---------------------------------------------------------------------------

SCAN_LINES = ["SCANNED PAPER TITLE", "MULTI OBJECT TRACKING WITH BELIEF PROPAGATION"]


def make_scanned_pdf(lines: list[str]) -> bytes:
    """生成「文本被渲染成图片」的扫描版 PDF（pypdf 提取不到文本）。"""
    import pymupdf

    source = pymupdf.open()
    for line in lines:
        page = source.new_page(width=520, height=180)
        page.insert_text((24, 80), line, fontsize=20)
    source_bytes = source.tobytes()
    source.close()

    rendered: list[tuple[int, int, bytes]] = []
    document = pymupdf.open(stream=source_bytes, filetype="pdf")
    try:
        for page in document:
            pixmap = page.get_pixmap(dpi=150)
            rendered.append((pixmap.width, pixmap.height, pixmap.tobytes("png")))
    finally:
        document.close()

    output = pymupdf.open()
    try:
        for width, height, png in rendered:
            page = output.new_page(width=width * 72 / 150, height=height * 72 / 150)
            page.insert_image(page.rect, stream=png)
        return output.tobytes()
    finally:
        output.close()


def make_text_pdf(text: str = "RAG retrieval augmented generation " * 8) -> bytes:
    """生成带文本层的普通 PDF（pypdf 能提取文本）。"""
    import pymupdf

    document = pymupdf.open()
    for _ in range(2):
        page = document.new_page(width=520, height=300)
        page.insert_textbox(
            pymupdf.Rect(24, 40, 500, 280), text, fontsize=12, fontname="helv"
        )
    try:
        return document.tobytes()
    finally:
        document.close()


def blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class FakeRapidOCR:
    """模拟 RapidOCR：每页返回「第 N 页识别文本」。"""

    def __init__(self, lines_per_page: list[str] | None = None) -> None:
        self.lines_per_page = lines_per_page or ["SCAN PAGE ONE TEXT", "SCAN PAGE TWO TEXT"]
        self.calls = 0

    def __call__(self, image):  # noqa: ANN001 - 模拟引擎签名
        assert isinstance(image, np.ndarray)
        assert image.ndim == 3 and image.shape[2] == 3
        text = self.lines_per_page[self.calls % len(self.lines_per_page)]
        self.calls += 1
        return [[[[0, 0], [10, 0], [10, 10], [0, 10]], text, 0.99]], None


@pytest.fixture()
def fake_engine(monkeypatch: pytest.MonkeyPatch) -> FakeRapidOCR:
    engine = FakeRapidOCR()
    monkeypatch.setattr(ocr, "_engine", None)
    monkeypatch.setattr(ocr, "_get_engine", lambda: engine)
    return engine


# ---------------------------------------------------------------------------
# needs_ocr / 引擎信息
# ---------------------------------------------------------------------------


def test_needs_ocr_detects_scanned_pdf() -> None:
    assert ocr.needs_ocr("", 1) is True
    assert ocr.needs_ocr("短文本", 2) is True
    # 总文本 < max(40, 页数*20)
    assert ocr.needs_ocr("a" * 30, 1) is True
    assert ocr.needs_ocr("a" * 100, 1) is False


def test_needs_ocr_page_count_thresholds() -> None:
    # 页数越多，总文本阈值越高：max(40, pages * 20)
    assert ocr.needs_ocr("字" * 199, 10) is True
    assert ocr.needs_ocr("字" * 200, 10) is False
    # page_count 缺省/异常时按 1 页处理
    assert ocr.needs_ocr("", 0) is True
    assert ocr.needs_ocr("字" * 40, 0) is False


def test_ocr_engine_info_env(monkeypatch: pytest.MonkeyPatch) -> None:
    info = ocr.ocr_engine_info()
    assert set(info) == {"available", "engine", "max_pages"}
    assert info["engine"] == "rapidocr"
    assert info["max_pages"] == 30

    monkeypatch.setenv("SYNAPSE_OCR_MAX_PAGES", "7")
    assert ocr.max_pages() == 7

    monkeypatch.setenv("SYNAPSE_OCR_ENABLED", "0")
    disabled = ocr.ocr_engine_info()
    assert disabled["available"] is False
    assert ocr.ocr_available() is False


# ---------------------------------------------------------------------------
# extract_scanned_pdf
# ---------------------------------------------------------------------------


def test_extract_scanned_pdf_with_mock_engine(
    fake_engine: FakeRapidOCR, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = make_scanned_pdf(["第一页", "第二页"])
    # 确认 fixture 真的没有文本层
    assert PdfReader(io.BytesIO(data)).pages[0].extract_text().strip() == ""

    monkeypatch.setenv("SYNAPSE_OCR_ENABLED", "1")
    result = ocr.extract_scanned_pdf(data)
    assert result["engine"] == "rapidocr"
    assert result["pages"] == 2
    assert result["ocr_pages"] == 2
    assert result["truncated"] is False
    assert "SCAN PAGE ONE TEXT" in str(result["text"])
    assert "SCAN PAGE TWO TEXT" in str(result["text"])
    assert fake_engine.calls == 2


def test_extract_scanned_pdf_respects_max_pages_truncated(
    fake_engine: FakeRapidOCR,
) -> None:
    data = make_scanned_pdf(["第一页", "第二页", "第三页"])
    result = ocr.extract_scanned_pdf(data, max_pages=1)
    assert result["pages"] == 3
    assert result["ocr_pages"] == 1
    assert result["truncated"] is True
    assert fake_engine.calls == 1


def test_extract_scanned_pdf_invalid_bytes_raises_chinese_error() -> None:
    with pytest.raises(ocr.OCRError) as excinfo:
        ocr.extract_scanned_pdf(b"not a real pdf")
    assert "PDF" in str(excinfo.value)


def test_extract_scanned_pdf_engine_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    data = make_scanned_pdf(["第一页"])
    monkeypatch.setattr(ocr, "_engine", None)
    monkeypatch.setattr(ocr, "_engine_class", lambda: None)
    with pytest.raises(ocr.OCRError) as excinfo:
        ocr.extract_scanned_pdf(data)
    assert "OCR" in str(excinfo.value)


# ---------------------------------------------------------------------------
# ingest 接入
# ---------------------------------------------------------------------------


def test_parse_pdf_uses_ocr_when_text_insufficient(fake_engine: FakeRapidOCR) -> None:
    data = make_scanned_pdf(["第一页", "第二页"])
    parsed = ingest.parse_file("scan-paper.pdf", data)
    assert parsed.source_type == "pdf"
    assert parsed.ocr_used is True
    assert parsed.ocr_pages == 2
    assert "SCAN PAGE ONE TEXT" in parsed.content
    assert "SCAN PAGE TWO TEXT" in parsed.content


def test_parse_text_pdf_does_not_trigger_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    data = make_text_pdf()
    calls: list[str] = []

    def _unexpected(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append("called")
        raise AssertionError("文本型 PDF 不应触发 OCR")

    monkeypatch.setattr(ocr, "extract_scanned_pdf", _unexpected)
    parsed = ingest.parse_file("text-paper.pdf", data)
    assert calls == []
    assert parsed.ocr_used is False
    assert parsed.ocr_pages == 0
    assert "RAG retrieval" in parsed.content


def test_parse_pdf_ocr_unavailable_degrades_to_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 文本层只有少量文字：needs_ocr=True，但原文非空 → 降级保留原文
    import pymupdf

    document = pymupdf.open()
    page = document.new_page(width=300, height=200)
    page.insert_text((24, 80), "Hello sparse text", fontsize=14)
    data = document.tobytes()
    document.close()

    monkeypatch.setenv("SYNAPSE_OCR_ENABLED", "0")
    monkeypatch.setattr(ocr, "_engine", None)
    parsed = ingest.parse_file("sparse.pdf", data)
    assert parsed.ocr_used is False
    assert "Hello sparse text" in parsed.content


def test_parse_pdf_ocr_unavailable_without_text_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNAPSE_OCR_ENABLED", "0")
    monkeypatch.setattr(ocr, "_engine", None)
    with pytest.raises(ingest.IngestError) as excinfo:
        ingest.parse_file("blank-scan.pdf", blank_pdf())
    message = str(excinfo.value)
    assert "未提取到文本" in message
    assert "OCR 不可用" in message


def test_parse_pdf_ocr_failure_keeps_original_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page(width=300, height=200)
    page.insert_text((24, 80), "Hello sparse text", fontsize=14)
    data = document.tobytes()
    document.close()

    def _broken(*args, **kwargs):  # noqa: ANN002, ANN003
        raise ocr.OCRError("第 1 页 OCR 识别失败")

    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_scanned_pdf", _broken)
    parsed = ingest.parse_file("broken-ocr.pdf", data)
    assert parsed.ocr_used is False
    assert "Hello sparse text" in parsed.content


def test_ocr_error_is_ingest_error_compatible() -> None:
    """OCRError 是 ValueError 子类，ingest 路由会转成中文错误。"""
    assert issubclass(ocr.OCRError, ValueError)


@pytest.mark.skipif(
    os.environ.get("SYNAPSE_RUN_REAL_OCR", "") != "1",
    reason="真实 RapidOCR 集成测试需设置 SYNAPSE_RUN_REAL_OCR=1（会加载模型）",
)
def test_real_rapidocr_scanned_pdf() -> None:  # pragma: no cover - 手动集成
    data = make_scanned_pdf(["SCANNED OCR INTEGRATION TEST"])
    parsed = ingest.parse_file("real-scan.pdf", data)
    assert parsed.ocr_used is True
    assert "SCAN" in parsed.content.upper()


# ---------------------------------------------------------------------------
# API 集成：扫描 PDF 上传入库 + health.ocr
# ---------------------------------------------------------------------------


def test_upload_scanned_pdf_stores_ocr_text(
    client: TestClient,
    fake_engine: FakeRapidOCR,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNAPSE_AUTO_ENRICH", "0")
    data = make_scanned_pdf(["第一页", "第二页"])
    response = client.post(
        "/api/documents/upload",
        files={"files": ("scanned-paper.pdf", data, "application/pdf")},
    )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["ok"] is True, item
    document = item["document"]
    chunks = client.get(f"/api/documents/{document['id']}/chunks").json()["items"]
    merged = "\n".join(str(chunk["text"]) for chunk in chunks)
    assert "SCAN PAGE ONE TEXT" in merged
    assert "SCAN PAGE TWO TEXT" in merged
    # 清理临时文档
    assert client.delete(f"/api/documents/{document['id']}").status_code == 200


def test_health_reports_ocr_status(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    assert set(payload["ocr"]) == {"available", "engine", "max_pages"}
    assert payload["ocr"]["engine"] == "rapidocr"
    assert payload["ocr"]["max_pages"] == 30
    assert payload["ocr"]["available"] is True
