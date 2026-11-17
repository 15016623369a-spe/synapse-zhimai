"""SQLite 访问层（标准库 sqlite3，无 ORM）。

- 每个操作使用独立连接（WAL 模式，读多写少场景足够）；
- 写操作通过 ``get_db()`` 上下文管理器自动提交 / 回滚；
- ``init_db()`` 可重复执行（``CREATE TABLE IF NOT EXISTS``）。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import config
from .utils import now_iso

logger = logging.getLogger("synapse.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT, source_type TEXT NOT NULL,
  source_url TEXT, tags TEXT NOT NULL DEFAULT '[]',
  summary TEXT, content TEXT, size INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ready',
  error TEXT, zone_id TEXT, enrich_version INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks(
  id TEXT PRIMARY KEY, doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL, text TEXT NOT NULL, char_start INTEGER, char_end INTEGER,
  token_count INTEGER NOT NULL DEFAULT 0, embedding BLOB,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS concepts(
  id TEXT PRIMARY KEY, label TEXT NOT NULL, norm TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'concept',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doc_concepts(
  doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  weight REAL NOT NULL, PRIMARY KEY(doc_id, concept_id)
);
CREATE TABLE IF NOT EXISTS links(
  id TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL,
  type TEXT NOT NULL, weight REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL, content TEXT NOT NULL, citations TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activity(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS zones(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL, description TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans(
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT,
  color TEXT NOT NULL,
  frequency TEXT NOT NULL,
  weekly_target INTEGER NOT NULL,
  start_date TEXT NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0,
  sort_order INTEGER NOT NULL DEFAULT 0,
  group_id TEXT,
  plan_date TEXT,
  start_time TEXT,
  end_time TEXT,
  location TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_groups(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  color TEXT NOT NULL,
  description TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_feedback(
  id TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  title TEXT,
  query TEXT,
  decision TEXT NOT NULL,
  doc_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_checkins(
  id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  checkin_date TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(plan_id, checkin_date)
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_links_source ON links(source);
CREATE INDEX IF NOT EXISTS idx_links_target ON links(target);
CREATE INDEX IF NOT EXISTS idx_checkins_plan_date ON plan_checkins(plan_id, checkin_date);
CREATE INDEX IF NOT EXISTS idx_checkins_date ON plan_checkins(checkin_date);
CREATE INDEX IF NOT EXISTS idx_feedback_url ON knowledge_feedback(url);
CREATE INDEX IF NOT EXISTS idx_feedback_decision ON knowledge_feedback(decision);
"""

SCHEMA_VERSION = "6"


def connect(path: Path | None = None) -> sqlite3.Connection:
    """建立连接并启用外键；调用方负责关闭。"""
    db_file = Path(path) if path is not None else config.db_path()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _migrate_documents_zone_column(conn: sqlite3.Connection) -> None:
    """老库迁移：补 ``documents.zone_id`` 列（幂等）。"""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(documents)")}
    if "zone_id" not in columns:
        conn.execute("ALTER TABLE documents ADD COLUMN zone_id TEXT")
        logger.info("数据库迁移：documents 表已新增 zone_id 列")


#: 计划分组 / 单日行程扩展列（旧库 ALTER TABLE 幂等补齐）
_PLANS_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("group_id", "TEXT"),
    ("plan_date", "TEXT"),
    ("start_time", "TEXT"),
    ("end_time", "TEXT"),
    ("location", "TEXT"),
)


def _migrate_documents_enrich_column(conn: sqlite3.Connection) -> None:
    """老库迁移：补 ``documents.enrich_version`` 列（幂等）。"""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(documents)")}
    if "enrich_version" not in columns:
        conn.execute(
            "ALTER TABLE documents ADD COLUMN enrich_version INTEGER NOT NULL DEFAULT 0"
        )
        logger.info("数据库迁移：documents 表已新增 enrich_version 列")


def _migrate_plans_columns(conn: sqlite3.Connection) -> None:
    """老库迁移：补齐 plans 的分组 / 单日行程字段（幂等）。"""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(plans)")}
    added: list[str] = []
    for name, declaration in _PLANS_NEW_COLUMNS:
        if name not in columns:
            conn.execute(f"ALTER TABLE plans ADD COLUMN {name} {declaration}")
            added.append(name)
    if added:
        logger.info("数据库迁移：plans 表已新增列 %s", ", ".join(added))


def init_db(path: Path | None = None) -> None:
    """建表 + 老库迁移 + 建索引 + 写入 schema_version；可重复调用。"""
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate_documents_zone_column(conn)
        _migrate_documents_enrich_column(conn)
        _migrate_plans_columns(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_zone ON documents(zone_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_plans_group ON plans(group_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_plans_plan_date ON plans(plan_date)"
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SCHEMA_VERSION,),
        )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_db(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """写事务上下文：成功提交，异常回滚，最后关闭连接。"""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 便捷查询函数
# ---------------------------------------------------------------------------


def fetch_all(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()
) -> list[sqlite3.Row]:
    return list(conn.execute(sql, tuple(params)).fetchall())


def fetch_one(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()
) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(params)).fetchone()


def fetch_value(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = (), default: Any = None
) -> Any:
    row = fetch_one(conn, sql, params)
    if row is None:
        return default
    return row[0]


def execute(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()
) -> sqlite3.Cursor:
    return conn.execute(sql, tuple(params))


def executemany(
    conn: sqlite3.Connection, sql: str, rows: Sequence[Sequence[Any]]
) -> None:
    conn.executemany(sql, rows)


def table_count(conn: sqlite3.Connection, table: str) -> int:
    if table not in {
        "documents",
        "chunks",
        "concepts",
        "links",
        "conversations",
        "messages",
        "activity",
        "zones",
        "plans",
        "plan_checkins",
        "plan_groups",
        "knowledge_feedback",
    }:
        raise ValueError(f"未知表名: {table}")
    return int(fetch_value(conn, f"SELECT COUNT(*) FROM {table}", default=0) or 0)


# ---------------------------------------------------------------------------
# settings 读写
# ---------------------------------------------------------------------------


def settings_all(conn: sqlite3.Connection) -> dict[str, str]:
    rows = fetch_all(conn, "SELECT key, value FROM settings")
    return {row["key"]: row["value"] for row in rows}


def setting_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = fetch_one(conn, "SELECT value FROM settings WHERE key = ?", (key,))
    return None if row is None else str(row["value"])


def setting_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------------------
# activity
# ---------------------------------------------------------------------------


def add_activity(conn: sqlite3.Connection, kind: str, detail: str) -> None:
    """记录一条动态（保留最近 200 条）。"""
    conn.execute(
        "INSERT INTO activity(kind, detail, created_at) VALUES(?, ?, ?)",
        (kind, detail[:200], now_iso()),
    )
    conn.execute(
        "DELETE FROM activity WHERE id NOT IN "
        "(SELECT id FROM activity ORDER BY id DESC LIMIT 200)"
    )


def recent_activity(conn: sqlite3.Connection, limit: int = 8) -> list[dict[str, str]]:
    rows = fetch_all(
        conn,
        "SELECT kind, detail, created_at FROM activity ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return [
        {"kind": str(row["kind"]), "detail": str(row["detail"]), "created_at": str(row["created_at"])}
        for row in rows
    ]
