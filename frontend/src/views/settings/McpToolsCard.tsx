import { useCallback, useEffect, useState } from "react";
import type { LucideIcon } from "lucide-react";
import {
  Check,
  ChevronRight,
  Database,
  FolderTree,
  Github,
  KeyRound,
  LoaderCircle,
  Plus,
  Plug,
  RefreshCw,
  ShieldCheck,
  Terminal,
  Trash2,
  TriangleAlert,
  Wifi,
  Wrench,
} from "lucide-react";
import { ConfirmDialog } from "../../components/Modal";
import { Switch } from "../../components/Switch";
import {
  deleteMcpServer,
  getMcpServers,
  testMcpServer,
  toUserMessage,
  updateMcpServer,
} from "../../lib/api";
import { useApp } from "../../state/AppContext";
import type {
  McpServer,
  McpServersResponse,
  McpSuggestion,
  McpTestResult,
  McpTool,
} from "../../types";
import { McpAddDialog } from "./McpAddDialog";

interface ServerMeta {
  icon: LucideIcon;
  kind: string;
}

const SERVER_META: Record<string, ServerMeta> = {
  filesystem: { icon: FolderTree, kind: "内置 · 只读文件系统" },
  sqlite: { icon: Database, kind: "内置 · SQLite 只读查询" },
  github: { icon: Github, kind: "内置 · GitHub REST 只读" },
};

function metaOf(server: McpServer): ServerMeta {
  return SERVER_META[server.id] ?? SERVER_META[server.builtin ?? ""] ?? { icon: Plug, kind: "自定义 · stdio" };
}

interface ServerState {
  label: string;
  dot: "connected" | "error" | "disabled";
}

function stateOf(server: McpServer): ServerState {
  if (!server.enabled) return { label: "未启用", dot: "disabled" };
  if (server.last_error) return { label: "错误", dot: "error" };
  if (server.connected) return { label: "已连接", dot: "connected" };
  return { label: "待连接", dot: "disabled" };
}

function toolsOf(server: McpServer): NonNullable<McpServer["tools"]> {
  return Array.isArray(server.tools) ? server.tools : [];
}

export function McpToolsCard() {
  const { toast } = useApp();
  const [data, setData] = useState<McpServersResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string[]>([]);
  const [togglingId, setTogglingId] = useState<string | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [savingTokenId, setSavingTokenId] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, McpTestResult>>({});
  const [tokenEditingId, setTokenEditingId] = useState<string | null>(null);
  const [tokenValue, setTokenValue] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<McpServer | null>(null);
  const [deleting, setDeleting] = useState(false);

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const next = await getMcpServers();
      const items = Array.isArray(next?.items) ? next.items : [];
      setData({ ...next, items });
      setError(null);
    } catch (err) {
      setError(toUserMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggleExpanded = (id: string): void => {
    setExpanded((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]));
  };

  const toggleServer = async (server: McpServer): Promise<void> => {
    if (togglingId) return;
    if (
      !server.enabled &&
      !server.builtin &&
      !(Array.isArray(server.allowed_tools) && server.allowed_tools.length > 0)
    ) {
      setExpanded((prev) => (prev.includes(server.id) ? prev : [...prev, server.id]));
      toast(
        "error",
        "还没有允许的工具",
        "请先测试连接、勾选信任的工具并「保存允许工具」，再启用该自定义 Server。",
      );
      return;
    }
    setTogglingId(server.id);
    try {
      const result = await updateMcpServer(server.id, { enabled: !server.enabled });
      if (result?.ok === false) {
        toast(
          "error",
          server.enabled ? "停用失败" : "启用失败，已保持关闭",
          result.error || "无法连接该 MCP Server，请展开查看错误或测试连接。",
        );
      } else {
        toast(
          "success",
          server.enabled ? `已停用「${server.name}」` : `已启用「${server.name}」`,
          server.enabled ? "该工具将不再出现在对话工具列表中。" : "对话中命中相关意图时会自动调用该工具。",
        );
      }
      await load();
    } catch (err) {
      toast("error", "保存失败", toUserMessage(err));
    } finally {
      setTogglingId(null);
    }
  };

  const runTest = async (server: McpServer): Promise<void> => {
    if (testingId) return;
    setTestingId(server.id);
    setTestResults((prev) => {
      const next = { ...prev };
      delete next[server.id];
      return next;
    });
    try {
      const result = await testMcpServer(server.id);
      setTestResults((prev) => ({ ...prev, [server.id]: result }));
      const count = Array.isArray(result.tools) ? result.tools.length : 0;
      if (result.ok === false || result.error) {
        toast("error", "连接失败", result.error || result.message || "无法连接该 MCP Server。");
      } else {
        toast("success", "连接成功", `已发现 ${count} 个工具。`);
      }
    } catch (err) {
      const message = toUserMessage(err);
      setTestResults((prev) => ({ ...prev, [server.id]: { ok: false, error: message } }));
      toast("error", "连接失败", message);
    } finally {
      setTestingId(null);
    }
  };

  const saveToken = async (server: McpServer): Promise<void> => {
    if (savingTokenId || !tokenValue.trim()) return;
    setSavingTokenId(server.id);
    try {
      const result = await updateMcpServer(server.id, { env: { GITHUB_TOKEN: tokenValue.trim() } });
      setTokenValue("");
      setTokenEditingId(null);
      if (result?.ok === false) {
        toast("error", "Token 已保存，但连接仍失败", result.error || "请检查 Token 权限后重试。");
      } else {
        toast("success", "Token 已保存", "接口不会返回明文；已脱敏显示为「已保存」。");
      }
      await load();
    } catch (err) {
      toast("error", "保存 Token 失败", toUserMessage(err));
    } finally {
      setSavingTokenId(null);
    }
  };

  const clearToken = async (server: McpServer): Promise<void> => {
    if (savingTokenId) return;
    setSavingTokenId(server.id);
    try {
      await updateMcpServer(server.id, { env: { GITHUB_TOKEN: null } });
      setTokenEditingId(null);
      setTokenValue("");
      toast("success", "Token 已清除", "GitHub 将回到匿名公开访问（受 API 限流限制）。");
      await load();
    } catch (err) {
      toast("error", "清除 Token 失败", toUserMessage(err));
    } finally {
      setSavingTokenId(null);
    }
  };

  const useBuiltinSuggestion = async (suggestion: McpSuggestion): Promise<string | null> => {
    const items = data?.items ?? [];
    const target =
      items.find((item) => item.id === suggestion.server_id) ??
      items.find((item) => item.builtin === suggestion.server_id) ??
      items.find((item) => item.id === suggestion.catalog_id) ??
      items.find((item) => item.builtin === suggestion.catalog_id);
    if (!target) {
      return "未找到对应的内置工具，请刷新 MCP 列表后重试；或切换到手动添加。";
    }
    if (target.enabled) {
      setExpanded((prev) => (prev.includes(target.id) ? prev : [...prev, target.id]));
      toast("success", "该内置工具已启用", `已定位并展开「${target.name}」。`);
      return null;
    }
    try {
      const result = await updateMcpServer(target.id, { enabled: true });
      if (result?.ok === false) {
        toast("error", "启用失败", result.error || "无法连接该内置工具。");
        await load();
        return String(result.error || "启用失败，请刷新后重试。");
      }
      setExpanded((prev) => (prev.includes(target.id) ? prev : [...prev, target.id]));
      toast(
        "success",
        `已启用「${target.name}」`,
        suggestion.requires_token && !target.has_token
          ? "提示：该工具需要 Token 才能稳定使用，请展开配置。"
          : "可以在对话中按需调用了。",
      );
      await load();
      return null;
    } catch (err) {
      return toUserMessage(err);
    }
  };

  const confirmDelete = async (): Promise<void> => {
    const target = deleteTarget;
    if (deleting || !target) return;
    setDeleting(true);
    try {
      const result = await deleteMcpServer(target.id);
      if (result?.ok === false) {
        throw new Error(result.error || "删除失败");
      }
      toast("success", "已删除自定义工具", `「${target.name}」已从 MCP 工具列表移除。`);
      setDeleteTarget(null);
      setExpanded((prev) => prev.filter((id) => id !== target.id));
      await load();
    } catch (err) {
      toast("error", "删除失败", toUserMessage(err));
    } finally {
      setDeleting(false);
    }
  };

  const items = data?.items ?? [];
  const enabledCount = data?.enabled_count ?? items.filter((server) => server.enabled).length;
  const toolCount = data?.tool_count ?? items.reduce((total, server) => total + toolsOf(server).length, 0);
  const busy = Boolean(togglingId);

  return (
    <section className="card settings-card mcp-card" data-card="mcp" aria-label="MCP 工具">
      <div className="settings-card-head">
        <div className="settings-card-icon violet">
          <Plug size={18} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 className="settings-card-title">MCP 工具</h2>
          <p className="settings-card-desc">
            通过 Model Context Protocol 连接外部工具（只读默认）。启用后，对话命中相关意图时会自动调用。
          </p>
        </div>
        <span className={`badge ${enabledCount > 0 ? "badge-mint" : "badge"}`}>
          <span className={`status-dot ${enabledCount > 0 ? "pulse" : "is-warn"}`} />
          {enabledCount > 0 ? `${enabledCount} 已启用 · ${toolCount} 工具` : "未启用"}
        </span>
        <button
          type="button"
          className="btn btn-sm mcp-add-btn"
          onClick={() => setAddOpen(true)}
        >
          <Plus size={13} />
          添加工具
        </button>
        <button
          type="button"
          className="btn btn-icon"
          onClick={() => void load()}
          disabled={loading}
          aria-label="刷新 MCP 工具状态"
          data-tip="刷新"
        >
          {loading ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}
        </button>
      </div>

      <div className="settings-body">
        {error && !data ? (
          <div className="settings-inline-error">
            <TriangleAlert size={14} />
            <span>MCP 配置获取失败：{error}</span>
            <button type="button" className="btn btn-sm" onClick={() => void load()}>
              <RefreshCw size={12} /> 重试
            </button>
          </div>
        ) : loading && !data ? (
          <div className="v2-loading">
            <LoaderCircle size={14} className="spin" /> 正在读取 MCP 工具配置…
          </div>
        ) : data ? (
          <>
            <div className="mcp-server-list">
              {items.map((server) => {
                const meta = metaOf(server);
                const Icon = meta.icon;
                const state = stateOf(server);
                const isCustom = !server.builtin;
                const commandLine = `${server.command ?? ""} ${(server.args ?? []).join(" ")}`.trim();
                const testedTools = Array.isArray(testResults[server.id]?.tools)
                  ? testResults[server.id]?.tools ?? []
                  : [];
                const tools = toolsOf(server).length > 0 ? toolsOf(server) : testedTools;
                const open = expanded.includes(server.id);
                const testing = testingId === server.id;
                const testResult = testResults[server.id];
                const tokenBusy = savingTokenId === server.id;
                const isGithub = server.id === "github" || server.builtin === "github";
                return (
                  <div
                    key={server.id}
                    className={`mcp-server${server.enabled ? " is-enabled" : ""}${
                      server.last_error ? " has-error" : ""
                    }`}
                    data-server={server.id}
                  >
                    <div className="mcp-server-head">
                      <button
                        type="button"
                        className="mcp-server-toggle"
                        onClick={() => toggleExpanded(server.id)}
                        aria-expanded={open}
                        aria-label={`${open ? "收起" : "展开"} ${server.name} 工具`}
                      >
                        <ChevronRight size={14} className={`mcp-chevron${open ? " open" : ""}`} />
                        <span className="mcp-server-icon">
                          <Icon size={15} />
                        </span>
                        <span className="mcp-server-main">
                          <span className="mcp-server-name">{server.name}</span>
                          <span
                            className="mcp-server-kind"
                            title={isCustom ? commandLine : undefined}
                          >
                            {isCustom
                              ? `自定义 · stdio${commandLine ? ` · ${commandLine}` : ""}`
                              : meta.kind}
                          </span>
                        </span>
                      </button>
                      <span className="mcp-server-status">
                        {togglingId === server.id ? (
                          <LoaderCircle size={11} className="spin" />
                        ) : (
                          <span className={`mcp-status-dot ${state.dot}`} />
                        )}
                        {state.label}
                      </span>
                      <span className="mcp-tool-count mono" title="已发现工具数量">
                        {tools.length} 工具
                      </span>
                      <Switch
                        checked={Boolean(server.enabled)}
                        onChange={() => void toggleServer(server)}
                        label={`启用 ${server.name}`}
                        disabled={busy}
                      />
                    </div>

                    {open ? (
                      <div className="mcp-server-body">
                        {tools.length > 0 ? (
                          <div className="mcp-tool-chips">
                            {tools.map((tool) => (
                              <span
                                key={tool.name}
                                className="mcp-tool-chip"
                                title={tool.description || tool.name}
                              >
                                <span className="mcp-tool-chip-name">{tool.name}</span>
                                {tool.description ? (
                                  <span className="mcp-tool-chip-desc">{tool.description}</span>
                                ) : null}
                              </span>
                            ))}
                          </div>
                        ) : (
                          <div className="mcp-empty-tools">
                            {server.enabled ? "尚未发现工具，可点击测试连接排查。" : "启用后可查看该 Server 的工具列表。"}
                          </div>
                        )}

                        {isGithub ? (
                          <div className="mcp-token">
                            <div className="mcp-token-head">
                              <KeyRound size={12} />
                              GitHub Token
                            </div>
                            {server.has_token && tokenEditingId !== server.id ? (
                              <div className="mcp-token-actions">
                                <span className="mcp-token-saved">
                                  <Check size={12} /> 已保存（脱敏）
                                </span>
                                <button
                                  type="button"
                                  className="btn btn-sm"
                                  onClick={() => {
                                    setTokenEditingId(server.id);
                                    setTokenValue("");
                                  }}
                                  disabled={tokenBusy}
                                >
                                  更新
                                </button>
                                <button
                                  type="button"
                                  className="btn btn-sm btn-ghost"
                                  onClick={() => void clearToken(server)}
                                  disabled={tokenBusy}
                                >
                                  <Trash2 size={12} /> 清除
                                </button>
                              </div>
                            ) : (
                              <div className="mcp-token-form">
                                <input
                                  className="input"
                                  type="password"
                                  value={tokenValue}
                                  aria-label="GitHub Token"
                                  placeholder={
                                    server.has_token
                                      ? "输入新 Token 更新（留空不修改）"
                                      : "ghp_…（可选，仅提升公开 API 限流额度）"
                                  }
                                  autoComplete="off"
                                  spellCheck={false}
                                  onChange={(event) => setTokenValue(event.target.value)}
                                />
                                <button
                                  type="button"
                                  className="btn btn-sm btn-primary"
                                  onClick={() => void saveToken(server)}
                                  disabled={tokenBusy || !tokenValue.trim()}
                                >
                                  {tokenBusy ? <LoaderCircle size={12} className="spin" /> : null}
                                  保存
                                </button>
                                {server.has_token ? (
                                  <button
                                    type="button"
                                    className="btn btn-sm btn-ghost"
                                    onClick={() => {
                                      setTokenEditingId(null);
                                      setTokenValue("");
                                    }}
                                    disabled={tokenBusy}
                                  >
                                    取消
                                  </button>
                                ) : null}
                              </div>
                            )}
                            <span className="field-hint">
                              Token 仅保存在本机数据库，接口不会返回明文；保存后只显示「已保存」。
                            </span>
                          </div>
                        ) : null}

                        {isCustom ? (
                          <div className="mcp-command-line" title={commandLine}>
                            <Terminal size={11} />
                            <span className="mono">{commandLine || "未配置启动命令"}</span>
                          </div>
                        ) : null}

                        {server.last_error ? (
                          <div className="mcp-error-line">
                            <TriangleAlert size={12} />
                            <span>{server.last_error}</span>
                          </div>
                        ) : null}

                        {isCustom ? (
                          <McpAllowedToolsEditor
                            server={server}
                            tools={tools}
                            onSaved={() => void load()}
                          />
                        ) : null}

                        <div className="mcp-server-actions">
                          <button
                            type="button"
                            className="btn btn-sm"
                            onClick={() => void runTest(server)}
                            disabled={testing || Boolean(testingId)}
                          >
                            {testing ? (
                              <LoaderCircle size={12} className="spin" />
                            ) : (
                              <Wifi size={12} />
                            )}
                            {testing ? "连接中…" : "测试连接"}
                          </button>
                          {testResult ? (
                            testResult.ok === false || testResult.error ? (
                              <span className="mcp-test-result fail">
                                <TriangleAlert size={11} />
                                {testResult.error || testResult.message || "连接失败"}
                              </span>
                            ) : (
                              <span className="mcp-test-result ok">
                                <Check size={11} />
                                连接成功 · {testResult.tool_count ?? (Array.isArray(testResult.tools) ? testResult.tools.length : 0)} 个工具
                                {typeof testResult.elapsed_ms === "number" ? ` · ${testResult.elapsed_ms}ms` : ""}
                              </span>
                            )
                          ) : null}
                          {isCustom ? (
                            <button
                              type="button"
                              className="btn btn-sm btn-danger mcp-delete-btn"
                              onClick={() => setDeleteTarget(server)}
                              title="删除该自定义 MCP Server"
                            >
                              <Trash2 size={12} />
                              删除
                            </button>
                          ) : null}
                        </div>
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>

            <div className="v2-note">
              <ShieldCheck size={13} />
              内置工具全部只读：文件系统仅限 data 目录、数据库仅允许 SELECT、GitHub 仅调用只读 REST
              接口。工具不会自动安装，也不会写回知识库；随时可关闭上方开关停用。
            </div>
          </>
        ) : null}
      </div>

      <McpAddDialog
        open={addOpen}
        onClose={() => setAddOpen(false)}
        onUseBuiltin={useBuiltinSuggestion}
        onCreated={(server) => {
          setExpanded((prev) => (prev.includes(server.id) ? prev : [...prev, server.id]));
          setTestResults((prev) => {
            const next = { ...prev };
            delete next[server.id];
            return next;
          });
          void load();
        }}
      />

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="删除自定义 MCP Server？"
        description={
          <span>
            将删除自定义工具 <strong>「{deleteTarget?.name ?? ""}」</strong>{" "}
            的配置（含允许工具列表）。已加入知识库的数据不受影响，内置工具不受影响。此操作不可撤销。
          </span>
        }
        confirmLabel="删除工具"
        loading={deleting}
        icon={<Trash2 size={16} />}
        onConfirm={() => void confirmDelete()}
        onClose={() => setDeleteTarget(null)}
      />
    </section>
  );
}

interface McpAllowedToolsEditorProps {
  server: McpServer;
  tools: McpTool[];
  onSaved: () => void;
}

/** 自定义 Server 的允许工具编辑器：勾选信任的工具后保存 allowed_tools。 */
function McpAllowedToolsEditor({ server, tools, onSaved }: McpAllowedToolsEditorProps) {
  const { toast } = useApp();
  const [selected, setSelected] = useState<string[]>(() =>
    Array.isArray(server.allowed_tools) ? [...server.allowed_tools] : [],
  );
  const [saving, setSaving] = useState(false);
  const allowedKey = (server.allowed_tools ?? []).join("|");

  useEffect(() => {
    setSelected(Array.isArray(server.allowed_tools) ? [...server.allowed_tools] : []);
  }, [server.id, allowedKey]);

  const toggle = (toolName: string): void => {
    setSelected((prev) =>
      prev.includes(toolName) ? prev.filter((item) => item !== toolName) : [...prev, toolName],
    );
  };

  const save = async (): Promise<void> => {
    if (saving) return;
    setSaving(true);
    try {
      const result = await updateMcpServer(server.id, { allowed_tools: selected });
      if (result?.ok === false) {
        toast("error", "保存失败", result.error || "无法保存允许工具。");
        return;
      }
      toast(
        "success",
        "允许工具已保存",
        selected.length > 0
          ? `对话只会调用已勾选的 ${selected.length} 个工具。`
          : "未勾选任何工具，该 Server 暂时不能启用。",
      );
      onSaved();
    } catch (err) {
      toast("error", "保存失败", toUserMessage(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mcp-allowed">
      <div className="mcp-allowed-head">
        <Wrench size={12} />
        允许的工具
        {tools.length > 0 ? (
          <span className="mcp-allowed-count mono">
            {selected.length}/{tools.length}
          </span>
        ) : null}
      </div>
      {tools.length === 0 ? (
        <div className="mcp-empty-tools">
          点击下方「测试连接」发现工具后，即可勾选允许的工具。
        </div>
      ) : (
        <div className="mcp-allowed-list">
          {tools.map((tool) => {
            const checked = selected.includes(tool.name);
            const readonly = tool.read_only !== false;
            return (
              <label
                key={tool.name}
                className={`mcp-allowed-tool${checked ? " is-checked" : ""}`}
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => toggle(tool.name)}
                  aria-label={`允许 ${tool.name}`}
                />
                <span className={`mcp-allowed-check${checked ? " on" : ""}`}>
                  {checked ? <Check size={10} /> : null}
                </span>
                <span className="mcp-allowed-tool-main">
                  <span className="mcp-allowed-tool-name mono">{tool.name}</span>
                  <span className="mcp-allowed-tool-desc">
                    {tool.description || (readonly ? "只读工具" : "可能包含写操作")}
                  </span>
                </span>
                {readonly ? (
                  <span className="mcp-allowed-badge">只读</span>
                ) : (
                  <span className="mcp-allowed-badge is-write">
                    <TriangleAlert size={9} />
                    写操作
                  </span>
                )}
              </label>
            );
          })}
        </div>
      )}
      <div className="mcp-allowed-actions">
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => void save()}
          disabled={saving || tools.length === 0}
        >
          {saving ? <LoaderCircle size={12} className="spin" /> : <Wrench size={12} />}
          保存允许工具（{selected.length}）
        </button>
        <span className="field-hint">
          未勾选的工具不会被对话调用；启用自定义 Server 需至少勾选一个。
        </span>
      </div>
    </div>
  );
}
