import { BookOpen, ExternalLink, FileText, X } from "lucide-react";
import type { Citation } from "../../types";
import { EmptyState } from "../../components/EmptyState";
import { formatExcerpt, formatScore, sourceTypeLabel } from "../../lib/format";
import { navigate } from "../../lib/router";

interface CitationPanelProps {
  open: boolean;
  citations: Citation[];
  activeIndex: number | null;
  onSelect: (index: number) => void;
  onClose: () => void;
}

export function CitationPanel({ open, citations, activeIndex, onSelect, onClose }: CitationPanelProps) {
  if (!open) return null;

  return (
    <aside className="citations-panel" aria-label="引用来源面板">
      <div className="citations-panel-head">
        <BookOpen size={15} style={{ color: "var(--amber)" }} />
        <span className="citations-panel-title">引用来源</span>
        <span className="citations-panel-sub">{citations.length ? `${citations.length} 条` : ""}</span>
        <button type="button" className="btn btn-icon" onClick={onClose} aria-label="收起引用来源面板">
          <X size={15} />
        </button>
      </div>

      {citations.length === 0 ? (
        <div className="citations-empty">
          <EmptyState
            variant="citations"
            title="还没有引用来源"
            description="当知脉根据你的文档给出回答后，相关的原文片段与相关度会显示在这里。"
            compact
          />
        </div>
      ) : (
        <div className="citations-panel-body">
          {citations.map((citation) => {
            const active = activeIndex === citation.index;
            return (
              <div
                key={`${citation.chunk_id}-${citation.index}`}
                className={`citation-card${active ? " active" : ""}`}
                onMouseEnter={() => onSelect(citation.index)}
              >
                <div className="citation-card-head">
                  <span className="citation-index">{citation.index}</span>
                  <div style={{ minWidth: 0 }}>
                    <div className="citation-card-title">{citation.doc_title}</div>
                    <div className="citation-card-meta">
                      <span>{sourceTypeLabel(citation.source_type)}</span>
                      <span>·</span>
                      <span>相关度 {formatScore(citation.score)}</span>
                    </div>
                  </div>
                </div>
                <div className="citation-score-bar">
                  <div
                    className="citation-score-fill"
                    style={{ width: `${Math.max(6, Math.min(100, Math.round(citation.score * 100)))}%` }}
                  />
                </div>
                <div className="citation-card-snippet">{formatExcerpt(citation.snippet)}</div>
                <button
                  type="button"
                  className="citation-open"
                  onClick={() => navigate("/library", { query: { doc: citation.doc_id } })}
                >
                  <FileText size={11} />
                  查看文档
                  <ExternalLink size={10} />
                </button>
              </div>
            );
          })}
        </div>
      )}
    </aside>
  );
}
