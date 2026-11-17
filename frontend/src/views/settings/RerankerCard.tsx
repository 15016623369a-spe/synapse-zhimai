import { useCallback, useEffect, useState } from "react";
import {
  LoaderCircle,
  RefreshCw,
  SlidersHorizontal,
  ThumbsDown,
  ThumbsUp,
  TriangleAlert,
} from "lucide-react";
import { getRerankerSummary, toUserMessage } from "../../lib/api";
import type { RerankerSummary } from "../../types";

const MAX_DOMAINS = 6;

function scorePercent(score: number | null | undefined): number {
  if (!Number.isFinite(score ?? NaN)) return 50;
  return Math.min(100, Math.max(0, Math.round((score as number) * 100)));
}

export function RerankerCard() {
  const [summary, setSummary] = useState<RerankerSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const next = await getRerankerSummary();
      setSummary({
        accepted: Number(next?.accepted ?? 0),
        rejected: Number(next?.rejected ?? 0),
        domains: Array.isArray(next?.domains) ? next.domains : [],
        description: String(next?.description ?? ""),
      });
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

  const accepted = summary?.accepted ?? 0;
  const rejected = summary?.rejected ?? 0;
  const domains = summary?.domains ?? [];
  const hasFeedback = accepted + rejected > 0 && domains.length > 0;
  const isEmpty = !hasFeedback;
  const shownDomains = domains.slice(0, MAX_DOMAINS);

  return (
    <section className="card settings-card rerank-card" data-card="reranker" aria-label="反馈偏好">
      <div className="settings-card-head">
        <div className="settings-card-icon amber">
          <SlidersHorizontal size={18} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 className="settings-card-title">反馈偏好（Reranker）</h2>
          <p className="settings-card-desc">
            你对「联网补充资料」候选的采纳 / 忽略会被记录，用于微调外部资料的排序权重。
          </p>
        </div>
        <span className="badge badge-violet">规则化偏好</span>
        <button
          type="button"
          className="btn btn-icon"
          onClick={() => void load()}
          disabled={loading}
          aria-label="刷新反馈偏好"
          data-tip="刷新"
        >
          {loading ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}
        </button>
      </div>

      <div className="settings-body">
        {error && !summary ? (
          <div className="settings-inline-error">
            <TriangleAlert size={14} />
            <span>反馈偏好获取失败：{error}</span>
            <button type="button" className="btn btn-sm" onClick={() => void load()}>
              <RefreshCw size={12} /> 重试
            </button>
          </div>
        ) : loading && !summary ? (
          <div className="v2-loading">
            <LoaderCircle size={14} className="spin" /> 正在读取反馈偏好…
          </div>
        ) : summary ? (
          <>
            <div className="rerank-stats">
              <div className="rerank-stat">
                <span className="rerank-stat-icon accepted">
                  <ThumbsUp size={13} />
                </span>
                <span className="rerank-stat-value mono">{accepted}</span>
                <span className="rerank-stat-label">已采纳</span>
              </div>
              <div className="rerank-stat">
                <span className="rerank-stat-icon rejected">
                  <ThumbsDown size={13} />
                </span>
                <span className="rerank-stat-value mono">{rejected}</span>
                <span className="rerank-stat-label">已忽略</span>
              </div>
            </div>

            {isEmpty ? (
              <div className="rerank-empty">
                <ThumbsUp size={15} />
                <div>
                  <div className="rerank-empty-title">还没有采纳 / 忽略记录</div>
                  在对话里搜集外部资料、勾选加入或忽略候选后，这里会显示你偏好的来源域名。
                </div>
              </div>
            ) : (
              <>
                <div className="rerank-domain-list" aria-label="偏好来源域名">
                  {shownDomains.map((domain) => {
                    const percent = scorePercent(domain.score);
                    return (
                      <div className="rerank-domain" key={domain.host}>
                        <span className="rerank-domain-main">
                          <span className="rerank-domain-host" title={domain.host}>
                            {domain.host}
                          </span>
                          <span className="rerank-domain-meta">
                            采纳 {domain.accepted} · 忽略 {domain.rejected}
                          </span>
                        </span>
                        <span className="rerank-domain-score">
                          <span className="rerank-bar" aria-hidden="true">
                            <span
                              className={`rerank-bar-fill${percent < 50 ? " low" : ""}`}
                              style={{ width: `${percent}%` }}
                            />
                          </span>
                          <span className="rerank-score-text mono">{percent}%</span>
                        </span>
                      </div>
                    );
                  })}
                </div>
                {domains.length > MAX_DOMAINS ? (
                  <span className="rerank-more">另有 {domains.length - MAX_DOMAINS} 个域名参与偏好加权</span>
                ) : null}
              </>
            )}

            <div className="v2-note">
              <SlidersHorizontal size={13} />
              {summary.description ||
                "你忽略的域名会在后续外部资料中降权，采纳的来源会加分；这是规则化偏好，不训练模型，也不会删除你的反馈。"}
            </div>
          </>
        ) : null}
      </div>
    </section>
  );
}
