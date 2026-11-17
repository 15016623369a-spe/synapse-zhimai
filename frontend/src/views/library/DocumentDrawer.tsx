import { useEffect, useState } from "react";
import { FileText, Globe, MessageSquare, RefreshCw, StickyNote, Trash2, X } from "lucide-react";
import type { DocumentChunk, DocumentItem } from "../../types";
import { getDocumentChunks, toUserMessage } from "../../lib/api";
import { formatDateTime, formatExcerpt, formatNumber, sourceTypeLabel } from "../../lib/format";
import { zoneColorFromId } from "../../lib/zones";
import { Drawer } from "../../components/Drawer";
import { SkeletonRows } from "../../components/Skeleton";

function HeaderIcon({ type }: { type: string }) {
  if (type === "url" || type === "html" || type === "htm" || type === "web") return <Globe size={18} />;
  if (type === "note") return <StickyNote size={18} />;
  return <FileText size={18} />;
}

interface DocumentDrawerProps {
  open: boolean;
  document: DocumentItem | null;
  onClose: () => void;
  onAsk: (document: DocumentItem) => void;
  onDelete: (document: DocumentItem) => void;
}

export function DocumentDrawer({ open, document, onClose, onAsk, onDelete }: DocumentDrawerProps) {
  const [chunks, setChunks] = useState<DocumentChunk[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadSeed, setReloadSeed] = useState(0);

  useEffect(() => {
    if (!open || !document) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getDocumentChunks(document.id, 50)
      .then((data) => {
        if (!cancelled) setChunks(data.items);
      })
      .catch((err) => {
        if (!cancelled) setError(toUserMessage(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, document, reloadSeed]);

  return (
    <Drawer open={open} onClose={onClose} label="文档详情">
      {document ? (
        <>
          <div className="drawer-head">
            <div className="doc-icon doc-icon-cyan" style={{ width: 40, height: 40 }}>
              <HeaderIcon type={document.source_type} />
            </div>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div className="drawer-title">{document.title}</div>
              <div className="drawer-sub">
                {sourceTypeLabel(document.source_type)}
                {document.filename ? ` · ${document.filename}` : ""}
              </div>
            </div>
            <button type="button" className="btn btn-icon" onClick={onClose} aria-label="关闭文档详情">
              <X size={16} />
            </button>
          </div>

          <div className="drawer-body">
            <div className="doc-drawer-tags">
              {document.tags.length > 0 ? (
                document.tags.map((tag) => (
                  <span key={tag} className="tag-chip">
                    {tag}
                  </span>
                ))
              ) : (
                <span className="dim" style={{ fontSize: 12 }}>
                  未设置标签
                </span>
              )}
            </div>

            {document.summary ? <div className="doc-summary-box">{document.summary}</div> : null}

            <div className="doc-meta-grid">
              <div className="doc-meta-item">
                <span className="doc-meta-label">字符数</span>
                <span className="doc-meta-value mono">{formatNumber(document.size)}</span>
              </div>
              <div className="doc-meta-item">
                <span className="doc-meta-label">分块</span>
                <span className="doc-meta-value mono">{formatNumber(document.chunk_count)}</span>
              </div>
              <div className="doc-meta-item">
                <span className="doc-meta-label">概念</span>
                <span className="doc-meta-value mono">{formatNumber(document.concept_count)}</span>
              </div>
              <div className="doc-meta-item">
                <span className="doc-meta-label">状态</span>
                <span className="doc-meta-value">
                  {document.status === "ready" ? (
                    <span className="badge badge-mint">已就绪</span>
                  ) : (
                    <span className="badge badge-rose">解析失败</span>
                  )}
                </span>
              </div>
              <div className="doc-meta-item">
                <span className="doc-meta-label">所属分区</span>
                <span className="doc-meta-value">
                  <span
                    className="zone-dot doc-meta-zone-dot"
                    style={{ background: zoneColorFromId(document.zone_id) }}
                  />
                  {document.zone_name ?? "未分区"}
                </span>
              </div>
              <div className="doc-meta-item">
                <span className="doc-meta-label">创建时间</span>
                <span className="doc-meta-value mono">{formatDateTime(document.created_at)}</span>
              </div>
              <div className="doc-meta-item">
                <span className="doc-meta-label">更新时间</span>
                <span className="doc-meta-value mono">{formatDateTime(document.updated_at)}</span>
              </div>
            </div>

            {document.source_url ? (
              <div className="doc-source-url">
                <span className="doc-meta-label">来源</span>
                <a href={document.source_url} target="_blank" rel="noopener noreferrer">
                  {document.source_url}
                </a>
              </div>
            ) : null}

            {document.status === "error" && document.error ? (
              <div className="page-error">
                <FileText size={16} />
                <span>{document.error}</span>
              </div>
            ) : null}

            <section>
              <div className="drawer-section-head">
                <span>分块预览</span>
                <span className="mono dim">{loading ? "加载中" : `${chunks.length} 个分块`}</span>
              </div>
              {loading ? (
                <SkeletonRows rows={3} />
              ) : error ? (
                <div className="page-error">
                  <RefreshCw size={15} />
                  <div style={{ flex: 1 }}>
                    <div>{error}</div>
                    <button
                      type="button"
                      className="btn btn-sm"
                      style={{ marginTop: 10 }}
                      onClick={() => setReloadSeed((seed) => seed + 1)}
                    >
                      <RefreshCw size={12} /> 重新加载
                    </button>
                  </div>
                </div>
              ) : chunks.length === 0 ? (
                <div className="drawer-empty">该文档还没有可预览的分块。</div>
              ) : (
                <div className="chunk-list">
                  {chunks.map((chunk) => (
                    <div key={chunk.id} className="chunk-card">
                      <div className="chunk-card-head">
                        <span className="chunk-index">#{chunk.idx + 1}</span>
                        <span className="chunk-range mono">
                          字符 {chunk.char_start ?? 0}–{chunk.char_end ?? chunk.text.length}
                        </span>
                      </div>
                      <p className="chunk-text">{formatExcerpt(chunk.text)}</p>
                    </div>
                  ))}
                </div>
              )}
            </section>
          </div>

          <div className="drawer-foot">
            <button
              type="button"
              className="btn btn-primary"
              style={{ flex: 1 }}
              onClick={() => onAsk(document)}
            >
              <MessageSquare size={14} />
              就这篇文档提问
            </button>
            <button type="button" className="btn btn-danger" onClick={() => onDelete(document)}>
              <Trash2 size={14} />
              删除
            </button>
          </div>
        </>
      ) : null}
    </Drawer>
  );
}
