"""文本分块：目标 450–650 字，块间约 80 字重叠。

流程：规范化文本 → 按段落切分 → 长段落按句子切分并打包成 ≤500 字的单元 →
贪心打包成 450–650 字的 chunk（必要时自适应缩小重叠，避免块过短）。
``char_start / char_end`` 相对规范化后的 ``documents.content``。
"""

from __future__ import annotations

import re

from ..utils import estimate_tokens, normalize_text

MIN_CHARS = 450
MAX_CHARS = 650
OVERLAP_CHARS = 80
MIN_UNIT_CHARS = 80
MAX_UNIT_CHARS = 500

_SENT_END_RE = re.compile(r"[。！？!?；;]+")


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    """按空行切段落，返回原文中的 [start, end) 区间。"""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    end = 0
    offset = 0
    for line in text.split("\n"):
        line_start = offset
        line_end = offset + len(line)
        offset = line_end + 1
        if line.strip():
            if start is None:
                start = line_start
            end = line_end
        elif start is not None:
            spans.append((start, end))
            start = None
    if start is not None:
        spans.append((start, end))
    return spans


def _sentence_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """在 [start, end) 内按句末标点切句。"""
    spans: list[tuple[int, int]] = []
    pos = start
    for match in _SENT_END_RE.finditer(text, start, end):
        seg_end = match.end()
        if seg_end > pos:
            spans.append((pos, seg_end))
            pos = seg_end
    if pos < end:
        spans.append((pos, end))
    return spans


def _hard_split(text: str, start: int, end: int, limit: int) -> list[tuple[int, int]]:
    """超长句按 ``limit`` 硬切，尽量落在空格 / 标点后。"""
    spans: list[tuple[int, int]] = []
    pos = start
    while pos < end:
        cut = min(pos + limit, end)
        if cut < end:
            window_start = max(pos + limit // 2, cut - 60)
            best = -1
            for mark in ("。", "！", "？", "；", "，", "、", " "):
                found = text.rfind(mark, window_start, cut)
                if found > best:
                    best = found + 1
            if best > pos:
                cut = best
        spans.append((pos, cut))
        pos = cut
    return spans


def _build_units(text: str) -> list[tuple[int, int]]:
    """把文档切成 ≤MAX_UNIT_CHARS 的句子 / 段落单元。"""
    units: list[tuple[int, int]] = []
    for para_start, para_end in _paragraph_spans(text):
        if para_end - para_start <= MAX_UNIT_CHARS:
            units.append((para_start, para_end))
            continue
        sentence_spans = _sentence_spans(text, para_start, para_end)
        expanded: list[tuple[int, int]] = []
        for seg_start, seg_end in sentence_spans:
            if seg_end - seg_start > MAX_UNIT_CHARS:
                expanded.extend(_hard_split(text, seg_start, seg_end, MAX_UNIT_CHARS))
            else:
                expanded.append((seg_start, seg_end))
        # 合并相邻句子直到接近 MAX_UNIT_CHARS，避免产生过多超短单元
        current_start, current_end = expanded[0]
        for seg_start, seg_end in expanded[1:]:
            if seg_end - current_start <= MAX_UNIT_CHARS:
                current_end = seg_end
            else:
                units.append((current_start, current_end))
                current_start, current_end = seg_start, seg_end
        units.append((current_start, current_end))

    # 过短段落（<MIN_UNIT_CHARS）与下一个单元合并
    merged: list[tuple[int, int]] = []
    for span_start, span_end in units:
        if merged:
            prev_start, prev_end = merged[-1]
            if prev_end - prev_start < MIN_UNIT_CHARS and span_end - prev_start <= MAX_CHARS:
                merged[-1] = (prev_start, span_end)
                continue
        merged.append((span_start, span_end))
    return merged


def _fit_units(
    units: list[tuple[int, int]], index: int, start: int
) -> tuple[int, int]:
    """从 ``index`` 开始向后塞单元，返回 (最后单元下标, 结束位置)。"""
    last = index
    end = units[index][1]
    while last + 1 < len(units) and units[last + 1][1] - start <= MAX_CHARS:
        last += 1
        end = units[last][1]
    return last, end


def _trim_start(text: str, start: int, limit: int) -> int:
    while start < limit and text[start].isspace():
        start += 1
    return start


def chunk_text(text: str) -> list[dict[str, object]]:
    """分块入口；返回 ``[{idx, text, char_start, char_end, token_count}]``。"""
    normalized = normalize_text(text)
    if not normalized:
        return []
    units = _build_units(normalized)
    if not units:
        return []

    chunks: list[dict[str, object]] = []
    index = 0
    prev_start = 0
    prev_end = 0

    while index < len(units):
        if chunks:
            candidate_overlaps = (OVERLAP_CHARS, OVERLAP_CHARS // 2, OVERLAP_CHARS // 4)
            best: tuple[int, int, int, int] | None = None
            for overlap in candidate_overlaps:
                start = _trim_start(
                    normalized, max(prev_start, prev_end - overlap), units[index][0]
                )
                last, end = _fit_units(units, index, start)
                length = end - start
                if best is None or length > best[2]:
                    best = (last, end, length, start)
                if length >= MIN_CHARS:
                    break
            assert best is not None
            last, end, _length, start = best
        else:
            start = units[index][0]
            last, end = _fit_units(units, index, start)

        segment = normalized[start:end]
        if segment.strip():
            chunks.append(
                {
                    "idx": len(chunks),
                    "text": segment,
                    "char_start": start,
                    "char_end": end,
                    "token_count": estimate_tokens(segment),
                }
            )
        index = last + 1
        prev_start, prev_end = start, end

    # 末尾过短的 chunk 合并进前一块（不超过最大长度）
    if len(chunks) > 1:
        last_chunk = chunks[-1]
        previous = chunks[-2]
        last_len = int(last_chunk["char_end"]) - int(last_chunk["char_start"])
        if last_len < 100:
            combined_end = int(last_chunk["char_end"])
            combined_start = int(previous["char_start"])
            if combined_end - combined_start <= MAX_CHARS:
                previous["text"] = normalized[combined_start:combined_end]
                previous["char_end"] = combined_end
                previous["token_count"] = estimate_tokens(str(previous["text"]))
                chunks.pop()
    return chunks
