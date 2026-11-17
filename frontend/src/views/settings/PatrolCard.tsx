import { useCallback, useEffect, useRef, useState } from "react";
import {
  Check,
  Clock,
  LoaderCircle,
  Play,
  Radar,
  RefreshCw,
  TriangleAlert,
} from "lucide-react";
import { Switch } from "../../components/Switch";
import { ApiError, getPatrolStatus, runPatrol, toUserMessage, updatePatrolConfig } from "../../lib/api";
import { formatDateTime, formatRelativeTime } from "../../lib/format";
import { useApp } from "../../state/AppContext";
import type {
  PatrolConfigInput,
  PatrolIssueBucket,
  PatrolIssues,
  PatrolResult,
  PatrolStatus,
} from "../../types";

const INTERVAL_OPTIONS = [6, 12, 24, 72];
const MAX_ENRICH_OPTIONS = [1, 2, 3, 5, 8, 12];

interface IssueDef {
  key: keyof PatrolIssues;
  label: string;
  tone: "warn" | "rose";
}

const ISSUE_DEFS: IssueDef[] = [
  { key: "pending_docs", label: "待整理", tone: "warn" },
  { key: "duplicate_docs", label: "重复标题", tone: "rose" },
  { key: "stale_docs", label: "过时", tone: "warn" },
  { key: "orphan_docs", label: "未分区", tone: "warn" },
  { key: "small_docs", label: "过短", tone: "warn" },
  { key: "missing_tags", label: "无标签", tone: "warn" },
];

function bucketOf(issues: PatrolIssues | null | undefined, key: keyof PatrolIssues): PatrolIssueBucket | null {
  const bucket = issues?.[key];
  if (!bucket || typeof bucket !== "object") return null;
  return { count: Number(bucket.count ?? 0), sample: bucket.sample, pairs: bucket.pairs };
}

function pairSide(value: string | { title?: string } | undefined): string {
  if (!value) return "";
  return typeof value === "string" ? value : value.title ?? "";
}

function formatDuration(ms: number | null | undefined): string {
  if (!Number.isFinite(ms ?? NaN)) return "—";
  const value = (ms as number) / 1000;
  if (value < 60) return `${value.toFixed(1)}s`;
  return `${Math.floor(value / 60)}m ${Math.round(value % 60)}s`;
}

function triggerLabel(trigger: string | null | undefined): string {
  if (trigger === "scheduled") return "定时触发";
  if (trigger === "manual") return "手动触发";
  return trigger || "手动触发";
}

export function PatrolCard() {
  const {
    toast,
    refreshStats,
    refreshHealth,
    bumpGraphRevision,
    bumpLibraryRevision,
  } = useApp();
  const [status, setStatus] = useState<PatrolStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [savingField, setSavingField] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [runResult, setRunResult] = useState<PatrolResult | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);

  const load = useCallback(async (silent = false): Promise<void> => {
    if (!silent) setLoading(true);
    try {
      const next = await getPatrolStatus();
      setStatus(next);
      setError(null);
    } catch (err) {
      setError(toUserMessage(err));
    } finally {
      if (!silent) setLoading(false);
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

  // 后台定时巡检进行中时，轮询刷新状态（静默，不闪加载骨架）。
  useEffect(() => {
    if (!status?.running) return;
    const timer = window.setInterval(() => {
      void load(true);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [status?.running, load]);

  const patchConfig = async (partial: PatrolConfigInput): Promise<void> => {
    const field = Object.keys(partial)[0] ?? "config";
    if (savingField || !status) return;
    setSavingField(field);
    setStatus((prev) => (prev ? { ...prev, ...partial } : prev));
    try {
      const response = await updatePatrolConfig(partial);
      if (response && typeof response === "object" && "enabled" in response) {
        setStatus(response as PatrolStatus);
      } else {
        await load(true);
      }
      toast("success", "巡检配置已保存", "配置会立即用于下一次定时巡检。");
    } catch (err) {
      toast("error", "保存失败", toUserMessage(err));
      await load(true);
    } finally {
      setSavingField(null);
    }
  };

  const handleRun = async (): Promise<void> => {
    if (running) return;
    setRunning(true);
    setRunResult(null);
    setRunError(null);
    try {
      const response = await runPatrol();
      const result = response?.result ?? (response as unknown as PatrolResult | null);
      setRunResult(result ?? null);
      if (response?.status) setStatus(response.status);
      else await load(true);
      const enrichedCount = result?.enriched?.filter((doc) => doc.ok !== false).length ?? 0;
      toast(
        "success",
        "知识巡检完成",
        enrichedCount > 0 ? `自动整理 ${enrichedCount} 篇文档，已刷新知识图谱。` : "已生成巡检报告。",
      );
      void refreshStats();
      void refreshHealth();
      bumpGraphRevision();
      bumpLibraryRevision();
    } catch (err) {
      const message = toUserMessage(err);
      setRunError(message);
      if (err instanceof ApiError && err.status === 409) {
        setStatus((prev) => (prev ? { ...prev, running: true } : prev));
        toast("info", "巡检正在运行中", "后台巡检尚未结束，完成后会自动刷新结果。");
      } else {
        toast("error", "巡检失败", message);
      }
    } finally {
      setRunning(false);
    }
  };

  const interval = status?.interval_hours ?? 24;
  const autoEnrich = status?.auto_enrich ?? true;
  const maxEnrich = status?.max_enrich ?? 3;
  const effectiveResult = runResult ?? status?.last_result ?? null;
  const issues = effectiveResult?.issues ?? null;
  const enrichedDocs = effectiveResult?.enriched ?? [];
  const enriched = enrichedDocs.filter((doc) => doc.ok !== false);
  const enrichedFailed = enrichedDocs.length - enriched.length;
  const isRunning = running || Boolean(status?.running);
  const disabled = savingField != null;

  const intervalOptions = INTERVAL_OPTIONS.includes(interval)
    ? INTERVAL_OPTIONS
    : [...INTERVAL_OPTIONS, interval].sort((a, b) => a - b);
  const maxEnrichOptions = MAX_ENRICH_OPTIONS.includes(maxEnrich)
    ? MAX_ENRICH_OPTIONS
    : [...MAX_ENRICH_OPTIONS, maxEnrich].sort((a, b) => a - b);

  const sampleLines: { category: string; text: string }[] = [];
  if (issues) {
    for (const def of ISSUE_DEFS) {
      const bucket = bucketOf(issues, def.key);
      if (!bucket) continue;
      if (def.key === "duplicate_docs" && Array.isArray(bucket.pairs)) {
        for (const pair of bucket.pairs.slice(0, 2)) {
          const a = pair.title_a ?? pairSide(pair.a);
          const b = pair.title_b ?? pairSide(pair.b);
          const similarity =
            typeof pair.similarity === "number" ? `（相似度 ${Math.round(pair.similarity * 100)}%）` : "";
          if (a || b) sampleLines.push({ category: def.label, text: `${a} ↔ ${b}${similarity}`.trim() });
        }
      }
      for (const item of (bucket.sample ?? []).slice(0, 2)) {
        if (item.title || item.reason) {
          sampleLines.push({ category: def.label, text: item.title || item.reason || "" });
        }
      }
      if (sampleLines.length >= 3) break;
    }
  }
  const shownSamples = sampleLines.slice(0, 3);

  return (
    <section className="card settings-card patrol-card" data-card="patrol" aria-label="知识巡检">
      <div className="settings-card-head">
        <div className="settings-card-icon mint">
          <Radar size={18} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 className="settings-card-title">知识巡检</h2>
          <p className="settings-card-desc">
            定时检查知识库健康度并自动整理待整理文档；只做元数据整理，不自动联网、不改原文。
          </p>
        </div>
        <span
          className={`badge ${
            isRunning ? "badge-cyan" : status?.enabled ? "badge-mint" : "badge"
          }`}
        >
          <span className={`status-dot ${isRunning ? "pulse" : status?.enabled ? "" : "is-warn"}`} />
          {isRunning ? "运行中" : status?.enabled ? "定时开启" : "已停用"}
        </span>
        <button
          type="button"
          className="btn btn-icon"
          onClick={() => void load()}
          disabled={loading || isRunning}
          aria-label="刷新知识巡检状态"
          data-tip="刷新"
        >
          {loading ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}
        </button>
      </div>

      <div className="settings-body">
        {error && !status ? (
          <div className="settings-inline-error">
            <TriangleAlert size={14} />
            <span>巡检状态获取失败：{error}</span>
            <button type="button" className="btn btn-sm" onClick={() => void load()}>
              <RefreshCw size={12} /> 重试
            </button>
          </div>
        ) : loading && !status ? (
          <div className="v2-loading">
            <LoaderCircle size={14} className="spin" /> 正在读取巡检状态…
          </div>
        ) : status ? (
          <>
            <div className="patrol-config">
              <div className="patrol-config-row">
                <div className="patrol-config-main">
                  <span className="patrol-config-title">启用定时巡检</span>
                  <span className="patrol-config-desc">
                    按周期自动检查知识库并生成巡检报告
                  </span>
                </div>
                <Switch
                  checked={Boolean(status.enabled)}
                  onChange={(next) => void patchConfig({ enabled: next })}
                  label="启用定时巡检"
                  disabled={disabled}
                />
              </div>

              <div className="patrol-config-row">
                <div className="patrol-config-main">
                  <span className="patrol-config-title">巡检周期</span>
                  <span className="patrol-config-desc">
                    {status.enabled ? `每 ${interval} 小时自动运行一次` : "启用后按所选周期运行"}
                  </span>
                </div>
                <select
                  className="select patrol-inline-select"
                  aria-label="巡检周期"
                  value={interval}
                  disabled={disabled}
                  onChange={(event) =>
                    void patchConfig({ interval_hours: Number(event.target.value) })
                  }
                >
                  {intervalOptions.map((hours) => (
                    <option key={hours} value={hours}>
                      每 {hours} 小时
                    </option>
                  ))}
                </select>
              </div>

              <div className="patrol-config-row">
                <div className="patrol-config-main">
                  <span className="patrol-config-title">自动整理待整理文档</span>
                  <span className="patrol-config-desc">
                    {autoEnrich
                      ? `每次最多整理 ${maxEnrich} 篇（AI / 本地整理，仅改标题、摘要、关键词）`
                      : "只生成巡检报告，不自动整理文档"}
                  </span>
                </div>
                <select
                  className="select patrol-inline-select"
                  aria-label="每次最多整理篇数"
                  value={maxEnrich}
                  disabled={disabled || !autoEnrich}
                  onChange={(event) => void patchConfig({ max_enrich: Number(event.target.value) })}
                >
                  {maxEnrichOptions.map((count) => (
                    <option key={count} value={count}>
                      最多 {count} 篇
                    </option>
                  ))}
                </select>
                <Switch
                  checked={autoEnrich}
                  onChange={(next) => void patchConfig({ auto_enrich: next })}
                  label="自动整理待整理文档"
                  disabled={disabled}
                  size="sm"
                />
              </div>

              <div className="patrol-config-foot">
                <span className="v2-hint">
                  <Clock size={12} />
                  上次巡检：{status.last_run_at ? formatRelativeTime(status.last_run_at) : "还没有记录"}
                  {status.last_run_at ? `（${formatDateTime(status.last_run_at)}）` : ""}
                </span>
                <span className="v2-hint">
                  下次巡检：
                  {status.next_run_at
                    ? formatDateTime(status.next_run_at)
                    : status.enabled
                      ? "启用后自动安排"
                      : "已停用"}
                </span>
              </div>
            </div>

            <div className="patrol-status-grid">
              <div className="patrol-status-cell">
                <span className="patrol-status-value mono">
                  {status.counts?.pending_docs ?? 0}
                </span>
                <span className="patrol-status-label">待整理文档</span>
              </div>
              <div className="patrol-status-cell">
                <span className="patrol-status-value mono">
                  {status.counts?.enriched_documents ?? 0}/{status.counts?.total_documents ?? 0}
                </span>
                <span className="patrol-status-label">已整理 / 全部文档</span>
              </div>
              <div className="patrol-status-cell">
                <span className="patrol-status-value">
                  {status.running ? "运行中" : "空闲"}
                </span>
                <span className="patrol-status-label">后台任务状态</span>
              </div>
            </div>

            <div className="v2-actions">
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void handleRun()}
                disabled={isRunning}
              >
                {isRunning ? <LoaderCircle size={14} className="spin" /> : <Play size={14} />}
                {isRunning ? "巡检中…" : "立即巡检"}
              </button>
              <span className="v2-hint">
                可能耗时 10–60 秒（自动整理较慢），期间可继续使用其他功能
              </span>
            </div>

            {isRunning ? (
              <div className="patrol-running" role="status" aria-live="polite">
                <LoaderCircle size={13} className="spin" />
                <span>
                  正在巡检知识库并整理待整理文档…
                  {elapsed > 0 ? ` 已用时 ${elapsed}s` : ""}
                </span>
              </div>
            ) : null}

            {runError ? (
              <div className="settings-inline-error">
                <TriangleAlert size={14} />
                <span>{runError}</span>
                <button type="button" className="btn btn-sm" onClick={() => void load()}>
                  刷新状态
                </button>
              </div>
            ) : null}

            {effectiveResult ? (
              <div className="v2-result">
                <div className="v2-result-head">
                  <span className="v2-result-icon">
                    <Check size={13} />
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="v2-result-title">巡检报告</div>
                    <div className="v2-result-meta">
                      <span>{triggerLabel(effectiveResult.trigger)}</span>
                      <span className="v2-dot" />
                      <span>耗时 {formatDuration(effectiveResult.duration_ms)}</span>
                      {effectiveResult.finished_at ? (
                        <>
                          <span className="v2-dot" />
                          <span>{formatDateTime(effectiveResult.finished_at)}</span>
                        </>
                      ) : null}
                      <span className="v2-dot" />
                      <span>
                        自动整理 <b>{enriched.length}</b> 篇
                      </span>
                      {enrichedFailed > 0 ? (
                        <>
                          <span className="v2-dot" />
                          <span>
                            失败 <b>{enrichedFailed}</b> 篇
                          </span>
                        </>
                      ) : null}
                      {effectiveResult.graph_rebuilt ? (
                        <>
                          <span className="v2-dot" />
                          <span>图谱已重建</span>
                        </>
                      ) : null}
                    </div>
                  </div>
                </div>

                {effectiveResult.summary ? (
                  <div className="v2-hint">{effectiveResult.summary}</div>
                ) : null}

                {enriched.length > 0 ? (
                  <div className="patrol-enriched-chips">
                    {enriched.slice(0, 6).map((doc) => (
                      <span className="patrol-enriched-chip" key={doc.id} title={doc.title || doc.id}>
                        {doc.title || doc.id}
                      </span>
                    ))}
                    {enriched.length > 6 ? (
                      <span className="patrol-enriched-more">+{enriched.length - 6}</span>
                    ) : null}
                  </div>
                ) : null}

                {issues ? (
                  <div className="patrol-issues" aria-label="巡检问题分类计数">
                    {ISSUE_DEFS.map((def) => {
                      const bucket = bucketOf(issues, def.key);
                      const count = bucket?.count ?? 0;
                      const tone = count > 0 ? def.tone : "";
                      return (
                        <span
                          key={def.key}
                          className={`patrol-issue-chip${tone ? ` ${tone}` : ""}`}
                          title={`${def.label}：${count} 项`}
                        >
                          {def.label} <b className="mono">{count}</b>
                        </span>
                      );
                    })}
                  </div>
                ) : (
                  <div className="v2-hint">巡检报告未返回问题分类。</div>
                )}

                {shownSamples.length > 0 ? (
                  <div className="patrol-samples">
                    {shownSamples.map((sample) => (
                      <span className="patrol-sample" key={`${sample.category}-${sample.text}`}>
                        <span className="patrol-sample-cat">· {sample.category}</span>
                        <span className="patrol-sample-title" title={sample.text}>
                          {sample.text}
                        </span>
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : (
              <div className="v2-note">
                <Radar size={13} />
                还没有巡检记录。可先开启定时巡检，或点击「立即巡检」生成第一份知识库健康报告。
              </div>
            )}
          </>
        ) : null}
      </div>
    </section>
  );
}
