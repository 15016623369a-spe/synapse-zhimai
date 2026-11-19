# Synapse · 知脉 — 产品规格与 API 契约

> 本文件是 README 中「SPEC.md」的实体版本，面向二次开发与验收。
> 代码是唯一事实来源；这里的契约变化时请同步更新本文件与 `docs/VERIFICATION.md`。

- 版本：v2.4.0
- 定位：单用户、自托管、中文优先的个人知识库 RAG 助手
- 运行形态：FastAPI 单进程 + SQLite 单文件；前端 React SPA 由同一进程托管
- 非目标：多租户、公网 SaaS、分布式向量库

---

## 1. 数据模型（SQLite）

| 表 | 作用 | 关键字段 |
|---|---|---|
| `documents` | 文档元数据与正文 | `id, title, filename, source_type, source_url, tags(JSON), summary, content, size, status, zone_id, enrich_version` |
| `chunks` | 分块正文与向量 | `id, doc_id, idx, text, char_start, char_end, token_count, embedding(BLOB)` |
| `concepts` | 图谱概念节点 | `id, label, norm, kind` |
| `doc_concepts` | 文档—概念 | `doc_id, concept_id, weight` |
| `links` | 图谱关系 | `source, target, type, weight`（`mentions/tagged/related/cooccur`） |
| `conversations` / `messages` | 会话与消息 | `messages.citations` 为 JSON 数组 |
| `zones` | 分区（知识岛） | `id, name, color, description, sort_order` |
| `plans` / `plan_checkins` / `plan_groups` | 计划打卡 | `frequency, weekly_target, plan_date, start_time, end_time, location, group_id` |
| `knowledge_feedback` | 外部知识采纳/忽略 | `url, query, decision(accepted/rejected), doc_id` |
| `settings` | KV 配置 | `llm_*`、`embedding_*`、`patrol_*`、`search_*` |
| `activity` | 操作流水 | `kind, detail` |

约定：

- 所有 ID 为带前缀字符串：`d_` 文档、`c_` 分块、`conv_` 会话、`m_` 消息、`p_` 计划。
- 时间统一 UTC ISO-8601（`now_iso()`）。
- `chunks` 没有页码字段：PDF 入库会清洗页眉/页脚/页码，`char_start/char_end` 是清洗后正文偏移。
- schema 版本写在 `settings.schema_version`；迁移为启动时幂等 `ALTER TABLE` 探测（见 `db.py`）。

---

## 2. 检索契约

`POST /api/search`

```json
{"query": "分块大小是多少？", "top_k": 6, "doc_ids": null}
```

响应：

```json
{
  "query": "...",
  "mode": "fact|multi|compare|set",
  "expanded_queries": ["..."],
  "doc_count": 2,
  "results": [
    {
      "chunk_id": "c_...",
      "doc_id": "d_...",
      "doc_title": "...",
      "text": "...",
      "snippet": "按问题挑出的相关句",
      "score": 0.98,
      "source_type": "md"
    }
  ]
}
```

**评分语义（重要）**：

- `score` 是 BM25 + 向量 cosine 经 RRF 融合、再按 `0.35 + 0.63 * score/max` 归一的**排名分**；
- Top-1 恒为约 `0.98`，**不是概率、不是跨 query 可比的置信度**；
- 前端/调用方应使用引用里的 `relevance`（`high/medium/low/document`）与 `rank`，不要展示成百分比。

检索流程：意图路由 → 多查询展开（≤3，确定性）→ 每查询 BM25 + hash/fastembed 向量 → RRF 融合
→ 文档级两阶段（文档分 + chunk 配额轮转，每文档默认 ≤2）→ 按融合分排序输出。

---

## 3. 对话契约（SSE）

`POST /api/chat`，`Content-Type: text/event-stream`。

事件顺序：

```
event: meta      {conversation_id, user_message_id, mode, reference, context_*}
event: sources   {citations: [...]}           # 生成前：候选来源（用于预取/预览）
event: tool      {phase, name, ...}           # 可选：MCP / 联网工具
event: gap       {needs_more, reason, suggested_queries, coverage?}
event: web_sources {provider, intent, queries, candidates}
event: delta     {text}
event: notice    {message, mode}              # 可选：LLM 失败降级
event: done      {message_id, conversation_id, citations, mode, title, gap, web_sources, ...}
event: error     {message}
```

**引用契约**：

```json
{
  "index": 1,
  "doc_id": "d_...",
  "doc_title": "...",
  "chunk_id": "c_...",
  "source_type": "md",
  "snippet": "...",
  "score": 0.98,
  "rank": 1,
  "relevance": "high",
  "kind": "chunk",
  "unverified": false
}
```

- `index` 与回答中的 `[n]` 一一对应，从 1 开始；
- `done.citations` 只包含回答里**实际出现**的 `[n]`（文档级全文引用除外）；
- 模型引用了不存在的编号时，记入 `done.context_diagnostics.invalid_citations`，不静默；
- 模型一个引用都没标时，返回前 3 条并置 `unverified: true`；
- `kind=document`（整篇文档总结）时 `score=null`、`relevance=document`，不伪造相似度。

**缺口判据**（`agent.detect_gap`）：

1. 未召回任何片段 → 缺口；
2. 问题中的英文术语 / 引号实体在语料中 `df=0` → 缺口；
3. 语料内登录词的 IDF 加权覆盖率 < 0.25 → 缺口。

刻意不使用「出现未登录词即缺口」：中文分词边界（如「粉水比」）会制造大量误报。

---

## 4. 文档接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/documents` | 列表、搜索、标签/分区过滤、分页 |
| POST | `/api/documents/upload` | 多文件上传（md/txt/pdf/docx/html，单文件 ≤25MB） |
| POST | `/api/documents/text` | 新建 Markdown 笔记 |
| POST | `/api/documents/url` | 抓取网页正文入库 |
| GET | `/api/documents/{id}` | 详情 |
| GET | `/api/documents/{id}/chunks` | 分块预览 |
| DELETE | `/api/documents/{id}` | 删除文档、分块、概念关系 |
| PUT | `/api/documents/zone` | 批量归档到分区 |

入库流水线：解析 → 清洗（PDF 页眉/页脚/断词）→ 分块（450–650 字、重叠 80）→ 概念抽取
→ 向量化 → 写库 → 重建该文档的图谱关系。`status` 取 `pending/ready/failed`。

---

## 5. 知识扩展（缺口 → 候选 → 人工审核 → 入库）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/extend/status` | 搜索 provider、采纳/忽略计数 |
| POST | `/api/extend/search` | 按问题检索外部候选（学术意图走 arXiv/OpenAlex） |
| POST | `/api/extend/commit` | 用户勾选后入库，新增文档 + `外部扩展` tag，不改原文档 |
| POST | `/api/extend/reject` | 记录忽略，后续候选过滤该 URL |

- 抓取失败且摘要 ≥30 字时以 `mode=snippet` 入库，`source_type=web`；
- 已入库 URL 会带 `already_in_kb=true`；被拒绝且未入库的 URL 不再推荐；
- 采纳/忽略只影响**外部候选排序**，不改变知识库检索排序（不要对外宣称"反馈提升召回"）。

---

## 6. 设置与运维

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/PUT | `/api/settings` | LLM / embedding / search 配置（Key 永不回显） |
| POST | `/api/settings/test` | 连通性测试 |
| GET/POST | `/api/admin/embedding/*` | 向量模式、测试、全量重建 |
| GET/POST | `/api/admin/enrich*` | 知识整理（标题/摘要/关键词/tags） |
| POST | `/api/admin/reseed` | 重置为演示数据（先备份 DB） |
| GET/PATCH/POST | `/api/patrol/*` | 定时巡检状态、配置、手动执行 |
| GET | `/api/health` | `{status, version, documents, chunks, concepts, llm, embedding, ocr, search}` |

安全边界（详见 `SECURITY.md`）：

- 默认只绑定 `127.0.0.1`；无登录/多租户，不要直接映射公网；
- `documents/url` 与 `extend` 抓取都做逐跳 SSRF 校验（首跳 + 每次重定向），内网/环回/保留网段拒绝；
- 自定义 MCP Server 可执行任意命令，只添加可信来源；
- API Key 明文存本地 SQLite，`data/` 与备份必须排除在版本库/交付物之外。

---

## 7. 版本与兼容

- API 字段只增不改；新增字段调用方可忽略（`rank/relevance/kind` 即如此）。
- `citations[].score` 允许为 `null`（v2.4 起，仅 `kind=document`）。
- 旧前端忽略新字段仍可渲染；新前端对旧数据缺失 `relevance` 时回落为 `low`。
