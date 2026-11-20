"""集中管理路径、限制与可选环境变量。

约定（见 SPEC 第 4 节）：配置优先级为 DB settings（UI 保存）> 环境变量 > 空；
本模块只负责「环境变量 + 路径」，DB 覆盖逻辑在 ``services.llm`` 中实现。
所有路径都在调用时读取环境变量，方便测试隔离。
"""

from __future__ import annotations

import os
from pathlib import Path

VERSION = "2.6.0"
APP_NAME = "Synapse · 知脉"

#: backend/ 目录
BASE_DIR = Path(__file__).resolve().parent.parent
#: 项目根目录（synapse/）
PROJECT_ROOT = BASE_DIR.parent

#: 上传限制
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
#: URL 抓取限制
MAX_URL_BYTES = 5 * 1024 * 1024
URL_TIMEOUT_SECONDS = 15.0
URL_USER_AGENT = "SynapseBot/1.0"

#: 默认采样温度
DEFAULT_TEMPERATURE = 0.2

#: 本地向量维度
EMBEDDING_DIM = 384

#: 支持的文件扩展名
SUPPORTED_EXTENSIONS = frozenset({"md", "markdown", "txt", "pdf", "docx", "html", "htm"})


def data_dir() -> Path:
    """数据目录：``SYNAPSE_DATA_DIR`` 或项目根 ``data/``。"""
    raw = os.environ.get("SYNAPSE_DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return PROJECT_ROOT / "data"


def db_path() -> Path:
    """SQLite 数据库文件路径。"""
    return data_dir() / "synapse.db"


def demo_docs_dir() -> Path:
    """演示文档目录。"""
    return data_dir() / "demo_docs"


def frontend_dist_dir() -> Path:
    """前端构建产物目录（可能尚不存在）。"""
    return PROJECT_ROOT / "frontend" / "dist"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def llm_env() -> dict[str, object]:
    """读取环境变量中的 LLM 配置（DB 覆盖在 services.llm 中完成）。"""
    temperature_raw = _env("SYNAPSE_LLM_TEMPERATURE", "")
    try:
        temperature = float(temperature_raw) if temperature_raw else DEFAULT_TEMPERATURE
    except ValueError:
        temperature = DEFAULT_TEMPERATURE
    return {
        "base_url": _env("SYNAPSE_LLM_BASE_URL"),
        "api_key": _env("SYNAPSE_LLM_API_KEY"),
        "model": _env("SYNAPSE_LLM_MODEL"),
        "temperature": temperature,
    }


def embeddings_env() -> dict[str, str]:
    """预留的远程 embeddings 配置（本版本不启用，仅暴露 provider 信息）。"""
    return {
        "base_url": _env("SYNAPSE_EMBEDDINGS_BASE_URL"),
        "api_key": _env("SYNAPSE_EMBEDDINGS_API_KEY"),
        "model": _env("SYNAPSE_EMBEDDINGS_MODEL"),
    }


def cors_origins() -> list[str]:
    """开发期允许的跨域来源（Vite 代理为同源，此处为直接访问 5174 的情况兜底）。"""
    return [
        "http://127.0.0.1:5174",
        "http://localhost:5174",
        "http://127.0.0.1:7800",
        "http://localhost:7800",
    ]
