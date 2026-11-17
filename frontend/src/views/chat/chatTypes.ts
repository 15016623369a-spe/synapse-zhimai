import type {
  ChatGapInfo,
  ChatMode,
  ChatToolResult,
  ChatToolStatus,
  Citation,
  ReferenceDocument,
  WebSourceCandidate,
} from "../../types";

/** 对话中一次工具调用的 UI 模型（由 SSE tool 事件 start/end 合并而来）。 */
export interface ChatToolCall {
  id: string;
  /** 工具全名，可能是 search_web 或 mcp_{server}_{tool}。 */
  name: string;
  args?: Record<string, unknown> | null;
  /** 兼容 search_web 的 query 字段。 */
  query?: string;
  status: "running" | "ok" | "error";
  summary?: string;
  count?: number;
  error?: string;
}

/** 对话页 UI 消息模型（在 API Message 基础上补充流式状态字段）。 */
export interface UiMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  createdAt: string;
  mode?: ChatMode | null;
  streaming?: boolean;
  stopped?: boolean;
  error?: string | null;
  notice?: string | null;
  reference?: ReferenceDocument | null;
  gap?: ChatGapInfo | null;
  /** 兼容旧的单一工具状态（search_web）；V2 展示以 tools 为准。 */
  tool?: ChatToolStatus | null;
  /** 本轮工具调用列表（MCP / 联网搜索通用）。 */
  tools?: ChatToolCall[];
  webSources?: WebSourceCandidate[];
  /** V2 多轮上下文：本轮回答是否结合了上文（继承文档/追问）。 */
  contextUsed?: boolean;
  contextSummary?: string | null;
}

export interface StreamState {
  assistantId: string;
  conversationId: string | null;
  buffer: string;
  content: string;
  timer: number | null;
  pendingDone: {
    message_id: string;
    conversation_id: string;
    citations: Citation[];
    mode: ChatMode;
    title: string;
    gap?: import("../../types").ChatGapInfo | null;
    web_sources?: import("../../types").WebSourceCandidate[];
    mcp_results?: ChatToolResult[];
    reference?: import("../../types").ReferenceDocument | null;
    context_used?: boolean;
    context_summary?: string | null;
    context_diagnostics?: Record<string, unknown> | null;
  } | null;
  pendingError: string | null;
  notice: string | null;
  mode: ChatMode | null;
  gap: ChatGapInfo | null;
  webSources: WebSourceCandidate[];
  contextUsed: boolean;
  contextSummary: string | null;
  tool: ChatToolStatus | null;
  tools: ChatToolCall[];
  stopped: boolean;
  finalized: boolean;
  eof: boolean;
  userText: string;
  docIds: string[] | null;
}
