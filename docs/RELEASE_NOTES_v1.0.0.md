# Synapse · 知脉 v1.0.0

自托管、中文优先的个人知识库 AI 助手：把文档、网页、笔记变成可对话、可漫游的第二大脑。

## 亮点

- **RAG 问答与引用溯源**：BM25 + 语义向量 + RRF；逐条 `[n]` 引用；文档级两阶段召回与查询意图路由（v2.3 完整召回@8 90%）。
- **3D 神经图谱 + 分区知识岛**：three.js/WebGL；文档/概念/关键词节点；分区隔离。
- **个人计划与打卡**：计划分组、每日/每周循环、单日行程、热力图、月历补记。
- **知识扩展与人工审核**：缺口检测 → 联网/学术检索 → 由你确认后才作为额外知识入库；原文档不改。
- **PDF 理解**：解析清洗、扫描件 OCR（RapidOCR）、AI 标题/摘要/关键词整理。
- **真实语义向量**：fastembed 多语言模型 / OpenAI 兼容 embeddings；一键重建。
- **MCP 工具**：文件系统 / SQLite / GitHub 只读内置 Server；「AI 帮我配」或手动添加 stdio MCP Server。
- **定时知识巡检**：自动整理待整理文档 + 重复/过时/未分区等检查。

## 快速开始

```bash
git clone https://github.com/15016623369a-spe/synapse-zhimai.git
cd synapse-zhimai
./start.sh
# 或 Docker：
docker compose up --build
# http://127.0.0.1:7800
```

只使用本地检索也能运行；配置任意 OpenAI 兼容模型（DeepSeek/OpenAI/Ollama）可开启 AI 生成、MCP、整理与巡检。

## 验证

- 后端 342 passed / 1 skipped
- API 契约 52/52、3D E2E 16/16
- 前端 typecheck/build、Playwright 专项验收
- GitHub Actions：backend + frontend + Docker 构建与运行态 smoke（含命名卷持久化）

## 安全提示

**单用户自托管工具**，没有登录/多租户隔离；不要直接暴露公网。API Key 保存在本地 SQLite；自定义 MCP Server 会执行用户提供的命令，只添加信任来源。详见 [SECURITY.md](../SECURITY.md)。

## 已知限制

- 扫描 OCR 只对新上传 PDF 生效；存量 PDF 需重新上传。
- 无答案问题的拒答阈值尚未实现。
- 本地语义模型首次使用需下载；MCP 自定义 Server 只支持 stdio。
- 当前版本不面向多用户/SaaS；公网部署需自行补认证、限流、TLS 与审计。

完整说明见 [README.md](../README.md)。
