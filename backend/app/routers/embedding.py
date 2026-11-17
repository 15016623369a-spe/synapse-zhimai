"""向量嵌入 API（V2 功能）：状态 / 配置 / 测试 / 重建。

- ``GET  /api/admin/embedding/status``：当前 provider/model/维度/重建状态；
- ``POST /api/admin/embedding/config``：保存配置（不自动重建）；
- ``POST /api/admin/embedding/test``：用当前或传入配置 embed 两句话验证；
- ``POST /api/admin/embedding/rebuild``：同步重建全部 chunk 向量（只改 embedding）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import embedding

router = APIRouter(tags=["embedding"])


class EmbeddingConfigIn(BaseModel):
    """embedding 配置写入项；``None`` 表示「不修改」。"""

    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class EmbeddingTestIn(BaseModel):
    """测试入参；省略字段时使用已保存配置。"""

    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


def _http_error(exc: embedding.EmbeddingError) -> HTTPException:
    return HTTPException(
        status_code=int(getattr(exc, "status_code", 400) or 400), detail=str(exc)
    )


@router.get("/admin/embedding/status")
def embedding_status() -> dict[str, object]:
    return embedding.status()


@router.post("/admin/embedding/config")
def update_embedding_config(payload: EmbeddingConfigIn) -> dict[str, object]:
    if not (payload.provider or "").strip():
        raise HTTPException(status_code=400, detail="缺少向量提供方（provider）")
    try:
        return embedding.update_config(payload.model_dump())
    except embedding.EmbeddingError as exc:
        raise _http_error(exc) from exc


@router.post("/admin/embedding/test")
def test_embedding(payload: EmbeddingTestIn | None = None) -> dict[str, object]:
    overrides = payload.model_dump(exclude_none=True) if payload is not None else {}
    try:
        return embedding.test_provider(overrides)
    except embedding.EmbeddingError as exc:
        raise _http_error(exc) from exc


@router.post("/admin/embedding/rebuild")
def rebuild_embedding() -> dict[str, object]:
    try:
        result = embedding.rebuild_all()
    except embedding.EmbeddingError as exc:
        raise _http_error(exc) from exc
    return {"ok": True, **result}
