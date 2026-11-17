"""演示数据种子：启动自动载入 / 手动重置。"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import config, db
from ..utils import now_iso
from . import graph, ingest

logger = logging.getLogger("synapse.seed")

ACTIVITY_SEED = "载入演示知识库"
ACTIVITY_RESET = "重置演示知识库"
CONCEPT_VERSION_KEY = "concept_extraction_version"


def demo_files() -> list[Path]:
    """返回 ``data/demo_docs/*.md``（不存在时返回空列表）。"""
    directory = config.demo_docs_dir()
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.md") if path.is_file())


def import_demo_docs() -> dict[str, int]:
    """逐个导入演示文档；单篇失败跳过并记录日志。"""
    files = demo_files()
    imported = 0
    skipped = 0
    for path in files:
        try:
            parsed = ingest.parse_file(path.name, path.read_bytes())
            ingest.create_document(parsed, rebuild=False, record_activity=False)
            imported += 1
        except Exception as exc:  # 单篇失败不影响其它文档与服务启动
            skipped += 1
            logger.warning("演示文档导入失败，已跳过：%s（%s）", path.name, exc)
    if imported:
        with db.get_db() as conn:
            graph.rebuild(conn)
            db.setting_set(conn, CONCEPT_VERSION_KEY, graph.CONCEPT_EXTRACTION_VERSION)
            db.add_activity(conn, "seed", ACTIVITY_SEED)
    logger.info("演示文档导入完成：成功 %d 篇，跳过 %d 篇", imported, skipped)
    return {"imported": imported, "skipped": skipped}


def seed_if_empty() -> dict[str, int] | None:
    """文档表为空且演示目录有 md 时自动载入；否则返回 None。"""
    with db.get_db() as conn:
        document_count = db.table_count(conn, "documents")
    if document_count > 0:
        logger.info("知识库已有 %d 篇文档，跳过自动载入演示数据", document_count)
        return None
    if not demo_files():
        logger.info("未发现演示文档（data/demo_docs 为空），跳过自动载入")
        return None
    return import_demo_docs()


def reset_demo() -> dict[str, int]:
    """清空文档 / 概念 / 链接（保留会话），重新导入演示文档。"""
    with db.get_db() as conn:
        conn.execute("DELETE FROM documents")
        conn.execute("DELETE FROM concepts")
        conn.execute("DELETE FROM links")
        db.add_activity(conn, "reseed", ACTIVITY_RESET)
    return import_demo_docs()


def ensure_concept_index() -> bool:
    """概念抽取规则升级后，自动重建已入库文档的概念与图谱（幂等）。

    返回 True 表示执行了重建。仅比较 ``settings.concept_extraction_version``，
    版本一致时不做任何事，因此每次启动都安全。
    """
    current = graph.CONCEPT_EXTRACTION_VERSION
    with db.get_db() as conn:
        stored = db.setting_get(conn, CONCEPT_VERSION_KEY)
        documents = db.fetch_all(conn, "SELECT id, content FROM documents")
        if stored == current and documents:
            return False
        if not documents:
            db.setting_set(conn, CONCEPT_VERSION_KEY, current)
            return False
        for document in documents:
            doc_id = str(document["id"])
            content = str(document["content"] or "")
            concepts = graph.extract_concepts(content)
            conn.execute("DELETE FROM doc_concepts WHERE doc_id = ?", (doc_id,))
            ingest.upsert_doc_concepts(conn, doc_id, concepts, now_iso())
        graph.rebuild(conn)
        db.setting_set(conn, CONCEPT_VERSION_KEY, current)
        db.add_activity(conn, "reindex", "更新概念抽取索引")
    logger.info("概念抽取规则已升级，已重建 %d 篇文档的概念与图谱", len(documents))
    return True
