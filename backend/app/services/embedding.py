"""向量表示：本地哈希（默认）/ 本地语义 fastembed / OpenAI 兼容 embeddings。

- ``hash``（默认，兼容旧数据）：jieba 分词 + 中文 bigram，blake2b 哈希到 384 维，
  带符号累加，权重 ``1 + log(tf)``，L2 归一化；无模型、离线可用；
- ``fastembed``：本地语义模型（默认 ``sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2``，
  384 维多语言），模型缓存 ``PROJECT_ROOT/.cache/fastembed``，模块级单例 + 锁；
- ``openai``：任意 OpenAI 兼容 ``/embeddings`` 接口（base_url/api_key/model 可配）。

约定：
- 配置存 settings 表：``embedding_provider/model/base_url/api_key/dim/version``（另存重建标记）；
- provider 切换后必须重建全部 chunk 向量，``routers/embedding.py`` 提供 status/config/test/rebuild；
- ``pack_vector`` / ``unpack_vector`` 维度动态（由 BLOB 长度决定），``cosine`` 自动按长度；
- 查询时 fastembed/openai 失败回退本地哈希并记录 warning；重建失败不写库（先算后写）。
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from functools import lru_cache
from typing import Any

import httpx
import jieba
import numpy as np

from .. import config, db
from ..utils import is_stopword, now_iso

jieba.setLogLevel(logging.WARNING)

#: 避免 HuggingFace 下载卡住（模型缓存缺失时用普通 HTTP 下载）
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

logger = logging.getLogger("synapse.embedding")

#: 本地哈希向量维度（固定，兼容旧数据）
HASH_DIM: int = config.EMBEDDING_DIM
#: 兼容旧代码的别名（哈希维度）
DIM: int = HASH_DIM

PROVIDERS: tuple[str, ...] = ("hash", "fastembed", "openai")
DEFAULT_FASTEMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
#: 常用 fastembed 模型维度（其余模型从 fastembed 元数据读取）
FASTEMBED_MODEL_DIMS: dict[str, int] = {
    DEFAULT_FASTEMBED_MODEL: 384,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "BAAI/bge-small-zh-v1.5": 512,
    "jinaai/jina-embeddings-v2-base-zh": 768,
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2": 768,
    "intfloat/multilingual-e5-large": 1024,
}
REBUILD_BATCH_SIZE = 32
OPENAI_TIMEOUT_SECONDS = 60.0
FASTEMBED_CACHE_DIR = config.PROJECT_ROOT / ".cache" / "fastembed"

#: 测试注入点：httpx 同步传输层（None 表示真实网络，OpenAI 兼容接口）
HTTP_SYNC_TRANSPORT: httpx.BaseTransport | None = None

#: settings 表 key
_KEY_PROVIDER = "embedding_provider"
_KEY_MODEL = "embedding_model"
_KEY_BASE_URL = "embedding_base_url"
_KEY_API_KEY = "embedding_api_key"
_KEY_DIM = "embedding_dim"
_KEY_VERSION = "embedding_version"
_KEY_ACTIVE_PROVIDER = "embedding_active_provider"
_KEY_ACTIVE_MODEL = "embedding_active_model"
_KEY_LAST_REBUILD = "embedding_last_rebuild"

_FASTEMBED_LOCK = threading.RLock()
_FASTEMBED_CACHE: dict[str, Any] = {}

_EN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.\-]*")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


class EmbeddingError(ValueError):
    """向量配置 / 计算失败；message 为中文可读信息。"""

    status_code = 400


class EmbeddingRemoteError(EmbeddingError):
    """远程 embedding 接口失败（网络 / 鉴权 / 响应异常），HTTP 502。"""

    status_code = 502


# ---------------------------------------------------------------------------
# 中文分词工具（检索 / 图谱 / 关键词仍在用，保持行为不变）
# ---------------------------------------------------------------------------


def _valid_search_token(token: str) -> bool:
    if not token:
        return False
    if _CJK_RUN_RE.fullmatch(token):
        return len(token) >= 2 and not is_stopword(token)
    if token.isdigit():
        return len(token) >= 2
    if _EN_RE.fullmatch(token):
        return len(token) >= 2 and not is_stopword(token.lower())
    return False


def tokenize_terms(text: str) -> list[str]:
    """检索用词：jieba 词 + 英文词，过滤停用词、标点与单字中文。"""
    if not text:
        return []
    terms: list[str] = []
    for raw in jieba.lcut(text):
        token = raw.strip()
        if not token:
            continue
        if _EN_RE.fullmatch(token):
            token = token.lower()
        if _valid_search_token(token):
            terms.append(token)
    return terms


@lru_cache(maxsize=16384)
def _cached_terms(text: str) -> tuple[str, ...]:
    return tuple(tokenize_terms(text))


def chunk_terms(text: str) -> list[str]:
    """带缓存的 chunk 分词（同一文本只分词一次）。"""
    return list(_cached_terms(text or ""))


def cjk_bigrams(text: str) -> list[str]:
    grams: list[str] = []
    for run in _CJK_RUN_RE.findall(text or ""):
        for index in range(len(run) - 1):
            grams.append(run[index : index + 2])
    return grams


def embedding_tokens(text: str) -> list[str]:
    """向量化 token：检索词 + 中文字符 bigram。"""
    return tokenize_terms(text) + cjk_bigrams(text)


# ---------------------------------------------------------------------------
# 本地哈希向量（默认 provider，兼容旧数据）
# ---------------------------------------------------------------------------


def _hash_index_sign(token: str) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    index = int.from_bytes(digest[:4], "big") % HASH_DIM
    sign = 1.0 if digest[4] & 1 else -1.0
    return index, sign


def _normalize(vector: np.ndarray) -> np.ndarray:
    array = np.asarray(vector, dtype=np.float32).ravel()
    norm = float(np.linalg.norm(array))
    if norm > 0:
        array = array / norm
    return array


def hash_embed(text: str) -> np.ndarray:
    """本地 384 维哈希向量（旧行为，默认 provider）。"""
    vector = np.zeros(HASH_DIM, dtype=np.float32)
    counts = Counter(embedding_tokens(text or ""))
    for token, count in counts.items():
        index, sign = _hash_index_sign(token)
        vector[index] += sign * (1.0 + math.log(count))
    return _normalize(vector)


def pack_vector(vector: np.ndarray) -> bytes:
    """float32 向量 → BLOB（维度动态）。"""
    array = np.asarray(vector, dtype=np.float32).ravel()
    return array.tobytes()


def unpack_vector(blob: bytes | None) -> np.ndarray:
    """BLOB → float32 向量；空值/损坏返回零长度向量（动态维度，不假设 384）。"""
    if not blob:
        return np.zeros(0, dtype=np.float32)
    try:
        array = np.frombuffer(blob, dtype=np.float32)
    except (TypeError, ValueError):  # pragma: no cover - 损坏的 BLOB
        return np.zeros(0, dtype=np.float32)
    return np.array(array, dtype=np.float32, copy=False)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """两个（可未归一化的）向量余弦相似度；维度不同返回 0。"""
    if a.size != b.size or a.size == 0:
        return 0.0
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def cosine_matrix(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """查询向量与矩阵（N×DIM，行已 L2 归一化）的余弦相似度。"""
    if matrix.size == 0 or query.size == 0:
        return np.zeros(matrix.shape[0] if matrix.ndim == 2 else 0, dtype=np.float32)
    query_norm = float(np.linalg.norm(query))
    if query_norm == 0.0:
        return np.zeros(matrix.shape[0], dtype=np.float32)
    if matrix.shape[1] != query.size:
        return np.zeros(matrix.shape[0], dtype=np.float32)
    row_norms = np.linalg.norm(matrix, axis=1)
    row_norms[row_norms == 0] = 1.0
    return (matrix @ query) / (row_norms * query_norm)


# ---------------------------------------------------------------------------
# 配置（DB settings > 环境变量 > 默认）
# ---------------------------------------------------------------------------


def _safe_settings() -> dict[str, str]:
    """读取 settings；数据库尚未初始化 / 不可用时回退空配置（默认哈希向量）。"""
    try:
        with db.get_db() as conn:
            return db.settings_all(conn)
    except Exception:  # pragma: no cover - 数据库异常时保持旧行为
        logger.debug("读取 embedding 配置失败，使用默认哈希向量", exc_info=True)
        return {}


def _int_setting(raw: object, default: int = 0) -> int:
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _build_config(stored: dict[str, str]) -> dict[str, str]:
    """把 settings / 环境变量合成当前生效配置（不访问数据库）。"""
    env = config.embeddings_env()
    provider = str(stored.get(_KEY_PROVIDER) or _env("SYNAPSE_EMBEDDINGS_PROVIDER") or "hash")
    provider = provider.strip().lower()
    if provider not in PROVIDERS:
        provider = "hash"
    env_model = str(env.get("model") or "").strip()
    if provider == "hash":
        model = ""
    elif provider == "fastembed":
        model = str(stored.get(_KEY_MODEL) or "").strip() or env_model or DEFAULT_FASTEMBED_MODEL
    else:
        model = str(stored.get(_KEY_MODEL) if _KEY_MODEL in stored else env_model).strip()
    if _KEY_BASE_URL in stored:
        base_url = str(stored.get(_KEY_BASE_URL) or "").strip().rstrip("/")
    else:
        base_url = str(env.get("base_url") or "").strip().rstrip("/")
    if _KEY_API_KEY in stored:
        api_key = str(stored.get(_KEY_API_KEY) or "").strip()
    else:
        api_key = str(env.get("api_key") or "").strip()
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
    }


def get_config() -> dict[str, str]:
    """当前生效的 embedding 配置。"""
    return _build_config(_safe_settings())


@lru_cache(maxsize=1)
def _fastembed_model_dims() -> dict[str, int]:
    """fastembed 支持的模型 → 维度（静态表 + fastembed 元数据）。"""
    dims = dict(FASTEMBED_MODEL_DIMS)
    try:
        from fastembed import TextEmbedding  # type: ignore[import-not-found]

        for item in TextEmbedding.list_supported_models():
            name = str(item.get("model") or "")
            dim = _int_setting(item.get("dim"), 0)
            if name and dim > 0:
                dims[name] = dim
    except Exception:  # pragma: no cover - fastembed 未安装时用静态表
        logger.debug("读取 fastembed 模型列表失败，使用内置模型表", exc_info=True)
    return dims


def _fastembed_available() -> bool:
    try:
        import importlib.util

        return (
            importlib.util.find_spec("fastembed") is not None
            and importlib.util.find_spec("onnxruntime") is not None
        )
    except Exception:  # pragma: no cover
        return False


def _expected_dim(cfg: dict[str, str], stored: dict[str, str]) -> int | None:
    """当前配置「应当」使用的向量维度；openai 未重建前未知（None）。"""
    provider = cfg["provider"]
    if provider == "hash":
        return HASH_DIM
    if provider == "fastembed":
        return _fastembed_model_dims().get(cfg["model"])
    if str(stored.get(_KEY_ACTIVE_PROVIDER) or "") == "openai":
        value = _int_setting(stored.get(_KEY_DIM), 0)
        return value if value > 0 else None
    return None


def _current_dim(cfg: dict[str, str], stored: dict[str, str]) -> int:
    """对外展示的维度：优先当前配置维度，其次数据库已重建维度。"""
    expected = _expected_dim(cfg, stored)
    if expected is not None:
        return expected
    if str(stored.get(_KEY_ACTIVE_PROVIDER) or "") == cfg["provider"]:
        return _int_setting(stored.get(_KEY_DIM), 0)
    return 0


def _provider_ready(cfg: dict[str, str]) -> bool:
    provider = cfg["provider"]
    if provider == "hash":
        return True
    if provider == "fastembed":
        return _fastembed_available()
    return bool(cfg["base_url"] and cfg["model"])


def _chunk_dimensions() -> set[int]:
    """数据库里现有 chunk 向量的维度集合（NULL 不计）。"""
    try:
        with db.get_db() as conn:
            rows = db.fetch_all(
                conn,
                "SELECT DISTINCT length(embedding) AS n FROM chunks "
                "WHERE embedding IS NOT NULL AND length(embedding) > 0",
            )
    except Exception:  # pragma: no cover - 数据库异常
        return set()
    return {int(row[0]) // 4 for row in rows if row[0] and int(row[0]) % 4 == 0}


def _needs_rebuild(
    cfg: dict[str, str], stored: dict[str, str], total_chunks: int, dims: set[int]
) -> bool:
    if total_chunks <= 0:
        return False
    expected = _expected_dim(cfg, stored)
    active_provider = stored.get(_KEY_ACTIVE_PROVIDER)
    if active_provider is None:
        # 旧库（V1）没有重建记录：默认认为当前向量是本地哈希 384 维
        if cfg["provider"] != "hash":
            return True
        return bool(dims) and dims != {HASH_DIM}
    if str(active_provider) != cfg["provider"]:
        return True
    if str(stored.get(_KEY_ACTIVE_MODEL) or "") != cfg["model"]:
        return True
    if expected is not None and dims and dims != {expected}:
        return True
    return False


def provider_info() -> dict[str, object]:
    """健康检查 / 设置页展示用：当前 provider 与维度（不含 key）。"""
    stored = _safe_settings()
    cfg = _build_config(stored)
    return {"provider": cfg["provider"], "dim": int(_current_dim(cfg, stored))}


def status() -> dict[str, object]:
    """``GET /api/admin/embedding/status`` 的完整状态。"""
    stored = _safe_settings()
    cfg = _build_config(stored)
    try:
        with db.get_db() as conn:
            total_chunks = int(db.fetch_value(conn, "SELECT COUNT(*) FROM chunks", default=0) or 0)
    except Exception:  # pragma: no cover
        total_chunks = 0
    dims = _chunk_dimensions()
    return {
        "provider": cfg["provider"],
        "model": cfg["model"],
        "dim": int(_current_dim(cfg, stored)),
        "total_chunks": total_chunks,
        "ready": _provider_ready(cfg),
        "needs_rebuild": _needs_rebuild(cfg, stored, total_chunks, dims),
        "has_api_key": bool(cfg["api_key"]),
        "providers": list(PROVIDERS),
        "last_rebuild": stored.get(_KEY_LAST_REBUILD) or None,
    }


def update_config(updates: dict[str, Any]) -> dict[str, object]:
    """保存 provider 配置（不自动重建）；返回最新 status。"""
    provider = str(updates.get("provider") or "").strip().lower()
    if provider not in PROVIDERS:
        raise EmbeddingError(
            f"不支持的向量提供方：{provider or '（空）'}（可选：{'、'.join(PROVIDERS)}）"
        )
    model_input = updates.get("model")
    model = str(model_input).strip() if model_input is not None else None
    base_url_input = updates.get("base_url")
    base_url = str(base_url_input).strip().rstrip("/") if base_url_input is not None else None
    api_key_input = updates.get("api_key")
    api_key = str(api_key_input).strip() if api_key_input is not None else None

    resolved_model = model or ""
    if provider == "fastembed":
        dims = _fastembed_model_dims()
        if resolved_model:
            if resolved_model not in dims:
                raise EmbeddingError(
                    f"不支持的 fastembed 模型：{resolved_model}"
                    f"（可选：{DEFAULT_FASTEMBED_MODEL}、BAAI/bge-small-zh-v1.5 等）"
                )
        else:
            stored = _safe_settings()
            previous = str(stored.get(_KEY_MODEL) or "").strip()
            resolved_model = previous if previous in dims else DEFAULT_FASTEMBED_MODEL
        if not _fastembed_available():
            raise EmbeddingError("本地语义模型不可用（fastembed 未安装）")

    with db.get_db() as conn:
        db.setting_set(conn, _KEY_PROVIDER, provider)
        if provider == "fastembed":
            db.setting_set(conn, _KEY_MODEL, resolved_model)
        elif model is not None:
            db.setting_set(conn, _KEY_MODEL, model)
        if base_url is not None:
            db.setting_set(conn, _KEY_BASE_URL, base_url)
        if api_key is not None:
            db.setting_set(conn, _KEY_API_KEY, api_key)
    logger.info(
        "Embedding 配置已更新：provider=%s model=%s",
        provider,
        resolved_model if provider == "fastembed" else model,
    )
    return status()


# ---------------------------------------------------------------------------
# fastembed（本地语义模型）
# ---------------------------------------------------------------------------


def _fastembed_encoder(model_name: str) -> Any:
    """获取 fastembed 模型单例（首次加载模型，之后复用）。"""
    with _FASTEMBED_LOCK:
        encoder = _FASTEMBED_CACHE.get(model_name)
        if encoder is not None:
            return encoder
        if not _fastembed_available():
            raise EmbeddingError("本地语义模型不可用（fastembed 未安装）")
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        try:
            from fastembed import TextEmbedding  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - 依赖损坏
            raise EmbeddingError("本地语义模型不可用（fastembed 导入失败）") from exc
        try:
            FASTEMBED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            encoder = TextEmbedding(
                model_name=model_name, cache_dir=str(FASTEMBED_CACHE_DIR)
            )
        except Exception as exc:
            raise EmbeddingError(f"本地语义模型加载失败：{str(exc)[:160]}") from exc
        _FASTEMBED_CACHE[model_name] = encoder
        logger.info("fastembed 模型已加载：%s", model_name)
        return encoder


def _fastembed_embed(model_name: str, texts: list[str], batch_size: int) -> list[np.ndarray]:
    encoder = _fastembed_encoder(model_name)
    with _FASTEMBED_LOCK:
        try:
            vectors = list(encoder.embed(texts, batch_size=max(1, int(batch_size))))
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError("本地语义模型计算失败") from exc
    return [_normalize(vector) for vector in vectors]


# ---------------------------------------------------------------------------
# OpenAI 兼容 /embeddings
# ---------------------------------------------------------------------------


def _remote_status_message(status_code: int) -> str:
    if status_code == 401:
        return "Embedding API 鉴权失败，请检查 API Key"
    if status_code == 403:
        return "Embedding API 拒绝访问，请检查 API Key 权限"
    if status_code == 404:
        return "未找到 Embedding 接口（404）：请检查 Base URL 是否正确"
    if status_code == 429:
        return "Embedding API 请求过于频繁，请稍后重试"
    if status_code >= 500:
        return "Embedding API 服务暂时不可用"
    return f"Embedding API 返回 HTTP {status_code}"


def _openai_embed(cfg: dict[str, str], texts: list[str], batch_size: int) -> list[np.ndarray]:
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    model = str(cfg.get("model") or "")
    if not base_url:
        raise EmbeddingError("尚未配置 Embedding API 地址（base_url）")
    if not model:
        raise EmbeddingError("尚未配置 Embedding 模型名称")
    url = f"{base_url}/embeddings"
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    step = max(1, int(batch_size))
    vectors: list[np.ndarray] = []
    timeout = httpx.Timeout(OPENAI_TIMEOUT_SECONDS, connect=15.0)
    try:
        with httpx.Client(
            timeout=timeout, transport=HTTP_SYNC_TRANSPORT, headers=headers
        ) as client:
            for start in range(0, len(texts), step):
                batch = texts[start : start + step]
                try:
                    response = client.post(
                        url, json={"model": model, "input": batch}
                    )
                except httpx.TimeoutException as exc:
                    raise EmbeddingRemoteError("Embedding API 请求超时（60 秒）") from exc
                except httpx.HTTPError as exc:
                    raise EmbeddingRemoteError(
                        "无法连接 Embedding API，请检查 Base URL 与服务是否启动"
                    ) from exc
                if response.status_code >= 400:
                    raise EmbeddingRemoteError(
                        _remote_status_message(response.status_code)
                    )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise EmbeddingRemoteError("Embedding API 返回内容无法解析") from exc
                items = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(items, list) or len(items) != len(batch):
                    raise EmbeddingRemoteError("Embedding API 返回的数据条数与请求不一致")
                ordered = sorted(
                    items,
                    key=lambda item: _int_setting(item.get("index"), 0)
                    if isinstance(item, dict)
                    else 0,
                )
                for item in ordered:
                    embedding = item.get("embedding") if isinstance(item, dict) else None
                    if not isinstance(embedding, list) or not embedding:
                        raise EmbeddingRemoteError("Embedding API 返回的向量为空或格式错误")
                    try:
                        vectors.append(np.asarray(embedding, dtype=np.float32).ravel())
                    except (TypeError, ValueError) as exc:
                        raise EmbeddingRemoteError(
                            "Embedding API 返回的向量格式错误"
                        ) from exc
    except EmbeddingError:
        raise
    except httpx.HTTPError as exc:  # pragma: no cover - 兜底
        raise EmbeddingRemoteError("Embedding API 请求失败") from exc
    if not vectors:
        raise EmbeddingRemoteError("Embedding API 未返回向量")
    dims = {int(vector.size) for vector in vectors}
    if len(dims) != 1:
        raise EmbeddingRemoteError("Embedding API 返回的向量维度不一致")
    return [_normalize(vector) for vector in vectors]


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


def _embed_provider(
    cfg: dict[str, str], texts: list[str], batch_size: int = REBUILD_BATCH_SIZE
) -> list[np.ndarray]:
    provider = cfg["provider"]
    if provider == "hash":
        return [hash_embed(text) for text in texts]
    if provider == "fastembed":
        return _fastembed_embed(cfg["model"], texts, batch_size)
    if provider == "openai":
        return _openai_embed(cfg, texts, batch_size)
    raise EmbeddingError(f"不支持的向量提供方：{provider}")


def embed_text(text: str) -> np.ndarray:
    """统一单条向量入口（检索用）：按当前配置计算；失败回退本地哈希并 warning。"""
    cfg = get_config()
    if cfg["provider"] == "hash":
        return hash_embed(text or "")
    try:
        vectors = _embed_provider(cfg, [str(text or "")], 1)
        return vectors[0]
    except Exception as exc:
        logger.warning("向量模型不可用，已回退本地哈希向量：%s", exc)
        return hash_embed(text or "")


def embed_texts(
    texts: list[str],
    batch_size: int = REBUILD_BATCH_SIZE,
    *,
    allow_fallback: bool = False,
) -> list[np.ndarray]:
    """批量向量入口（ingest / 重建用）；默认失败直接抛错（不偷偷写混合维度）。"""
    items = [str(text or "") for text in texts]
    if not items:
        return []
    cfg = get_config()
    if cfg["provider"] == "hash":
        return [hash_embed(text) for text in items]
    try:
        return _embed_provider(cfg, items, batch_size)
    except Exception:
        if not allow_fallback:
            raise
        logger.warning("向量模型不可用，批量回退本地哈希向量", exc_info=True)
        return [hash_embed(text) for text in items]


def test_provider(overrides: dict[str, Any] | None = None) -> dict[str, object]:
    """测试当前 / 传入配置：embed 两句话并返回相似度（不写库）。"""
    stored = _safe_settings()
    cfg = _build_config(stored)
    if overrides:
        for key in ("provider", "model", "base_url", "api_key"):
            if overrides.get(key) is None:
                continue
            cfg[key] = str(overrides[key]).strip()
        cfg["base_url"] = cfg["base_url"].rstrip("/")
        if cfg["provider"] not in PROVIDERS:
            raise EmbeddingError(
                f"不支持的向量提供方：{cfg['provider'] or '（空）'}"
                f"（可选：{'、'.join(PROVIDERS)}）"
            )
        if cfg["provider"] == "hash":
            cfg["model"] = ""
        elif cfg["provider"] == "fastembed":
            cfg["model"] = cfg["model"] or DEFAULT_FASTEMBED_MODEL
            dims = _fastembed_model_dims()
            if cfg["model"] not in dims:
                raise EmbeddingError(
                    f"不支持的 fastembed 模型：{cfg['model']}"
                    f"（可选：{DEFAULT_FASTEMBED_MODEL}、BAAI/bge-small-zh-v1.5 等）"
                )
            if not _fastembed_available():
                raise EmbeddingError("本地语义模型不可用（fastembed 未安装）")
        elif cfg["provider"] == "openai":
            if not cfg["base_url"]:
                raise EmbeddingError("尚未配置 Embedding API 地址（base_url）")
            if not cfg["model"]:
                raise EmbeddingError("尚未配置 Embedding 模型名称")
    sentences = ["语义检索依靠向量相似度召回相关文档。", "知识图谱用节点和边表达实体之间的关系。"]
    vectors = _embed_provider(cfg, sentences, len(sentences))
    dims = {int(vector.size) for vector in vectors}
    if len(dims) != 1:
        raise EmbeddingError("生成的向量维度不一致")
    return {
        "ok": True,
        "provider": cfg["provider"],
        "dim": int(vectors[0].size),
        "similarity": round(float(cosine(vectors[0], vectors[1])), 6),
    }


def rebuild_all(batch_size: int = REBUILD_BATCH_SIZE) -> dict[str, object]:
    """同步重建所有 chunk 向量：先全部计算，再单事务写库（失败不破坏旧向量）。"""
    stored = _safe_settings()
    cfg = _build_config(stored)
    provider = cfg["provider"]
    if provider == "fastembed":
        if not _fastembed_available():
            raise EmbeddingError("本地语义模型不可用（fastembed 未安装）")
        if cfg["model"] not in _fastembed_model_dims():
            raise EmbeddingError(
                f"不支持的 fastembed 模型：{cfg['model']}"
                f"（可选：{DEFAULT_FASTEMBED_MODEL}、BAAI/bge-small-zh-v1.5 等）"
            )
    elif provider == "openai":
        if not cfg["base_url"]:
            raise EmbeddingError("尚未配置 Embedding API 地址（base_url）")
        if not cfg["model"]:
            raise EmbeddingError("尚未配置 Embedding 模型名称")

    started = time.perf_counter()
    try:
        with db.get_db() as conn:
            rows = db.fetch_all(
                conn, "SELECT id, text FROM chunks ORDER BY doc_id, idx"
            )
    except Exception as exc:  # pragma: no cover
        raise EmbeddingError("读取分块数据失败，无法重建向量") from exc
    if not rows:
        return {
            "chunks": 0,
            "seconds": 0.0,
            "provider": provider,
            "dim": int(_current_dim(cfg, stored)),
        }
    ids = [str(row["id"]) for row in rows]
    texts = [str(row["text"] or "") for row in rows]
    vectors = _embed_provider(cfg, texts, max(1, int(batch_size)))
    if len(vectors) != len(ids):
        raise EmbeddingError("向量数量与分块数量不一致，重建已取消")
    dims = {int(vector.size) for vector in vectors}
    if len(dims) != 1:
        raise EmbeddingError("生成的向量维度不一致，重建已取消")
    dim = dims.pop()
    now = now_iso()
    try:
        with db.get_db() as conn:
            conn.executemany(
                "UPDATE chunks SET embedding = ? WHERE id = ?",
                [(pack_vector(vector), chunk_id) for vector, chunk_id in zip(vectors, ids)],
            )
            version = _int_setting(db.setting_get(conn, _KEY_VERSION), 0)
            db.setting_set(conn, _KEY_VERSION, str(version + 1))
            db.setting_set(conn, _KEY_DIM, str(dim))
            db.setting_set(conn, _KEY_ACTIVE_PROVIDER, provider)
            db.setting_set(conn, _KEY_ACTIVE_MODEL, cfg["model"])
            db.setting_set(conn, _KEY_LAST_REBUILD, now)
    except Exception as exc:
        raise EmbeddingError("写入向量失败，旧向量已保留") from exc
    seconds = round(time.perf_counter() - started, 2)
    logger.info(
        "向量重建完成：provider=%s model=%s dim=%d chunks=%d 耗时 %.2fs",
        provider,
        cfg["model"],
        dim,
        len(ids),
        seconds,
    )
    return {
        "chunks": len(ids),
        "seconds": seconds,
        "provider": provider,
        "dim": dim,
    }
