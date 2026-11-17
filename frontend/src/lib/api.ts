import type {
  ChatRequest,
  ChatStreamEvent,
  ChunkList,
  ConversationDetail,
  ConversationList,
  DocumentItem,
  DocumentList,
  EmbeddingConfigInput,
  EmbeddingRebuildResult,
  EmbeddingStatus,
  EmbeddingTestResult,
  ExtendCommitItem,
  ExtendCommitResponse,
  ExtendRejectResponse,
  ExtendSearchInput,
  ExtendSearchResponse,
  EnrichRequest,
  EnrichResult,
  EnrichStatus,
  ExtendStatus,
  GraphData,
  GraphNodeDetail,
  Health,
  McpServerCreateInput,
  McpServerCreateResponse,
  McpServerDeleteResponse,
  McpServerMutationResult,
  McpServerUpdate,
  McpServersResponse,
  McpSuggestResponse,
  McpTestResult,
  PatrolConfigInput,
  PatrolRunResponse,
  PatrolStatus,
  RerankerSummary,
  ReseedResult,
  SearchResponse,
  SettingsData,
  SettingsTestResult,
  SettingsUpdate,
  CheckinDeleteResponse,
  CheckinListResponse,
  CheckinResponse,
  Plan,
  PlanGroup,
  PlanGroupInput,
  PlanGroupResponse,
  PlanGroupUpdateInput,
  PlanGroupsResponse,
  PlanInput,
  PlanKind,
  PlanResponse,
  PlansListResponse,
  PlansOverview,
  PlanUpdateInput,
  PlanCheckin,
  Stats,
  UploadResponse,
  Zone,
  ZoneAssignResult,
  ZoneList,
} from "../types";

const API_BASE = "/api";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function fallbackMessage(status: number): string {
  if (status === 400) return "请求内容不合法，请检查后重试。";
  if (status === 404) return "请求的内容不存在。";
  if (status === 413) return "文件过大，无法处理。";
  if (status === 422) return "提交的内容格式不正确。";
  if (status >= 500) return "服务内部出错，请稍后重试。";
  return `请求失败（HTTP ${status}）。`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, init);
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new ApiError(0, "无法连接到知脉服务，请确认后端已经启动。");
  }

  if (!response.ok) {
    let detail = "";
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload?.detail === "string") detail = payload.detail;
    } catch {
      /* 响应体不是 JSON，使用兜底文案 */
    }
    throw new ApiError(response.status, detail || fallbackMessage(response.status));
  }

  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(response.status, "服务返回了无法解析的数据。");
  }
}

function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

/** 将任意异常转换成用户可读的中文提示。 */
export function toUserMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) {
    if (isAbortError(error)) return "请求已取消。";
    if (/Failed to fetch|NetworkError|Load failed/i.test(error.message)) {
      return "无法连接到知脉服务，请确认后端已经启动。";
    }
    return error.message;
  }
  return "发生了未知错误，请稍后重试。";
}

/* ---------------------------------- 元信息 --------------------------------- */

export function getHealth(): Promise<Health> {
  return request<Health>("/health");
}

export function getStats(): Promise<Stats> {
  return request<Stats>("/stats");
}

/* ---------------------------------- 文档 ---------------------------------- */

export function listDocuments(params?: {
  q?: string;
  tag?: string;
  limit?: number;
  offset?: number;
}): Promise<DocumentList> {
  const query = new URLSearchParams();
  if (params?.q) query.set("q", params.q);
  if (params?.tag) query.set("tag", params.tag);
  if (params?.limit != null) query.set("limit", String(params.limit));
  if (params?.offset != null) query.set("offset", String(params.offset));
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<DocumentList>(`/documents${suffix}`);
}

export function getDocument(id: string): Promise<DocumentItem> {
  return request<DocumentItem>(`/documents/${encodeURIComponent(id)}`);
}

export function getDocumentChunks(id: string, limit = 50): Promise<ChunkList> {
  return request<ChunkList>(`/documents/${encodeURIComponent(id)}/chunks?limit=${limit}`);
}

export function deleteDocument(id: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/documents/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function uploadDocuments(files: File[], tags?: string): Promise<UploadResponse> {
  const form = new FormData();
  for (const file of files) form.append("files", file);
  if (tags) form.append("tags", tags);
  return request<UploadResponse>("/documents/upload", { method: "POST", body: form });
}

export function addTextDocument(input: {
  title: string;
  content: string;
  tags: string[];
  source_type: string;
}): Promise<{ ok: boolean; document: DocumentItem }> {
  return request<{ ok: boolean; document: DocumentItem }>(
    "/documents/text",
    jsonInit("POST", input),
  );
}

export function addUrlDocument(input: {
  url: string;
  title: string | null;
  tags: string[];
}): Promise<{ ok: boolean; document: DocumentItem }> {
  return request<{ ok: boolean; document: DocumentItem }>("/documents/url", jsonInit("POST", input));
}

/* ---------------------------------- 检索 ---------------------------------- */

export function search(input: {
  query: string;
  top_k?: number;
  doc_ids?: string[] | null;
}): Promise<SearchResponse> {
  return request<SearchResponse>("/search", jsonInit("POST", input));
}

/* ---------------------------------- 会话 ---------------------------------- */

export function listConversations(): Promise<ConversationList> {
  return request<ConversationList>("/conversations");
}

export function getConversation(id: string): Promise<ConversationDetail> {
  return request<ConversationDetail>(`/conversations/${encodeURIComponent(id)}`);
}

export function deleteConversation(id: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/conversations/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

/* ---------------------------------- 聊天 ---------------------------------- */

function parseSseBlock(block: string): ChatStreamEvent | null {
  let eventName = "";
  const dataLines: string[] = [];
  for (const rawLine of block.split("\n")) {
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      eventName = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (!eventName && dataLines.length === 0) return null;
  const rawData = dataLines.join("\n");
  let payload: unknown = null;
  if (rawData) {
    try {
      payload = JSON.parse(rawData);
    } catch {
      payload = null;
    }
  }

  switch (eventName) {
    case "meta":
    case "sources":
    case "gap":
    case "tool":
    case "web_sources":
    case "delta":
    case "notice":
    case "done":
    case "error":
      return payload ? ({ type: eventName, data: payload } as ChatStreamEvent) : null;
    default:
      return null;
  }
}

/**
 * 通过 fetch + ReadableStream 手写解析 SSE；支持 AbortController 中断。
 */
export async function streamChat(
  body: ChatRequest,
  onEvent: (event: ChatStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new ApiError(0, "无法连接到知脉服务，请确认后端已经启动。");
  }

  if (!response.ok) {
    let detail = "";
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload?.detail === "string") detail = payload.detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(response.status, detail || fallbackMessage(response.status));
  }
  if (!response.body) {
    throw new ApiError(0, "当前浏览器不支持流式回答。");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  const dispatch = (block: string): void => {
    const event = parseSseBlock(block);
    if (event) onEvent(event);
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let separatorIndex = buffer.search(/\r?\n\r?\n/);
      while (separatorIndex !== -1) {
        const match = buffer.slice(separatorIndex).match(/^\r?\n\r?\n/);
        const sepLength = match ? match[0].length : 2;
        const block = buffer.slice(0, separatorIndex);
        buffer = buffer.slice(separatorIndex + sepLength);
        dispatch(block);
        separatorIndex = buffer.search(/\r?\n\r?\n/);
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) dispatch(buffer);
  } finally {
    reader.releaseLock();
  }
}

/* ---------------------------------- 图谱 ---------------------------------- */

export function getGraph(params?: {
  mode?: "global" | "zones";
  max_concepts?: number;
  min_link_weight?: number;
  min_concept_degree?: number;
}): Promise<GraphData> {
  const query = new URLSearchParams();
  if (params?.mode === "zones") query.set("mode", "zones");
  if (params?.max_concepts != null) query.set("max_concepts", String(params.max_concepts));
  if (params?.min_link_weight != null) query.set("min_link_weight", String(params.min_link_weight));
  if (params?.min_concept_degree != null) {
    query.set("min_concept_degree", String(params.min_concept_degree));
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<GraphData>(`/graph${suffix}`);
}

export function getGraphNode(id: string, zoneId?: string): Promise<GraphNodeDetail> {
  const query = new URLSearchParams({ id });
  if (zoneId) query.set("zone_id", zoneId);
  return request<GraphNodeDetail>(`/graph/node?${query.toString()}`);
}

/* ------------------------------ 分区（Zones） ----------------------------- */

export function listZones(): Promise<ZoneList> {
  return request<ZoneList>("/zones");
}

export function createZone(input: {
  name: string;
  color?: string;
  description?: string | null;
}): Promise<{ zone: Zone }> {
  return request<{ zone: Zone }>("/zones", jsonInit("POST", input));
}

export function updateZone(
  zoneId: string,
  input: { name?: string; color?: string; description?: string | null },
): Promise<{ zone: Zone }> {
  return request<{ zone: Zone }>(`/zones/${encodeURIComponent(zoneId)}`, jsonInit("PUT", input));
}

export function deleteZone(zoneId: string): Promise<{ ok: boolean; unassigned_count: number }> {
  return request<{ ok: boolean; unassigned_count: number }>(
    `/zones/${encodeURIComponent(zoneId)}`,
    { method: "DELETE" },
  );
}

export function assignDocumentsZone(input: {
  doc_ids: string[];
  zone_id: string | null;
}): Promise<ZoneAssignResult> {
  return request<ZoneAssignResult>("/documents/zone", jsonInit("PUT", input));
}

/* ---------------------------------- 设置 ---------------------------------- */

export function getSettings(): Promise<SettingsData> {
  return request<SettingsData>("/settings");
}

export function updateSettings(body: SettingsUpdate): Promise<SettingsData> {
  return request<SettingsData>("/settings", jsonInit("PUT", body));
}

export function testSettings(): Promise<SettingsTestResult> {
  return request<SettingsTestResult>("/settings/test", { method: "POST" });
}

export function reseedData(): Promise<ReseedResult> {
  return request<ReseedResult>("/admin/reseed", jsonInit("POST", { force: true }));
}

/* ----------------------------- 知识库智能整理 ----------------------------- */

export function getEnrichStatus(): Promise<EnrichStatus> {
  return request<EnrichStatus>("/admin/enrich/status");
}

export function enrichDocuments(input: EnrichRequest): Promise<EnrichResult> {
  return request<EnrichResult>("/admin/enrich", jsonInit("POST", input));
}

/* ------------------------------ 计划与打卡 -------------------------------- */

export function listPlans(
  includeArchived = false,
  kind: PlanKind = "all",
): Promise<PlansListResponse> {
  const query = new URLSearchParams();
  if (includeArchived) query.set("include_archived", "1");
  if (kind !== "all") query.set("kind", kind);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<PlansListResponse>(`/plans${suffix}`);
}

export function listPlanGroups(): Promise<PlanGroupsResponse> {
  return request<PlanGroupsResponse>("/plan-groups");
}

export function createPlanGroup(input: PlanGroupInput): Promise<PlanGroupResponse> {
  return request<PlanGroupResponse>("/plan-groups", jsonInit("POST", input));
}

export function updatePlanGroup(
  groupId: string,
  input: PlanGroupUpdateInput,
): Promise<PlanGroupResponse> {
  return request<PlanGroupResponse>(
    `/plan-groups/${encodeURIComponent(groupId)}`,
    jsonInit("PUT", input),
  );
}

export function deletePlanGroup(
  groupId: string,
): Promise<{ ok: boolean; ungrouped_count: number }> {
  return request<{ ok: boolean; ungrouped_count: number }>(
    `/plan-groups/${encodeURIComponent(groupId)}`,
    { method: "DELETE" },
  );
}

export function getPlan(id: string): Promise<PlanResponse> {
  return request<PlanResponse>(`/plans/${encodeURIComponent(id)}`);
}

export function createPlan(input: PlanInput): Promise<PlanResponse> {
  return request<PlanResponse>("/plans", jsonInit("POST", input));
}

export function updatePlan(id: string, input: PlanUpdateInput): Promise<PlanResponse> {
  return request<PlanResponse>(`/plans/${encodeURIComponent(id)}`, jsonInit("PUT", input));
}

export function deletePlan(id: string): Promise<{ ok: boolean; deleted_checkins: number }> {
  return request<{ ok: boolean; deleted_checkins: number }>(`/plans/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function getPlansOverview(days = 120): Promise<PlansOverview> {
  return request<PlansOverview>(`/plans/overview?days=${days}`);
}

export function listCheckins(
  planId: string,
  range?: { start?: string; end?: string },
): Promise<CheckinListResponse> {
  const query = new URLSearchParams();
  if (range?.start) query.set("start", range.start);
  if (range?.end) query.set("end", range.end);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<CheckinListResponse>(`/plans/${encodeURIComponent(planId)}/checkins${suffix}`);
}

export function addCheckin(
  planId: string,
  input: { date?: string; note?: string | null },
): Promise<CheckinResponse> {
  return request<CheckinResponse>(
    `/plans/${encodeURIComponent(planId)}/checkins`,
    jsonInit("POST", input),
  );
}

export function removeCheckin(
  planId: string,
  date: string,
): Promise<CheckinDeleteResponse> {
  return request<CheckinDeleteResponse>(
    `/plans/${encodeURIComponent(planId)}/checkins/${encodeURIComponent(date)}`,
    { method: "DELETE" },
  );
}

export type { Plan, PlanCheckin, PlanGroup };

/* ------------------------------ 联网知识扩展 ------------------------------ */

export function getExtendStatus(): Promise<ExtendStatus> {
  return request<ExtendStatus>("/extend/status");
}

export function extendSearch(input: ExtendSearchInput): Promise<ExtendSearchResponse> {
  return request<ExtendSearchResponse>("/extend/search", jsonInit("POST", input));
}

export function extendCommit(
  items: ExtendCommitItem[],
  zoneId: string | null,
): Promise<ExtendCommitResponse> {
  return request<ExtendCommitResponse>(
    "/extend/commit",
    jsonInit("POST", { items, zone_id: zoneId }),
  );
}

export function extendReject(
  items: Array<{ url: string; title?: string }>,
  query: string,
): Promise<ExtendRejectResponse> {
  return request<ExtendRejectResponse>("/extend/reject", jsonInit("POST", { items, query }));
}

/* ------------------------------ V2：MCP 工具 ------------------------------ */

export function getMcpServers(): Promise<McpServersResponse> {
  return request<McpServersResponse>("/mcp/servers");
}

export function updateMcpServer(
  serverId: string,
  input: McpServerUpdate,
): Promise<McpServerMutationResult> {
  return request<McpServerMutationResult>(
    `/mcp/servers/${encodeURIComponent(serverId)}`,
    jsonInit("PATCH", input),
  );
}

export function testMcpServer(serverId: string): Promise<McpTestResult> {
  return request<McpTestResult>(`/mcp/servers/${encodeURIComponent(serverId)}/test`, {
    method: "POST",
  });
}

export function createMcpServer(input: McpServerCreateInput): Promise<McpServerCreateResponse> {
  return request<McpServerCreateResponse>("/mcp/servers", jsonInit("POST", input));
}

export function deleteMcpServer(serverId: string): Promise<McpServerDeleteResponse> {
  return request<McpServerDeleteResponse>(
    `/mcp/servers/${encodeURIComponent(serverId)}`,
    { method: "DELETE" },
  );
}

/** AI 帮我配：让后端从受控目录推荐 MCP Server 配置（不执行命令）。 */
export function suggestMcpServers(input: {
  need: string;
  max_suggestions?: number;
}): Promise<McpSuggestResponse> {
  return request<McpSuggestResponse>("/mcp/suggest", jsonInit("POST", input));
}

/* ------------------------------ V2：语义向量 ------------------------------ */

export function getEmbeddingStatus(): Promise<EmbeddingStatus> {
  return request<EmbeddingStatus>("/admin/embedding/status");
}

export function updateEmbeddingConfig(input: EmbeddingConfigInput): Promise<EmbeddingStatus> {
  return request<EmbeddingStatus>("/admin/embedding/config", jsonInit("POST", input));
}

export function testEmbedding(): Promise<EmbeddingTestResult> {
  return request<EmbeddingTestResult>("/admin/embedding/test", { method: "POST" });
}

export function rebuildEmbeddings(): Promise<EmbeddingRebuildResult> {
  return request<EmbeddingRebuildResult>("/admin/embedding/rebuild", { method: "POST" });
}

/* --------------------------- V2：反馈偏好 Reranker ------------------------- */

export function getRerankerSummary(): Promise<RerankerSummary> {
  return request<RerankerSummary>("/reranker/summary");
}

/* ------------------------------ V2：知识巡检 ------------------------------- */

export function getPatrolStatus(): Promise<PatrolStatus> {
  return request<PatrolStatus>("/patrol/status");
}

export function updatePatrolConfig(input: PatrolConfigInput): Promise<PatrolStatus | Record<string, unknown>> {
  return request("/patrol/config", jsonInit("PATCH", input));
}

export function runPatrol(): Promise<PatrolRunResponse> {
  return request<PatrolRunResponse>("/patrol/run", { method: "POST" });
}
