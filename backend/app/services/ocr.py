"""扫描版 PDF OCR（PyMuPDF 渲染 + RapidOCR）。

- ``needs_ocr`` 判断 PDF 是否属于「扫描件 / 图片型」（文本过少）；
- ``extract_scanned_pdf``：每页 200dpi 渲染成位图后用 RapidOCR 识别；
  RapidOCR 模型加载约 1–2 秒，因此模块级单例 + 线程锁，只初始化一次；
- 超过 ``SYNAPSE_OCR_MAX_PAGES``（默认 30）只识别前 N 页并标记 ``truncated``；
- 引擎缺失 / 渲染失败抛中文 ``OCRError``，由 ``ingest`` 决定是否降级。

环境变量：
- ``SYNAPSE_OCR_ENABLED``：默认 1；设为 0/false/no 可整体关闭 OCR（测试用）；
- ``SYNAPSE_OCR_MAX_PAGES``：单文件最多识别页数，默认 30。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

import numpy as np

logger = logging.getLogger("synapse.ocr")

ENGINE_NAME = "rapidocr"
DEFAULT_MAX_PAGES = 30
RENDER_DPI = 200
_RENDER_ZOOM = RENDER_DPI / 72.0


class OCRError(ValueError):
    """OCR 失败；message 为中文可读信息。"""


#: 模块级单例 + 可重入锁（初始化与推理都串行，避免 ONNX 会话并发问题）
_engine_lock = threading.RLock()
_engine: Any | None = None


def ocr_enabled() -> bool:
    """OCR 总开关（``SYNAPSE_OCR_ENABLED``，默认开启）。"""
    raw = os.environ.get("SYNAPSE_OCR_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def max_pages() -> int:
    """单文件最多 OCR 页数（``SYNAPSE_OCR_MAX_PAGES``，默认 30，范围 1–500）。"""
    raw = os.environ.get("SYNAPSE_OCR_MAX_PAGES", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_PAGES
    except ValueError:
        value = DEFAULT_MAX_PAGES
    return max(1, min(value, 500))


def _engine_class() -> Any | None:
    """尝试导入 RapidOCR 类；依赖缺失返回 None（不抛异常）。"""
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - 依赖缺失环境
        logger.debug("RapidOCR 不可用（未安装 rapidocr-onnxruntime）")
        return None
    return RapidOCR


def _pymupdf_module() -> Any | None:
    try:
        import pymupdf  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - 依赖缺失环境
        logger.debug("PyMuPDF 不可用（未安装 pymupdf）")
        return None
    return pymupdf


def ocr_available() -> bool:
    """OCR 是否可用（开关打开 + PyMuPDF / RapidOCR 依赖齐全）。"""
    if not ocr_enabled():
        return False
    return _pymupdf_module() is not None and _engine_class() is not None


def ocr_engine_info() -> dict[str, object]:
    """``/api/health`` 使用的 OCR 状态。"""
    return {
        "available": ocr_available(),
        "engine": ENGINE_NAME,
        "max_pages": max_pages(),
    }


def _get_engine() -> Any:
    """获取 RapidOCR 单例（首次调用加载模型，约 1–2 秒）。"""
    global _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        factory = _engine_class()
        if factory is None:
            raise OCRError("OCR 引擎不可用（未安装 rapidocr-onnxruntime）")
        try:
            _engine = factory()
        except Exception as exc:  # pragma: no cover - 模型文件损坏等
            raise OCRError("OCR 引擎初始化失败，请检查依赖是否安装完整") from exc
        logger.info("RapidOCR 引擎已就绪")
        return _engine


def needs_ocr(pdf_text: str, page_count: int) -> bool:
    """判断 PDF 是否需要 OCR：总文本过少或平均每页字符数过低。"""
    text = pdf_text or ""
    pages = max(1, int(page_count or 0))
    compact = "".join(text.split())
    total = len(compact)
    if total < max(40, pages * 20):
        return True
    return total / pages < 20


def _render_pages(data: bytes, page_limit: int) -> tuple[list[np.ndarray], int, bool]:
    """把 PDF 前 ``page_limit`` 页渲染成 RGB→BGR 的 numpy 图（RapidOCR 输入约定）。"""
    pymupdf = _pymupdf_module()
    if pymupdf is None:
        raise OCRError("OCR 依赖缺失（PyMuPDF 不可用）")
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise OCRError("PDF 打开失败，文件可能已损坏") from exc
    try:
        total_pages = int(document.page_count)
        if total_pages <= 0:
            raise OCRError("PDF 没有可识别的页面")
        limit = min(total_pages, max(1, int(page_limit)))
        matrix = pymupdf.Matrix(_RENDER_ZOOM, _RENDER_ZOOM)
        images: list[np.ndarray] = []
        for index in range(limit):
            try:
                page = document.load_page(index)
                pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            except Exception as exc:
                raise OCRError(f"第 {index + 1} 页渲染失败，无法进行 OCR") from exc
            width, height, channels = int(pixmap.width), int(pixmap.height), int(pixmap.n)
            samples = np.frombuffer(pixmap.samples, dtype=np.uint8)
            if channels == 3:
                rgb = samples.reshape(height, width, 3)
            elif channels == 4:
                rgb = samples.reshape(height, width, 4)[:, :, :3]
            elif channels == 1:
                rgb = np.repeat(samples.reshape(height, width, 1), 3, axis=2)
            else:  # pragma: no cover - 非预期通道数
                raise OCRError(f"第 {index + 1} 页图像格式不支持（{channels} 通道）")
            # RapidOCR 沿用 OpenCV 的 BGR 约定，这里把 RGB 翻转成 BGR
            images.append(np.ascontiguousarray(rgb[:, :, ::-1]))
        return images, total_pages, limit < total_pages
    finally:
        try:
            document.close()
        except Exception:  # pragma: no cover - 关闭失败无影响
            pass


def _page_lines(result: Any) -> list[str]:
    """从 RapidOCR 结果 ``[[box, text, score], ...]`` 中提取非空文本行。"""
    lines: list[str] = []
    for item in result or []:
        try:
            text = str(item[1]).strip()
        except (IndexError, TypeError):
            continue
        if text:
            lines.append(text)
    return lines


def extract_scanned_pdf(
    data: bytes, max_pages: int = DEFAULT_MAX_PAGES
) -> dict[str, object]:
    """OCR 扫描版 PDF，返回 ``{text, pages, ocr_pages, truncated, engine}``。

    - ``pages``：PDF 总页数；``ocr_pages``：实际识别页数；
    - 页数超过 ``max_pages`` 时只识别前 N 页并返回 ``truncated=True``；
    - 失败抛 :class:`OCRError`（中文信息）。
    """
    if not data:
        raise OCRError("PDF 内容为空，无法 OCR")
    images, total_pages, truncated = _render_pages(data, max_pages)
    if not images:
        raise OCRError("PDF 没有可识别的页面")
    with _engine_lock:
        engine = _get_engine()
        page_texts: list[str] = []
        for page_index, image in enumerate(images):
            try:
                result, _elapse = engine(image)
            except OCRError:
                raise
            except Exception as exc:
                raise OCRError(f"第 {page_index + 1} 页 OCR 识别失败") from exc
            lines = _page_lines(result)
            page_texts.append("\n".join(lines))
    text = "\n\n".join(page for page in page_texts if page.strip()).strip()
    logger.info(
        "扫描 PDF OCR 完成：%d/%d 页，提取 %d 字符%s",
        len(images),
        total_pages,
        len(text),
        "（已截断）" if truncated else "",
    )
    return {
        "text": text,
        "pages": total_pages,
        "ocr_pages": len(images),
        "truncated": truncated,
        "engine": ENGINE_NAME,
    }
