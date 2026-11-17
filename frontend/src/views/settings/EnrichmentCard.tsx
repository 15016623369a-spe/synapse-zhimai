import { useCallback, useEffect, useRef, useState } from "react";
import {
  Check,
  FileText,
  LoaderCircle,
  RefreshCw,
  SkipForward,
  Sparkles,
  TriangleAlert,
} from "lucide-react";
import { enrichDocuments, getEnrichStatus, toUserMessage } from "../../lib/api";
import { useApp } from "../../state/AppContext";
import type { EnrichResult, EnrichStatus } from "../../types";

const ENRICH_LIMIT = 12;
const MAX_UPDATED_PREVIEW = 3;
const MAX_KEYWORD_CHIPS = 8;

export function EnrichmentCard() {
  const {
    refreshStats,
    refreshHealth,
    bumpGraphRevision,
    bumpLibraryRevision,
    toast,
  } = useApp();
  const [status, setStatus] = useState<EnrichStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState<"pdf" | "all" | null>(null);
  const [result, setResult] = useState<EnrichResult | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const timerRef = useRef<number | null>(null);

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const next = await getEnrichStatus();
      setStatus(next);
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

  useEffect(() => {
    if (!running) {
      if (timerRef.current != null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
      return;
    }
    setElapsed(0);
    timerRef.current = window.setInterval(() => setElapsed((value) => value + 1), 1000);
    return () => {
      if (timerRef.current != null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [running]);

  const run = async (scope: "pdf" | "all"): Promise<void> => {
    if (running) return;
    setRunning(scope);
    setRunError(null);
    setResult(null);
    try {
      const response = await enrichDocuments({
        scope,
        force: false,
        limit: ENRICH_LIMIT,
      });
      setResult(response);
      const modeLabel = response.mode === "local" ? "本地整理" : "AI 整理";
      if (response.updated.length > 0) {
        toast(
          "success",
          `${modeLabel}完成：更新 ${response.updated.length} 篇`,
          "已刷新知识图谱与知识库，本次只改标题/摘要/关键词等元数据，原文未变。",
        );
      } else {
        toast(
          "info",
          `${modeLabel}完成`,
          response.failed.length > 0
            ? `有 ${response.failed.length} 篇整理失败，可稍后重试。`
            : "没有需要更新的文档。",
        );
      }
      await load();
      void refreshStats();
      void refreshHealth();
      bumpGraphRevision();
      bumpLibraryRevision();
    } catch (err) {
      const message = toUserMessage(err);
      setRunError(message);
      toast("error", "智能整理失败", message);
    } finally {
      setRunning(null);
    }
  };

  const mode = status?.mode ?? (status?.llm_configured ? "llm" : "local");
  const isLocal = mode === "local";
  const pdfPending = status?.pending_pdf ?? 0;
  const allPending = status?.pending_all ?? 0;
  const disabled = Boolean(running) || loading;

  return (
    <section className="card settings-card enrich-card">
      <div className="settings-card-head">
        <div className="settings-card-icon violet">
          <Sparkles size={18} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 className="settings-card-title">知识库智能整理</h2>
          <p className="settings-card-desc">
            让 AI 阅读文档，自动提炼标题、摘要与关键词，重建更清晰的知识图谱。只改元数据，不改原文。
          </p>
        </div>
        <button
          type="button"
          className="btn btn-icon"
          onClick={() => void load()}
          disabled={loading || Boolean(running)}
          aria-label="刷新智能整理状态"
          data-tip="刷新"
        >
          {loading ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}
        </button>
      </div>

      <div className="settings-body enrich-body">
        {error && !status ? (
          <div className="settings-inline-error">
            <TriangleAlert size={14} />
            <span>整理状态获取失败：{error}</span>
            <button type="button" className="btn btn-sm" onClick={() => void load()}>
              <RefreshCw size={12} /> 重试
            </button>
          </div>
        ) : loading && !status ? (
          <div className="enrich-loading">
            <LoaderCircle size={14} className="spin" /> 正在读取整理状态…
          </div>
        ) : status ? (
          <>
            <div className="enrich-stat-grid">
              <div className="enrich-stat">
                <span className="enrich-stat-value mono">
                  {status.enriched_documents}
                  <span className="enrich-stat-total">/{status.total_documents}</span>
                </span>
                <span className="enrich-stat-label">已整理文档</span>
              </div>
              <div className="enrich-stat">
                <span className="enrich-stat-value mono">{status.pending_pdf}</span>
                <span className="enrich-stat-label">待整理 PDF</span>
              </div>
              <div className="enrich-stat">
                <span className="enrich-stat-value mono">{status.pending_all}</span>
                <span className="enrich-stat-label">全部待整理</span>
              </div>
              <div className="enrich-stat">
                <span className={`enrich-mode-badge${isLocal ? " local" : ""}`}>
                  {isLocal ? "本地整理" : "AI 提炼"}
                </span>
                <span className="enrich-stat-label">
                  {isLocal ? "无需模型，英文关键词可用" : "使用已配置模型"}
                </span>
              </div>
            </div>

            {isLocal ? (
              <div className="enrich-hint">
                <FileText size={12} />
                当前未配置大模型，将使用本地关键词抽取；配置模型后可获得更准确的标题、摘要与关键词。
              </div>
            ) : (
              <div className="enrich-hint">
                <Sparkles size={12} />
                使用已配置模型整理，每篇最长约 60 秒；失败会回退本地抽取。
              </div>
            )}

            <div className="enrich-actions">
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={() => void run("pdf")}
                disabled={disabled || pdfPending === 0}
              >
                {running === "pdf" ? (
                  <LoaderCircle size={13} className="spin" />
                ) : (
                  <Sparkles size={13} />
                )}
                {isLocal ? "本地整理 PDF" : "智能整理 PDF"}
                {pdfPending > 0 ? <span className="enrich-btn-count">{pdfPending}</span> : null}
              </button>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => void run("all")}
                disabled={disabled || allPending === 0}
              >
                {running === "all" ? (
                  <LoaderCircle size={13} className="spin" />
                ) : (
                  <FileText size={13} />
                )}
                整理全部待整理
                {allPending > 0 ? <span className="enrich-btn-count">{allPending}</span> : null}
              </button>
              <span className="enrich-limit-hint">每次最多整理 {ENRICH_LIMIT} 篇</span>
            </div>

            {running ? (
              <div className="enrich-progress" role="status" aria-live="polite">
                <LoaderCircle size={14} className="spin" />
                <div>
                  <div className="enrich-progress-title">
                    正在{running === "pdf" ? "整理待整理 PDF" : "整理全部待整理文档"}…
                  </div>
                  <div className="enrich-progress-desc">
                    已用时 {elapsed}s · 完成后自动刷新知识图谱、知识库与统计
                  </div>
                </div>
              </div>
            ) : null}

            {runError ? (
              <div className="enrich-run-error">
                <TriangleAlert size={13} />
                <span>{runError}</span>
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => void load()}
                  disabled={loading}
                >
                  刷新状态
                </button>
              </div>
            ) : null}

            {result ? (
              <div className="enrich-result">
                <div className="enrich-result-head">
                  <span className="enrich-result-icon">
                    <Check size={14} />
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="enrich-result-title">本次整理完成</div>
                    <div className="enrich-result-meta">
                      成功 <b>{result.updated.length}</b> 篇
                      <span className="enrich-dot" />
                      跳过 <b>{result.skipped.length}</b> 篇
                      <span className="enrich-dot" />
                      失败 <b>{result.failed.length}</b> 篇
                      {result.graph_rebuilt ? (
                        <>
                          <span className="enrich-dot" />
                          图谱已重建
                        </>
                      ) : null}
                    </div>
                  </div>
                </div>
                {result.updated.length > 0 ? (
                  <div className="enrich-updated-list">
                    {result.updated.slice(0, MAX_UPDATED_PREVIEW).map((doc) => (
                      <div className="enrich-updated-item" key={doc.id}>
                        <div className="enrich-updated-title" title={doc.title}>
                          {doc.title}
                        </div>
                        <div className="enrich-keyword-chips">
                          {doc.keywords.slice(0, MAX_KEYWORD_CHIPS).map((keyword) => (
                            <span className="enrich-keyword-chip" key={keyword}>
                              {keyword}
                            </span>
                          ))}
                          {doc.keywords.length > MAX_KEYWORD_CHIPS ? (
                            <span className="enrich-keyword-more">
                              +{doc.keywords.length - MAX_KEYWORD_CHIPS}
                            </span>
                          ) : null}
                        </div>
                      </div>
                    ))}
                    {result.updated.length > MAX_UPDATED_PREVIEW ? (
                      <div className="enrich-more-hint">
                        还有 {result.updated.length - MAX_UPDATED_PREVIEW} 篇已更新，可在知识库查看
                      </div>
                    ) : null}
                  </div>
                ) : null}
                {result.skipped.length > 0 || result.failed.length > 0 ? (
                  <div className="enrich-reason-list">
                    {[...result.skipped, ...result.failed].slice(0, 4).map((item) => (
                      <div className="enrich-reason" key={item.id}>
                        {item.reason || item.error ? (
                          <>
                            <TriangleAlert size={10} />
                            <span>
                              {item.id}：{item.reason || item.error}
                            </span>
                          </>
                        ) : null}
                      </div>
                    ))}
                  </div>
                ) : null}
                <div className="enrich-refresh-hint">
                  <SkipForward size={11} />
                  已刷新图谱与知识库；可在下方「数据与存储」查看最新文档数。
                </div>
              </div>
            ) : null}

            {pdfPending === 0 && allPending === 0 && !running ? (
              <div className="enrich-done-hint">
                <Check size={13} />
                所有文档都已完成整理，图谱关键词已就绪。
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </section>
  );
}
