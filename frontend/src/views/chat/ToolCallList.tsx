import { Check, LoaderCircle, TriangleAlert } from "lucide-react";
import { truncate } from "../../lib/format";
import type { ChatToolCall } from "./chatTypes";

const MCP_SERVER_LABELS: Record<string, string> = {
  filesystem: "文件系统",
  sqlite: "知识库数据库",
  github: "GitHub",
};

interface ToolNameInfo {
  kind: string;
  tool: string | null;
  isMcp: boolean;
}

function describeToolName(name: string): ToolNameInfo {
  if (name === "search_web") return { kind: "联网搜索", tool: null, isMcp: false };
  if (name.startsWith("mcp_")) {
    const rest = name.slice(4);
    for (const [serverId, label] of Object.entries(MCP_SERVER_LABELS)) {
      if (rest.startsWith(`${serverId}_`)) {
        return { kind: label, tool: rest.slice(serverId.length + 1), isMcp: true };
      }
    }
    // 自定义 Server：id 形如 srv_<12 hex>，qualified name = mcp_srv_<id>_<tool>
    const custom = rest.match(/^(srv_[0-9a-f]{6,})_([A-Za-z0-9_-]+)$/i);
    if (custom) {
      return { kind: "自定义 MCP", tool: custom[2], isMcp: true };
    }
    return { kind: "MCP", tool: rest, isMcp: true };
  }
  return { kind: name, tool: null, isMcp: false };
}

function formatArgs(args: Record<string, unknown> | null | undefined, query?: string): string {
  if (query) return `查询：${truncate(query, 60)}`;
  if (!args) return "";
  const parts: string[] = [];
  for (const [key, value] of Object.entries(args)) {
    if (value === null || value === undefined || value === "") continue;
    let text: string;
    if (typeof value === "string") text = value;
    else if (typeof value === "number" || typeof value === "boolean") text = String(value);
    else {
      try {
        text = JSON.stringify(value);
      } catch {
        text = String(value);
      }
    }
    parts.push(`${key}: ${truncate(text, 54)}`);
    if (parts.length >= 3) break;
  }
  return parts.join(" · ");
}

function statusTextOf(call: ChatToolCall): string {
  const running = call.status === "running";
  if (call.name === "search_web") {
    if (running) return call.query ? `正在联网搜索：${truncate(call.query, 60)}` : "正在联网搜索…";
    if (call.status === "error") return call.summary || call.error || "联网搜索失败";
    return `已找到 ${call.count ?? 0} 条外部结果`;
  }
  if (running) return "调用中…";
  if (call.status === "error") return call.summary || call.error || "调用失败";
  return call.summary || "已返回结果";
}

/** 对话内通用工具调用卡片：兼容 search_web 与 MCP 工具。 */
export function ToolCallList({ tools }: { tools: ChatToolCall[] }) {
  if (tools.length === 0) return null;

  return (
    <div className="tool-calls" aria-label="工具调用">
      {tools.map((call) => {
        const info = describeToolName(call.name);
        const running = call.status === "running";
        const Icon = running ? LoaderCircle : call.status === "error" ? TriangleAlert : Check;
        // MCP 工具始终展示参数（start 显示参数、end 显示「摘要 · 参数」）；
        // search_web 的 query 已包含在状态文案中，避免重复。
        const argsText = call.name !== "search_web" ? formatArgs(call.args, call.query) : "";
        const meta = statusTextOf(call);
        const className = `tool-call${call.status === "error" ? " is-error" : call.status === "ok" ? " is-ok" : ""}`;
        return (
          <div
            key={call.id}
            className={className}
            aria-label={`工具调用 · ${info.kind}${info.tool ? ` / ${info.tool}` : ""}`}
          >
            <span className="tool-call-icon">
              <Icon size={12} className={running ? "spin" : undefined} />
            </span>
            <span className="tool-call-main">
              <span className="tool-call-head">
                <span className="tool-call-kind">{info.kind}</span>
                {info.tool ? <span className="tool-call-name mono">{info.tool}</span> : null}
                {info.isMcp ? <span className="tool-call-badge">MCP</span> : null}
                {!running && call.name === "search_web" && call.count != null ? (
                  <span className="tool-call-count mono">{call.count} 条</span>
                ) : null}
              </span>
              <span className="tool-call-meta">
                {meta}
                {argsText && !meta.includes(argsText) ? ` · ${argsText}` : ""}
              </span>
            </span>
          </div>
        );
      })}
    </div>
  );
}
