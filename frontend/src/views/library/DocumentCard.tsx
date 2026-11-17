import { FileText, Globe, MessageSquare, StickyNote, Trash2 } from "lucide-react";
import type { DocumentItem } from "../../types";
import { formatNumber, formatRelativeTime, sourceTypeLabel } from "../../lib/format";
import { zoneColorFromId } from "../../lib/zones";

function DocTypeIcon({ type }: { type: string }) {
  if (type === "url" || type === "html" || type === "htm" || type === "web") {
    return <Globe size={17} />;
  }
  if (type === "note") return <StickyNote size={17} />;
  return <FileText size={17} />;
}

function typeTone(type: string): string {
  if (type === "url" || type === "html" || type === "htm" || type === "web") return "violet";
  if (type === "note") return "mint";
  return "cyan";
}

interface DocumentCardProps {
  document: DocumentItem;
  index: number;
  onOpen: (document: DocumentItem) => void;
  onAsk: (document: DocumentItem) => void;
  onDelete: (document: DocumentItem) => void;
}

export function DocumentCard({ document, index, onOpen, onAsk, onDelete }: DocumentCardProps) {
  const tone = typeTone(document.source_type);
  const failed = document.status === "error";

  return (
    <article
      className="doc-card card card-hover"
      style={{ animationDelay: `${Math.min(index, 12) * 26}ms` }}
      onClick={() => onOpen(document)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen(document);
        }
      }}
      role="button"
      tabIndex={0}
      aria-label={`查看文档 ${document.title}`}
    >
      <div className="doc-card-top">
        <div className={`doc-icon doc-icon-${tone}`}>
          <DocTypeIcon type={document.source_type} />
        </div>
        <div className="doc-card-meta-top">
          <span className="doc-type" title={document.filename ?? undefined}>
            {document.filename ?? sourceTypeLabel(document.source_type)}
          </span>
          <span className="doc-time">{formatRelativeTime(document.updated_at)}</span>
        </div>
        {failed ? (
          <span className="badge badge-rose doc-status" title={document.error ?? "解析失败"}>
            解析失败
          </span>
        ) : (
          <span className="doc-status-dot" title="已就绪" />
        )}
      </div>

      <h3 className="doc-title" title={document.title}>
        {document.title}
      </h3>
      <p className="doc-summary">{document.summary || "暂无摘要。"}</p>

      {document.zone_name || document.zone_id ? (
        <div className="doc-zone">
          <span
            className="zone-dot"
            style={{ background: zoneColorFromId(document.zone_id) }}
          />
          {document.zone_name ?? "已分区"}
        </div>
      ) : null}

      {document.tags.length > 0 ? (
        <div className="doc-tags">
          {document.tags.slice(0, 4).map((tag) => (
            <span key={tag} className="tag-chip">
              {tag}
            </span>
          ))}
        </div>
      ) : null}

      <div className="doc-card-foot">
        <span className="doc-chunk-count">
          <span className="mono">{formatNumber(document.chunk_count)}</span> 分块
        </span>
        <span className="doc-foot-sep" />
        <span>
          <span className="mono">{formatNumber(document.concept_count)}</span> 概念
        </span>
        <span className="doc-foot-sep" />
        <span>
          <span className="mono">{formatNumber(document.size)}</span> 字
        </span>
        <div className="doc-actions">
          <button
            type="button"
            className="doc-action-btn"
            onClick={(event) => {
              event.stopPropagation();
              onAsk(document);
            }}
            aria-label={`就《${document.title}》提问`}
            data-tip="就这篇提问"
          >
            <MessageSquare size={14} />
          </button>
          <button
            type="button"
            className="doc-action-btn danger"
            onClick={(event) => {
              event.stopPropagation();
              onDelete(document);
            }}
            aria-label={`删除文档《${document.title}》`}
            data-tip="删除"
          >
            <Trash2 size={14} />
          </button>
        </div>
      </div>
    </article>
  );
}
