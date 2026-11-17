import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  BookOpen,
  Check,
  ExternalLink,
  FileText,
  Globe,
  History,
  LoaderCircle,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  SkipForward,
  ThumbsDown,
  ThumbsUp,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import { contextHintLabel } from "../../lib/format";
import { navigate } from "../../lib/router";
import { Modal } from "../../components/Modal";
import { SkeletonRows } from "../../components/Skeleton";
import {
  extendCommit,
  extendReject,
  extendSearch,
  getExtendStatus,
  listZones,
  toUserMessage,
} from "../../lib/api";
import { useApp } from "../../state/AppContext";
import type {
  ExtendCommitResponse,
  ExtendStatus,
  WebSourceCandidate,
  Zone,
} from "../../types";

interface ExtensionPanelProps {
  open: boolean;
  question: string;
  conversationId: string | null;
  initialCandidates?: WebSourceCandidate[];
  initialQueries?: string[];
  onClose: () => void;
  onCommitted: (result: ExtendCommitResponse) => void;
}

interface CandidateItem extends WebSourceCandidate {
  manual?: boolean;
}

function urlKey(url: string): string {
  return url.trim();
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function dedupeCandidates(items: WebSourceCandidate[]): CandidateItem[] {
  const seen = new Set<string>();
  const result: CandidateItem[] = [];
  for (const item of items) {
    const key = urlKey(item.url);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    result.push({ ...item, url: key });
  }
  return result;
}

function eligible(candidate: CandidateItem): boolean {
  return !candidate.already_in_kb && !candidate.rejected_before;
}

const AUTHORITY_META: Record<string, { label: string; className: string }> = {
  academic: { label: "学术", className: "is-academic" },
  official: { label: "官方", className: "is-official" },
  wiki: { label: "百科", className: "is-wiki" },
  media: { label: "媒体", className: "is-media" },
  community: { label: "社区", className: "is-community" },
  unknown: { label: "未知", className: "is-unknown" },
};

const AUTHORITY_LABEL_KEYS: Record<string, string> = {
  学术: "academic",
  官方: "official",
  百科: "wiki",
  媒体: "media",
  社区: "community",
  未知: "unknown",
};

function authorityInfo(candidate: CandidateItem): { label: string; className: string } {
  const category =
    candidate.authority_category ||
    (typeof candidate.authority === "string" && AUTHORITY_META[candidate.authority]
      ? candidate.authority
      : "") ||
    (candidate.authority_label ? AUTHORITY_LABEL_KEYS[candidate.authority_label] : "") ||
    "";
  if (category && AUTHORITY_META[category]) {
    return {
      label: candidate.authority_label || AUTHORITY_META[category].label,
      className: AUTHORITY_META[category].className,
    };
  }
  return { label: candidate.authority_label || "未知", className: "is-unknown" };
}

function authorityCategoryOf(candidate: CandidateItem): string {
  const category =
    candidate.authority_category ||
    (typeof candidate.authority === "string" && AUTHORITY_META[candidate.authority]
      ? candidate.authority
      : "") ||
    (candidate.authority_label ? AUTHORITY_LABEL_KEYS[candidate.authority_label] : "") ||
    "unknown";
  return category;
}

/** 默认只勾选高相关且非社区来源的候选；预填/手动候选在无相关度时保持勾选。 */
function defaultSelected(candidate: CandidateItem): boolean {
  if (candidate.already_in_kb || candidate.rejected_before) return false;
  if (candidate.manual) return true;
  if (authorityCategoryOf(candidate) === "community") return false;
  if (typeof candidate.relevance === "number") return candidate.relevance >= 0.5;
  return true;
}

export function ExtensionPanel({
  open,
  question,
  conversationId,
  initialCandidates,
  initialQueries,
  onClose,
  onCommitted,
}: ExtensionPanelProps) {
  const { toast } = useApp();
  const [status, setStatus] = useState<ExtendStatus | null>(null);
  const [zones, setZones] = useState<Zone[]>([]);
  const [provider, setProvider] = useState("");
  const [queries, setQueries] = useState<string[]>([]);
  const [candidates, setCandidates] = useState<CandidateItem[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [filteredCount, setFilteredCount] = useState(0);
  const [intent, setIntent] = useState("");
  const [contextUsed, setContextUsed] = useState(false);
  const [contextSummary, setContextSummary] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [manualUrl, setManualUrl] = useState("");
  const [manualTitle, setManualTitle] = useState("");
  const [manualError, setManualError] = useState<string | null>(null);
  const [zoneId, setZoneId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [result, setResult] = useState<ExtendCommitResponse | null>(null);
  const [searchSeed, setSearchSeed] = useState(0);
  const requestSeq = useRef(0);

  const refreshStatus = useCallback(async (): Promise<void> => {
    try {
      const next = await getExtendStatus();
      setStatus(next);
      setProvider((prev) => prev || next.provider);
    } catch {
      /* 状态刷新失败不打断主流程 */
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const seq = requestSeq.current + 1;
    requestSeq.current = seq;
    setLoading(true);
    setError(null);
    setResult(null);
    setCandidates([]);
    setSelected(new Set());
    setFilteredCount(0);
    setIntent("");
    setContextUsed(false);
    setContextSummary(null);
    setManualUrl("");
    setManualTitle("");
    setManualError(null);
    setZoneId("");

    const load = async (): Promise<void> => {
      try {
        const [statusResult, zonesResult] = await Promise.allSettled([
          getExtendStatus(),
          listZones(),
        ]);
        if (cancelled || requestSeq.current !== seq) return;
        if (statusResult.status === "fulfilled") {
          setStatus(statusResult.value);
          setProvider(statusResult.value.provider);
        }
        if (zonesResult.status === "fulfilled") {
          setZones(zonesResult.value.items);
        }
        if (initialCandidates && initialCandidates.length > 0) {
          const items = dedupeCandidates(initialCandidates);
          setCandidates(items);
          setSelected(new Set(items.filter(defaultSelected).map((item) => item.url)));
          setQueries(initialQueries ?? []);
          setFilteredCount(0);
          // 预填候选（来自 chat 的工具搜索结果）时，按候选实际来源修正 provider/intent，
          // 不能沿用全局 Bing 状态，否则学术候选会显示成 bing。
          const sources = new Set(
            items.map((item) => String(item.source || "")).filter(Boolean),
          );
          if (sources.has("arxiv")) setProvider("arxiv");
          else if (sources.size === 1) setProvider([...sources][0]);
          setIntent(
            items.some((item) => authorityCategoryOf(item) === "academic")
              ? "academic"
              : "",
          );
          setLoading(false);
          return;
        }
        const response = await extendSearch({
          question,
          queries: initialQueries && initialQueries.length > 0 ? initialQueries : undefined,
          conversation_id: conversationId,
          max_results: 8,
        });
        if (cancelled || requestSeq.current !== seq) return;
        const items = dedupeCandidates(
          response.candidates.filter((candidate) => !candidate.rejected_before),
        );
        setCandidates(items);
        setSelected(new Set(items.filter(defaultSelected).map((item) => item.url)));
        setFilteredCount(response.filtered_count ?? 0);
        setIntent(response.intent ?? "");
        setProvider(
          response.provider ||
            (statusResult.status === "fulfilled" ? statusResult.value.provider : ""),
        );
        setQueries(response.queries ?? []);
        setContextUsed(Boolean(response.context_used));
        setContextSummary(response.context_summary ?? null);
      } catch (err) {
        if (!cancelled && requestSeq.current === seq) setError(toUserMessage(err));
      } finally {
        if (!cancelled && requestSeq.current === seq) setLoading(false);
      }
    };

    void load();
    return () => {
      cancelled = true;
    };
    // initialCandidates 仅随 open/question 变化时注入，避免重复搜索
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, question, conversationId, searchSeed]);

  const selectableCount = useMemo(
    () => candidates.filter((candidate) => selected.has(candidate.url) && eligible(candidate)).length,
    [candidates, selected],
  );

  const toggleCandidate = (url: string): void => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(url)) next.delete(url);
      else next.add(url);
      return next;
    });
  };

  const removeCandidate = (url: string): void => {
    setCandidates((prev) => prev.filter((item) => item.url !== url));
    setSelected((prev) => {
      const next = new Set(prev);
      next.delete(url);
      return next;
    });
  };

  const rejectItems = async (items: CandidateItem[]): Promise<void> => {
    if (items.length === 0 || rejecting) return;
    setRejecting(true);
    try {
      const response = await extendReject(
        items.map((item) => ({ url: item.url, title: item.title })),
        question,
      );
      for (const item of items) removeCandidate(item.url);
      toast("success", "已记录忽略", `已忽略 ${response.remembered} 条资料，之后不会再推荐。`);
      void refreshStatus();
    } catch (err) {
      toast("error", "忽略失败", toUserMessage(err));
    } finally {
      setRejecting(false);
    }
  };

  const addManual = (): void => {
    setManualError(null);
    const raw = manualUrl.trim();
    if (!raw) {
      setManualError("请输入链接地址。");
      return;
    }
    let parsed: URL;
    try {
      parsed = new URL(raw);
    } catch {
      setManualError("请输入合法的 http(s) 链接。");
      return;
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      setManualError("只支持 http(s) 链接。");
      return;
    }
    const url = parsed.toString();
    if (candidates.some((candidate) => urlKey(candidate.url) === url)) {
      setManualError("这个链接已经在候选列表里了。");
      return;
    }
    const title = manualTitle.trim() || parsed.hostname.replace(/^www\./, "");
    setCandidates((prev) => [
      ...prev,
      {
        url,
        title,
        snippet: "手动添加的外部链接，加入后会抓取正文；抓取失败时使用标题与摘要。",
        source: "manual",
        already_in_kb: false,
        rejected_before: false,
        manual: true,
      },
    ]);
    setSelected((prev) => new Set(prev).add(url));
    setManualUrl("");
    setManualTitle("");
  };

  const commit = async (): Promise<void> => {
    if (submitting || selectableCount <= 0) return;
    const items = candidates.filter(
      (candidate) => selected.has(candidate.url) && eligible(candidate),
    );
    setSubmitting(true);
    try {
      const response = await extendCommit(
        items.map((item) => ({ url: item.url, title: item.title, snippet: item.snippet })),
        zoneId || null,
      );
      setResult(response);
      for (const item of items) removeCandidate(item.url);
      if (response.added.length > 0) {
        toast(
          "success",
          `已新增 ${response.added.length} 篇外部知识`,
          "原文档未修改，新增内容已标记「外部扩展」并会参与后续检索。",
        );
      } else {
        toast("info", "没有新增文档", response.skipped[0]?.reason ?? "这些链接未能入库。");
      }
      void refreshStatus();
      onCommitted(response);
    } catch (err) {
      toast("error", "加入知识库失败", toUserMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  const retrySearch = (): void => {
    setSearchSeed((seed) => seed + 1);
  };

  const disabled = status ? !status.enabled || !status.can_search : false;
  const remaining = candidates.length;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="知识扩展 · 由你确认"
      description="只会新增外部知识，不会修改你已有的文档。"
      icon={<Globe size={17} />}
      width={860}
      footer={
        <div className="extend-footer">
          <label className="extend-zone">
            <span className="extend-zone-label">加入分区</span>
            <select
              className="select-input extend-zone-select"
              value={zoneId}
              onChange={(event) => setZoneId(event.target.value)}
            >
              <option value="">未分区</option>
              {zones.map((zone) => (
                <option key={zone.id} value={zone.id}>
                  {zone.name}
                </option>
              ))}
            </select>
          </label>
          <div className="extend-footer-actions">
            {selectableCount === 0 ? (
              <span className="extend-footer-hint">先勾选候选资料</span>
            ) : null}
            <button
              type="button"
              className="btn extend-reject-btn"
              onClick={() =>
                void rejectItems(
                  candidates.filter(
                    (candidate) => selected.has(candidate.url) && eligible(candidate),
                  ),
                )
              }
              disabled={rejecting || selectableCount === 0}
              title={selectableCount === 0 ? "请先勾选要忽略的候选资料" : "记录忽略所选，之后不再推荐"}
            >
              {rejecting ? <LoaderCircle size={13} className="spin" /> : <SkipForward size={13} />}
              忽略所选
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void commit()}
              disabled={submitting || selectableCount === 0}
            >
              {submitting ? <LoaderCircle size={14} className="spin" /> : <Plus size={14} />}
              {submitting ? "正在加入…" : `加入知识库（${selectableCount} 条）`}
            </button>
          </div>
        </div>
      }
    >
      <div className="extend-panel">
        <div className="extend-question">
          <Search size={14} />
          <div className="extend-question-main">
            <span className="extend-question-label">本轮问题</span>
            <span className="extend-question-text">{question}</span>
          </div>
          {provider ? (
            <span className="extend-provider-badge">
              <Globe size={11} />
              {provider}
            </span>
          ) : null}
          {intent === "academic" ? (
            <span className="extend-intent-badge">学术检索</span>
          ) : null}
        </div>

        {contextUsed ? (
          <div
            className="extend-context-hint"
            title={contextHintLabel(contextSummary)}
            aria-label="已结合上文提示"
          >
            <History size={12} />
            <span className="extend-context-text">{contextHintLabel(contextSummary)}</span>
          </div>
        ) : null}

        <div className="extend-meta-row">
          {queries.length > 0 ? (
            <div className="extend-query-chips">
              {queries.map((query) => (
                <span key={query} className="extend-query-chip">
                  {query}
                </span>
              ))}
            </div>
          ) : (
            <span className="extend-meta-hint">自动生成检索词并联网搜索</span>
          )}
          <div className="extend-feedback-counts">
            <span className="mono">采纳 {status?.accepted_count ?? 0}</span>
            <span className="mono">忽略 {status?.rejected_count ?? 0}</span>
          </div>
        </div>

        <div className="extend-feedback-note">
          <ShieldCheck size={12} />
          你的选择会被记住：忽略的链接不再推荐；采纳内容会进入后续检索。
        </div>

        {disabled ? (
          <div className="extend-disabled-banner">
            <TriangleAlert size={14} />
            <span>
              联网搜索当前不可用{status?.provider ? `（${status.provider}）` : ""}，你仍可以手动添加链接后再加入知识库。
            </span>
          </div>
        ) : null}

        {loading ? (
          <div className="extend-loading">
            <div className="extend-loading-head">
              <LoaderCircle size={14} className="spin" />
              正在联网搜索补充资料…
            </div>
            <SkeletonRows rows={4} />
          </div>
        ) : error ? (
          <div className="extend-error">
            <TriangleAlert size={16} />
            <div style={{ flex: 1 }}>
              <div className="extend-error-title">搜索补充资料失败</div>
              <div className="extend-error-text">{error}</div>
              <button type="button" className="btn btn-sm" onClick={retrySearch}>
                <RefreshCw size={12} /> 重试
              </button>
            </div>
          </div>
        ) : (
          <>
            {result ? (
              <div className="extend-result">
                <div className="extend-result-head">
                  <span className="extend-result-icon">
                    <Check size={14} />
                  </span>
                  <div style={{ flex: 1 }}>
                    <div className="extend-result-title">
                      {result.added.length > 0
                        ? `已加入 ${result.added.length} 篇外部知识`
                        : "本次没有新增文档"}
                    </div>
                    <div className="extend-result-desc">
                      原文档未修改；已加入内容会参与后续检索。
                    </div>
                  </div>
                </div>
                {result.added.length > 0 ? (
                  <div className="extend-result-list">
                    {result.added.map((doc) => (
                      <div key={doc.id} className="extend-result-item">
                        <Check size={12} />
                        <span className="extend-result-name" title={doc.title}>
                          {doc.title}
                        </span>
                        <span className="extend-result-host">
                          {doc.source_url ? hostOf(doc.source_url) : "外部扩展"}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : null}
                {result.added.length > 0 ? (
                  <div className="extend-result-actions">
                    <button
                      type="button"
                      className="btn btn-sm extend-result-link"
                      onClick={() => {
                        onClose();
                        navigate("/library");
                      }}
                    >
                      <BookOpen size={12} />
                      前往知识库查看
                    </button>
                  </div>
                ) : null}
                {result.skipped.length > 0 ? (
                  <div className="extend-skipped-list">
                    {result.skipped.map((item) => (
                      <div key={item.url} className="extend-skipped-item">
                        <span className="extend-skipped-url" title={item.url}>
                          {hostOf(item.url)}
                        </span>
                        <span className="extend-skipped-reason">{item.reason}</span>
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : null}

            <div className="extend-list-head">
              <span>候选资料</span>
              <span className="extend-list-meta">
                {filteredCount > 0 ? (
                  <span className="extend-filtered-hint">
                    已过滤 {filteredCount} 条低相关/低质量结果
                  </span>
                ) : null}
                <span className="mono dim">
                  {remaining} 条{selectableCount > 0 ? ` · 已选 ${selectableCount}` : ""}
                </span>
              </span>
            </div>

            {remaining === 0 ? (
              <div className="extend-empty">
                <Search size={18} />
                <div>
                  <div className="extend-empty-title">没有找到可加入的资料</div>
                  <div className="extend-empty-desc">
                    可以重试搜索，或在下面手动粘贴一个链接加入知识库。
                  </div>
                </div>
                <button type="button" className="btn btn-sm" onClick={retrySearch}>
                  <RefreshCw size={12} /> 重新搜索
                </button>
              </div>
            ) : (
              <div className="extend-candidate-list">
                {candidates.map((candidate) => {
                  const inKb = Boolean(candidate.already_in_kb);
                  const isSelected = selected.has(candidate.url);
                  return (
                    <div
                      key={candidate.url}
                      className={`extend-candidate${inKb ? " is-in-kb" : ""}${
                        isSelected ? " is-selected" : ""
                      }`}
                    >
                      <label className="extend-checkbox">
                        <input
                          type="checkbox"
                          checked={isSelected}
                          disabled={inKb}
                          onChange={() => toggleCandidate(candidate.url)}
                          aria-label={`选择 ${candidate.title}`}
                        />
                        <span className="extend-checkbox-box" aria-hidden="true">
                          {isSelected ? <Check size={11} /> : null}
                        </span>
                      </label>
                      <div className="extend-candidate-main">
                        <div className="extend-candidate-head">
                          <a
                            className="extend-candidate-title"
                            href={candidate.url}
                            target="_blank"
                            rel="noreferrer noopener"
                            title={candidate.title}
                          >
                            {candidate.title || candidate.url}
                            <ExternalLink size={11} />
                          </a>
                          {inKb ? (
                            <span className="extend-state-badge is-kb">已在知识库</span>
                          ) : candidate.manual ? (
                            <span className="extend-state-badge is-manual">手动添加</span>
                          ) : (
                            <span
                              className={`extend-authority-badge ${authorityInfo(candidate).className}`}
                            >
                              {authorityInfo(candidate).label}
                            </span>
                          )}
                          {!inKb && typeof candidate.relevance === "number" ? (
                            <span className="extend-relevance">
                              相关度 <b>{Math.round(candidate.relevance * 100)}%</b>
                            </span>
                          ) : null}
                          {!inKb && candidate.feedback_label ? (
                            <span
                              className={`extend-feedback-badge${
                                typeof candidate.feedback_score === "number" && candidate.feedback_score < 0
                                  ? " is-negative"
                                  : ""
                              }`}
                              title={`反馈偏好：${candidate.feedback_label}`}
                            >
                              {typeof candidate.feedback_score === "number" && candidate.feedback_score < 0 ? (
                                <ThumbsDown size={9} />
                              ) : (
                                <ThumbsUp size={9} />
                              )}
                              {candidate.feedback_label}
                            </span>
                          ) : null}
                          {candidate.pdf_url ? (
                            <a
                              className="extend-pdf-link"
                              href={candidate.pdf_url}
                              target="_blank"
                              rel="noreferrer noopener"
                              title="打开 arXiv PDF"
                            >
                              <FileText size={10} />
                              PDF
                            </a>
                          ) : null}
                        </div>
                        <div className="extend-candidate-host">{hostOf(candidate.url)}</div>
                        {candidate.snippet ? (
                          <p className="extend-candidate-snippet">{candidate.snippet}</p>
                        ) : null}
                      </div>
                      <button
                        type="button"
                        className="btn btn-icon extend-ignore-btn"
                        onClick={() => void rejectItems([candidate])}
                        disabled={rejecting}
                        title="忽略这个链接，之后不再推荐"
                        aria-label={`忽略 ${candidate.title}`}
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  );
                })}
              </div>
            )}

            <div className="extend-manual">
              <div className="extend-manual-head">
                <Plus size={13} />
                手动添加链接
                <span className="extend-manual-hint">抓取正文或摘要后加入，同样需要你确认</span>
              </div>
              <div className="extend-manual-row">
                <input
                  className="input extend-manual-url"
                  value={manualUrl}
                  placeholder="https://example.com/article"
                  onChange={(event) => {
                    setManualUrl(event.target.value);
                    setManualError(null);
                  }}
                  spellCheck={false}
                />
                <input
                  className="input extend-manual-title"
                  value={manualTitle}
                  placeholder="标题（可选）"
                  onChange={(event) => setManualTitle(event.target.value)}
                />
                <button type="button" className="btn btn-sm" onClick={addManual}>
                  <Plus size={12} /> 添加
                </button>
              </div>
              {manualError ? <div className="field-error">{manualError}</div> : null}
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}
