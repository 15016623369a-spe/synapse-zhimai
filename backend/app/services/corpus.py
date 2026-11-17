"""语料统计缓存：为相关性打分 / 知识缺口检测提供 IDF。

- 以全库 chunk 的词频计算 df，按 (db 路径, chunk 数, 最新 chunk 时间) 缓存；
- chunk 内容变化或库切换时自动失效；DB 不可用时退化为空统计。
"""

from __future__ import annotations

import math
from collections import Counter

from .. import config, db
from . import embedding

IDF_CAP = 3.0
_CACHE: dict[str, object] = {}


def corpus_stats() -> tuple[Counter[str], int]:
    """返回 ``(term -> document frequency, chunk 总数)``。"""
    try:
        with db.get_db() as conn:
            row = db.fetch_one(
                conn, "SELECT COUNT(*) AS total, MAX(created_at) AS latest FROM chunks"
            )
            total = int(row["total"] or 0) if row is not None else 0
            signature = (
                str(config.db_path()),
                total,
                str(row["latest"] or "") if row is not None else "",
            )
            if _CACHE.get("signature") == signature and _CACHE.get("df") is not None:
                return _CACHE["df"], int(_CACHE.get("n") or 0)  # type: ignore[return-value]
            rows = db.fetch_all(conn, "SELECT text FROM chunks")
    except Exception:  # 数据库不可用（如纯单元测试）时退化为空统计
        return Counter(), 0
    df: Counter[str] = Counter()
    for chunk_row in rows:
        df.update(set(embedding.chunk_terms(str(chunk_row["text"]))))
    _CACHE.update({"signature": signature, "df": df, "n": len(rows)})
    return df, len(rows)


def idf(term: str, df: Counter[str], corpus_size: int, cap: float = IDF_CAP) -> float:
    if corpus_size <= 0:
        return 1.0
    value = math.log(
        1.0 + (corpus_size - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5)
    )
    return min(cap, value)
