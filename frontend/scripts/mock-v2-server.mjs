#!/usr/bin/env node
/**
 * Synapse V2 前端本地 Mock API（仅用于 UI 开发 / Playwright 验收，不碰 live 7800/data）。
 *
 * 启动：
 *   node scripts/mock-v2-server.mjs            # 默认 127.0.0.1:7850
 *   MOCK_PORT=7850 node scripts/mock-v2-server.mjs
 *
 * 配合 Vite：
 *   VITE_PROXY_TARGET=http://127.0.0.1:7850 npm run dev -- --port 5174
 *
 * 覆盖：health/settings/stats、MCP、embedding、reranker、patrol、SSE /chat、
 *       conversations、graph/zones 等页面所需的最小接口，带可变的交互状态。
 */
import http from "node:http";

const PORT = Number(process.env.MOCK_PORT || 7850);
const HOST = "127.0.0.1";

const nowIso = () => new Date().toISOString();
const hoursAgo = (hours) => new Date(Date.now() - hours * 3600_000).toISOString();
const hoursLater = (hours) => new Date(Date.now() + hours * 3600_000).toISOString();
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const BUILTIN_TOOLS = {
  filesystem: [
    { name: "list_directory", description: "列出目录内的文件与子目录", read_only: true, input_schema: { type: "object", properties: { path: { type: "string" } } } },
    { name: "read_text_file", description: "读取文本文件内容（最多 12000 字）", read_only: true, input_schema: { type: "object", properties: { path: { type: "string" } } } },
    { name: "search_files", description: "按文件名通配符搜索（如 *.md）", read_only: true, input_schema: { type: "object", properties: { pattern: { type: "string" } } } },
    { name: "get_file_info", description: "查看文件大小 / 修改时间 / 类型", read_only: true, input_schema: { type: "object", properties: { path: { type: "string" } } } },
  ],
  sqlite: [
    { name: "list_tables", description: "列出数据库表与行数", read_only: true, input_schema: { type: "object", properties: {} } },
    { name: "describe_table", description: "查看表结构（列名 / 类型）", read_only: true, input_schema: { type: "object", properties: { table: { type: "string" } } } },
    { name: "query", description: "执行单条只读 SELECT 查询", read_only: true, input_schema: { type: "object", properties: { sql: { type: "string" } } } },
  ],
  github: [
    { name: "search_repositories", description: "搜索 GitHub 仓库", read_only: true, input_schema: { type: "object", properties: { query: { type: "string" } } } },
    { name: "get_repository", description: "读取仓库信息", read_only: true, input_schema: { type: "object", properties: { repo: { type: "string" } } } },
    { name: "list_issues", description: "列出仓库 Issue", read_only: true, input_schema: { type: "object", properties: { repo: { type: "string" } } } },
    { name: "get_file_contents", description: "读取仓库文件内容", read_only: true, input_schema: { type: "object", properties: { repo: { type: "string" }, path: { type: "string" } } } },
  ],
};

const CUSTOM_TOOLS = [
  { name: "list_reports", description: "列出报告目录", read_only: true },
  { name: "read_report", description: "读取报告文件内容", read_only: true },
  { name: "write_report", description: "写入/修改报告（写操作）", read_only: false },
];

const MCP_SUGGEST_CATALOG = {
  filesystem: {
    catalog_id: "filesystem",
    kind: "builtin",
    server_id: "filesystem",
    name: "文件系统",
    description: "读取 data 目录下的文档与目录列表（只读）",
    rationale: "你的需求涉及本地文档，内置文件系统工具可直接只读访问 data 目录。",
    requires_token: false,
    test_note: "启用即用：仅访问 data 目录，只读；无需 Key。",
  },
  sqlite: {
    catalog_id: "sqlite",
    kind: "builtin",
    server_id: "sqlite",
    name: "知识库数据库",
    description: "只读查询知识库 SQLite（仅 SELECT）",
    rationale: "查询数据库的需求可启用内置 SQLite 工具，只允许 SELECT。",
    requires_token: false,
    test_note: "启用即用：仅允许 SELECT，不会修改数据。",
  },
  github: {
    catalog_id: "github",
    kind: "builtin",
    server_id: "github",
    name: "GitHub",
    description: "搜索仓库、读取 Issue 与文件内容（只读 REST）",
    rationale: "GitHub 仓库相关需求可启用内置 GitHub 工具。",
    requires_token: true,
    test_note: "建议配置 GITHUB_TOKEN 避免匿名 API 限流；仍为只读。",
  },
  fetch: {
    catalog_id: "fetch",
    kind: "custom",
    name: "网页抓取工具",
    description: "抓取网页正文并转为 Markdown",
    rationale: "网页内容需求可使用受控目录中的 fetch MCP Server。",
    command: "uvx",
    args: ["mcp-server-fetch"],
    env_keys: [],
    requires_token: false,
    test_note: "来自安全目录：uvx mcp-server-fetch，仅读取网页内容。",
  },
  memory: {
    catalog_id: "memory",
    kind: "custom",
    name: "记忆存储工具",
    description: "跨会话记忆与检索（knowledge graph memory）",
    rationale: "需要持久化记忆时可用受控目录中的 memory MCP Server。",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-memory"],
    env_keys: ["MEMORY_FILE_PATH"],
    requires_token: false,
    test_note: "来自安全目录：npx @modelcontextprotocol/server-memory。",
  },
  git: {
    catalog_id: "git",
    kind: "custom",
    name: "Git 仓库读取",
    description: "只读查看本地 git 仓库历史与文件",
    rationale: "仓库相关需求可用受控目录中的 git MCP Server。",
    command: "uvx",
    args: ["mcp-server-git", "--repository", "."],
    env_keys: [],
    requires_token: false,
    test_note: "来自安全目录：uvx mcp-server-git，仅只读。",
  },
};

const state = {
  llm: {
    base_url: "https://api.deepseek.com/v1",
    model: "deepseek-chat",
    configured: true,
    api_key_set: true,
    temperature: 0.2,
  },
  mcp: {
    servers: [
      {
        id: "filesystem",
        name: "文件系统",
        builtin: "filesystem",
        transport: "stdio",
        command: "/mock/python",
        args: ["-m", "app.mcp_servers.filesystem_server", "--root", "data"],
        allowed_tools: null,
        enabled: true,
        has_token: false,
        connected: true,
        last_error: null,
      },
      {
        id: "sqlite",
        name: "知识库数据库",
        builtin: "sqlite",
        transport: "stdio",
        command: "/mock/python",
        args: ["-m", "app.mcp_servers.sqlite_server", "--db", "data/synapse.db"],
        allowed_tools: null,
        enabled: false,
        has_token: false,
        connected: false,
        last_error: null,
      },
      {
        id: "github",
        name: "GitHub",
        builtin: "github",
        transport: "stdio",
        command: "/mock/python",
        args: ["-m", "app.mcp_servers.github_server"],
        allowed_tools: null,
        enabled: true,
        has_token: true,
        connected: false,
        last_error: "GitHub REST 请求失败：API rate limit exceeded（可在展开后配置 Token 提升额度）",
      },
    ],
  },
  embedding: {
    provider: "hash",
    model: "hash-local",
    base_url: "",
    dim: 384,
    total_chunks: 1538,
    ready: true,
    needs_rebuild: true,
    has_api_key: false,
    providers: ["hash", "fastembed", "openai"],
    last_rebuild: null,
  },
  reranker: {
    accepted: 12,
    rejected: 8,
    domains: [
      { host: "arxiv.org", accepted: 7, rejected: 1, score: 0.8 },
      { host: "github.com", accepted: 4, rejected: 1, score: 0.71 },
      { host: "zhihu.com", accepted: 1, rejected: 5, score: 0.25 },
      { host: "csdn.net", accepted: 0, rejected: 4, score: 0.17 },
      { host: "openai.com", accepted: 2, rejected: 0, score: 0.75 },
      { host: "wikipedia.org", accepted: 1, rejected: 0, score: 0.67 },
      { host: "medium.com", accepted: 1, rejected: 1, score: 0.5 },
    ],
    description:
      "你忽略的域名（zhihu.com / csdn.net）会在后续外部资料中降权，采纳的来源会加分；这是规则化偏好，不训练模型。",
  },
  patrol: {
    enabled: true,
    interval_hours: 24,
    auto_enrich: true,
    max_enrich: 3,
    running: false,
    last_run_at: hoursAgo(5),
    next_run_at: hoursLater(19),
    counts: { pending_docs: 5, total_documents: 31, enriched_documents: 26 },
    last_result: {
      started_at: hoursAgo(5.1),
      finished_at: hoursAgo(5),
      duration_ms: 18432,
      trigger: "manual",
      enriched: [
        { id: "doc-21", title: "RAG 检索增强生成综述", ok: true },
        { id: "doc-22", title: "多目标跟踪的置信传播", ok: true },
        { id: "doc-23", title: "知识图谱构建实践", ok: true },
      ],
      issues: {
        pending_docs: { count: 5, sample: [{ id: "doc-31", title: "KV Cache 优化笔记" }, { id: "doc-32", title: "Agent 工具调用综述" }] },
        duplicate_docs: {
          count: 2,
          pairs: [
            {
              a: { id: "doc-21", title: "RAG 检索增强生成综述" },
              b: { id: "doc-24", title: "RAG 检索增强生成综述（副本）" },
              similarity: 0.95,
            },
          ],
        },
        stale_docs: { count: 3, sample: [{ id: "doc-11", title: "2024 年度技术趋势" }] },
        orphan_docs: { count: 4, sample: [{ id: "doc-12", title: "未归档的会议纪要" }] },
        small_docs: { count: 6, sample: [{ id: "doc-13", title: "随手记录：向量维度选择" }] },
        missing_tags: { count: 8, sample: [{ id: "doc-14", title: "临时笔记：Rerank 实验" }] },
      },
      summary: "巡检完成：自动整理 3 篇待整理文档，发现 5 篇待整理、2 组重复标题、3 篇过时、4 篇未分区、6 篇过短、8 篇无标签。",
    },
  },
  conversations: [
    {
      id: "mock-conv-history",
      title: "知识库里的 RAG 主题",
      updated_at: hoursAgo(26),
      messages: [
        { id: "msg-h1", role: "user", content: "知识库里有哪些 RAG 相关的资料？", citations: [], created_at: hoursAgo(26.1) },
        {
          id: "msg-h2",
          role: "assistant",
          content: "知识库中与 RAG 相关的资料主要有 3 篇：……",
          citations: [
            { index: 1, doc_id: "doc-21", doc_title: "RAG 检索增强生成综述", chunk_id: "chunk-201", source_type: "md", snippet: "检索增强生成（RAG）通过在生成前引入外部知识……", score: 0.95 },
          ],
          created_at: hoursAgo(26),
        },
      ],
    },
  ],
};

const INITIAL_STATE = JSON.parse(JSON.stringify(state));
const resetState = () => {
  const fresh = JSON.parse(JSON.stringify(INITIAL_STATE));
  Object.assign(state, fresh);
};

/* ----------------------------------- 工具 ---------------------------------- */

function json(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

function readJson(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf-8");
      if (!raw) return resolve(null);
      try {
        resolve(JSON.parse(raw));
      } catch {
        resolve(null);
      }
    });
    req.on("error", () => resolve(null));
  });
}

function sseStart(res) {
  res.writeHead(200, {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    Connection: "keep-alive",
  });
  res.write(": connected\n\n");
}

function sseSend(res, event, data) {
  res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

function embeddingStatusPayload() {
  const emb = state.embedding;
  return {
    provider: emb.provider,
    model: emb.model,
    dim: emb.dim,
    total_chunks: emb.total_chunks,
    ready: emb.ready,
    needs_rebuild: emb.needs_rebuild,
    has_api_key: emb.has_api_key,
    providers: emb.providers,
    last_rebuild: emb.last_rebuild,
  };
}

function mcpServersPayload() {
  const items = state.mcp.servers.map((server) => {
    const enabled = Boolean(server.enabled);
    const serverTools = server.builtin
      ? BUILTIN_TOOLS[server.builtin] ?? []
      : server.custom_tools ?? CUSTOM_TOOLS;
    const tools = enabled && !server.last_error ? serverTools : [];
    return {
      ...server,
      enabled,
      connected: enabled && !server.last_error,
      tools,
    };
  });
  return {
    items,
    enabled_count: items.filter((server) => server.enabled).length,
    tool_count: items.reduce((total, server) => total + server.tools.length, 0),
  };
}

function patrolStatusPayload() {
  return {
    enabled: state.patrol.enabled,
    interval_hours: state.patrol.interval_hours,
    auto_enrich: state.patrol.auto_enrich,
    max_enrich: state.patrol.max_enrich,
    running: state.patrol.running,
    last_run_at: state.patrol.last_run_at,
    next_run_at: state.patrol.next_run_at,
    last_result: state.patrol.last_result,
    counts: state.patrol.counts,
  };
}

function statsPayload() {
  return {
    documents: 31,
    chunks: 1538,
    concepts: 267,
    links: 412,
    zones: 4,
    tags: [
      { name: "RAG", count: 9 },
      { name: "Agent", count: 7 },
      { name: "知识图谱", count: 6 },
      { name: "Rerank", count: 4 },
    ],
    llm_configured: true,
    recent: [
      { kind: "patrol", detail: "知识巡检完成：自动整理 3 篇文档", created_at: hoursAgo(5) },
      { kind: "enrich", detail: "智能整理更新 3 篇文档", created_at: hoursAgo(5) },
    ],
  };
}

function settingsPayload() {
  return {
    llm: { ...state.llm },
    embedding: { provider: state.embedding.provider, dim: state.embedding.dim },
    data: { documents: 31, chunks: 1538 },
  };
}

const CITATION = {
  index: 1,
  doc_id: "doc-21",
  doc_title: "RAG 检索增强生成综述",
  chunk_id: "chunk-201",
  source_type: "md",
  snippet: "检索增强生成（RAG）在生成前引入外部知识，降低模型幻觉并提升事实一致性……",
  score: 0.95,
};

const CITATION_2 = {
  index: 2,
  doc_id: "doc-22",
  doc_title: "多目标跟踪的置信传播",
  chunk_id: "chunk-202",
  source_type: "pdf",
  snippet: "置信传播（Belief Propagation）在多目标跟踪中用于关联跨帧目标……",
  score: 0.91,
};

const PAPER_REFERENCE = {
  doc_id: "d_94cb7908bdca",
  title: "Transformer-BP 多目标跟踪研究框架",
  filename: "Transformer-BP_FUSION2024.pdf",
  source_type: "pdf",
};

const PAPER_CITATIONS = [
  {
    index: 1,
    doc_id: "d_94cb7908bdca",
    doc_title: "Transformer-BP 多目标跟踪研究框架",
    chunk_id: "chunk-tb1",
    source_type: "pdf",
    snippet: "本文提出 Transformer-BP 框架，将 Transformer 的全局建模能力与置信传播的概率图推理结合……",
    score: 0.96,
  },
  {
    index: 2,
    doc_id: "d_94cb7908bdca",
    doc_title: "Transformer-BP 多目标跟踪研究框架",
    chunk_id: "chunk-tb2",
    source_type: "pdf",
    snippet: "创新点包括：跨帧注意力关联、可学习的信念传播消息传递以及轨迹级一致性约束……",
    score: 0.94,
  },
];

const CONTEXT_SUMMARY = "已结合上文《Transformer-BP 多目标跟踪研究框架》· 追问创新点";

const CONTEXT_CANDIDATES = [
  {
    url: "https://arxiv.org/abs/2410.06294",
    title: "Neural Enhanced Multiobject Tracking: NEBP+",
    snippet: "Belief propagation enhanced multi-object tracking with learned feature fusion.",
    source: "arxiv",
    relevance: 0.35,
    authority: "academic",
    authority_label: "学术论文",
    feedback_label: "",
  },
  {
    url: "https://arxiv.org/abs/2401.00002",
    title: "Interplay Between Belief Propagation and Transformer",
    snippet: "Differential-attention message passing transformer for tracking.",
    source: "arxiv",
    relevance: 0.35,
    authority: "academic",
    authority_label: "学术论文",
  },
  {
    url: "https://arxiv.org/abs/2401.00003",
    title: "A Cooperative Vehicle Localization Framework Based on Belief Propagation and Transformer",
    snippet: "Belief propagation and transformer for cooperative localization and trajectory prediction.",
    source: "openalex",
    relevance: 0.34,
    authority: "academic",
    authority_label: "学术论文",
  },
];

const WEB_CANDIDATES = [
  {
    url: "https://arxiv.org/abs/2401.00001",
    title: "Retrieval-Augmented Generation for Knowledge-Intensive NLP",
    snippet: "系统综述 RAG 的检索器、生成器与联合训练方法。",
    source: "arxiv",
    relevance: 0.93,
    authority: "academic",
    authority_label: "学术论文",
    feedback_label: "你采纳过类似来源",
  },
  {
    url: "https://github.com/microsoft/graphrag",
    title: "microsoft/graphrag: Graph-based RAG",
    snippet: "基于知识图谱的模块化 RAG 实现。",
    source: "bing",
    relevance: 0.86,
    authority: "official",
    feedback_label: "你采纳过该来源",
  },
  {
    url: "https://zhihu.com/question/000000",
    title: "如何评价 RAG 技术？—— 知乎",
    snippet: "社区讨论 RAG 的优缺点与工程实践。",
    source: "bing",
    relevance: 0.72,
    authority: "community",
    feedback_label: "你曾忽略该来源",
  },
];

/* --------------------------------- SSE 聊天 -------------------------------- */

async function handleChat(req, res) {
  const body = await readJson(req);
  const message = String(body?.message ?? "").trim() || "你好";
  const conversationId = String(body?.conversation_id ?? "") || `mock-conv-${Date.now()}`;
  const wantsMcp = /(文件|目录|读取|数据库|sql|表|github|仓库|issue|mcp|工具)/i.test(message);
  const wantsWeb = !wantsMcp && /(联网|搜索|补充|扩展|外部资料|搜集)/.test(message);
  // 多轮上下文：追问短句继承上文论文文档（模拟 task-5 后端契约）
  const wantsFollowUp = !wantsMcp && !wantsWeb && /(创新点|再总结|继续|它呢|这篇)/.test(message);
  const mentionsPaper = !wantsMcp && !wantsWeb && /Transformer-BP/.test(message);
  const contextUsed = wantsFollowUp;
  const contextSummary = contextUsed ? CONTEXT_SUMMARY : null;
  const inheritedReference = wantsFollowUp || mentionsPaper ? PAPER_REFERENCE : null;

  sseStart(res);
  const send = (event, data) => sseSend(res, event, data);

  send("meta", {
    conversation_id: conversationId,
    user_message_id: `mock-user-${Date.now()}`,
    mode: "llm",
    reference: inheritedReference,
    context_used: contextUsed,
    context_summary: contextSummary,
  });

  const citations = wantsMcp
    ? []
    : inheritedReference
      ? PAPER_CITATIONS
      : [CITATION, CITATION_2];
  send("sources", { citations });

  if (wantsWeb) {
    send("gap", {
      needs_more: true,
      reason: "知识库中相关材料较少，可联网检索补充资料，加入前由你确认。",
      suggested_queries: ["RAG 最新进展 2026", "GraphRAG 工程实践"],
    });
    send("tool", { phase: "start", name: "search_web", query: "RAG 最新进展 2026" });
    await sleep(420);
    send("tool", { phase: "end", name: "search_web", count: WEB_CANDIDATES.length });
    send("web_sources", {
      provider: "bing",
      intent: "general",
      queries: ["RAG 最新进展 2026"],
      candidates: WEB_CANDIDATES,
    });
  }

  if (wantsMcp) {
    send("tool", {
      phase: "start",
      name: "mcp_filesystem_list_directory",
      args: { path: "." },
    });
    await sleep(560);
    send("tool", {
      phase: "end",
      name: "mcp_filesystem_list_directory",
      ok: true,
      summary: "读取到 3 个 Markdown 文件",
    });

    send("tool", {
      phase: "start",
      name: "mcp_github_search_repositories",
      args: { query: "synapse knowledge graph", max_results: 5 },
    });
    await sleep(520);
    send("tool", {
      phase: "end",
      name: "mcp_github_search_repositories",
      ok: false,
      summary: "GitHub 请求失败：API rate limit exceeded，请在设置中配置 Token",
    });
  }

  const answer = contextUsed
    ? [
        "基于上文《Transformer-BP 多目标跟踪研究框架》，这篇论文的创新点主要有 4 点 [1][2]：",
        "",
        "1. **跨帧注意力关联**：用 Transformer 对多帧目标特征做全局注意力建模，替代传统仅依赖 IoU/外观的局部匹配。",
        "2. **可学习信念传播**：把置信传播的消息传递过程参数化，使概率图推理能端到端训练。",
        "3. **轨迹级一致性约束**：在关联阶段引入轨迹历史与运动一致性，减少身份切换。",
        "4. **轻量化融合**：将 Transformer 与 BP 推理分阶段解耦，在保持精度的同时控制计算开销。",
        "",
        "以上内容基于上文继承的论文文档检索片段回答，未重新检索其他资料。",
      ].join("\n")
    : wantsMcp
    ? [
        "我通过 MCP 工具读取了 `data` 目录，找到 3 个 Markdown 文件：",
        "",
        "- `README.md` — 项目说明",
        "- `notes/rag-basics.md` — RAG 基础笔记",
        "- `notes/knowledge-graph.md` — 知识图谱实践",
        "",
        "GitHub 工具这次因未配置 Token 被限流，然后我基于文件系统结果回答：`data` 目录下共有 3 个 Markdown 文件。",
        "",
        "> 工具均为只读调用，不会修改你的文件或数据库。",
      ].join("\n")
    : wantsWeb
      ? [
          "我在知识库中找到了一些相关材料，并联网补充了外部资料（尚未入库）。",
          "",
          "综合来看，RAG 的关键在于检索质量与上下文组织：先召回高相关分块，再按引用编号约束生成，可显著降低幻觉。",
          "",
          "右侧「外部参考」列表中的资料需要你确认后才加入知识库。",
        ].join("\n")
      : [
          "根据知识库中的资料，RAG（检索增强生成）可以概括为三个步骤：",
          "",
          "1. **检索**：把问题向量化，从分块中召回最相关的内容。",
          "2. **组织**：把检索结果按相关度排序，拼入提示词上下文。",
          "3. **生成**：大模型基于资料回答，并用引用编号标注来源。",
          "",
          "这样可以在不重新训练模型的前提下，让回答贴近你的个人知识库。",
        ].join("\n");

  for (let index = 0; index < answer.length; index += 9) {
    send("delta", { text: answer.slice(index, index + 9) });
    await sleep(24);
  }

  const done = {
    message_id: `mock-assistant-${Date.now()}`,
    conversation_id: conversationId,
    citations,
    mode: "llm",
    title: message.slice(0, 28) || "新对话",
    gap: wantsWeb
      ? { needs_more: true, reason: "知识库中相关材料较少，可联网检索补充资料。" }
      : null,
    web_sources: wantsWeb ? WEB_CANDIDATES : [],
    reference: inheritedReference,
    context_used: contextUsed,
    context_summary: contextSummary,
    context_diagnostics: contextUsed
      ? {
          history_messages: 2,
          history_chars: 420,
          context_chars: 132,
          token_budget: 58,
          topic_terms: ["Transformer-BP", "Transformer", "Belief Propagation", "BP", "多目标跟踪"],
          doc_context_chars: 0,
          retrieval_chunks: 2,
          prompt_chars: 2100,
        }
      : null,
  };
  if (wantsMcp) {
    done.mcp_results = [
      {
        name: "mcp_filesystem_list_directory",
        args: { path: "." },
        ok: true,
        text: "README.md\nnotes/rag-basics.md\nnotes/knowledge-graph.md",
      },
      {
        name: "mcp_github_search_repositories",
        args: { query: "synapse knowledge graph" },
        ok: false,
        text: "API rate limit exceeded",
      },
    ];
  }
  send("done", done);
  res.end();
}

/* ---------------------------------- 路由 ---------------------------------- */

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", `http://${HOST}:${PORT}`);
  const path = url.pathname;
  const method = req.method ?? "GET";

  try {
    if (path === "/api/mock/reset" && method === "POST") {
      resetState();
      return json(res, 200, { ok: true });
    }

    if (path === "/api/health") {
      return json(res, 200, {
        status: "ok",
        version: "2.0.0-mock",
        documents: 31,
        chunks: 1538,
        concepts: 267,
        llm: { ...state.llm },
        embedding: { provider: state.embedding.provider, dim: state.embedding.dim },
        search: { provider: "bing", enabled: true },
        ocr: { available: true, engine: "rapidocr", max_pages: 30 },
      });
    }

    if (path === "/api/stats") return json(res, 200, statsPayload());

    if (path === "/api/settings" && method === "GET") return json(res, 200, settingsPayload());
    if (path === "/api/settings" && method === "PUT") {
      const body = (await readJson(req)) ?? {};
      if (body.llm) state.llm = { ...state.llm, ...body.llm, configured: Boolean((body.llm.base_url ?? state.llm.base_url) && (body.llm.model ?? state.llm.model)) };
      return json(res, 200, settingsPayload());
    }
    if (path === "/api/settings/test") {
      return json(res, 200, { ok: true, latency_ms: 128, model: state.llm.model, message: "连接成功（mock）" });
    }

    if (path === "/api/admin/enrich/status") {
      return json(res, 200, {
        llm_configured: true,
        can_enrich: true,
        total_documents: 31,
        enriched_documents: 26,
        pending_pdf: 0,
        pending_all: 5,
        sample_pending: [
          { id: "doc-31", title: "KV Cache 优化笔记", source_type: "md" },
          { id: "doc-32", title: "Agent 工具调用综述", source_type: "pdf" },
        ],
        mode: "llm",
      });
    }

    if (path === "/api/extend/search" && method === "POST") {
      const body = (await readJson(req)) ?? {};
      const hasContext = Boolean(body.conversation_id);
      await sleep(260);
      return json(res, 200, {
        ok: true,
        provider: hasContext ? "openalex" : "bing",
        intent: hasContext ? "academic" : "general",
        queries: hasContext
          ? [
              "Transformer-BP 多目标跟踪研究框架 创新点",
              "Transformer-BP 多目标跟踪研究框架",
              "Transformer Belief Propagation BP 多目标跟踪",
            ]
          : ["RAG 最新进展 2026"],
        candidates: hasContext ? CONTEXT_CANDIDATES : WEB_CANDIDATES,
        filtered_count: 0,
        took_ms: hasContext ? 2427 : 320,
        context_used: hasContext,
        context_summary: hasContext ? CONTEXT_SUMMARY : null,
      });
    }

    if (path === "/api/extend/status") {
      return json(res, 200, {
        enabled: true,
        provider: "bing",
        label: "必应搜索",
        requires_approval: true,
        accepted_count: state.reranker.accepted,
        rejected_count: state.reranker.rejected,
        can_search: true,
      });
    }

    /* ------------------------------- MCP -------------------------------- */

    if (path === "/api/mcp/suggest" && method === "POST") {
      const body = (await readJson(req)) ?? {};
      const need = String(body.need ?? "").trim();
      if (!need) return json(res, 400, { detail: "需求描述不能为空" });
      if (/未接入|没接入|没有接入|没有模型|no-llm|离线/i.test(need)) {
        return json(res, 503, { detail: "请先在设置接入模型，或手动添加" });
      }
      if (/AI失败|故障|报错测试/i.test(need)) {
        return json(res, 502, { detail: "AI 推荐失败，请稍后重试或手动添加" });
      }
      await sleep(900);
      const picks = [];
      if (/文件|文档|目录|读取/i.test(need)) picks.push("filesystem");
      if (/sqlite|数据库|sql/i.test(need)) picks.push("sqlite");
      if (/github|仓库|issue|repo/i.test(need)) picks.push("github");
      if (/网页|抓取|fetch|爬取/i.test(need)) picks.push("fetch");
      if (/记忆|memory/i.test(need)) picks.push("memory");
      if (/git|版本/i.test(need)) picks.push("git");
      for (const id of ["filesystem", "fetch", "sqlite", "memory", "git", "github"]) {
        if (picks.length >= 2) break;
        if (!picks.includes(id)) picks.push(id);
      }
      const limit = Number(body.max_suggestions) || 2;
      const suggestions = picks
        .slice(0, Math.max(1, Math.min(limit, 3)))
        .map((id) => MCP_SUGGEST_CATALOG[id])
        .filter(Boolean);
      return json(res, 200, {
        ok: true,
        need,
        suggestions,
        notes: [
          "AI 只从受控目录推荐配置，不会自动执行或安装；启用前仍需测试连接并勾选允许的工具。",
        ],
      });
    }

    if (path === "/api/mcp/servers" && method === "GET") {
      return json(res, 200, mcpServersPayload());
    }
    if (path === "/api/mcp/status") {
      const payload = mcpServersPayload();
      return json(res, 200, { enabled_count: payload.enabled_count, tool_count: payload.tool_count });
    }

    if (path === "/api/mcp/servers" && method === "POST") {
      const body = (await readJson(req)) ?? {};
      const name = String(body.name ?? "").trim();
      const command = String(body.command ?? "").trim();
      if (!name) return json(res, 400, { detail: "名称不能为空" });
      if (!command) return json(res, 400, { detail: "启动命令不能为空" });
      const hex = Math.random().toString(16).slice(2, 14).padEnd(12, "0");
      const server = {
        id: `srv_${hex}`,
        name,
        builtin: null,
        transport: "stdio",
        command,
        args: Array.isArray(body.args) ? body.args.map((item) => String(item)) : [],
        env: body.env && typeof body.env === "object" ? { ...body.env } : {},
        allowed_tools: Array.isArray(body.allowed_tools) ? body.allowed_tools : null,
        enabled: false,
        has_token: false,
        connected: false,
        last_error: null,
        custom_tools: CUSTOM_TOOLS,
      };
      state.mcp.servers.push(server);
      const payload = mcpServersPayload().items.find((item) => item.id === server.id);
      return json(res, 200, { ok: true, error: null, server: payload });
    }

    const mcpDeleteMatch = path.match(/^\/api\/mcp\/servers\/([^/]+)$/);
    if (mcpDeleteMatch && method === "DELETE") {
      const serverId = decodeURIComponent(mcpDeleteMatch[1]);
      const target = state.mcp.servers.find((item) => item.id === serverId);
      if (!target) return json(res, 404, { detail: "MCP Server 不存在" });
      if (target.builtin) {
        return json(res, 400, { detail: "内置 MCP Server 不可删除，可在设置中关闭启用" });
      }
      state.mcp.servers = state.mcp.servers.filter((item) => item.id !== serverId);
      return json(res, 200, { ok: true, error: null, id: serverId });
    }

    const mcpMatch = path.match(/^\/api\/mcp\/servers\/([^/]+)(\/test)?$/);
    if (mcpMatch) {
      const serverId = decodeURIComponent(mcpMatch[1]);
      const target = state.mcp.servers.find((item) => item.id === serverId);
      if (!target) return json(res, 404, { detail: "MCP server 不存在" });

      if (method === "PATCH") {
        const body = (await readJson(req)) ?? {};
        if (Array.isArray(body.allowed_tools) || body.allowed_tools === null) {
          target.allowed_tools = Array.isArray(body.allowed_tools)
            ? body.allowed_tools.map((item) => String(item)).filter(Boolean)
            : null;
        }
        if (body.env && typeof body.env === "object") {
          if ("GITHUB_TOKEN" in body.env) {
            const token = body.env.GITHUB_TOKEN;
            if (token === null) target.has_token = false;
            else if (typeof token === "string" && token.length > 0) target.has_token = true;
            // 空字符串不修改
          }
        }
        const wantEnable = typeof body.enabled === "boolean" ? body.enabled : target.enabled;
        if (wantEnable && !target.builtin) {
          const allowed = Array.isArray(target.allowed_tools) ? target.allowed_tools : [];
          if (allowed.length === 0) {
            target.enabled = false;
            target.connected = false;
            const server = mcpServersPayload().items.find((item) => item.id === target.id);
            return json(res, 200, {
              ok: false,
              error: "自定义 MCP Server 必须显式配置 allowed_tools（非空）后才能启用",
              server,
            });
          }
          if (/invalid|missing/i.test(`${target.command} ${(target.args ?? []).join(" ")}`)) {
            target.enabled = false;
            target.connected = false;
            target.last_error = "连接失败，已保持关闭：无法启动 MCP Server（命令不存在或参数错误）";
            const server = mcpServersPayload().items.find((item) => item.id === target.id);
            return json(res, 200, {
              ok: false,
              error: `连接失败，已保持关闭：${target.last_error}`,
              server,
            });
          }
        }
        target.enabled = wantEnable;
        if (target.enabled) {
          if (target.builtin === "github" && !target.has_token) {
            target.connected = false;
            target.last_error = "未配置 GITHUB_TOKEN，匿名访问被 GitHub 限流（403）";
          } else {
            target.connected = true;
            target.last_error = null;
          }
        } else {
          target.connected = false;
          target.last_error = null;
        }
        const server = mcpServersPayload().items.find((item) => item.id === target.id);
        return json(res, 200, { ok: true, error: null, server });
      }

      if (method === "POST" && mcpMatch[2] === "/test") {
        await sleep(450);
        if (!target.builtin && /invalid|missing/i.test(`${target.command} ${(target.args ?? []).join(" ")}`)) {
          return json(res, 200, {
            ok: false,
            error: "连接失败：无法启动 MCP Server（命令不存在或参数错误）",
            tools: [],
          });
        }
        if (target.builtin === "github" && !target.has_token) {
          return json(res, 200, {
            ok: false,
            error: "GitHub 连接失败：未配置 GITHUB_TOKEN，匿名请求被限流（403）",
            tools: [],
          });
        }
        const tools = target.builtin
          ? BUILTIN_TOOLS[target.builtin] ?? []
          : target.custom_tools ?? CUSTOM_TOOLS;
        return json(res, 200, {
          ok: true,
          tools,
          tool_count: tools.length,
          elapsed_ms: 420,
        });
      }
    }

    /* ----------------------------- Embedding ---------------------------- */

    if (path === "/api/admin/embedding/status") {
      return json(res, 200, embeddingStatusPayload());
    }
    if (path === "/api/admin/embedding/config" && method === "POST") {
      const body = (await readJson(req)) ?? {};
      const provider = String(body.provider ?? state.embedding.provider);
      if (!["hash", "fastembed", "openai"].includes(provider)) {
        return json(res, 400, { detail: "不支持的 embedding provider" });
      }
      const previous = { provider: state.embedding.provider, model: state.embedding.model, dim: state.embedding.dim };
      state.embedding.provider = provider;
      if (provider === "hash") {
        state.embedding.model = "hash-local";
        state.embedding.dim = 384;
        state.embedding.base_url = "";
      } else if (provider === "fastembed") {
        state.embedding.model = String(body.model ?? "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2");
        state.embedding.dim = state.embedding.model.includes("bge") ? 512 : 384;
      } else {
        state.embedding.model = String(body.model ?? "text-embedding-3-small");
        state.embedding.dim = 1536;
        state.embedding.base_url = String(body.base_url ?? "");
        if (typeof body.api_key === "string" && body.api_key.length > 0) state.embedding.has_api_key = true;
      }
      state.embedding.needs_rebuild =
        previous.provider !== state.embedding.provider ||
        previous.model !== state.embedding.model ||
        previous.dim !== state.embedding.dim;
      return json(res, 200, embeddingStatusPayload());
    }
    if (path === "/api/admin/embedding/test" && method === "POST") {
      await sleep(380);
      return json(res, 200, {
        ok: true,
        provider: state.embedding.provider,
        dim: state.embedding.dim,
        similarity: 0.873,
        message: "两条测试文本语义相似度 87.3%",
      });
    }
    if (path === "/api/admin/embedding/rebuild" && method === "POST") {
      await sleep(1300);
      state.embedding.needs_rebuild = false;
      state.embedding.last_rebuild = nowIso();
      return json(res, 200, {
        ok: true,
        chunks: state.embedding.total_chunks,
        seconds: 34.2,
        provider: state.embedding.provider,
        dim: state.embedding.dim,
      });
    }

    /* ------------------------------ Reranker ---------------------------- */

    if (path === "/api/reranker/summary") {
      return json(res, 200, { ...state.reranker });
    }

    /* ------------------------------- Patrol ----------------------------- */

    if (path === "/api/patrol/status") {
      return json(res, 200, patrolStatusPayload());
    }
    if (path === "/api/patrol/config" && method === "PATCH") {
      const body = (await readJson(req)) ?? {};
      if (typeof body.enabled === "boolean") state.patrol.enabled = body.enabled;
      if (typeof body.auto_enrich === "boolean") state.patrol.auto_enrich = body.auto_enrich;
      if (Number.isFinite(body.interval_hours)) {
        const hours = Number(body.interval_hours);
        if (hours < 1 || hours > 168) return json(res, 400, { detail: "巡检周期需要在 1–168 小时之间" });
        state.patrol.interval_hours = hours;
        state.patrol.next_run_at = hoursLater(hours);
      }
      if (Number.isFinite(body.max_enrich)) {
        const count = Number(body.max_enrich);
        if (count < 1 || count > 12) return json(res, 400, { detail: "每次最多整理 1–12 篇" });
        state.patrol.max_enrich = count;
      }
      if (state.patrol.enabled && !state.patrol.next_run_at) {
        state.patrol.next_run_at = nowIso();
      }
      return json(res, 200, patrolStatusPayload());
    }
    if (path === "/api/patrol/run" && method === "POST") {
      if (state.patrol.running) return json(res, 409, { detail: "巡检正在运行中，请稍后再试" });
      state.patrol.running = true;
      await sleep(1400);
      state.patrol.running = false;
      const startedAt = new Date(Date.now() - 1400).toISOString();
      const enriched = [
        { id: "doc-31", title: "KV Cache 优化笔记", ok: true },
        { id: "doc-32", title: "Agent 工具调用综述", ok: true },
        { id: "doc-33", title: "向量数据库选型对比", ok: false, error: "模型超时，下次重试" },
      ];
      const issues = {
        pending_docs: { count: 2, sample: [{ id: "doc-33", title: "向量数据库选型对比" }] },
        duplicate_docs: {
          count: 2,
          pairs: [
            {
              a: { id: "doc-21", title: "RAG 检索增强生成综述" },
              b: { id: "doc-24", title: "RAG 检索增强生成综述（副本）" },
              similarity: 0.95,
            },
          ],
        },
        stale_docs: { count: 3, sample: [{ id: "doc-11", title: "2024 年度技术趋势" }] },
        orphan_docs: { count: 4, sample: [{ id: "doc-12", title: "未归档的会议纪要" }] },
        small_docs: { count: 6, sample: [{ id: "doc-13", title: "随手记录：向量维度选择" }] },
        missing_tags: { count: 8, sample: [{ id: "doc-14", title: "临时笔记：Rerank 实验" }] },
      };
      const result = {
        ok: true,
        started_at: startedAt,
        finished_at: nowIso(),
        duration_ms: 12300,
        trigger: "manual",
        enriched,
        graph_rebuilt: true,
        issues,
        summary: `巡检完成：自动整理 ${enriched.filter((doc) => doc.ok).length} 篇待整理文档，发现 2 篇待整理、2 组重复标题、3 篇过时、4 篇未分区、6 篇过短、8 篇无标签。`,
      };
      state.patrol.last_result = result;
      state.patrol.last_run_at = result.finished_at;
      state.patrol.next_run_at = hoursLater(state.patrol.interval_hours);
      state.patrol.counts = {
        ...state.patrol.counts,
        pending_docs: 2,
        enriched_documents: Math.min(state.patrol.counts.total_documents, state.patrol.counts.enriched_documents + 3),
      };
      return json(res, 200, { ok: true, result, status: patrolStatusPayload() });
    }

    /* -------------------------------- 会话 ------------------------------ */

    if (path === "/api/conversations" && method === "GET") {
      return json(res, 200, {
        items: state.conversations.map((item) => ({
          id: item.id,
          title: item.title,
          updated_at: item.updated_at,
          message_count: item.messages.length,
        })),
      });
    }
    const convMatch = path.match(/^\/api\/conversations\/([^/]+)$/);
    if (convMatch) {
      const conversation = state.conversations.find((item) => item.id === decodeURIComponent(convMatch[1]));
      if (!conversation) return json(res, 404, { detail: "会话不存在" });
      if (method === "DELETE") {
        state.conversations = state.conversations.filter((item) => item !== conversation);
        return json(res, 200, { ok: true });
      }
      return json(res, 200, {
        id: conversation.id,
        title: conversation.title,
        created_at: conversation.updated_at,
        updated_at: conversation.updated_at,
        messages: conversation.messages,
      });
    }
    if (path === "/api/chat" && method === "POST") {
      return handleChat(req, res);
    }

    /* -------------------------------- 图谱 ------------------------------ */

    if (path === "/api/graph") {
      const mode = url.searchParams.get("mode") ?? "global";
      if (mode === "zones") {
        return json(res, 200, {
          nodes: [
            { id: "zone-a", type: "document", label: "RAG 与检索", weight: 8, degree: 2, zone_id: "zone-1", zone_name: "AI 工程", zone_color: "#4fd8ff" },
            { id: "zone-b", type: "document", label: "知识图谱", weight: 6, degree: 2, zone_id: "zone-1", zone_name: "AI 工程", zone_color: "#4fd8ff" },
          ],
          links: [{ id: "link-z", source: "zone-a", target: "zone-b", type: "related", weight: 2 }],
          meta: { node_count: 2, link_count: 1, document_count: 2, zone_count: 1, generated_at: nowIso() },
          zones: [
            { id: "zone-1", name: "AI 工程", color: "#4fd8ff", document_count: 2, node_count: 2, link_count: 1 },
            { id: null, name: "未分区", color: "#5b6f8c", document_count: 0, node_count: 0, link_count: 0 },
          ],
        });
      }
      return json(res, 200, {
        nodes: [
          { id: "doc:1", type: "document", label: "RAG 检索增强生成综述", weight: 9, degree: 2, doc_id: "doc-21", tags: ["RAG", "检索"], zone_id: "zone-1", zone_name: "AI 工程", zone_color: "#4fd8ff" },
          { id: "doc:2", type: "document", label: "知识图谱构建实践", weight: 7, degree: 2, doc_id: "doc-23", tags: ["知识图谱"], zone_id: "zone-1", zone_name: "AI 工程", zone_color: "#4fd8ff" },
          { id: "concept:rag", type: "concept", label: "RAG", weight: 8, degree: 2 },
          { id: "concept:graph", type: "concept", label: "知识图谱", weight: 6, degree: 2 },
          { id: "tag:ai", type: "tag", label: "AI 工程", weight: 5, degree: 2 },
        ],
        links: [
          { id: "l1", source: "doc:1", target: "concept:rag", type: "mentions", weight: 3 },
          { id: "l2", source: "doc:2", target: "concept:graph", type: "mentions", weight: 3 },
          { id: "l3", source: "doc:1", target: "tag:ai", type: "tagged", weight: 2 },
          { id: "l4", source: "doc:2", target: "tag:ai", type: "tagged", weight: 2 },
          { id: "l5", source: "concept:rag", target: "concept:graph", type: "cooccur", weight: 2 },
        ],
        meta: { node_count: 5, link_count: 5, document_count: 2, zone_count: 1, generated_at: nowIso() },
        zones: [
          { id: "zone-1", name: "AI 工程", color: "#4fd8ff", document_count: 2, node_count: 5, link_count: 5 },
          { id: null, name: "未分区", color: "#5b6f8c", document_count: 0, node_count: 0, link_count: 0 },
        ],
      });
    }
    if (path === "/api/graph/node") {
      return json(res, 200, {
        node: { id: "doc:1", type: "document", label: "RAG 检索增强生成综述", weight: 9, degree: 2, doc_id: "doc-21" },
        docs: [{ id: "doc-21", title: "RAG 检索增强生成综述", tags: ["RAG"], summary: "综述" }],
        chunks: [{ id: "chunk-201", doc_id: "doc-21", doc_title: "RAG 检索增强生成综述", text: "检索增强生成……" }],
        related: [
          { id: "concept:rag", label: "RAG", type: "concept", weight: 8 },
          { id: "tag:ai", label: "AI 工程", type: "tag", weight: 5 },
        ],
      });
    }
    if (path === "/api/zones") {
      return json(res, 200, {
        items: [
          { id: "zone-1", name: "AI 工程", color: "#4fd8ff", description: "AI 工程与检索", sort_order: 0, document_count: 2, created_at: hoursAgo(48), updated_at: hoursAgo(24) },
        ],
        unassigned_count: 0,
      });
    }

    if (path === "/api/documents") {
      return json(res, 200, {
        items: [
          { id: "doc-21", title: "RAG 检索增强生成综述", filename: "rag.md", source_type: "md", source_url: null, tags: ["RAG"], summary: "综述", size: 12000, chunk_count: 12, concept_count: 8, status: "ready", error: null, created_at: hoursAgo(72), updated_at: hoursAgo(24) },
        ],
        total: 1,
      });
    }

    return json(res, 404, { detail: `mock api 未实现：${method} ${path}` });
  } catch (error) {
    console.error("[mock] 处理请求失败：", method, path, error);
    if (!res.headersSent) return json(res, 500, { detail: "mock server error" });
    res.end();
  }
});

server.listen(PORT, HOST, () => {
  console.log(`[mock] Synapse V2 mock API 已启动：http://${HOST}:${PORT}`);
  console.log(`[mock] Vite 示例：VITE_PROXY_TARGET=http://${HOST}:${PORT} npm run dev -- --port 5174`);
});
