"""内置只读 SQLite MCP Server（FastMCP + stdio）。

启动方式::

    python -m app.mcp_servers.sqlite_server --db <sqlite 路径>

安全限制：
- 只读 URI 打开（``file:...?mode=ro``），从连接层拒绝任何写操作；
- 仅允许单条 ``SELECT`` / ``WITH`` / ``EXPLAIN``，禁止 INSERT/UPDATE/DELETE/PRAGMA/ATTACH 等；
- 结果强制 LIMIT（≤100 行），并用 progress handler 中止超长查询。
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import urllib.parse
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

logger = logging.getLogger("synapse.mcp.sqlite")

#: 列表最多返回的表数
MAX_TABLES = 30
#: 单次查询最多返回行数
MAX_QUERY_ROWS = 100
#: 默认返回行数
DEFAULT_QUERY_ROWS = 50
#: progress handler 上限（每 1000 条 VM 指令检查一次，约 500 万条指令后中止）
MAX_PROGRESS_CHECKS = 5000

#: 只读查询允许的首关键字
ALLOWED_PREFIXES = frozenset({"select", "with", "explain"})
#: 显式禁止的写 / 危险关键字（字符串字面量已剔除后再匹配）
FORBIDDEN_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "truncate",
    "attach",
    "detach",
    "pragma",
    "vacuum",
    "reindex",
    "analyze",
    "begin",
    "commit",
    "rollback",
    "savepoint",
    "release",
    "load_extension",
)
_FORBIDDEN_RE = re.compile(
    r"(?i)\b(" + "|".join(FORBIDDEN_KEYWORDS) + r")\b"
)
_LIMIT_RE = re.compile(r"(?i)\blimit\s+\d+")
_LEADING_KEYWORD_RE = re.compile(r"(?is)^(select|with|explain)\b")
_EXPLAIN_RE = re.compile(r"(?is)^explain\b")


class SQLiteToolError(ValueError):
    """SQLite 工具错误（消息为中文，可直接展示）。"""


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<blob {len(bytes(value))} bytes>"
    return str(value)


def _strip_sql_literals(sql: str) -> str:
    """剔除 SQL 注释与字符串 / 标识符字面量，避免误判关键字。

    仅用于安全检查，不用于实际执行。
    """
    result: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "-" and index + 1 < length and sql[index + 1] == "-":
            newline = sql.find("\n", index)
            index = length if newline < 0 else newline
            result.append(" ")
        elif char == "/" and index + 1 < length and sql[index + 1] == "*":
            end = sql.find("*/", index + 2)
            index = length if end < 0 else end + 2
            result.append(" ")
        elif char in {"'", '"', "`"}:
            quote = char
            index += 1
            while index < length:
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            result.append(" '' ")
        elif char == "[":
            end = sql.find("]", index)
            index = length if end < 0 else end + 1
            result.append(" '' ")
        else:
            result.append(char)
            index += 1
    return "".join(result)


def _validate_readonly_sql(sql: str) -> str:
    """校验 SQL 为单条只读查询，返回可执行的语句（去掉尾部分号）。"""
    text = str(sql or "").strip()
    if not text:
        raise SQLiteToolError("SQL 不能为空")
    code = _strip_sql_literals(text)
    match = _LEADING_KEYWORD_RE.match(code.strip())
    if match is None:
        raise SQLiteToolError("只允许执行只读查询：SQL 必须以 SELECT / WITH / EXPLAIN 开头")
    body = text.rstrip().rstrip(";").rstrip()
    if not body:
        raise SQLiteToolError("SQL 不能为空")
    if ";" in _strip_sql_literals(body):
        raise SQLiteToolError("一次只能执行一条 SQL 语句")
    forbidden = _FORBIDDEN_RE.search(_strip_sql_literals(body))
    if forbidden is not None:
        raise SQLiteToolError(
            f"只读限制：禁止 {forbidden.group(1).upper()} 等写操作，仅支持 SELECT / WITH / EXPLAIN"
        )
    return body


class SQLiteTools:
    """只读 SQLite 查询工具（可单独实例化用于测试）。"""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path).expanduser().resolve()
        if not path.exists():
            raise SQLiteToolError(f"数据库文件不存在：{path}")
        if not path.is_file():
            raise SQLiteToolError(f"数据库路径不是文件：{path}")
        self.path = path
        self.uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self.uri, uri=True, timeout=10.0)
        except sqlite3.Error as exc:
            raise SQLiteToolError(f"无法以只读方式打开数据库：{exc}") from exc
        checks = {"count": 0}

        def _progress() -> int:
            checks["count"] += 1
            return 1 if checks["count"] > MAX_PROGRESS_CHECKS else 0

        conn.set_progress_handler(_progress, 1000)
        return conn

    @staticmethod
    def _quote_identifier(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def _table_names(self, conn: sqlite3.Connection, limit: int) -> list[str]:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT ?",
            (limit,),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def list_tables(self) -> dict[str, object]:
        conn = self._connect()
        try:
            tables = self._table_names(conn, MAX_TABLES + 1)
            truncated = len(tables) > MAX_TABLES
            tables = tables[:MAX_TABLES]
            items: list[dict[str, object]] = []
            for name in tables:
                try:
                    row = conn.execute(
                        f"SELECT COUNT(*) FROM {self._quote_identifier(name)}"
                    ).fetchone()
                    rows = int(row[0]) if row is not None else 0
                except sqlite3.Error:
                    rows = -1
                items.append({"name": name, "rows": rows})
        except sqlite3.Error as exc:
            raise SQLiteToolError(f"读取表列表失败：{exc}") from exc
        finally:
            conn.close()
        return {
            "ok": True,
            "database": str(self.path),
            "tables": items,
            "count": len(items),
            "truncated": truncated,
        }

    def describe_table(self, table: str) -> dict[str, object]:
        name = str(table or "").strip()
        if not name:
            raise SQLiteToolError("表名不能为空")
        conn = self._connect()
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                (name,),
            ).fetchone()
            if exists is None:
                raise SQLiteToolError(
                    f"表不存在：{name}（可先调用 list_tables 查看可用表）"
                )
            rows = conn.execute(
                'SELECT name, type, "notnull", dflt_value, pk FROM pragma_table_info(?)',
                (name,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise SQLiteToolError(f"读取表结构失败：{exc}") from exc
        finally:
            conn.close()
        columns = [
            {
                "name": str(row[0]),
                "type": str(row[1] or ""),
                "nullable": not bool(row[2]),
                "default": _json_value(row[3]),
                "primary_key": bool(row[4]),
            }
            for row in rows
        ]
        return {
            "ok": True,
            "database": str(self.path),
            "table": name,
            "columns": columns,
            "count": len(columns),
        }

    def query(self, sql: str, limit: int = DEFAULT_QUERY_ROWS) -> dict[str, object]:
        body = _validate_readonly_sql(sql)
        try:
            cap = max(1, min(int(limit), MAX_QUERY_ROWS))
        except (TypeError, ValueError):
            cap = DEFAULT_QUERY_ROWS
        is_explain = _EXPLAIN_RE.match(body) is not None
        has_limit = _LIMIT_RE.search(_strip_sql_literals(body)) is not None
        if is_explain or has_limit:
            statement = body
        else:
            # 外层包一层 LIMIT，确保最多返回 cap 行（cap+1 用于判断截断）
            statement = f"SELECT * FROM (\n{body}\n) LIMIT {cap + 1}"
        conn = self._connect()
        try:
            try:
                cursor = conn.execute(statement)
                rows = cursor.fetchmany(cap + 1)
            except sqlite3.OperationalError as exc:
                message = str(exc)
                if "interrupt" in message.lower():
                    raise SQLiteToolError(
                        "查询执行时间过长，已中止（只读安全限制），请缩小查询范围"
                    ) from exc
                raise SQLiteToolError(f"SQL 执行失败：{message}") from exc
            columns = [str(desc[0]) for desc in cursor.description or []]
        finally:
            conn.close()
        truncated = len(rows) > cap
        rows = rows[:cap]
        return {
            "ok": True,
            "database": str(self.path),
            "columns": columns,
            "rows": [[_json_value(value) for value in row] for row in rows],
            "row_count": len(rows),
            "truncated": truncated,
            "limit": cap,
        }


def build_server(db_path: str | Path) -> FastMCP:
    tools = SQLiteTools(db_path)
    server = FastMCP("synapse-sqlite")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_tables() -> dict[str, object]:
        """列出数据库中的表名与行数（只读，最多 30 张表）。"""
        return tools.list_tables()

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def describe_table(table: str) -> dict[str, object]:
        """查看某张表的列名 / 类型 / 是否可空 / 主键（只读）。"""
        return tools.describe_table(table)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def query(sql: str, limit: int = DEFAULT_QUERY_ROWS) -> dict[str, object]:
        """执行只读 SQL 查询（仅 SELECT / WITH / EXPLAIN，最多返回 100 行）。

        limit 为返回行数上限（1-100）；不要传 INSERT/UPDATE/DELETE 等写操作。
        """
        return tools.query(sql, limit)

    return server


def _default_db() -> Path:
    env = os.environ.get("SYNAPSE_MCP_DB", "").strip()
    if env:
        return Path(env).expanduser()
    from .. import config

    return config.db_path()


def main() -> None:
    parser = argparse.ArgumentParser(description="Synapse 只读 SQLite MCP Server")
    parser.add_argument("--db", default="", help="SQLite 数据库路径（默认 data/synapse.db）")
    parsed = parser.parse_args()
    db_path = Path(parsed.db).expanduser() if parsed.db.strip() else _default_db()
    try:
        server = build_server(db_path)
    except SQLiteToolError as exc:
        raise SystemExit(f"数据库 MCP Server 启动失败：{exc}") from exc
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
