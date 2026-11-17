import { useCallback, useEffect, useRef, useState } from "react";
import {
  Bot,
  Check,
  Cpu,
  Database,
  Eye,
  EyeOff,
  Info,
  KeyRound,
  LoaderCircle,
  RefreshCw,
  Save,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  TriangleAlert,
  Waypoints,
  Wifi,
  X,
} from "lucide-react";
import { ConfirmDialog } from "../components/Modal";
import { AboutCard } from "./settings/AboutCard";
import { EmbeddingCard } from "./settings/EmbeddingCard";
import { EnrichmentCard } from "./settings/EnrichmentCard";
import { ExtensionStatusCard } from "./settings/ExtensionStatusCard";
import { McpToolsCard } from "./settings/McpToolsCard";
import { PatrolCard } from "./settings/PatrolCard";
import { RerankerCard } from "./settings/RerankerCard";
import {
  reseedData,
  testSettings,
  toUserMessage,
  updateSettings,
} from "../lib/api";
import { formatNumber } from "../lib/format";
import { navigate } from "../lib/router";
import { useApp } from "../state/AppContext";
import type { SettingsData, SettingsTestResult } from "../types";

interface Preset {
  key: "deepseek" | "openai" | "ollama" | "custom";
  label: string;
  baseUrl: string;
  model: string;
  icon: typeof Sparkles;
}

const PRESETS: Preset[] = [
  {
    key: "deepseek",
    label: "DeepSeek",
    baseUrl: "https://api.deepseek.com/v1",
    model: "deepseek-chat",
    icon: Sparkles,
  },
  {
    key: "openai",
    label: "OpenAI",
    baseUrl: "https://api.openai.com/v1",
    model: "gpt-4o-mini",
    icon: Bot,
  },
  {
    key: "ollama",
    label: "Ollama",
    baseUrl: "http://127.0.0.1:11434/v1",
    model: "qwen2.5:7b",
    icon: Cpu,
  },
  {
    key: "custom",
    label: "自定义",
    baseUrl: "",
    model: "",
    icon: SlidersHorizontal,
  },
];

interface FormState {
  baseUrl: string;
  model: string;
  temperature: number;
}

function formFromSettings(settings: SettingsData | null): FormState {
  return {
    baseUrl: settings?.llm.base_url ?? "",
    model: settings?.llm.model ?? "",
    temperature: typeof settings?.llm.temperature === "number" ? settings.llm.temperature : 0.2,
  };
}

function detectPreset(baseUrl: string, model: string): Preset["key"] {
  const normalized = baseUrl.toLowerCase();
  if (normalized.includes("deepseek")) return "deepseek";
  if (normalized.includes("openai.com") || normalized.includes("api.openai")) return "openai";
  if (normalized.includes("11434") || normalized.includes("ollama")) return "ollama";
  if (!normalized && !model) return "custom";
  return "custom";
}

export function SettingsView() {
  const {
    settings,
    refreshSettings,
    refreshHealth,
    stats,
    refreshStats,
    bumpGraphRevision,
    toast,
  } = useApp();

  const [form, setForm] = useState<FormState>(() => formFromSettings(settings));
  const [preset, setPreset] = useState<Preset["key"]>(() =>
    detectPreset(settings?.llm.base_url ?? "", settings?.llm.model ?? ""),
  );
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [clearKeyPending, setClearKeyPending] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<SettingsTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false);
  const [reseedConfirmOpen, setReseedConfirmOpen] = useState(false);
  const [reseeding, setReseeding] = useState(false);
  const [settingsLoadError, setSettingsLoadError] = useState<string | null>(null);
  const initializedRef = useRef(false);

  const loadSettings = useCallback(async () => {
    setSettingsLoadError(null);
    const data = await refreshSettings();
    if (!data) {
      setSettingsLoadError("无法读取模型配置，请确认后端服务是否正常运行。");
      return;
    }
    if (!initializedRef.current) {
      initializedRef.current = true;
      setForm(formFromSettings(data));
      setPreset(detectPreset(data.llm.base_url, data.llm.model));
    }
  }, [refreshSettings]);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  const configured = settings?.llm.configured ?? false;
  const apiKeySet = settings?.llm.api_key_set ?? false;

  const applyPreset = (next: Preset): void => {
    setPreset(next.key);
    if (next.key === "custom") return;
    setForm((prev) => ({ ...prev, baseUrl: next.baseUrl, model: next.model }));
    setFormError(null);
  };

  const buildPayload = () => {
    const llm: {
      base_url: string;
      model: string;
      api_key?: string;
      temperature: number;
    } = {
      base_url: form.baseUrl.trim(),
      model: form.model.trim(),
      temperature: form.temperature,
    };
    if (clearKeyPending) {
      llm.api_key = "";
    } else if (apiKey.trim()) {
      llm.api_key = apiKey.trim();
    }
    return { llm };
  };

  const handleSave = async (): Promise<void> => {
    if (saving) return;
    const baseUrl = form.baseUrl.trim();
    const model = form.model.trim();
    if ((baseUrl && !model) || (!baseUrl && model)) {
      setFormError("Base URL 与模型名需要同时填写，或同时留空。");
      return;
    }
    if (baseUrl && !/^https?:\/\/.+/i.test(baseUrl)) {
      setFormError("Base URL 需要以 http:// 或 https:// 开头。");
      return;
    }
    if (!Number.isFinite(form.temperature) || form.temperature < 0 || form.temperature > 2) {
      setFormError("温度需要设置在 0 到 2 之间。");
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      const updated = await updateSettings(buildPayload());
      setApiKey("");
      setClearKeyPending(false);
      setForm(formFromSettings(updated));
      setPreset(detectPreset(updated.llm.base_url, updated.llm.model));
      toast("success", "模型配置已保存", updated.llm.configured ? "AI 模式已启用。" : "当前仍为本地检索模式。");
      await loadSettings();
      void refreshHealth();
    } catch (error) {
      setFormError(toUserMessage(error));
      toast("error", "保存失败", toUserMessage(error));
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async (): Promise<void> => {
    if (testing) return;
    setTesting(true);
    setTestError(null);
    setTestResult(null);
    try {
      const result = await testSettings();
      setTestResult(result);
    } catch (error) {
      setTestError(toUserMessage(error));
    } finally {
      setTesting(false);
    }
  };

  const handleClear = async (): Promise<void> => {
    setClearConfirmOpen(false);
    try {
      await updateSettings({
        llm: { base_url: "", model: "", api_key: "", temperature: 0.2 },
      });
      setApiKey("");
      setClearKeyPending(false);
      setForm({ baseUrl: "", model: "", temperature: 0.2 });
      setPreset("custom");
      setTestResult(null);
      toast("success", "模型配置已清除", "知脉已回到完全离线的本地检索模式。");
      await loadSettings();
      void refreshHealth();
    } catch (error) {
      toast("error", "清除失败", toUserMessage(error));
    }
  };

  const handleReseed = async (): Promise<void> => {
    if (reseeding) return;
    setReseeding(true);
    try {
      const result = await reseedData();
      toast(
        "success",
        "演示数据已重置",
        `成功导入 ${result.imported} 篇文档，跳过 ${result.skipped} 篇。`,
      );
      setReseedConfirmOpen(false);
      void refreshStats();
      void refreshHealth();
      bumpGraphRevision();
      await loadSettings();
    } catch (error) {
      toast("error", "重置失败", toUserMessage(error));
    } finally {
      setReseeding(false);
    }
  };

  const data = settings?.data ?? { documents: stats?.documents ?? 0, chunks: stats?.chunks ?? 0 };

  return (
    <div className="page-scroll">
      <div className="page settings-page">
        <div className="page-header">
          <div>
            <h1 className="page-heading">设置</h1>
            <p className="page-sub">
              接入你的大模型，管理知识库数据与智能能力，让知脉更懂你。
            </p>
          </div>
        </div>

        {settingsLoadError ? (
          <div className="page-error" style={{ marginBottom: 18 }}>
            <TriangleAlert size={17} />
            <div style={{ flex: 1 }}>
              <div style={{ color: "var(--text)", fontWeight: 600, marginBottom: 3 }}>
                无法读取配置
              </div>
              <div>{settingsLoadError}</div>
              <button
                type="button"
                className="btn btn-sm"
                style={{ marginTop: 12 }}
                onClick={() => void loadSettings()}
              >
                <RefreshCw size={12} /> 重试
              </button>
            </div>
          </div>
        ) : null}

        <div className="settings-grid">
          {/* 模型接入 */}
          <section className="card settings-card">
            <div className="settings-card-head">
              <div className="settings-card-icon cyan">
                <Cpu size={18} />
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <h2 className="settings-card-title">模型接入</h2>
                <p className="settings-card-desc">
                  接入任意 OpenAI 兼容模型（DeepSeek / OpenAI / Ollama / 自定义）。未配置时使用
                  完全离线的本地检索模式。
                </p>
              </div>
              <span className={`badge ${configured ? "badge-mint" : "badge-cyan"}`}>
                <span className={`status-dot ${configured ? "pulse" : "is-warn"}`} />
                {configured ? "已接入" : "本地检索"}
              </span>
            </div>

            <div className="settings-body">
              <div className="provider-grid">
                {PRESETS.map((item) => {
                  const Icon = item.icon;
                  const active = preset === item.key;
                  return (
                    <button
                      key={item.key}
                      type="button"
                      className={`provider-btn${active ? " active" : ""}`}
                      onClick={() => applyPreset(item)}
                      aria-pressed={active}
                    >
                      <Icon size={15} />
                      <span>{item.label}</span>
                      {active ? <Check size={12} className="provider-check" /> : null}
                    </button>
                  );
                })}
              </div>

              <div className="settings-fields">
                <label className="field">
                  <span className="field-label">
                    <span>Base URL</span>
                    <span className="field-hint">OpenAI 兼容接口地址</span>
                  </span>
                  <input
                    className="input"
                    value={form.baseUrl}
                    placeholder="https://api.deepseek.com/v1"
                    onChange={(event) => {
                      setForm((prev) => ({ ...prev, baseUrl: event.target.value }));
                      setPreset("custom");
                    }}
                    spellCheck={false}
                  />
                </label>

                <label className="field">
                  <span className="field-label">
                    <span>模型名</span>
                    <span className="field-hint">例如 deepseek-chat、gpt-4o-mini</span>
                  </span>
                  <input
                    className="input"
                    value={form.model}
                    placeholder="deepseek-chat"
                    onChange={(event) => {
                      setForm((prev) => ({ ...prev, model: event.target.value }));
                      setPreset((prev) => (prev === "custom" ? prev : "custom"));
                    }}
                    spellCheck={false}
                  />
                </label>

                <div className="field">
                  <span className="field-label">
                    <span>API Key</span>
                    <span className="field-hint">
                      {apiKeySet ? "已保存，留空表示不修改" : "Ollama 等本地模型可留空"}
                    </span>
                  </span>
                  <div className="input-wrap">
                    <KeyRound size={15} />
                    <input
                      className="input"
                      type={showKey ? "text" : "password"}
                      value={apiKey}
                      placeholder={clearKeyPending ? "保存后会清除已保存的 Key" : "sk-…"}
                      onChange={(event) => {
                        setApiKey(event.target.value);
                        setClearKeyPending(false);
                      }}
                      autoComplete="off"
                      spellCheck={false}
                    />
                    <span className="input-suffix">
                      <button
                        type="button"
                        className="btn btn-icon"
                        style={{ width: 28, height: 28 }}
                        onClick={() => setShowKey((value) => !value)}
                        aria-label={showKey ? "隐藏 API Key" : "显示 API Key"}
                      >
                        {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
                      </button>
                    </span>
                  </div>
                  <div className="key-extra-row">
                    <span className="field-hint">
                      Key 仅保存在本机数据库，接口永不返回明文。
                    </span>
                    {apiKeySet ? (
                      <button
                        type="button"
                        className="key-clear"
                        onClick={() => {
                          setClearKeyPending((value) => !value);
                          setApiKey("");
                        }}
                      >
                        {clearKeyPending ? (
                          <>
                            <X size={11} /> 取消清除
                          </>
                        ) : (
                          <>
                            <Trash2 size={11} /> 清除已保存的 Key
                          </>
                        )}
                      </button>
                    ) : null}
                  </div>
                </div>

                <div className="field">
                  <span className="field-label">
                    <span>温度</span>
                    <span className="field-hint">数值越低回答越稳定，推荐 0.2</span>
                  </span>
                  <div className="temperature-row">
                    <input
                      type="range"
                      min={0}
                      max={1.5}
                      step={0.05}
                      value={form.temperature}
                      className="range"
                      onChange={(event) =>
                        setForm((prev) => ({ ...prev, temperature: Number(event.target.value) }))
                      }
                      aria-label="模型温度"
                    />
                    <span className="temperature-value mono">{form.temperature.toFixed(2)}</span>
                  </div>
                </div>
              </div>

              {formError ? (
                <div className="settings-inline-error">
                  <TriangleAlert size={14} />
                  <span>{formError}</span>
                </div>
              ) : null}

              {testResult ? (
                <div className={`test-result ${testResult.ok ? "ok" : "fail"}`}>
                  {testResult.ok ? <Wifi size={15} /> : <TriangleAlert size={15} />}
                  <div>
                    <div className="test-result-title">
                      {testResult.ok ? "连接成功" : "连接失败"}
                      {testResult.ok && testResult.latency_ms != null
                        ? ` · ${Math.round(testResult.latency_ms)} ms`
                        : ""}
                      {testResult.ok && testResult.model ? ` · ${testResult.model}` : ""}
                    </div>
                    <div className="test-result-msg">{testResult.message}</div>
                  </div>
                </div>
              ) : testError ? (
                <div className="test-result fail">
                  <TriangleAlert size={15} />
                  <div>
                    <div className="test-result-title">测试请求失败</div>
                    <div className="test-result-msg">{testError}</div>
                  </div>
                </div>
              ) : null}

              <div className="settings-actions">
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void handleSave()}
                  disabled={saving}
                >
                  {saving ? <LoaderCircle size={14} className="spin" /> : <Save size={14} />}
                  {saving ? "保存中…" : "保存配置"}
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => void handleTest()}
                  disabled={testing}
                >
                  {testing ? <LoaderCircle size={14} className="spin" /> : <Wifi size={14} />}
                  {testing ? "测试中…" : "测试连接"}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  style={{ marginLeft: "auto", color: "var(--dim)" }}
                  onClick={() => setClearConfirmOpen(true)}
                  disabled={!configured && !apiKeySet}
                >
                  <Trash2 size={13} />
                  清除模型配置
                </button>
              </div>
              <div className="settings-foot-hint">
                <Info size={12} />
                测试连接使用已保存的配置，修改后请先保存再测试。
              </div>
            </div>
          </section>

          {/* 右栏 */}
          <div className="settings-side">
            <section className="card settings-card">
              <div className="settings-card-head">
                <div className="settings-card-icon violet">
                  <Database size={18} />
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <h2 className="settings-card-title">数据与存储</h2>
                  <p className="settings-card-desc">知识库数据存放在本机 SQLite，完全自托管。</p>
                </div>
              </div>
              <div className="settings-body">
                <div className="data-stat-grid">
                  <div className="data-stat">
                    <span className="data-stat-value mono">{formatNumber(data.documents)}</span>
                    <span className="data-stat-label">文档</span>
                  </div>
                  <div className="data-stat">
                    <span className="data-stat-value mono">{formatNumber(data.chunks)}</span>
                    <span className="data-stat-label">分块</span>
                  </div>
                  <div className="data-stat">
                    <span className="data-stat-value mono">
                      {formatNumber(stats?.concepts ?? 0)}
                    </span>
                    <span className="data-stat-label">概念</span>
                  </div>
                  <div className="data-stat">
                    <span className="data-stat-value mono">{formatNumber(stats?.links ?? 0)}</span>
                    <span className="data-stat-label">连接</span>
                  </div>
                </div>

                <div className="settings-row">
                  <div className="settings-row-main">
                    <span className="settings-row-title">演示数据</span>
                    <span className="settings-row-desc">
                      清空当前知识库并重新导入内置演示文档，不会删除对话历史。
                    </span>
                  </div>
                  <button
                    type="button"
                    className="btn btn-sm btn-danger"
                    onClick={() => setReseedConfirmOpen(true)}
                    disabled={reseeding}
                  >
                    {reseeding ? <LoaderCircle size={13} className="spin" /> : <RefreshCw size={13} />}
                    重置
                  </button>
                </div>

                <button
                  type="button"
                  className="settings-link"
                  onClick={() => navigate("/graph")}
                >
                  <Waypoints size={14} />
                  查看知识图谱
                </button>
              </div>
            </section>

            <EnrichmentCard />

            <ExtensionStatusCard />
          </div>
        </div>

        {/* V2 新增：MCP 工具 / 语义向量 / 反馈偏好 / 知识巡检 */}
        <div className="settings-v2-grid">
          <div className="settings-v2-col">
            <McpToolsCard />
            <EmbeddingCard />
          </div>
          <div className="settings-v2-col">
            <RerankerCard />
            <PatrolCard />
            <AboutCard />
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={clearConfirmOpen}
        title="清除模型配置？"
        description="清除后知脉将回到完全离线的本地检索模式，API Key 也会从数据库删除。此操作不可撤销。"
        confirmLabel="清除配置"
        loading={false}
        icon={<Trash2 size={16} />}
        onConfirm={() => void handleClear()}
        onClose={() => setClearConfirmOpen(false)}
      />

      <ConfirmDialog
        open={reseedConfirmOpen}
        title="重置演示数据？"
        description={
          <span>
            当前知识库中的 <strong>{data.documents}</strong> 份文档、全部分块、概念与图谱连接将被清空，
            然后重新导入内置演示文档。对话历史不会被删除。此操作不可撤销。
          </span>
        }
        confirmLabel="重置数据"
        loading={reseeding}
        icon={<RefreshCw size={16} />}
        onConfirm={() => void handleReseed()}
        onClose={() => setReseedConfirmOpen(false)}
      />
    </div>
  );
}
