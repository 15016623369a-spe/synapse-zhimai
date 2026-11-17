import {
  BookOpen,
  Check,
  CircleAlert,
  Copy,
  Database,
  ExternalLink,
  Globe,
  History,
  Info,
  LoaderCircle,
  RefreshCw,
  Search,
  Sparkles,
  Square,
} from "lucide-react";
import type { WebSourceCandidate } from "../../types";
import type { UiMessage } from "./chatTypes";
import { Markdown } from "../../components/Markdown";
import { contextHintLabel, formatExcerpt } from "../../lib/format";
import { ToolCallList } from "./ToolCallList";

function formatClock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
}

const MODE_LABELS = {
  llm: { label: "AI 生成", className: "badge badge-violet", icon: Sparkles },
  extractive: { label: "本地检索", className: "badge badge-cyan", icon: Database },
} as const;

interface MessageBubbleProps {
  message: UiMessage;
  copied: boolean;
  onCopy: (message: UiMessage) => void;
  onRetry: () => void;
  onCitationClick: (message: UiMessage, index: number) => void;
  onOpenCitations: (message: UiMessage, index?: number) => void;
  question?: string;
  onExtend?: (question: string, candidates?: WebSourceCandidate[], queries?: string[]) => void;
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

export function MessageBubble({
  message,
  copied,
  onCopy,
  onRetry,
  onCitationClick,
  onOpenCitations,
  question,
  onExtend,
}: MessageBubbleProps) {
  if (message.role === "user") {
    return (
      <div className="msg msg-user">
        <div className="msg-body">
          <div className="msg-user-bubble">{message.content}</div>
        </div>
        <div className="msg-avatar msg-avatar-user" aria-hidden="true">
          我
        </div>
      </div>
    );
  }

  const modeInfo = message.mode ? MODE_LABELS[message.mode] : null;
  const ModeIcon = modeInfo?.icon ?? LoaderCircle;
  const hasCitations = message.citations.length > 0;

  return (
    <div className="msg msg-assistant">
      <div className="msg-avatar msg-avatar-assistant" aria-hidden="true">
        知
      </div>
      <div className="msg-body">
        <div className={`assistant-card${message.error ? " is-error" : ""}`}>
          <div className="msg-head">
            <span className="msg-author">知脉</span>
            {modeInfo ? (
              <span className={modeInfo.className}>
                <ModeIcon size={10} />
                {modeInfo.label}
              </span>
            ) : message.streaming ? (
              <span className="badge badge-cyan">
                <LoaderCircle size={10} className="spin" />
                检索中
              </span>
            ) : null}
            {message.reference ? (
              <span
                className="msg-reference-chip"
                title={message.reference.filename ?? message.reference.title}
              >
                <BookOpen size={10} />
                知识库文档《{message.reference.title}》
              </span>
            ) : null}
            {message.contextUsed ? (
              <span
                className="msg-context-chip"
                title={contextHintLabel(message.contextSummary)}
                aria-label="多轮上下文提示"
              >
                <History size={10} />
                <span className="msg-context-text">
                  {contextHintLabel(message.contextSummary)}
                </span>
              </span>
            ) : null}
            <span className="msg-time">{formatClock(message.createdAt)}</span>
            <div className="msg-head-actions">
              {question && onExtend ? (
                <button
                  type="button"
                  className="msg-icon-btn extend-msg-btn"
                  onClick={() => onExtend(question)}
                  aria-label="联网搜索补充资料"
                  data-tip="补充资料"
                  disabled={message.streaming}
                >
                  <Globe size={14} />
                </button>
              ) : null}
              {message.content ? (
                <button
                  type="button"
                  className="msg-icon-btn"
                  onClick={() => onCopy(message)}
                  aria-label="复制回答"
                  data-tip="复制"
                >
                  {copied ? <Check size={14} style={{ color: "var(--mint)" }} /> : <Copy size={14} />}
                </button>
              ) : null}
              {hasCitations ? (
                <button
                  type="button"
                  className="msg-icon-btn"
                  onClick={() => onOpenCitations(message)}
                  aria-label="查看引用来源"
                  data-tip="引用来源"
                >
                  <BookOpen size={14} />
                </button>
              ) : null}
            </div>
          </div>

          {message.notice ? (
            <div className="msg-notice">
              <Info size={13} />
              <span>{message.notice}</span>
            </div>
          ) : null}

          {message.tools && message.tools.length > 0 ? <ToolCallList tools={message.tools} /> : null}

          {message.content ? (
            <Markdown
              content={message.content}
              onCitationClick={
                hasCitations ? (index) => onCitationClick(message, index) : undefined
              }
            />
          ) : message.streaming ? (
            <div className="typing-dots" aria-label="正在生成回答">
              <span />
              <span />
              <span />
            </div>
          ) : null}

          {message.streaming && message.content ? (
            <span className="stream-caret" aria-hidden="true" />
          ) : null}

          {message.stopped ? (
            <div className="msg-stopped">
              <Square size={9} fill="currentColor" />
              已停止生成
            </div>
          ) : null}

          {message.error ? (
            <div className="msg-error">
              <div className="msg-error-head">
                <CircleAlert size={14} />
                <span>回答中断</span>
              </div>
              <div className="msg-error-text">{message.error}</div>
              <button type="button" className="btn btn-sm" onClick={onRetry}>
                <RefreshCw size={12} />
                重试
              </button>
            </div>
          ) : null}

          {message.gap?.needs_more && !message.reference && !message.error ? (
            <div className="gap-card">
              <div className="gap-card-head">
                <span className="gap-card-icon">
                  <Search size={14} />
                </span>
                <div style={{ minWidth: 0 }}>
                  <div className="gap-card-title">知识库资料有限</div>
                  <div className="gap-card-text">
                    {message.gap.reason ||
                      "当前知识库中没有找到足够信息，可以联网检索补充资料，加入前由你确认。"}
                  </div>
                </div>
              </div>
              {(message.gap.suggested_queries?.length ?? 0) > 0 ? (
                <div className="gap-queries">
                  {(message.gap.suggested_queries ?? []).slice(0, 4).map((query) => (
                    <button
                      key={query}
                      type="button"
                      className="gap-query-chip"
                      onClick={() => onExtend?.(question ?? "", undefined, [query])}
                      title={`用「${query}」搜索补充资料`}
                    >
                      {query}
                    </button>
                  ))}
                </div>
              ) : null}
              <button
                type="button"
                className="btn btn-sm gap-cta"
                onClick={() =>
                  onExtend?.(question ?? "", undefined, message.gap?.suggested_queries ?? [])
                }
              >
                <Globe size={12} />
                搜索补充资料
              </button>
            </div>
          ) : null}

          {message.webSources && message.webSources.length > 0 && !message.reference && !message.error ? (
            <div className="web-sources-card">
              <div className="web-sources-head">
                <span className="web-sources-title">
                  <Globe size={12} />
                  外部参考 · 未入库
                </span>
                <span className="web-sources-count mono">
                  {message.webSources.length} 条
                </span>
              </div>
              <div className="web-sources-list">
                {message.webSources.slice(0, 3).map((source) => (
                  <div className="web-source-item" key={source.url}>
                    <span className="web-source-main">
                      <span className="web-source-title" title={source.title}>
                        {source.title || source.url}
                      </span>
                      <span className="web-source-host mono">{hostOf(source.url)}</span>
                      {source.feedback_label ? (
                        <span
                          className={`web-source-feedback${
                            typeof source.feedback_score === "number" && source.feedback_score < 0
                              ? " is-negative"
                              : ""
                          }`}
                          title={`反馈偏好：${source.feedback_label}`}
                        >
                          {source.feedback_label}
                        </span>
                      ) : null}
                    </span>
                    <span className="web-source-snippet">{source.snippet}</span>
                  </div>
                ))}
              </div>
              <button
                type="button"
                className="btn btn-sm web-sources-cta"
                onClick={() => onExtend?.(question ?? "", message.webSources)}
              >
                <ExternalLink size={12} />
                查看并选择加入知识库
              </button>
            </div>
          ) : null}

          {hasCitations && !message.error ? (
            <div className="citations-block">
              <div className="citations-label">
                <BookOpen size={12} />
                参考来源 · {message.citations.length}
              </div>
              <div className="citation-chips">
                {message.citations.map((citation) => (
                  <button
                    key={`${citation.chunk_id}-${citation.index}`}
                    type="button"
                    className="citation-chip"
                    onClick={() => onOpenCitations(message, citation.index)}
                  >
                    <span className="citation-index">{citation.index}</span>
                    <span className="citation-chip-main">
                      <span className="citation-chip-title">{citation.doc_title}</span>
                      <span className="citation-chip-snippet">{formatExcerpt(citation.snippet)}</span>
                    </span>
                  </button>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

