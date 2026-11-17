import { useEffect, useState } from "react";
import {
  Check,
  LoaderCircle,
  Plug,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Terminal,
  TriangleAlert,
  Wrench,
} from "lucide-react";
import { Modal } from "../../components/Modal";
import {
  createMcpServer,
  deleteMcpServer,
  suggestMcpServers,
  testMcpServer,
  toUserMessage,
  updateMcpServer,
} from "../../lib/api";
import { useApp } from "../../state/AppContext";
import type { McpServer, McpSuggestion, McpTool } from "../../types";

interface McpAddDialogProps {
  open: boolean;
  onClose: () => void;
  /** 创建/保存成功（关闭弹窗）后回调，用于刷新列表并展开新行。 */
  onCreated: (server: McpServer) => void;
  /**
   * AI 建议为内置 Server 时由父组件负责启用现有 preset（不创建重复 server）。
   * 返回中文错误信息表示失败，null 表示成功。
   */
  onUseBuiltin: (suggestion: McpSuggestion) => Promise<string | null>;
}

type Phase = "form" | "testing" | "tools";
type Tab = "ai" | "manual";

interface AiPrefill {
  rationale: string;
  testNote: string;
  requiresToken: boolean;
  envKeys: string[];
}

const AI_EXAMPLES = [
  "我想让模型读取 data 目录下的文档",
  "我想查询我的 SQLite 数据库",
  "我想搜索 GitHub 仓库",
  "我想抓取网页内容并整理",
];

function splitLines(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function parseEnvText(value: string): { env: Record<string, string>; error: string | null } {
  const env: Record<string, string> = {};
  for (const line of splitLines(value)) {
    const eq = line.indexOf("=");
    if (eq <= 0) {
      return { env: {}, error: `环境变量格式应为 KEY=VALUE，当前行：${line}` };
    }
    const key = line.slice(0, eq).trim();
    const val = line.slice(eq + 1).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) {
      return { env: {}, error: `环境变量名不合法：${key}` };
    }
    env[key] = val;
  }
  return { env, error: null };
}

function notesText(notes: string[] | string | null | undefined): string {
  if (!notes) return "";
  return Array.isArray(notes) ? notes.join(" ") : String(notes);
}

/**
 * 添加 MCP 工具弹窗：两个入口
 * - 「AI 帮我配」：描述需求 → 后端从受控目录推荐配置 → 使用建议（内置直接启用 / 自定义预填表单）；
 * - 「手动添加」：填写 stdio 命令 → 创建 → 自动测试发现工具 → 勾选允许工具 → 保存/启用。
 */
export function McpAddDialog({ open, onClose, onCreated, onUseBuiltin }: McpAddDialogProps) {
  const { toast } = useApp();
  const [tab, setTab] = useState<Tab>("ai");
  const [phase, setPhase] = useState<Phase>("form");
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [argsText, setArgsText] = useState("");
  const [envText, setEnvText] = useState("");
  const [draft, setDraft] = useState<McpServer | null>(null);
  const [tools, setTools] = useState<McpTool[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"create" | "test" | "save" | "enable" | "cancel" | null>(
    null,
  );

  const [need, setNeed] = useState("");
  const [suggesting, setSuggesting] = useState(false);
  const [aiError, setAiError] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<McpSuggestion[]>([]);
  const [aiNotes, setAiNotes] = useState("");
  const [usingCatalog, setUsingCatalog] = useState<string | null>(null);
  const [aiPrefill, setAiPrefill] = useState<AiPrefill | null>(null);

  const reset = (): void => {
    setTab("ai");
    setPhase("form");
    setName("");
    setCommand("");
    setArgsText("");
    setEnvText("");
    setDraft(null);
    setTools([]);
    setSelected(new Set());
    setError(null);
    setTestError(null);
    setBusy(null);
    setNeed("");
    setSuggesting(false);
    setAiError(null);
    setSuggestions([]);
    setAiNotes("");
    setUsingCatalog(null);
    setAiPrefill(null);
  };

  useEffect(() => {
    if (open) reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const askAi = async (): Promise<void> => {
    if (suggesting) return;
    const needText = need.trim();
    if (!needText) {
      setAiError("请先描述你的需求，例如「我想让模型读取 data 目录下的文档」。");
      return;
    }
    setSuggesting(true);
    setAiError(null);
    setSuggestions([]);
    setAiNotes("");
    try {
      const response = await suggestMcpServers({ need: needText, max_suggestions: 2 });
      if (response?.ok === false) {
        setAiError(String(response.notes || "AI 推荐失败，请稍后重试或手动添加。"));
        return;
      }
      const nextSuggestions = Array.isArray(response.suggestions) ? response.suggestions : [];
      setSuggestions(nextSuggestions);
      setAiNotes(notesText(response.notes));
      if (nextSuggestions.length === 0) {
        setAiError("AI 没有给出可用建议，请换一种描述，或切换到手动添加。");
      }
    } catch (err) {
      setAiError(toUserMessage(err));
    } finally {
      setSuggesting(false);
    }
  };

  const useSuggestion = async (suggestion: McpSuggestion): Promise<void> => {
    if (usingCatalog) return;
    if (suggestion.kind === "builtin") {
      setUsingCatalog(suggestion.catalog_id);
      setAiError(null);
      try {
        const failure = await onUseBuiltin(suggestion);
        if (failure) {
          setAiError(failure);
          return;
        }
        reset();
        onClose();
      } finally {
        setUsingCatalog(null);
      }
      return;
    }
    // 自定义配置：切到手动表单并预填（仍走测试→勾选→保存/启用流程）
    setTab("manual");
    setPhase("form");
    setError(null);
    setTestError(null);
    setName(suggestion.name ?? "");
    setCommand(suggestion.command ?? "");
    setArgsText((suggestion.args ?? []).join("\n"));
    const envKeys = Array.isArray(suggestion.env_keys) ? suggestion.env_keys : [];
    setEnvText(envKeys.map((key) => `${key}=`).join("\n"));
    const hasPlaceholder = (suggestion.args ?? []).some((arg) => /<[^>]+>/.test(arg));
    setAiPrefill({
      rationale: suggestion.rationale ?? "",
      testNote: hasPlaceholder
        ? `${suggestion.test_note ?? ""} 参数中的 <…> 占位符请替换为你的实际路径。`.trim()
        : suggestion.test_note ?? "",
      requiresToken: Boolean(suggestion.requires_token),
      envKeys,
    });
  };

  const discoverTools = async (server: McpServer): Promise<void> => {
    setPhase("testing");
    setBusy("test");
    setTestError(null);
    try {
      const result = await testMcpServer(server.id);
      if (result?.ok === false || result?.error) {
        setTools([]);
        setTestError(String(result?.error || "连接测试失败，请检查启动命令、参数与环境变量。"));
      } else {
        setTools(Array.isArray(result.tools) ? result.tools : []);
        setTestError(null);
      }
    } catch (err) {
      setTools([]);
      setTestError(toUserMessage(err));
    } finally {
      setPhase("tools");
      setBusy(null);
    }
  };

  const createAndTest = async (): Promise<void> => {
    if (busy) return;
    setError(null);
    const nameText = name.trim();
    const commandText = command.trim();
    if (!nameText) {
      setError("请填写显示名称（最多 40 个字符）。");
      return;
    }
    if (nameText.length > 40) {
      setError("名称最多 40 个字符。");
      return;
    }
    if (!commandText) {
      setError("请填写启动命令，例如 npx / python / uvx。");
      return;
    }
    const { env, error: envError } = parseEnvText(envText);
    if (envError) {
      setError(envError);
      return;
    }
    setBusy("create");
    setPhase("testing");
    try {
      const args = splitLines(argsText);
      const created = await createMcpServer({
        name: nameText,
        command: commandText,
        args,
        env: Object.keys(env).length > 0 ? env : null,
      });
      if (created?.ok === false || !created?.server) {
        throw new Error(created?.error || "创建自定义 MCP Server 失败。");
      }
      setDraft(created.server);
      await discoverTools(created.server);
    } catch (err) {
      setPhase("form");
      setBusy(null);
      setError(toUserMessage(err));
    }
  };

  const cancelAndDispose = async (): Promise<void> => {
    if (busy || usingCatalog || suggesting) return;
    if (draft) {
      setBusy("cancel");
      try {
        await deleteMcpServer(draft.id);
      } catch {
        /* 草稿清理失败不阻塞关闭 */
      } finally {
        setBusy(null);
      }
    }
    reset();
    onClose();
  };

  /** 测试失败后返回表单：删除已创建草稿，保留填写内容便于修改命令。 */
  const backToForm = async (): Promise<void> => {
    if (busy || !draft) return;
    setBusy("cancel");
    try {
      await deleteMcpServer(draft.id);
    } catch {
      /* ignore */
    } finally {
      setBusy(null);
    }
    setDraft(null);
    setTools([]);
    setSelected(new Set());
    setTestError(null);
    setError(null);
    setPhase("form");
  };

  const save = async (enable: boolean): Promise<void> => {
    if (busy || !draft) return;
    if (enable && selected.size === 0) {
      setError("请先勾选至少一个允许的工具再启用。");
      return;
    }
    setBusy(enable ? "enable" : "save");
    setError(null);
    try {
      const allowed = [...selected];
      const result = await updateMcpServer(
        draft.id,
        enable ? { allowed_tools: allowed, enabled: true } : { allowed_tools: allowed },
      );
      if (result?.ok === false) {
        setError(
          String(
            result.error ||
              "启用失败：无法连接该 Server。配置已保存但保持关闭，可稍后重试或取消删除。",
          ),
        );
        return;
      }
      toast(
        "success",
        enable ? "自定义工具已启用" : "已保存自定义工具",
        enable
          ? "对话命中相关意图时会调用你勾选的工具；只读工具更安全。"
          : "当前未启用；可展开该行继续测试、调整允许工具或启用。",
      );
      const created = draft;
      reset();
      onClose();
      onCreated(created);
    } catch (err) {
      setError(toUserMessage(err));
    } finally {
      setBusy(null);
    }
  };

  const toggleTool = (toolName: string): void => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(toolName)) next.delete(toolName);
      else next.add(toolName);
      return next;
    });
  };

  const readonlyTools = tools.filter((tool) => tool.read_only !== false);
  const manualPhase = tab === "manual" || phase !== "form";
  const showToolsPhase = phase === "tools";
  const aiTabVisible = phase === "form";

  const footer = (
    <>
      <button
        type="button"
        className="btn"
        onClick={() => void cancelAndDispose()}
        disabled={busy !== null || suggesting || usingCatalog !== null}
      >
        取消
      </button>
      {manualPhase && showToolsPhase && testError ? (
        <>
          <button
            type="button"
            className="btn"
            onClick={() => void backToForm()}
            disabled={busy !== null}
          >
            返回修改
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => draft && void discoverTools(draft)}
            disabled={busy !== null}
          >
            {busy === "test" ? <LoaderCircle size={13} className="spin" /> : null}
            重新测试
          </button>
        </>
      ) : null}
      {manualPhase && showToolsPhase && !testError ? (
        <>
          <button
            type="button"
            className="btn"
            onClick={() => void save(false)}
            disabled={busy !== null}
          >
            {busy === "save" ? <LoaderCircle size={13} className="spin" /> : null}
            仅保存
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void save(true)}
            disabled={busy !== null || selected.size === 0}
            title={selected.size === 0 ? "请先勾选至少一个允许的工具" : undefined}
          >
            {busy === "enable" ? <LoaderCircle size={13} className="spin" /> : null}
            保存并启用（{selected.size}）
          </button>
        </>
      ) : null}
      {manualPhase && phase === "form" ? (
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => void createAndTest()}
          disabled={busy !== null}
        >
          {busy === "create" ? (
            <LoaderCircle size={13} className="spin" />
          ) : (
            <Wrench size={13} />
          )}
          创建并测试
        </button>
      ) : null}
    </>
  );

  return (
    <Modal
      open={open}
      onClose={() => void cancelAndDispose()}
      title="添加 MCP 工具"
      description="AI 从受控目录推荐配置，或手动注册 stdio MCP Server；启用前都需要测试连接并勾选允许的工具。"
      icon={<Plug size={18} />}
      width={700}
      closeOnBackdrop={busy === null && !suggesting && usingCatalog === null}
      footer={footer}
    >
      {aiTabVisible ? (
        <div className="mcp-tabs" role="tablist" aria-label="添加工具方式">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "ai"}
            className={`mcp-tab${tab === "ai" ? " active" : ""}`}
            onClick={() => setTab("ai")}
          >
            <Sparkles size={13} />
            AI 帮我配
            <span className="mcp-tab-badge">推荐</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "manual"}
            className={`mcp-tab${tab === "manual" ? " active" : ""}`}
            onClick={() => setTab("manual")}
          >
            <Wrench size={13} />
            手动添加
          </button>
        </div>
      ) : null}

      {tab === "ai" && phase === "form" ? (
        <div className="mcp-ai">
          <div className="mcp-ai-intro">
            <Sparkles size={14} />
            <div>
              描述你想让模型做什么，AI 会从后端受控目录里挑选合适的 MCP Server 配置。
              <b>AI 不会自动执行或安装任何命令</b>；使用建议后仍需测试连接、勾选允许的工具才会启用。
            </div>
          </div>
          <label className="field">
            <span className="field-label">
              <span>你的需求</span>
              <span className="field-hint">AI 只推荐配置，不执行命令</span>
            </span>
            <textarea
              className="textarea mcp-ai-textarea"
              value={need}
              onChange={(event) => {
                setNeed(event.target.value);
                setAiError(null);
              }}
              placeholder="例如：我想让模型读取 data 目录下的文档 / 查询我的 SQLite / 搜索 GitHub 仓库 / 抓取网页内容"
              aria-label="AI 需求描述"
              spellCheck={false}
            />
          </label>
          <div className="mcp-ai-examples">
            {AI_EXAMPLES.map((example) => (
              <button
                key={example}
                type="button"
                className="tag-chip"
                onClick={() => {
                  setNeed(example);
                  setAiError(null);
                }}
              >
                {example}
              </button>
            ))}
          </div>
          <div className="mcp-ai-actions">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void askAi()}
              disabled={suggesting}
            >
              {suggesting ? (
                <LoaderCircle size={14} className="spin" />
              ) : (
                <Sparkles size={14} />
              )}
              {suggesting ? "AI 正在推荐…" : "让 AI 推荐工具"}
            </button>
            <span className="v2-hint">需要先在「模型接入」中配置大模型</span>
          </div>

          {aiError ? (
            <div className="mcp-ai-error" role="alert">
              <TriangleAlert size={15} />
              <div className="mcp-ai-error-main">
                <div className="mcp-ai-error-title">AI 推荐失败</div>
                <div className="mcp-ai-error-text">{aiError}</div>
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => setTab("manual")}
                >
                  切换到手动添加
                </button>
              </div>
            </div>
          ) : null}

          {suggestions.length > 0 ? (
            <>
              <div className="mcp-ai-results-head">
                <span>
                  <Sparkles size={12} /> AI 推荐 {suggestions.length} 个配置
                </span>
                <span className="mcp-ai-results-hint">来自受控目录 · 不会自动执行/安装</span>
              </div>
              <div className="mcp-ai-cards">
                {suggestions.map((suggestion) => {
                  const isBuiltin = suggestion.kind === "builtin";
                  const using = usingCatalog === suggestion.catalog_id;
                  return (
                    <div className="mcp-ai-card" key={suggestion.catalog_id}>
                      <div className="mcp-ai-card-head">
                        <span className={`mcp-ai-kind${isBuiltin ? " builtin" : " custom"}`}>
                          {isBuiltin ? "内置" : "自定义"}
                        </span>
                        <span className="mcp-ai-name">{suggestion.name}</span>
                        {suggestion.requires_token ? (
                          <span className="mcp-ai-token-badge">需要 Token</span>
                        ) : null}
                      </div>
                      <div className="mcp-ai-desc">{suggestion.description}</div>
                      {suggestion.rationale ? (
                        <div className="mcp-ai-rationale">
                          <Sparkles size={10} />
                          {suggestion.rationale}
                        </div>
                      ) : null}
                      {suggestion.command ? (
                        <div
                          className="mcp-ai-command mono"
                          title={`${suggestion.command} ${(suggestion.args ?? []).join(" ")}`}
                        >
                          {suggestion.command} {(suggestion.args ?? []).join(" ")}
                        </div>
                      ) : null}
                      {suggestion.test_note ? (
                        <div className="mcp-ai-note">
                          <ShieldCheck size={10} />
                          {suggestion.test_note}
                        </div>
                      ) : null}
                      <button
                        type="button"
                        className="btn btn-sm btn-primary mcp-ai-use"
                        onClick={() => void useSuggestion(suggestion)}
                        disabled={usingCatalog !== null}
                      >
                        {using ? <LoaderCircle size={12} className="spin" /> : null}
                        {isBuiltin ? "使用这个配置（启用内置）" : "使用这个配置（预填表单）"}
                      </button>
                    </div>
                  );
                })}
              </div>
              {aiNotes ? <div className="mcp-ai-notes">{aiNotes}</div> : null}
            </>
          ) : null}

          <div className="v2-note">
            <ShieldAlert size={13} />
            安全说明：AI 只能从后端受控目录推荐 MCP Server，不会自动执行、安装或修改系统；所有工具仍需测试
            连接并由你勾选 allowed_tools 后才会被对话调用。
          </div>
        </div>
      ) : null}

      {manualPhase && phase === "form" ? (
        <div className="mcp-wizard">
          {aiPrefill ? (
            <div className="mcp-ai-prefill">
              <Sparkles size={13} />
              <div>
                已套用 AI 推荐配置{aiPrefill.rationale ? `：${aiPrefill.rationale}` : ""}
                {aiPrefill.requiresToken ? "（需要 Token，请填写下方环境变量值）" : ""}
                {aiPrefill.testNote ? ` · ${aiPrefill.testNote}` : ""}
              </div>
            </div>
          ) : null}
          <div className="mcp-wizard-grid">
            <label className="field">
              <span className="field-label">
                <span>显示名称</span>
                <span className="field-hint">最多 40 字</span>
              </span>
              <input
                className="input"
                value={name}
                onChange={(event) => {
                  setName(event.target.value);
                  setError(null);
                }}
                placeholder="例如：我的文件工具"
                aria-label="MCP 显示名称"
                spellCheck={false}
              />
            </label>
            <label className="field">
              <span className="field-label">
                <span>启动命令</span>
                <span className="field-hint">仅支持 stdio</span>
              </span>
              <input
                className="input"
                value={command}
                onChange={(event) => {
                  setCommand(event.target.value);
                  setError(null);
                }}
                placeholder="npx / python / uvx / 可执行文件路径"
                aria-label="MCP 启动命令"
                spellCheck={false}
              />
            </label>
          </div>
          <label className="field">
            <span className="field-label">
              <span>参数（每行一个）</span>
              <span className="field-hint">原样传给 stdio 进程，按顺序排列</span>
            </span>
            <textarea
              className="textarea mcp-wizard-textarea"
              value={argsText}
              onChange={(event) => {
                setArgsText(event.target.value);
                setError(null);
              }}
              placeholder={"-y\n@modelcontextprotocol/server-filesystem\n/你的/目录"}
              aria-label="MCP 启动参数"
              spellCheck={false}
            />
          </label>
          <label className="field">
            <span className="field-label">
              <span>环境变量（可选）</span>
              <span className="field-hint">每行 KEY=VALUE；仅保存在本机，接口不返回明文</span>
            </span>
            <textarea
              className="textarea mcp-wizard-textarea"
              value={envText}
              onChange={(event) => {
                setEnvText(event.target.value);
                setError(null);
              }}
              placeholder={"GITHUB_TOKEN=ghp_...\nFOO=bar"}
              aria-label="MCP 环境变量"
              spellCheck={false}
            />
          </label>
          <div className="mcp-safety-note">
            <ShieldAlert size={14} />
            <div>
              <b>安全提示：</b>自定义 MCP Server 的命令会在本机执行，且可能包含写操作。请只添加你信任的
              Server；创建后默认关闭，需要先测试发现工具、勾选允许的工具后才能启用。内置工具仍保持只读。
            </div>
          </div>
          {error ? (
            <div className="settings-inline-error" role="alert">
              <TriangleAlert size={14} />
              <span>{error}</span>
            </div>
          ) : null}
        </div>
      ) : null}

      {phase === "testing" ? (
        <div className="mcp-wizard-loading" role="status" aria-live="polite">
          <LoaderCircle size={16} className="spin" />
          <div>
            <div className="mcp-wizard-loading-title">
              {busy === "create" ? "正在创建自定义 MCP Server…" : "正在连接并发现工具…"}
            </div>
            <div className="mcp-wizard-loading-desc">
              会以 stdio 方式启动一次 Server 并读取工具列表；首次启动可能需要几秒。
            </div>
          </div>
        </div>
      ) : null}

      {phase === "tools" ? (
        <div className="mcp-wizard">
          {aiPrefill ? (
            <div className="mcp-ai-prefill">
              <Sparkles size={13} />
              <div>
                来自 AI 推荐配置{aiPrefill.rationale ? `：${aiPrefill.rationale}` : ""}
              </div>
            </div>
          ) : null}
          {draft ? (
            <div className="mcp-wizard-server">
              <Terminal size={13} />
              <span className="mcp-wizard-server-name">{draft.name}</span>
              <span
                className="mcp-wizard-server-command mono"
                title={`${draft.command} ${(draft.args ?? []).join(" ")}`}
              >
                {draft.command} {(draft.args ?? []).join(" ")}
              </span>
            </div>
          ) : null}

          {testError ? (
            <div className="mcp-wizard-test-error" role="alert">
              <TriangleAlert size={14} />
              <div>
                <div className="mcp-wizard-test-error-title">连接测试失败</div>
                <div className="mcp-wizard-test-error-text">{testError}</div>
                <div className="mcp-wizard-test-error-hint">
                  请检查启动命令、参数与环境变量后可「重新测试」；返回修改会删除当前草稿。
                </div>
              </div>
            </div>
          ) : (
            <>
              <div className="mcp-wizard-notice">
                <ShieldAlert size={13} />
                <span>
                  已发现 <b>{tools.length}</b> 个工具。自定义 Server 可能包含写操作，
                  <b>默认全不勾选</b>，请只勾选你信任的工具（对话只会调用勾选的工具）。
                </span>
              </div>
              <div className="mcp-wizard-tools" aria-label="发现的自定义工具">
                {tools.map((tool) => {
                  const checked = selected.has(tool.name);
                  const readonly = tool.read_only !== false;
                  return (
                    <label
                      key={tool.name}
                      className={`mcp-wizard-tool${checked ? " is-checked" : ""}`}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleTool(tool.name)}
                        aria-label={`允许工具 ${tool.name}`}
                      />
                      <span className={`mcp-wizard-tool-check${checked ? " on" : ""}`}>
                        {checked ? <Check size={11} /> : null}
                      </span>
                      <span className="mcp-wizard-tool-main">
                        <span className="mcp-wizard-tool-head">
                          <span className="mcp-wizard-tool-name mono">{tool.name}</span>
                          {readonly ? (
                            <span className="mcp-wizard-tool-badge">只读</span>
                          ) : (
                            <span className="mcp-wizard-tool-badge is-write">
                              <TriangleAlert size={9} /> 可能写数据
                            </span>
                          )}
                        </span>
                        {tool.description ? (
                          <span className="mcp-wizard-tool-desc">{tool.description}</span>
                        ) : null}
                      </span>
                    </label>
                  );
                })}
              </div>
              <div className="mcp-wizard-tool-actions">
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => setSelected(new Set(readonlyTools.map((tool) => tool.name)))}
                >
                  全选只读工具（{readonlyTools.length}）
                </button>
                <button
                  type="button"
                  className="btn btn-sm btn-ghost"
                  onClick={() => setSelected(new Set())}
                >
                  清空选择
                </button>
                <span className="mcp-wizard-selected">
                  已选 {selected.size} / {tools.length}
                </span>
              </div>
            </>
          )}

          {error ? (
            <div className="settings-inline-error" role="alert">
              <TriangleAlert size={14} />
              <span>{error}</span>
            </div>
          ) : null}
        </div>
      ) : null}
    </Modal>
  );
}
