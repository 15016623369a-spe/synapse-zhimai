"""分块与文本规范化单元测试（SPEC 5.2 / 10.1）。"""

from __future__ import annotations

from app.services import chunking
from app.utils import normalize_text


def _long_text(repeat: int = 12) -> str:
    base = (
        "检索增强生成需要先把文档切分成分块，再为每个分块建立向量索引，"
        "这样用户提问时才能快速找到最相关的上下文。混合检索结合了关键词匹配和语义相似度，"
        "通常用 RRF 融合两路排序结果，并给命中原句的片段额外加成。"
        "分块大小会直接影响上下文的质量和答案的准确性，太小则信息不完整，太大则会稀释相关性。"
    )
    return "".join(f"第{i}节：{base}" for i in range(1, repeat + 1))


def test_normalize_text_collapses_whitespace_and_zero_width() -> None:
    raw = "\ufeff第一行\r\n\r\n\r\n  第二行\u200b   \t 文本  "
    assert normalize_text(raw) == "第一行\n\n第二行 文本"


def test_normalize_text_is_idempotent() -> None:
    raw = "a\r\n\r\n\r\nb   c"
    once = normalize_text(raw)
    assert normalize_text(once) == once


def test_chunk_empty_text() -> None:
    assert chunking.chunk_text("") == []
    assert chunking.chunk_text("   \n\n  ") == []


def test_short_text_single_chunk() -> None:
    text = "这是一段很短的个人笔记，讲的是今天读一本书的收获，以及接下来要做的三件事。"
    chunks = chunking.chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0]["text"] == normalize_text(text)
    assert chunks[0]["idx"] == 0
    assert int(chunks[0]["char_start"]) == 0
    assert int(chunks[0]["char_end"]) == len(normalize_text(text))


def test_chunk_count_and_size_bounds() -> None:
    text = _long_text()
    chunks = chunking.chunk_text(text)
    assert len(chunks) >= 2
    sizes = [len(str(chunk["text"])) for chunk in chunks]
    assert max(sizes) <= chunking.MAX_CHARS
    # 除最后一块外，正文块应接近 450–650 字区间
    for size in sizes[:-1]:
        assert size >= 400
    assert sum(sizes) / len(sizes) >= 420


def test_chunk_positions_match_normalized_text() -> None:
    text = _long_text()
    normalized = normalize_text(text)
    chunks = chunking.chunk_text(text)
    for position, chunk in enumerate(chunks):
        start = int(chunk["char_start"])
        end = int(chunk["char_end"])
        assert chunk["idx"] == position
        assert 0 <= start < end <= len(normalized)
        assert normalized[start:end] == chunk["text"]
        assert int(chunk["token_count"]) > 0


def test_consecutive_chunks_overlap() -> None:
    chunks = chunking.chunk_text(_long_text())
    assert len(chunks) >= 2
    for previous, current in zip(chunks, chunks[1:]):
        assert int(current["char_start"]) < int(previous["char_end"])
        overlap = int(previous["char_end"]) - int(current["char_start"])
        assert 20 <= overlap <= chunking.OVERLAP_CHARS


def test_long_paragraph_is_split_by_sentences() -> None:
    text = "这是一句用于测试分块逻辑的中文句子，内容足够长以便触发句子切分。" * 60
    chunks = chunking.chunk_text(text)
    assert len(chunks) >= 3
    assert all(len(str(chunk["text"])) <= chunking.MAX_CHARS for chunk in chunks)
