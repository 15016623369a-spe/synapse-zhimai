"""内置只读 MCP Servers（FastMCP + stdio 传输）。

三个 server 均以独立子进程运行，由 :mod:`app.services.mcp` 短连接调用：

- ``filesystem_server``：受限根目录内的只读文件浏览 / 读取 / 文件名搜索；
- ``sqlite_server``：SQLite 只读查询（仅 SELECT / WITH / EXPLAIN，强制 LIMIT）；
- ``github_server``：只读 GitHub REST（可选 ``GITHUB_TOKEN``）。

安全约定：所有 server 不提供任何写操作；错误信息为中文，可直接展示给用户。
"""
