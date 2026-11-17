/** 与 SPEC 第 7 节 API 契约严格对应的类型定义。 */

export type ChatMode = "llm" | "extractive";

export interface LlmStatus {
  configured: boolean;
  model: string;
  base_url: string;
}

export interface EmbeddingInfo {
  provider: string;
  dim: number;
}

export interface Health {
  status: string;
  version: string;
  search?: { provider: string; enabled: boolean };
  documents: number;
  chunks: number;
  concepts: number;
  llm: LlmStatus;
  embedding: EmbeddingInfo;
  /** V2：扫描版 PDF OCR 状态（旧后端可能没有该字段）。 */
  ocr?: OcrStatus | null;
}

export interface DocumentItem {
  id: string;
  title: string;
  filename: string | null;
  source_type: string;
  source_url: string | null;
  tags: string[];
  summary: string | null;
  size: number;
  chunk_count: number;
  concept_count: number;
  status: string;
  error: string | null;
  zone_id?: string | null;
  zone_name?: string | null;
  created_at: string;
  updated_at: string;
}

export interface DocumentList {
  items: DocumentItem[];
  total: number;
}

export interface DocumentChunk {
  id: string;
  doc_id: string;
  idx: number;
  text: string;
  char_start: number | null;
  char_end: number | null;
}

export interface ChunkList {
  items: DocumentChunk[];
}

export interface Citation {
  index: number;
  doc_id: string;
  doc_title: string;
  chunk_id: string;
  source_type: string;
  snippet: string;
  score: number;
}

export interface SearchHit {
  chunk_id: string;
  doc_id: string;
  doc_title: string;
  text: string;
  snippet: string;
  score: number;
  source_type: string;
}

export interface SearchResponse {
  query: string;
  results: SearchHit[];
}

export interface ConversationSummary {
  id: string;
  title: string;
  updated_at: string;
  message_count: number;
}

export interface ConversationList {
  items: ConversationSummary[];
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  created_at: string;
}

export interface ConversationDetail {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  messages: Message[];
}

export interface ChatRequest {
  message: string;
  conversation_id: string | null;
  doc_ids: string[] | null;
  top_k: number;
}

export interface ReferenceDocument {
  doc_id: string;
  title: string;
  filename?: string | null;
  source_type?: string;
}

export interface ChatMetaEvent {
  conversation_id: string;
  user_message_id: string;
  mode: ChatMode;
  reference?: ReferenceDocument | null;
  /** V2 多轮上下文：是否结合了上文/继承文档。 */
  context_used?: boolean;
  context_summary?: string | null;
}

export interface ChatSourcesEvent {
  citations: Citation[];
}

export interface ChatGapInfo {
  needs_more: boolean;
  reason: string;
  suggested_queries?: string[];
}

export interface ChatToolStatus {
  phase: "start" | "end";
  name: string;
  /** 兼容旧 search_web 工具事件。 */
  query?: string;
  count?: number;
  /** V2 通用工具事件：参数 / 成功标记 / 结果摘要。 */
  args?: Record<string, unknown> | null;
  ok?: boolean;
  summary?: string;
  error?: string | null;
}

export interface ChatToolResult {
  name: string;
  args?: Record<string, unknown> | null;
  ok?: boolean;
  text?: string;
}

export type CandidateAuthority =
  | "academic"
  | "official"
  | "wiki"
  | "media"
  | "community"
  | "unknown";

export interface WebSourceCandidate {
  url: string;
  title: string;
  snippet: string;
  source: string;
  already_in_kb?: boolean;
  rejected_before?: boolean;
  /** 0–1 相关度（新扩展接口返回）。 */
  relevance?: number;
  /** 后端可能返回字符串类别，也可能返回数值权威分。 */
  authority?: CandidateAuthority | string | number;
  authority_label?: string;
  authority_category?: CandidateAuthority | string;
  arxiv_id?: string;
  pdf_url?: string | null;
  /** V2 反馈偏好：-1..1，负数表示你曾忽略过该来源。 */
  feedback_score?: number;
  /** V2 反馈偏好中文解释，空字符串表示无信号。 */
  feedback_label?: string;
}

export interface ChatWebSourcesEvent {
  provider: string;
  queries: string[];
  candidates: WebSourceCandidate[];
}

export interface ChatDeltaEvent {
  text: string;
}

export interface ChatNoticeEvent {
  message: string;
  mode: ChatMode;
}

export interface ChatDoneEvent {
  message_id: string;
  conversation_id: string;
  citations: Citation[];
  mode: ChatMode;
  title: string;
  gap?: ChatGapInfo | null;
  web_sources?: WebSourceCandidate[];
  /** V2：本轮 MCP 工具调用结果（可能缺失）。 */
  mcp_results?: ChatToolResult[];
  /** V2：追问继承上文文档时返回该文档，可作为 meta.reference 的兜底。 */
  reference?: ReferenceDocument | null;
  /** V2 多轮上下文：是否结合了上文/继承文档。 */
  context_used?: boolean;
  context_summary?: string | null;
  /** 仅调试用（history/context token 诊断），前端不展示。 */
  context_diagnostics?: Record<string, unknown> | null;
}

export interface ChatErrorEvent {
  message: string;
}

export type ChatStreamEvent =
  | { type: "meta"; data: ChatMetaEvent }
  | { type: "sources"; data: ChatSourcesEvent }
  | { type: "gap"; data: ChatGapInfo }
  | { type: "tool"; data: ChatToolStatus }
  | { type: "web_sources"; data: ChatWebSourcesEvent }
  | { type: "delta"; data: ChatDeltaEvent }
  | { type: "notice"; data: ChatNoticeEvent }
  | { type: "done"; data: ChatDoneEvent }
  | { type: "error"; data: ChatErrorEvent };

export type GraphNodeType = "document" | "concept" | "tag";

export interface GraphNode {
  id: string;
  type: GraphNodeType;
  label: string;
  weight: number;
  degree: number;
  doc_id?: string;
  tags?: string[];
  summary?: string;
  zone_id?: string | null;
  zone_name?: string | null;
  zone_color?: string | null;
}

export interface GraphZoneMeta {
  id: string | null;
  name: string;
  color: string;
  document_count: number;
  node_count: number;
  link_count: number;
}

export interface GraphLink {
  id: string;
  source: string;
  target: string;
  type: "mentions" | "tagged" | "related" | "cooccur" | string;
  weight: number;
}

export interface GraphMeta {
  node_count: number;
  link_count: number;
  document_count: number;
  zone_count?: number;
  generated_at: string;
}

export interface GraphData {
  nodes: GraphNode[];
  links: GraphLink[];
  meta: GraphMeta;
  zones?: GraphZoneMeta[];
}

export interface Zone {
  id: string;
  name: string;
  color: string;
  description: string | null;
  sort_order: number;
  document_count: number;
  created_at: string;
  updated_at: string;
}

export interface ZoneList {
  items: Zone[];
  unassigned_count: number;
}

export interface ZoneAssignResult {
  ok: boolean;
  updated: number;
  skipped: number;
  zone: { id: string; name: string } | null;
}

export interface GraphNodeDoc {
  id: string;
  title: string;
  tags: string[];
  summary: string | null;
}

export interface GraphNodeChunk {
  id: string;
  doc_id: string;
  doc_title: string;
  text: string;
}

export interface GraphRelatedNode {
  id: string;
  label: string;
  type: GraphNodeType;
  weight: number;
}

export interface GraphNodeDetail {
  node: GraphNode;
  docs: GraphNodeDoc[];
  chunks: GraphNodeChunk[];
  related: GraphRelatedNode[];
}

export interface TagCount {
  name: string;
  count: number;
}

export interface ActivityItem {
  kind: string;
  detail: string;
  created_at: string;
}

export interface Stats {
  documents: number;
  chunks: number;
  concepts: number;
  links: number;
  zones?: number;
  tags: TagCount[];
  llm_configured: boolean;
  recent: ActivityItem[];
}

export interface SettingsLlm {
  base_url: string;
  model: string;
  configured: boolean;
  api_key_set: boolean;
  temperature: number;
}

export interface SettingsData {
  llm: SettingsLlm;
  embedding: EmbeddingInfo;
  data: { documents: number; chunks: number };
}

export interface SettingsUpdate {
  llm: {
    base_url: string;
    model: string;
    api_key?: string;
    temperature: number;
  };
}

export interface SettingsTestResult {
  ok: boolean;
  latency_ms?: number;
  model?: string;
  message: string;
}

export interface UploadItemResult {
  ok: boolean;
  filename?: string;
  error?: string;
  document?: DocumentItem;
}

export interface UploadResponse {
  items: UploadItemResult[];
}

export interface ReseedResult {
  ok: boolean;
  imported: number;
  skipped: number;
}

/* ---------------------------- 计划与打卡（Plans） --------------------------- */

export type PlanFrequency = "daily" | "weekly" | "once";

export type PlanKind = "all" | "recurring" | "once";

export type PlanKindFilter = PlanKind;

export interface PlanGroup {
  id: string;
  name: string;
  color: string;
  description: string | null;
  sort_order: number;
  plan_count: number;
  created_at: string;
  updated_at: string;
}

export interface PlanGroupsResponse {
  items: PlanGroup[];
  ungrouped_count: number;
}

export interface PlanGroupResponse {
  group: PlanGroup;
}

export interface PlanGroupInput {
  name: string;
  color?: string;
  description?: string | null;
}

export type PlanGroupUpdateInput = Partial<PlanGroupInput & { sort_order: number }>;

export interface Plan {
  id: string;
  title: string;
  description: string | null;
  color: string;
  frequency: PlanFrequency;
  weekly_target: number;
  start_date: string;
  group_id: string | null;
  group_name: string;
  group_color: string;
  plan_date: string | null;
  start_time: string | null;
  end_time: string | null;
  location: string | null;
  checked: boolean;
  archived: boolean;
  sort_order: number;
  created_at: string;
  updated_at: string;
  today_checked: boolean;
  week_checked: number;
  week_target: number;
  current_streak: number;
  longest_streak: number;
  total_checkins: number;
  last_checkin_date: string | null;
}

export interface PlansListResponse {
  items: Plan[];
  today: string;
  week_start: string;
  week_end?: string;
}

export interface PlanResponse {
  plan: Plan;
}

export interface PlanCheckin {
  id: string;
  plan_id: string;
  date: string;
  note: string | null;
  created_at: string;
}

export interface CheckinListResponse {
  items: PlanCheckin[];
}

export interface CheckinResponse {
  ok: boolean;
  checkin: PlanCheckin;
  plan: Plan;
}

export interface CheckinDeleteResponse {
  ok: boolean;
  plan: Plan;
}

export interface HeatmapDay {
  date: string;
  count: number;
}

export interface RecentPlanCheckin {
  plan_id: string;
  plan_title: string;
  color: string;
  date: string;
  note: string | null;
  created_at: string;
}

export interface PlansOverviewStats {
  active_plans: number;
  today_total: number;
  today_checked: number;
  week_checked_total: number;
  week_target_total: number;
  best_current_streak: number;
  best_plan_title: string | null;
  total_checkins: number;
  group_count?: number;
  today_schedule_total?: number;
  today_schedule_checked?: number;
  upcoming_schedule_total?: number;
  recurring_today_total?: number;
  recurring_today_checked?: number;
}

export interface PlansOverview {
  today: string;
  week_start: string;
  week_end: string;
  stats: PlansOverviewStats;
  heatmap: HeatmapDay[];
  recent: RecentPlanCheckin[];
}

export interface PlanInput {
  title: string;
  description?: string | null;
  color?: string;
  frequency?: PlanFrequency;
  weekly_target?: number;
  start_date?: string;
  group_id?: string | null;
  plan_date?: string | null;
  start_time?: string | null;
  end_time?: string | null;
  location?: string | null;
}

export type PlanUpdateInput = Partial<
  Pick<
    Plan,
    | "title"
    | "description"
    | "color"
    | "frequency"
    | "weekly_target"
    | "start_date"
    | "group_id"
    | "plan_date"
    | "start_time"
    | "end_time"
    | "location"
    | "archived"
    | "sort_order"
  >
>;

/* ---------------------------- 联网知识扩展（Extend） ----------------------- */

export interface ExtendStatus {
  enabled: boolean;
  provider: string;
  label: string;
  requires_approval: boolean;
  accepted_count: number;
  rejected_count: number;
  can_search: boolean;
}

export interface ExtendSearchInput {
  question: string;
  queries?: string[];
  conversation_id?: string | null;
  max_results?: number;
}

export interface ExtendSearchResponse {
  ok: boolean;
  provider: string;
  queries: string[];
  candidates: WebSourceCandidate[];
  took_ms: number;
  filtered_count?: number;
  intent?: "academic" | "general" | string;
  /** V2 多轮上下文：扩展搜索是否使用了对话上文。 */
  context_used?: boolean;
  context_summary?: string | null;
}

export interface ExtendCommitItem {
  url: string;
  title: string;
  snippet?: string;
}

export interface ExtendSkippedItem {
  url: string;
  reason: string;
}

export interface ExtendCommitResponse {
  ok: boolean;
  added: DocumentItem[];
  skipped: ExtendSkippedItem[];
  message: string;
}

export interface ExtendRejectResponse {
  ok: boolean;
  remembered: number;
}

/* ------------------------- 知识库智能整理（Enrich） ------------------------ */

export interface EnrichPendingDoc {
  id: string;
  title: string;
  source_type: string;
}

export interface EnrichStatus {
  llm_configured: boolean;
  can_enrich: boolean;
  total_documents: number;
  enriched_documents: number;
  pending_pdf: number;
  pending_all: number;
  sample_pending: EnrichPendingDoc[];
  mode?: "llm" | "local";
}

export interface EnrichUpdatedDoc {
  id: string;
  title: string;
  summary: string;
  tags: string[];
  keywords: string[];
}

export interface EnrichSkippedDoc {
  id: string;
  reason?: string;
  error?: string;
}

export interface EnrichResult {
  ok: boolean;
  processed: number;
  updated: EnrichUpdatedDoc[];
  skipped: EnrichSkippedDoc[];
  failed: EnrichSkippedDoc[];
  graph_rebuilt: boolean;
  mode?: "llm" | "local";
}

export type EnrichScope = "pdf" | "all" | "doc_ids";

export interface EnrichRequest {
  scope: EnrichScope;
  doc_ids?: string[] | null;
  force?: boolean;
  limit?: number;
}

/* ------------------------------- V2：MCP 工具 ------------------------------ */

export interface McpTool {
  name: string;
  description?: string;
  input_schema?: Record<string, unknown> | null;
  read_only?: boolean;
}

export interface McpServer {
  id: string;
  name: string;
  builtin?: string | null;
  enabled: boolean;
  transport?: string;
  command?: string;
  args?: string[];
  allowed_tools?: string[] | null;
  has_token?: boolean;
  connected?: boolean;
  tools?: McpTool[];
  last_error?: string | null;
}

export interface McpServersResponse {
  items: McpServer[];
  enabled_count?: number;
  tool_count?: number;
}

export interface McpServerUpdate {
  enabled?: boolean;
  allowed_tools?: string[] | null;
  /** Token 等环境变量；空字符串表示不修改，null 表示清除。 */
  env?: Record<string, string | null> | null;
}

export interface McpTestResult {
  ok?: boolean;
  tools?: McpTool[];
  tool_count?: number;
  error?: string | null;
  message?: string;
  elapsed_ms?: number;
  latency_ms?: number;
}

export interface McpServerMutationResult {
  ok?: boolean;
  error?: string | null;
  server?: McpServer;
}

export interface McpServerCreateInput {
  name: string;
  command: string;
  args?: string[];
  env?: Record<string, string> | null;
  allowed_tools?: string[] | null;
}

export interface McpServerCreateResponse {
  ok?: boolean;
  error?: string | null;
  server?: McpServer;
}

export interface McpServerDeleteResponse {
  ok?: boolean;
  error?: string | null;
  id?: string;
}

/** AI 推荐 MCP 工具（task-8 契约，安全目录由后端控制）。 */
export interface McpSuggestion {
  catalog_id: string;
  kind: "builtin" | "custom" | string;
  server_id?: string;
  name: string;
  description: string;
  rationale?: string;
  command?: string;
  args?: string[];
  env_keys?: string[];
  requires_token?: boolean;
  test_note?: string;
}

export interface McpSuggestResponse {
  ok?: boolean;
  need?: string;
  suggestions?: McpSuggestion[];
  notes?: string[] | string | null;
}

/* ------------------------------ V2：语义向量 ------------------------------ */

export interface OcrStatus {
  available: boolean;
  engine?: string;
  max_pages?: number;
}

export interface EmbeddingStatus {
  provider: string;
  model?: string;
  dim: number;
  total_chunks?: number;
  ready?: boolean;
  needs_rebuild?: boolean;
  has_api_key?: boolean;
  providers?: string[];
  last_rebuild?: string | null;
  /** 后端如把 OCR 状态并入 embedding status 时可直接使用。 */
  ocr?: OcrStatus | null;
}

export interface EmbeddingConfigInput {
  provider: string;
  model?: string;
  base_url?: string;
  api_key?: string;
}

export interface EmbeddingTestResult {
  ok?: boolean;
  provider?: string;
  dim?: number;
  similarity?: number;
  message?: string;
  error?: string | null;
}

export interface EmbeddingRebuildResult {
  ok?: boolean;
  chunks?: number;
  seconds?: number;
  provider?: string;
  dim?: number;
  message?: string;
}

/* --------------------------- V2：反馈偏好 Reranker ------------------------- */

export interface RerankerDomain {
  host: string;
  accepted: number;
  rejected: number;
  /** 0–1 平滑偏好分。 */
  score: number;
}

export interface RerankerSummary {
  accepted: number;
  rejected: number;
  domains: RerankerDomain[];
  description: string;
}

/* ------------------------------ V2：知识巡检 ------------------------------- */

export interface PatrolIssueSample {
  id?: string;
  title?: string;
  reason?: string;
}

export interface PatrolIssuePairSide {
  id?: string;
  title?: string;
}

export interface PatrolIssuePair {
  /** 兼容两种后端 shape：{title_a,title_b} 或 {a:{id,title},b:{id,title}}。 */
  title_a?: string;
  title_b?: string;
  a?: string | PatrolIssuePairSide;
  b?: string | PatrolIssuePairSide;
  similarity?: number;
}

export interface PatrolIssueBucket {
  count: number;
  sample?: PatrolIssueSample[];
  pairs?: PatrolIssuePair[];
}

export interface PatrolIssues {
  pending_docs?: PatrolIssueBucket;
  duplicate_docs?: PatrolIssueBucket;
  stale_docs?: PatrolIssueBucket;
  orphan_docs?: PatrolIssueBucket;
  small_docs?: PatrolIssueBucket;
  missing_tags?: PatrolIssueBucket;
}

export interface PatrolEnrichedDoc {
  id: string;
  title?: string;
  ok?: boolean;
  error?: string | null;
}

export interface PatrolResult {
  ok?: boolean;
  started_at?: string;
  finished_at?: string;
  duration_ms?: number;
  trigger?: string;
  enriched?: PatrolEnrichedDoc[];
  graph_rebuilt?: boolean;
  issues?: PatrolIssues | null;
  summary?: string;
  error?: string | null;
}

export interface PatrolCounts {
  pending_docs?: number;
  total_documents?: number;
  enriched_documents?: number;
}

export interface PatrolStatus {
  enabled: boolean;
  interval_hours: number;
  auto_enrich: boolean;
  max_enrich: number;
  running?: boolean;
  last_run_at?: string | null;
  next_run_at?: string | null;
  last_result?: PatrolResult | null;
  counts?: PatrolCounts;
}

export interface PatrolConfigInput {
  enabled?: boolean;
  interval_hours?: number;
  auto_enrich?: boolean;
  max_enrich?: number;
}

export interface PatrolRunResponse {
  ok?: boolean;
  result?: PatrolResult;
  status?: PatrolStatus;
}
