"""内置 MCP Servers 单元测试：filesystem / sqlite（直接调用工具实现，不启子进程）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.mcp_servers.filesystem_server import (
    FileSystemError,
    FileSystemTools,
    build_server as build_filesystem_server,
)
from app.mcp_servers.sqlite_server import (
    SQLiteToolError,
    SQLiteTools,
    build_server as build_sqlite_server,
)

# ---------------------------------------------------------------------------
# 文件系统
# ---------------------------------------------------------------------------


@pytest.fixture()
def fs_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    (root / "notes.md").write_text("# 笔记\n这是 Markdown 内容。", encoding="utf-8")
    (root / "plain.txt").write_text("纯文本内容", encoding="utf-8")
    docs = root / "docs"
    docs.mkdir()
    (docs / "readme.md").write_text("# README\n子目录文件。", encoding="utf-8")
    hidden = root / ".hidden.md"
    hidden.write_text("隐藏文件", encoding="utf-8")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    (root / "server.key").write_text("PRIVATE", encoding="utf-8")
    return root


@pytest.fixture()
def fs(fs_root: Path) -> FileSystemTools:
    return FileSystemTools(fs_root)


def test_filesystem_list_directory(fs: FileSystemTools) -> None:
    payload = fs.list_directory(".")
    assert payload["ok"] is True
    assert payload["path"] == "."
    names = {item["name"]: item for item in payload["items"]}
    assert "notes.md" in names
    assert names["notes.md"]["type"] == "file"
    assert names["notes.md"]["size"] > 0
    assert names["docs"]["type"] == "directory"
    assert names["docs"]["size"] == 0
    # 隐藏文件保留但标记；敏感文件（.env / *.key）不出现
    assert names[".hidden.md"]["hidden"] is True
    assert ".env" not in names
    assert "server.key" not in names
    assert payload["count"] == len(payload["items"])
    assert payload["truncated"] is False

    nested = fs.list_directory("docs")
    assert [item["name"] for item in nested["items"]] == ["readme.md"]

    with pytest.raises(FileSystemError, match="不是目录"):
        fs.list_directory("notes.md")


def test_filesystem_read_text_file(fs: FileSystemTools) -> None:
    payload = fs.read_text_file("notes.md")
    assert payload["ok"] is True
    assert "这是 Markdown 内容" in payload["content"]
    assert payload["truncated"] is False

    long_file = Path(fs.root) / "long.txt"
    long_file.write_text("字" * 500, encoding="utf-8")
    truncated = fs.read_text_file("long.txt", max_chars=100)
    assert truncated["truncated"] is True
    assert len(truncated["content"]) <= 100


def test_filesystem_rejects_oversize_and_binary(fs: FileSystemTools) -> None:
    big = Path(fs.root) / "big.txt"
    big.write_bytes(b"a" * (200 * 1024 + 1))
    with pytest.raises(FileSystemError, match="文件过大"):
        fs.read_text_file("big.txt")

    binary = Path(fs.root) / "image.bin"
    binary.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00binary-data")
    with pytest.raises(FileSystemError, match="二进制"):
        fs.read_text_file("image.bin")

    with pytest.raises(FileSystemError, match="不是文件"):
        fs.read_text_file("docs")


def test_filesystem_blocks_path_escape(fs: FileSystemTools, tmp_path: Path) -> None:
    for bad in ("..", "../etc/passwd", "/etc/passwd", "docs/../../notes.md"):
        with pytest.raises(FileSystemError, match="拒绝访问"):
            fs.resolve(bad)

    outside = tmp_path / "outside.txt"
    outside.write_text("外部文件", encoding="utf-8")
    link = Path(fs.root) / "escape_link"
    try:
        link.symlink_to(outside)
    except OSError:  # 某些环境无法创建符号链接
        pytest.skip("当前环境不支持符号链接")
    with pytest.raises(FileSystemError, match="拒绝访问"):
        fs.read_text_file("escape_link")


def test_filesystem_sensitive_files_blocked(fs: FileSystemTools) -> None:
    for bad in (".env", "server.key", "./.env"):
        with pytest.raises(FileSystemError, match="敏感"):
            fs.resolve(bad)
    search = fs.search_files("*")
    paths = {item["path"] for item in search["items"]}
    assert ".env" not in paths
    assert "server.key" not in paths


def test_filesystem_search_and_info(fs: FileSystemTools) -> None:
    result = fs.search_files("*.md")
    assert result["ok"] is True
    paths = {item["path"] for item in result["items"]}
    assert paths == {"notes.md", ".hidden.md", "docs/readme.md"}
    assert result["truncated"] is False

    limited = fs.search_files("*.md", max_results=1)
    assert len(limited["items"]) == 1
    assert limited["truncated"] is True

    with pytest.raises(FileSystemError):
        fs.search_files("../*")
    with pytest.raises(FileSystemError):
        fs.search_files("/etc/*")

    info = fs.get_file_info("notes.md")
    assert info["path"] == "notes.md"
    assert info["type"] == "file"
    assert info["size"] > 0
    assert info["mtime"].endswith("Z")
    dir_info = fs.get_file_info("docs")
    assert dir_info["type"] == "directory"


def test_filesystem_build_server_registers_readonly_tools(fs_root: Path) -> None:
    server = build_filesystem_server(fs_root)
    assert server.name == "synapse-filesystem"


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_db(tmp_path: Path) -> Path:
    db_file = tmp_path / "data.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE docs(id INTEGER PRIMARY KEY, title TEXT, size INTEGER)")
    conn.executemany(
        "INSERT INTO docs(title, size) VALUES(?, ?)",
        [(f"文档 {index}", index * 10) for index in range(1, 151)],
    )
    conn.execute("CREATE TABLE tags(id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO tags(name) VALUES('mcp')")
    conn.commit()
    conn.close()
    return db_file


@pytest.fixture()
def sqlite_tools(sqlite_db: Path) -> SQLiteTools:
    return SQLiteTools(sqlite_db)


def test_sqlite_list_tables_and_describe(sqlite_tools: SQLiteTools) -> None:
    payload = sqlite_tools.list_tables()
    assert payload["ok"] is True
    tables = {item["name"]: item["rows"] for item in payload["tables"]}
    assert tables == {"docs": 150, "tags": 1}

    described = sqlite_tools.describe_table("docs")
    assert described["table"] == "docs"
    columns = [column["name"] for column in described["columns"]]
    assert columns == ["id", "title", "size"]
    assert described["columns"][0]["primary_key"] is True

    with pytest.raises(SQLiteToolError, match="表不存在"):
        sqlite_tools.describe_table("missing_table")


def test_sqlite_select_and_cte_query(sqlite_tools: SQLiteTools) -> None:
    payload = sqlite_tools.query("SELECT id, title FROM docs ORDER BY id")
    assert payload["ok"] is True
    assert payload["columns"] == ["id", "title"]
    assert payload["row_count"] == 50
    assert payload["truncated"] is True
    assert payload["rows"][0] == [1, "文档 1"]

    cte = sqlite_tools.query(
        "WITH big AS (SELECT * FROM docs WHERE size > 100) SELECT COUNT(*) AS n FROM big"
    )
    assert cte["ok"] is True
    assert cte["rows"][0][0] == 140


def test_sqlite_limit_is_enforced(sqlite_tools: SQLiteTools) -> None:
    explicit = sqlite_tools.query("SELECT * FROM docs LIMIT 500", limit=100)
    assert explicit["row_count"] == 100
    assert explicit["truncated"] is True
    assert explicit["limit"] == 100

    small = sqlite_tools.query("SELECT * FROM docs", limit=5)
    assert small["row_count"] == 5
    assert small["truncated"] is True

    one = sqlite_tools.query("SELECT * FROM docs LIMIT 3")
    assert one["row_count"] == 3
    assert one["truncated"] is False


def test_sqlite_rejects_write_and_dangerous_sql(sqlite_tools: SQLiteTools, sqlite_db: Path) -> None:
    forbidden = [
        "INSERT INTO docs(title) VALUES('x')",
        "UPDATE docs SET title='x'",
        "DELETE FROM docs",
        "DROP TABLE docs",
        "ALTER TABLE docs ADD COLUMN x TEXT",
        "PRAGMA table_info(docs)",
        "ATTACH DATABASE '/tmp/x.db' AS x",
        "VACUUM",
        "SELECT 1; DROP TABLE docs",
        "  -- comment\nDELETE FROM docs",
    ]
    for sql in forbidden:
        with pytest.raises(SQLiteToolError, match="只读|一条 SQL"):
            sqlite_tools.query(sql)

    # 写语句被拒绝后数据库内容保持不变
    conn = sqlite3.connect(sqlite_db)
    assert conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 150
    conn.close()


def test_sqlite_explain_and_readonly_connection(sqlite_tools: SQLiteTools) -> None:
    explained = sqlite_tools.query("EXPLAIN SELECT * FROM docs")
    assert explained["ok"] is True
    assert explained["row_count"] >= 1
    assert explained["truncated"] is False

    # 只读 URI：即使绕过 SQL 校验直接执行写语句也会被 sqlite 拒绝
    conn = sqlite_tools._connect()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO docs(title, size) VALUES('x', 1)")
    conn.close()
    assert sqlite_tools.query("SELECT * FROM docs LIMIT 1")["ok"] is True


def test_sqlite_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SQLiteToolError, match="数据库文件不存在"):
        SQLiteTools(tmp_path / "missing.db")


def test_sqlite_build_server(sqlite_db: Path) -> None:
    server = build_sqlite_server(sqlite_db)
    assert server.name == "synapse-sqlite"


# ---------------------------------------------------------------------------
# 递归搜索（Lead 反馈：子目录 / 符号链接目录中的 Markdown 必须能被找到）
# ---------------------------------------------------------------------------


def test_filesystem_search_is_recursive_for_nested_dirs(tmp_path: Path) -> None:
    root = tmp_path / "root"
    demo = root / "demo_docs"
    deep = demo / "nested" / "deep"
    deep.mkdir(parents=True)
    (root / "synapse.db").write_bytes(b"SQLite format 3\x00")
    (demo / "a.md").write_text("# A", encoding="utf-8")
    (demo / "b.md").write_text("# B", encoding="utf-8")
    (deep / "c.md").write_text("# C", encoding="utf-8")
    tools = FileSystemTools(root)

    result = tools.search_files("*.md")
    assert result["ok"] is True
    assert result["recursive"] is True
    assert result["count"] == 3
    assert {item["path"] for item in result["items"]} == {
        "demo_docs/a.md",
        "demo_docs/b.md",
        "demo_docs/nested/deep/c.md",
    }

    # 含 "/" 的模式按路径段匹配：* 不跨目录、** 跨任意层
    scoped = tools.search_files("demo_docs/*.md")
    assert {item["path"] for item in scoped["items"]} == {
        "demo_docs/a.md",
        "demo_docs/b.md",
    }
    deep_only = tools.search_files("**/deep/*.md")
    assert {item["path"] for item in deep_only["items"]} == {
        "demo_docs/nested/deep/c.md"
    }


def test_filesystem_search_follows_symlinked_subdir_metadata(tmp_path: Path) -> None:
    """复现 Lead 7801 集成目录形态：root 内 demo_docs 是指向真实目录的符号链接。"""
    real = tmp_path / "data" / "demo_docs"
    real.mkdir(parents=True)
    for index in range(3):
        (real / f"doc-{index}.md").write_text(f"# doc {index}", encoding="utf-8")
    root = tmp_path / "integration"
    root.mkdir()
    (root / "synapse.db").write_bytes(b"SQLite format 3\x00")
    (root / "demo_docs").symlink_to(real)
    tools = FileSystemTools(root)

    listing = tools.list_directory(".")
    demo_item = next(item for item in listing["items"] if item["name"] == "demo_docs")
    assert demo_item["type"] == "directory"
    assert demo_item["symlink"] is True

    result = tools.search_files("*.md")
    assert result["count"] == 3
    assert {item["path"] for item in result["items"]} == {
        "demo_docs/doc-0.md",
        "demo_docs/doc-1.md",
        "demo_docs/doc-2.md",
    }
    assert all(item.get("symlinked") is True for item in result["items"])
    assert result["symlinked_count"] == 3

    # 安全边界不变：符号链接目标在 root 外，读取内容仍被拒绝
    with pytest.raises(FileSystemError, match="拒绝访问"):
        tools.read_text_file("demo_docs/doc-0.md")


def test_filesystem_search_symlink_strict_mode_and_cycles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)
    (child / "x.md").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    (root / "linked").symlink_to(outside)
    (child / "loop").symlink_to(root)  # 目录环，遍历不能死循环
    tools = FileSystemTools(root)

    result = tools.search_files("*.md")
    assert {item["path"] for item in result["items"]} == {"child/x.md", "linked/secret.md"}
    # 符号链接循环被 visited 集合截断，不会重复 / 卡死
    assert result["count"] == 2

    # 严格模式：不跟随目录符号链接（保持「解析后必须位于 root 内」语义）
    monkeypatch.setenv("SYNAPSE_MCP_FS_FOLLOW_SYMLINKS", "0")
    strict = tools.search_files("*.md")
    assert {item["path"] for item in strict["items"]} == {"child/x.md"}
