import { useCallback, useEffect, useState } from "react";
import { Globe, LoaderCircle, RefreshCw, ShieldCheck } from "lucide-react";
import { getExtendStatus, toUserMessage } from "../../lib/api";
import type { ExtendStatus } from "../../types";

export function ExtensionStatusCard() {
  const [status, setStatus] = useState<ExtendStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const next = await getExtendStatus();
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

  return (
    <section className="card settings-card extension-status-card">
      <div className="settings-card-head">
        <div className="settings-card-icon cyan">
          <Globe size={18} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 className="settings-card-title">联网知识扩展</h2>
          <p className="settings-card-desc">
            知识不足时可联网检索；外部内容需你确认后才会新增，不修改原文档。
          </p>
        </div>
        <button
          type="button"
          className="btn btn-icon"
          onClick={() => void load()}
          disabled={loading}
          aria-label="刷新联网知识扩展状态"
          data-tip="刷新"
        >
          {loading ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}
        </button>
      </div>
      <div className="settings-body extension-status-body">
        {error && !status ? (
          <div className="settings-inline-error">
            <span>状态获取失败：{error}</span>
          </div>
        ) : loading && !status ? (
          <div className="extension-status-loading">
            <LoaderCircle size={14} className="spin" /> 正在读取搜索配置…
          </div>
        ) : status ? (
          <>
            <div className="settings-row">
              <div className="settings-row-main">
                <span className="settings-row-title">搜索提供方</span>
                <span className="settings-row-desc">
                  {status.label || status.provider} · 无需 API Key
                </span>
              </div>
              <span className={`badge ${status.enabled ? "badge-cyan" : "badge"}`}>
                {status.enabled ? "已启用" : "未启用"}
              </span>
            </div>
            <div className="settings-row">
              <div className="settings-row-main">
                <span className="settings-row-title">
                  <ShieldCheck size={12} /> 人工确认
                </span>
                <span className="settings-row-desc">
                  {status.requires_approval
                    ? "所有外部资料需你勾选后才会加入知识库"
                    : "当前配置会自动加入外部资料"}
                </span>
              </div>
              <span className={`badge ${status.requires_approval ? "badge-violet" : "badge"}`}>
                {status.requires_approval ? "必须确认" : "自动"}
              </span>
            </div>
            <div className="extension-status-counts">
              <div className="extension-status-count">
                <span className="mono">{status.accepted_count}</span>
                <span>已采纳</span>
              </div>
              <div className="extension-status-count">
                <span className="mono">{status.rejected_count}</span>
                <span>已忽略</span>
              </div>
              <p className="extension-status-hint">
                外部文档以「外部扩展」标签新增；可后端 `.env` 配置本次搜索 provider（bing / searxng / mock）。
              </p>
            </div>
          </>
        ) : (
          <div className="extension-status-loading">暂无状态信息</div>
        )}
      </div>
    </section>
  );
}
