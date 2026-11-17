"""内置只读文件系统 MCP Server（FastMCP + stdio）。

启动方式::

    python -m app.mcp_servers.filesystem_server --root <目录>

安全限制：
- 显式访问的路径（``..``、绝对路径、符号链接）解析后必须位于 ``--root`` 内，否则拒绝；
- 搜索会递归子目录，并跟随 root 内的**目录符号链接**（只返回文件名 / 大小等元数据，
  文件内容读取仍然严格限制在 root 内）——可用 ``SYNAPSE_MCP_FS_FOLLOW_SYMLINKS=0``
  关闭符号链接跟随，恢复纯字面路径遍历；
- 敏感文件（``.env``、``*.key``、``.venv``、``.git`` 等）不可列出、搜索、读取；
- 不提供任何写操作，单文件读取上限 200KB / 12000 字符。
"""

from __future__ import annotations

import argparse
import fnmatch
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

logger = logging.getLogger("synapse.mcp.filesystem")

#: 单次目录列表最多返回项数
MAX_LIST_ITEMS = 200
#: 单个文件读取上限（字节）
MAX_READ_BYTES = 200 * 1024
#: 单个文件单次返回字符上限
MAX_READ_CHARS = 12000
#: 搜索最多返回结果数
MAX_SEARCH_RESULTS = 50
#: 搜索最大递归深度（防止大目录长时间遍历）
MAX_SEARCH_DEPTH = 8
#: 搜索最多遍历的目录数 / 检查的文件数（防止超大目录长时间遍历）
MAX_SEARCH_DIRS = 5000
MAX_SEARCH_ENTRIES = 20000

#: 敏感文件名 / 目录名（小写比较），一律拒绝访问
SENSITIVE_NAMES = frozenset(
    {
        ".env",
        ".venv",
        "venv",
        ".git",
        ".hg",
        "__pycache__",
        "node_modules",
        ".ds_store",
        ".ssh",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "id_rsa",
        "id_ed25519",
        "credentials",
    }
)
#: 敏感文件后缀，一律拒绝访问
SENSITIVE_SUFFIXES = (".key", ".pem", ".p12", ".pfx", ".keystore", ".crt")


class FileSystemError(ValueError):
    """文件系统工具错误（消息为中文，可直接展示）。"""


def _iso_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_sensitive_name(name: str) -> bool:
    lowered = name.lower()
    return lowered in SENSITIVE_NAMES or lowered.endswith(SENSITIVE_SUFFIXES)


def _match_search_pattern(pattern: str, relative: str, name: str) -> bool:
    """搜索模式匹配：纯文件名模式匹配 basename；含 "/" 的模式按路径段匹配（``**`` 可跨目录）。"""
    if "/" not in pattern:
        return fnmatch.fnmatch(name, pattern)
    parts = tuple(Path(relative).parts)
    segments = tuple(segment for segment in pattern.split("/") if segment)
    memo: dict[tuple[int, int], bool] = {}

    def match(pattern_index: int, path_index: int) -> bool:
        key = (pattern_index, path_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(segments):
            result = path_index == len(parts)
        elif segments[pattern_index] == "**":
            result = any(
                match(pattern_index + 1, next_index)
                for next_index in range(path_index, len(parts) + 1)
            )
        elif path_index >= len(parts):
            result = False
        else:
            result = fnmatch.fnmatch(parts[path_index], segments[pattern_index]) and match(
                pattern_index + 1, path_index + 1
            )
        memo[key] = result
        return result

    return match(0, 0)


def _follow_symlinks() -> bool:
    """搜索时是否跟随目录符号链接（默认跟随；``SYNAPSE_MCP_FS_FOLLOW_SYMLINKS=0`` 关闭）。"""
    raw = os.environ.get("SYNAPSE_MCP_FS_FOLLOW_SYMLINKS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _looks_binary(data: bytes) -> bool:
    """粗判二进制：含 NUL 或不可打印控制字符比例过高。"""
    sample = data[:8192]
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    control = sum(1 for byte in sample if byte < 32 and byte not in (9, 10, 13))
    return control / len(sample) > 0.02


class FileSystemTools:
    """受限根目录的只读文件系统操作（可单独实例化用于测试）。"""

    def __init__(self, root: str | Path) -> None:
        root_path = Path(root).expanduser().resolve()
        if not root_path.is_dir():
            raise FileSystemError(f"文件系统根目录不存在或不是目录：{root_path}")
        self.root = root_path

    # ------------------------------------------------------------------
    # 路径安全
    # ------------------------------------------------------------------
    def _relative_parts(self, path: Path) -> tuple[str, ...]:
        try:
            return path.relative_to(self.root).parts
        except ValueError:
            return ()

    def _ensure_not_sensitive(self, path: Path) -> None:
        for part in self._relative_parts(path):
            if _is_sensitive_name(part):
                raise FileSystemError(
                    "拒绝访问：该路径包含敏感文件或目录（.env / 密钥 / 虚拟环境等）"
                )

    def resolve(self, raw: str | Path | None) -> Path:
        """把用户路径解析为 root 内的绝对路径；越界 / 敏感路径抛错。"""
        text = str(raw if raw is not None else ".").strip() or "."
        if "\x00" in text:
            raise FileSystemError("路径包含非法字符")
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:  # 符号链接环等
            raise FileSystemError(f"路径无法解析：{text}") from exc
        if not resolved.is_relative_to(self.root):
            raise FileSystemError(
                "拒绝访问：路径超出允许的根目录（不允许 .. 或绝对路径逃逸）"
            )
        self._ensure_not_sensitive(resolved)
        return resolved

    def _display(self, path: Path) -> str:
        relative = path.relative_to(self.root).as_posix()
        return "." if relative == "." else relative

    # ------------------------------------------------------------------
    # 工具实现
    # ------------------------------------------------------------------
    def list_directory(self, path: str = ".") -> dict[str, object]:
        target = self.resolve(path)
        if not target.exists():
            raise FileSystemError(f"目录不存在：{self._display(target)}")
        if not target.is_dir():
            raise FileSystemError(f"不是目录：{self._display(target)}")
        try:
            entries = sorted(
                target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())
            )
        except PermissionError as exc:
            raise FileSystemError(f"没有权限读取目录：{self._display(target)}") from exc
        items: list[dict[str, object]] = []
        truncated = False
        for entry in entries:
            if _is_sensitive_name(entry.name):
                continue
            if len(items) >= MAX_LIST_ITEMS:
                truncated = True
                break
            try:
                is_dir = entry.is_dir()
                size = 0 if is_dir else entry.stat().st_size
            except OSError:
                continue
            items.append(
                {
                    "name": entry.name,
                    "type": "directory" if is_dir else "file",
                    "size": size,
                    "hidden": entry.name.startswith("."),
                    "symlink": entry.is_symlink(),
                }
            )
        return {
            "ok": True,
            "root": str(self.root),
            "path": self._display(target),
            "items": items,
            "count": len(items),
            "truncated": truncated,
        }

    def read_text_file(
        self, path: str, max_chars: int = MAX_READ_CHARS
    ) -> dict[str, object]:
        target = self.resolve(path)
        if not target.exists():
            raise FileSystemError(f"文件不存在：{self._display(target)}")
        if not target.is_file():
            raise FileSystemError(f"不是文件：{self._display(target)}")
        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            raise FileSystemError(
                f"文件过大（{size} 字节），超过 200KB 只读限制，已拒绝读取"
            )
        data = target.read_bytes()
        if _looks_binary(data):
            raise FileSystemError("拒绝读取：该文件看起来是二进制文件")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = data.decode("gb18030")
            except UnicodeDecodeError as exc:
                raise FileSystemError("拒绝读取：文件不是 UTF-8 / GB18030 文本") from exc
        try:
            limit = max(1, min(int(max_chars), MAX_READ_CHARS))
        except (TypeError, ValueError):
            limit = MAX_READ_CHARS
        truncated = len(text) > limit
        return {
            "ok": True,
            "path": self._display(target),
            "size": size,
            "chars": len(text),
            "truncated": truncated,
            "content": text[:limit],
        }

    def _iter_search_files(self):
        """递归遍历 root 内文件；返回 ``(path, via_symlink)``，跳过敏感项。

        - 深度 / 目录数 / 文件检查数均有上限，避免大目录卡死；
        - 跟随 root 内的目录符号链接（用 resolve 后 visited 集合防环）；
          ``SYNAPSE_MCP_FS_FOLLOW_SYMLINKS=0`` 时只遍历字面目录。
        """
        follow = _follow_symlinks()
        stack: list[tuple[Path, int, bool]] = [(self.root, 0, False)]
        visited: set[Path] = {self.root}
        directories = 0
        checked = 0
        while stack:
            current, depth, via_symlink = stack.pop()
            directories += 1
            if directories > MAX_SEARCH_DIRS:
                return
            try:
                entries = sorted(current.iterdir(), key=lambda item: item.name.lower())
            except OSError:
                continue
            for entry in entries:
                if _is_sensitive_name(entry.name):
                    continue
                try:
                    is_link = entry.is_symlink()
                    if entry.is_dir():
                        if is_link and not follow:
                            continue
                        target = entry.resolve()
                        if target in visited:
                            continue
                        if depth + 1 > MAX_SEARCH_DEPTH:
                            continue
                        visited.add(target)
                        stack.append((entry, depth + 1, via_symlink or is_link))
                    elif entry.is_file():
                        checked += 1
                        if checked > MAX_SEARCH_ENTRIES:
                            return
                        yield entry, via_symlink or is_link
                except OSError:
                    continue

    def search_files(
        self, pattern: str = "*.md", max_results: int = MAX_SEARCH_RESULTS
    ) -> dict[str, object]:
        raw = str(pattern or "").strip()
        if not raw:
            raise FileSystemError("搜索模式不能为空（例如 *.md）")
        if raw.startswith(("/", "~")) or ".." in Path(raw).parts:
            raise FileSystemError("文件匹配模式不合法：不允许绝对路径或 ..")
        try:
            limit = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
        except (TypeError, ValueError):
            limit = MAX_SEARCH_RESULTS

        items: list[dict[str, object]] = []
        truncated = False
        symlinked_count = 0
        for full, via_symlink in self._iter_search_files():
            try:
                relative = full.relative_to(self.root).as_posix()
            except ValueError:  # pragma: no cover - 遍历始终从 root 出发
                continue
            if not _match_search_pattern(raw, relative, full.name):
                continue
            if len(items) >= limit:
                truncated = True
                break
            try:
                size = full.stat().st_size
            except OSError:
                continue
            item: dict[str, object] = {
                "path": relative,
                "name": full.name,
                "size": size,
            }
            if via_symlink:
                item["symlinked"] = True
                symlinked_count += 1
            items.append(item)
        payload: dict[str, object] = {
            "ok": True,
            "pattern": raw,
            "root": str(self.root),
            "recursive": True,
            "items": items,
            "count": len(items),
            "truncated": truncated,
        }
        if symlinked_count:
            payload["symlinked_count"] = symlinked_count
        return payload

    def get_file_info(self, path: str) -> dict[str, object]:
        target = self.resolve(path)
        if not target.exists():
            raise FileSystemError(f"路径不存在：{self._display(target)}")
        try:
            stat = target.stat()
        except OSError as exc:
            raise FileSystemError(f"无法读取文件信息：{self._display(target)}") from exc
        is_dir = target.is_dir()
        name = target.name
        return {
            "ok": True,
            "path": self._display(target),
            "name": name,
            "type": "directory" if is_dir else "file",
            "size": 0 if is_dir else stat.st_size,
            "mtime": _iso_time(stat.st_mtime),
            "hidden": name.startswith("."),
        }


def build_server(root: str | Path) -> FastMCP:
    """按 root 构建 FastMCP server（工具注册为只读）。"""
    tools = FileSystemTools(root)
    server = FastMCP("synapse-filesystem")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_directory(path: str = ".") -> dict[str, object]:
        """列出根目录内某个目录下一层的文件与子目录（只读，最多 200 项，不递归）。

        path 为相对根目录的路径，默认 "." 表示根目录。
        如需按名称 / 模式查找文件（例如列出所有 Markdown），请改用 search_files。
        """
        return tools.list_directory(path)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def read_text_file(path: str, max_chars: int = MAX_READ_CHARS) -> dict[str, object]:
        """读取根目录内的文本文件内容（只读，UTF-8，最多 12000 字符）。

        超过 200KB 或二进制文件会被拒绝；path 为相对根目录的路径。
        """
        return tools.read_text_file(path, max_chars)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def search_files(
        pattern: str = "*.md", max_results: int = MAX_SEARCH_RESULTS
    ) -> dict[str, object]:
        """按文件名 glob 递归搜索根目录及所有子目录内的文件（只读，例如 *.md、*rag*）。

        用户要求「列出 / 查找文件」时优先使用本工具：会递归所有子目录（包括 root 内的
        符号链接目录），返回相对路径，最多 50 条；也支持相对路径模式（如 demo_docs/*.md）。
        """
        return tools.search_files(pattern, max_results)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_file_info(path: str) -> dict[str, object]:
        """获取根目录内文件或目录的基本信息（大小 / 修改时间 / 类型）。"""
        return tools.get_file_info(path)

    return server


def _default_root() -> Path:
    env = os.environ.get("SYNAPSE_MCP_FS_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    from .. import config  # 延迟导入：独立运行时也能给出友好错误

    return config.data_dir()


def main() -> None:
    parser = argparse.ArgumentParser(description="Synapse 只读文件系统 MCP Server")
    parser.add_argument("--root", default="", help="允许访问的根目录（默认 data/）")
    parsed = parser.parse_args()
    root = Path(parsed.root).expanduser() if parsed.root.strip() else _default_root()
    try:
        server = build_server(root)
    except FileSystemError as exc:
        raise SystemExit(f"文件系统 MCP Server 启动失败：{exc}") from exc
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
