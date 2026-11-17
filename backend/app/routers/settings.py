"""设置：模型配置读写 / 连接测试（绝不返回 API Key 明文）。"""

from __future__ import annotations

from fastapi import APIRouter

from .. import db
from ..schemas import SettingsIn, SettingsTestIn
from ..services import embedding, llm

router = APIRouter()


def _settings_payload() -> dict[str, object]:
    config = llm.get_config()
    with db.get_db() as conn:
        documents = db.table_count(conn, "documents")
        chunks = db.table_count(conn, "chunks")
    return {
        "llm": {
            "base_url": str(config["base_url"]),
            "model": str(config["model"]),
            "configured": llm.is_configured(config),
            "api_key_set": bool(str(config["api_key"] or "")),
            "temperature": float(config["temperature"]),
        },
        "embedding": embedding.provider_info(),
        "data": {"documents": documents, "chunks": chunks},
    }


@router.get("/settings")
def get_settings() -> dict[str, object]:
    return _settings_payload()


@router.put("/settings")
def update_settings(payload: SettingsIn) -> dict[str, object]:
    if payload.llm is not None:
        llm.update_config(
            {
                "base_url": payload.llm.base_url,
                "model": payload.llm.model,
                "api_key": payload.llm.api_key,
                "temperature": payload.llm.temperature,
            }
        )
    return _settings_payload()


@router.post("/settings/test")
async def test_settings(payload: SettingsTestIn | None = None) -> dict[str, object]:
    config = llm.get_config()
    if payload is not None and payload.llm is not None:
        overrides = payload.llm.model_dump(exclude_none=True)
        config.update(overrides)
    return await llm.test_connection(config)
