import { useCallback, useEffect, useRef, useState } from "react";
import type { LucideIcon } from "lucide-react";
import {
  Boxes,
  Check,
  Cloud,
  Cpu,
  Eye,
  EyeOff,
  Hash,
  KeyRound,
  LoaderCircle,
  RefreshCw,
  ScanLine,
  Save,
  Sparkles,
  TriangleAlert,
  Wifi,
} from "lucide-react";
import { ConfirmDialog } from "../../components/Modal";
import {
  getEmbeddingStatus,
  rebuildEmbeddings,
  testEmbedding,
  toUserMessage,
  updateEmbeddingConfig,
} from "../../lib/api";
import { formatDateTime, formatNumber } from "../../lib/format";
import { useApp } from "../../state/AppContext";
import type {
  EmbeddingConfigInput,
  EmbeddingRebuildResult,
  EmbeddingStatus,
  EmbeddingTestResult,
  OcrStatus,
} from "../../types";

interface ProviderOption {
  key: string;
  label: string;
  desc: string;
  icon: LucideIcon;
}

const PROVIDERS: ProviderOption[] = [
  { key: "hash", label: "本地哈希", desc: "最快 · 弱语义 · 兼容旧数据", icon: Hash },
  { key: "fastembed", label: "本地语义模型", desc: "推荐 · 真实语义 · 完全本地", icon: Cpu },
  { key: "openai", label: "API（OpenAI 兼容）", desc: "外部嵌入接口 · 需 Key", icon: Cloud },
];

const FAST_EMBED_MODELS: { value: string; label: string }[] = [
  {
    value: "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    label: "paraphrase-multilingual-MiniLM-L12-v2 · 多语言 384 维",
  },
  { value: "BAAI/bge-small-zh-v1.5", label: "bge-small-zh-v1.5 · 中文 512 维" },
];

const PROVIDER_LABELS: Record<string, string> = {
  hash: "本地哈希",
  fastembed: "本地语义模型",
  openai: "API（OpenAI 兼容）",
};

function providerLabel(provider: string | null | undefined): string {
  if (!provider) return "未知";
  return PROVIDER_LABELS[provider] ?? provider;
}

function fastEmbedDefaultModel(): string {
  return FAST_EMBED_MODELS[0].value;
}

function normalizeModel(provider: string, model: string | undefined | null): string {
  if (provider !== "fastembed") return model ?? "";
  const value = (model ?? "").trim();
  if (!value) return fastEmbedDefaultModel();
  return value;
}

function lastRebuildLabel(value: EmbeddingStatus["last_rebuild"]): string {
  if (!value) return "—";
  if (typeof value === "string") {
    const formatted = formatDateTime(value);
    return formatted === "—" ? "已记录" : formatted;
  }
  if (typeof value === "object") return "已记录";
  return "—";
}

export function EmbeddingCard() {
  const { health, refreshHealth, refreshStats, toast } = useApp();
  const [status, setStatus] = useState<EmbeddingStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [provider, setProvider] = useState("hash");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [configError, setConfigError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<EmbeddingTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  const [rebuildOpen, setRebuildOpen] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildSeconds, setRebuildSeconds] = useState(0);
  const [rebuildResult, setRebuildResult] = useState<EmbeddingRebuildResult | null>(null);
  const [rebuildError, setRebuildError] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);
  const initializedRef = useRef(false);

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const next = await getEmbeddingStatus();
      setStatus(next);
      setError(null);
      if (!initializedRef.current) {
        initializedRef.current = true;
        const nextProvider = next.provider || "hash";
        setProvider(nextProvider);
        setModel(normalizeModel(nextProvider, next.model));
      }
    } catch (err) {
      setError(toUserMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!rebuilding) {
      if (timerRef.current != null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
      return;
    }
    setRebuildSeconds(0);
    timerRef.current = window.setInterval(() => setRebuildSeconds((value) => value + 1), 1000);
    return () => {
      if (timerRef.current != null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [rebuilding]);

  const chooseProvider = (next: ProviderOption): void => {
    setProvider(next.key);
    setConfigError(null);
    setTestResult(null);
    setTestError(null);
    if (next.key === "fastembed") {
      setModel((prev) => normalizeModel("fastembed", prev || status?.model));
    }
  };

  const buildPayload = (): EmbeddingConfigInput | null => {
    if (provider === "openai") {
      const url = baseUrl.trim();
      const modelName = model.trim();
      if (!url || !modelName) {
        setConfigError("API 模式需要同时填写 Base URL 与模型名。");
        return null;
      }
      if (!/^https?:\/\/.+/i.test(url)) {
        setConfigError("Base URL 需要以 http:// 或 https:// 开头。");
        return null;
      }
      const payload: EmbeddingConfigInput = { provider, base_url: url, model: modelName };
      if (apiKey.trim()) payload.api_key = apiKey.trim();
      return payload;
    }
    if (provider === "fastembed") {
      const modelName = normalizeModel("fastembed", model);
      if (!modelName) {
        setConfigError("请选择语义模型。");
        return null;
      }
      return { provider, model: modelName };
    }
    return { provider };
  };

  const handleSave = async (): Promise<void> => {
    if (saving) return;
    setConfigError(null);
    const payload = buildPayload();
    if (!payload) return;
    setSaving(true);
    try {
      const next = await updateEmbeddingConfig(payload);
      if (next && typeof next === "object" && "provider" in next) {
        setStatus(next as EmbeddingStatus);
      } else {
        await load();
      }
      setApiKey("");
      setTestResult(null);
      setTestError(null);
      toast(
        "success",
        "向量配置已保存",
        payload.provider === "hash"
          ? "本地哈希模式已生效；如已切换过语义模型，请重建全部向量。"
          : "如当前向量与配置不一致，请重建全部向量后检索才会使用新向量。",
      );
      void refreshHealth();
      void refreshStats();
    } catch (err) {
      setConfigError(toUserMessage(err));
      toast("error", "保存配置失败", toUserMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async (): Promise<void> => {
    if (testing) return;
    setTesting(true);
    setTestResult(null);
    setTestError(null);
    try {
      const result = await testEmbedding();
      setTestResult(result);
    } catch (err) {
      setTestError(toUserMessage(err));
    } finally {
      setTesting(false);
    }
  };

  const openRebuild = (): void => {
    if (rebuilding) return;
    setRebuildOpen(true);
  };

  const handleRebuild = async (): Promise<void> => {
    if (rebuilding) return;
    setRebuildOpen(false);
    setRebuilding(true);
    setRebuildResult(null);
    setRebuildError(null);
    try {
      const result = await rebuildEmbeddings();
      setRebuildResult(result);
      toast(
        "success",
        "向量重建完成",
        `已重建 ${formatNumber(result.chunks ?? status?.total_chunks ?? 0)} 个分块向量。`,
      );
      await load();
      void refreshHealth();
      void refreshStats();
    } catch (err) {
      const message = toUserMessage(err);
      setRebuildError(message);
      toast("error", "向量重建失败", message);
    } finally {
      setRebuilding(false);
    }
  };

  const currentProvider = status?.provider || provider;
  const dim = status?.dim ?? 384;
  const totalChunks = status?.total_chunks ?? 0;
  const needsRebuild = status?.needs_rebuild ?? false;
  const ocr: OcrStatus | null | undefined = health?.ocr ?? status?.ocr ?? null;
  const ocrReady = ocr?.available ?? false;
  const ocrMaxPages = ocr?.max_pages ?? 30;
  const ocrEngine = (ocr?.engine ?? "rapidocr").toLowerCase() === "rapidocr" ? "RapidOCR" : ocr?.engine || "RapidOCR";

  const modelOptions = (() => {
    const known = FAST_EMBED_MODELS.map((item) => item.value);
    if (model && !known.includes(model)) {
      return [{ value: model, label: `${model} · 当前配置` }, ...FAST_EMBED_MODELS];
    }
    return FAST_EMBED_MODELS;
  })();

  return (
    <section className="card settings-card embed-card" data-card="embedding" aria-label="语义向量">
      <div className="settings-card-head">
        <div className="settings-card-icon cyan">
          <Boxes size={18} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 className="settings-card-title">语义向量</h2>
          <p className="settings-card-desc">
            选择把文本转成向量的方式。切换向量化方式后需重建全部向量，检索才会使用新的语义表示。
          </p>
        </div>
        <span className={`badge ${needsRebuild ? "badge-amber" : "badge-mint"}`}>
          {needsRebuild ? "待重建" : "就绪"}
        </span>
        <button
          type="button"
          className="btn btn-icon"
          onClick={() => void load()}
          disabled={loading || rebuilding}
          aria-label="刷新语义向量状态"
          data-tip="刷新"
        >
          {loading ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}
        </button>
      </div>

      <div className="settings-body">
        {error && !status ? (
          <div className="settings-inline-error">
            <TriangleAlert size={14} />
            <span>向量状态获取失败：{error}</span>
            <button type="button" className="btn btn-sm" onClick={() => void load()}>
              <RefreshCw size={12} /> 重试
            </button>
          </div>
        ) : loading && !status ? (
          <div className="v2-loading">
            <LoaderCircle size={14} className="spin" /> 正在读取向量状态…
          </div>
        ) : status ? (
          <>
            <div className="v2-stat-grid">
              <div className="v2-stat">
                <span className="v2-stat-value cyan">{providerLabel(currentProvider)}</span>
                <span className="v2-stat-label">当前向量化方式</span>
              </div>
              <div className="v2-stat">
                <span className="v2-stat-value mono">{dim} 维</span>
                <span className="v2-stat-label">向量维度</span>
              </div>
              <div className="v2-stat">
                <span className="v2-stat-value mono">{formatNumber(totalChunks)}</span>
                <span className="v2-stat-label">已建向量分块</span>
              </div>
              <div className="v2-stat">
                <span className="v2-stat-value">
                  <span className={`badge ${needsRebuild ? "badge-amber" : "badge-mint"}`}>
                    {needsRebuild ? "需重建" : "已就绪"}
                  </span>
                </span>
                <span className="v2-stat-label">
                  上次重建：{lastRebuildLabel(status.last_rebuild)}
                </span>
              </div>
            </div>

            {needsRebuild ? (
              <div className="v2-warn">
                <TriangleAlert size={14} />
                <div className="v2-warn-main">
                  当前配置与已建向量不一致，检索会继续使用旧向量。重建全部向量后新配置才会生效。
                </div>
                <button
                  type="button"
                  className="btn btn-sm btn-warn"
                  onClick={openRebuild}
                  disabled={rebuilding}
                >
                  立即重建
                </button>
              </div>
            ) : null}

            <div className="embed-provider-grid" role="radiogroup" aria-label="向量化方式">
              {PROVIDERS.map((item) => {
                const Icon = item.icon;
                const active = provider === item.key;
                return (
                  <button
                    key={item.key}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    className={`embed-provider-btn${active ? " active" : ""}`}
                    onClick={() => chooseProvider(item)}
                  >
                    <span className="embed-provider-top">
                      <Icon size={14} />
                      <span>{item.label}</span>
                      {active ? <Check size={11} className="provider-check" /> : null}
                    </span>
                    <span className="embed-provider-desc">{item.desc}</span>
                  </button>
                );
              })}
            </div>

            <div className="embed-fields">
              {provider === "fastembed" ? (
                <>
                  <label className="field">
                    <span className="field-label">
                      <span>语义模型</span>
                      <span className="field-hint">首次使用会自动下载模型，之后完全本地运行</span>
                    </span>
                    <select
                      className="select embed-select"
                      value={normalizeModel("fastembed", model)}
                      onChange={(event) => setModel(event.target.value)}
                      aria-label="fastembed 语义模型"
                    >
                      {modelOptions.map((item) => (
                        <option key={item.value} value={item.value}>
                          {item.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <div className="v2-hint">
                    <Cpu size={12} />
                    本地语义模型在 CPU 上运行，重建约 1500 个分块通常需要数十秒；失败会自动回退哈希向量。
                  </div>
                </>
              ) : provider === "openai" ? (
                <>
                  <label className="field">
                    <span className="field-label">
                      <span>Base URL</span>
                      <span className="field-hint">OpenAI 兼容 /embeddings 接口地址</span>
                    </span>
                    <input
                      className="input"
                      value={baseUrl}
                      placeholder="https://api.openai.com/v1"
                      onChange={(event) => setBaseUrl(event.target.value)}
                      spellCheck={false}
                    />
                  </label>
                  <label className="field">
                    <span className="field-label">
                      <span>模型名</span>
                      <span className="field-hint">例如 text-embedding-3-small</span>
                    </span>
                    <input
                      className="input"
                      value={model}
                      placeholder="text-embedding-3-small"
                      onChange={(event) => setModel(event.target.value)}
                      spellCheck={false}
                    />
                  </label>
                  <div className="field">
                    <span className="field-label">
                      <span>API Key</span>
                      <span className="field-hint">
                        {status.has_api_key ? "已保存，留空表示不修改" : "用于调用外部嵌入接口"}
                      </span>
                    </span>
                    <div className="input-wrap">
                      <KeyRound size={15} />
                      <input
                        className="input"
                        type={showKey ? "text" : "password"}
                        value={apiKey}
                        placeholder={status.has_api_key ? "sk-…（留空不修改）" : "sk-…"}
                        onChange={(event) => setApiKey(event.target.value)}
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
                  </div>
                  <div className="v2-hint">
                    <KeyRound size={12} />
                    Key 仅保存在本机数据库，接口永不返回明文；重建后的向量维度由接口返回决定。
                  </div>
                </>
              ) : (
                <div className="v2-hint">
                  <Hash size={12} />
                  本地哈希向量无需联网、速度最快，但语义能力较弱。切换到「本地语义模型」可显著提升中文语义检索效果。
                </div>
              )}
            </div>

            {configError ? (
              <div className="settings-inline-error">
                <TriangleAlert size={14} />
                <span>{configError}</span>
              </div>
            ) : null}

            {testResult ? (
              <div className={`v2-result${testResult.ok === false ? " is-fail" : ""}`}>
                <div className="v2-result-head">
                  <span className="v2-result-icon">
                    {testResult.ok === false ? <TriangleAlert size={13} /> : <Check size={13} />}
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="v2-result-title">
                      {testResult.ok === false ? "测试未通过" : "向量测试通过"}
                    </div>
                    <div className="v2-result-meta">
                      <span>{providerLabel(testResult.provider ?? currentProvider)}</span>
                      <span className="v2-dot" />
                      <span>
                        {testResult.dim ?? dim} 维
                        {typeof testResult.similarity === "number"
                          ? ` · 相似度 ${(testResult.similarity * 100).toFixed(1)}%`
                          : ""}
                      </span>
                      {testResult.message ? (
                        <>
                          <span className="v2-dot" />
                          <span>{testResult.message}</span>
                        </>
                      ) : null}
                    </div>
                  </div>
                </div>
              </div>
            ) : testError ? (
              <div className="v2-result is-fail">
                <div className="v2-result-head">
                  <span className="v2-result-icon">
                    <TriangleAlert size={13} />
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="v2-result-title">测试请求失败</div>
                    <div className="v2-result-meta">{testError}</div>
                  </div>
                </div>
              </div>
            ) : null}

            {rebuilding ? (
              <div className="v2-progress" role="status" aria-live="polite">
                <LoaderCircle size={14} className="spin" />
                <div>
                  <div className="v2-progress-title">正在重建全部向量…</div>
                  <div className="v2-progress-desc">
                    已用时 {rebuildSeconds}s · 共 {formatNumber(totalChunks)} 个分块；期间检索会继续使用旧向量，请勿关闭页面。
                  </div>
                </div>
              </div>
            ) : rebuildResult ? (
              <div className="v2-result">
                <div className="v2-result-head">
                  <span className="v2-result-icon">
                    <Check size={13} />
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="v2-result-title">全部向量重建完成</div>
                    <div className="v2-result-meta">
                      重建 <b>{formatNumber(rebuildResult.chunks ?? totalChunks)}</b> 个分块
                      <span className="v2-dot" />
                      耗时 <b>{rebuildResult.seconds != null ? `${rebuildResult.seconds.toFixed(1)}s` : "—"}</b>
                      <span className="v2-dot" />
                      {providerLabel(rebuildResult.provider ?? currentProvider)}
                      <span className="v2-dot" />
                      <b>{rebuildResult.dim ?? dim} 维</b>
                    </div>
                  </div>
                </div>
              </div>
            ) : rebuildError ? (
              <div className="v2-result is-fail">
                <div className="v2-result-head">
                  <span className="v2-result-icon">
                    <TriangleAlert size={13} />
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="v2-result-title">向量重建失败</div>
                    <div className="v2-result-meta">{rebuildError} · 旧向量未被破坏，可稍后重试。</div>
                  </div>
                </div>
              </div>
            ) : null}

            <div className="v2-actions">
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void handleSave()}
                disabled={saving || rebuilding}
              >
                {saving ? <LoaderCircle size={14} className="spin" /> : <Save size={14} />}
                {saving ? "保存中…" : "保存配置"}
              </button>
              <button
                type="button"
                className="btn"
                onClick={() => void handleTest()}
                disabled={testing || rebuilding}
              >
                {testing ? <LoaderCircle size={14} className="spin" /> : <Wifi size={14} />}
                {testing ? "测试中…" : "测试连接"}
              </button>
              <button
                type="button"
                className="btn btn-warn push-right"
                onClick={openRebuild}
                disabled={rebuilding}
              >
                {rebuilding ? <LoaderCircle size={14} className="spin" /> : <Sparkles size={14} />}
                {rebuilding ? "重建中…" : "重建全部向量"}
              </button>
            </div>
            <div className="settings-foot-hint">
              <Wifi size={12} />
              测试连接使用已保存的配置；修改配置后请先保存，再重建向量。
            </div>

            <div className="embed-ocr-row">
              <span className="embed-ocr-icon">
                <ScanLine size={14} />
              </span>
              <span className="embed-ocr-main">
                <span className="embed-ocr-title">扫描版 PDF OCR</span>
                <span className="embed-ocr-desc">
                  {ocrReady
                    ? `${ocrEngine} 已就绪，单文件最多 ${ocrMaxPages} 页；扫描件入库时自动识别文字。`
                    : ocr === null || ocr === undefined
                      ? `OCR 状态未知（后端未报告，通常为 ${ocrEngine} 已就绪，最多 ${ocrMaxPages} 页）。`
                      : "OCR 当前不可用，扫描版 PDF 将无法提取文字。"}
                </span>
              </span>
              <span className={`badge ${ocrReady ? "badge-mint" : ocr === null || ocr === undefined ? "badge" : "badge-rose"}`}>
                {ocrReady ? "已就绪" : ocr === null || ocr === undefined ? "未知" : "不可用"}
              </span>
            </div>
          </>
        ) : null}
      </div>

      <ConfirmDialog
        open={rebuildOpen}
        title="重建全部向量？"
        description={
          <span>
            将使用当前配置（<strong>{providerLabel(currentProvider)}</strong> · {dim} 维）重新计算全部{" "}
            <strong>{formatNumber(totalChunks)}</strong> 个分块的向量。重建期间检索仍使用旧向量，完成后自动切换；
            预计耗时数十秒到数分钟，请勿关闭页面。此操作只改向量，不改原文与分块。
          </span>
        }
        confirmLabel="开始重建"
        loading={rebuilding}
        danger={false}
        icon={<Sparkles size={16} />}
        onConfirm={() => void handleRebuild()}
        onClose={() => setRebuildOpen(false)}
      />
    </section>
  );
}
