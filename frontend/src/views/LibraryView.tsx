import { useCallback, useEffect, useRef, useState } from "react";
import {
  CloudUpload,
  FileText,
  Filter,
  Globe,
  Layers,
  RefreshCw,
  Search,
  Sparkles,
  Trash2,
  Waypoints,
  X,
} from "lucide-react";
import { useApp } from "../state/AppContext";
import {
  deleteDocument,
  getDocument,
  listDocuments,
  toUserMessage,
  uploadDocuments,
} from "../lib/api";
import { formatNumber } from "../lib/format";
import { navigate } from "../lib/router";
import { DocumentCard } from "./library/DocumentCard";
import { DocumentDrawer } from "./library/DocumentDrawer";
import { DocumentModals } from "./library/DocumentModals";
import { ConfirmDialog } from "../components/Modal";
import { EmptyState } from "../components/EmptyState";
import { SkeletonDocCard } from "../components/Skeleton";
import { Skeleton } from "../components/Skeleton";
import type { DocumentItem } from "../types";

const ALLOWED_EXTENSIONS = ["md", "markdown", "txt", "pdf", "docx", "html", "htm"];
const MAX_FILE_SIZE = 25 * 1024 * 1024;

interface UploadProgress {
  total: number;
  names: string[];
}

interface LibraryViewProps {
  query: Record<string, string>;
}

export function LibraryView({ query }: LibraryViewProps) {
  const {
    stats,
    statsError,
    statsLoading,
    refreshStats,
    toast,
    bumpGraphRevision,
    bumpLibraryRevision,
    libraryRevision,
    startChat,
  } = useApp();

  const [docs, setDocs] = useState<DocumentItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [activeTag, setActiveTag] = useState<string | null>(null);
  const [refreshSeed, setRefreshSeed] = useState(0);
  const [uploadProgress, setUploadProgress] = useState<UploadProgress | null>(null);
  const [dragging, setDragging] = useState(false);
  const [noteOpen, setNoteOpen] = useState(false);
  const [urlOpen, setUrlOpen] = useState(false);
  const [drawerDoc, setDrawerDoc] = useState<DocumentItem | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<DocumentItem | null>(null);
  const [deleting, setDeleting] = useState(false);

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const dragCounterRef = useRef(0);

  /* ------------------------------- 搜索防抖 ------------------------------- */

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search.trim()), 260);
    return () => window.clearTimeout(timer);
  }, [search]);

  /* -------------------------------- 路由参数 ------------------------------- */

  useEffect(() => {
    if (typeof query.q === "string") setSearch(query.q);
  }, [query.q]);

  useEffect(() => {
    if (!query.doc) return;
    let cancelled = false;
    const local = docs.find((item) => item.id === query.doc);
    if (local) {
      setDrawerDoc(local);
      return;
    }
    getDocument(query.doc)
      .then((document) => {
        if (!cancelled) setDrawerDoc(document);
      })
      .catch(() => {
        if (!cancelled) setDrawerDoc(null);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query.doc, docs]);

  /* -------------------------------- 文档列表 ------------------------------- */

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    listDocuments({
      q: debouncedSearch || undefined,
      tag: activeTag ?? undefined,
      limit: 100,
    })
      .then((data) => {
        if (cancelled) return;
        setDocs(data.items);
        setTotal(data.total);
        setError(null);
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
  }, [debouncedSearch, activeTag, refreshSeed, libraryRevision]);

  const refreshLibrary = useCallback(() => {
    setRefreshSeed((seed) => seed + 1);
    void refreshStats();
  }, [refreshStats]);

  const handleSaved = (): void => {
    refreshLibrary();
    bumpGraphRevision();
  };

  /* --------------------------------- 上传 ---------------------------------- */

  const processFiles = useCallback(
    async (fileList: FileList | File[]): Promise<void> => {
      const incoming = Array.from(fileList);
      if (incoming.length === 0) return;

      const accepted: File[] = [];
      let rejected = 0;
      for (const file of incoming) {
        const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
        if (!ALLOWED_EXTENSIONS.includes(extension) || file.size > MAX_FILE_SIZE) {
          rejected += 1;
        } else {
          accepted.push(file);
        }
      }

      if (rejected > 0) {
        toast(
          "error",
          `${rejected} 个文件未上传`,
          `仅支持 ${ALLOWED_EXTENSIONS.join(" / ")} 格式，单文件不超过 25 MB。`,
        );
      }
      if (accepted.length === 0) return;

      setUploadProgress({ total: accepted.length, names: accepted.map((file) => file.name) });
      try {
        const result = await uploadDocuments(accepted);
        const succeeded = result.items.filter((item) => item.ok).length;
        const failed = result.items.filter((item) => !item.ok);
        if (succeeded > 0) {
          toast(
            "success",
            `成功导入 ${succeeded} 篇文档`,
            "已建立分块、概念与图谱连接，可在对话中提问了。",
          );
        }
        if (failed.length > 0) {
          const first = failed[0];
          toast(
            "error",
            `${failed.length} 个文件导入失败`,
            first ? `${first.filename ?? "文件"}：${first.error ?? "解析失败"}` : "请稍后重试。",
          );
        }
        refreshLibrary();
        bumpGraphRevision();
      } catch (err) {
        toast("error", "上传失败", toUserMessage(err));
      } finally {
        setUploadProgress(null);
      }
    },
    [bumpGraphRevision, refreshLibrary, toast],
  );

  const openFilePicker = (): void => {
    if (uploadProgress) return;
    fileInputRef.current?.click();
  };

  const handleDragEnter = (event: React.DragEvent): void => {
    event.preventDefault();
    dragCounterRef.current += 1;
    if (event.dataTransfer.types.includes("Files")) setDragging(true);
  };

  const handleDragOver = (event: React.DragEvent): void => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  };

  const handleDragLeave = (event: React.DragEvent): void => {
    event.preventDefault();
    dragCounterRef.current = Math.max(0, dragCounterRef.current - 1);
    if (dragCounterRef.current === 0) setDragging(false);
  };

  const handleDrop = (event: React.DragEvent): void => {
    event.preventDefault();
    dragCounterRef.current = 0;
    setDragging(false);
    if (event.dataTransfer.files.length > 0) void processFiles(event.dataTransfer.files);
  };

  /* ------------------------------- 文档操作 -------------------------------- */

  const handleAsk = (document: DocumentItem): void => {
    setDrawerDoc(null);
    startChat({
      question: `请总结《${document.title}》的核心内容。`,
      docIds: [document.id],
      docTitle: document.title,
    });
  };

  const confirmDelete = async (): Promise<void> => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await deleteDocument(deleteTarget.id);
      toast("success", "文档已删除", `《${deleteTarget.title}》及其分块、图谱连接已移除。`);
      if (drawerDoc?.id === deleteTarget.id) setDrawerDoc(null);
      setDocs((prev) => prev.filter((item) => item.id !== deleteTarget.id));
      setTotal((value) => Math.max(0, value - 1));
      setDeleteTarget(null);
      refreshLibrary();
      bumpGraphRevision();
    } catch (err) {
      toast("error", "删除失败", toUserMessage(err));
    } finally {
      setDeleting(false);
    }
  };

  const tags = stats?.tags ?? [];
  const visibleTags = tags.slice(0, 12);
  const filtered = Boolean(debouncedSearch || activeTag);

  const statCards = [
    {
      label: "文档",
      value: stats?.documents ?? (statsLoading ? null : total),
      icon: FileText,
      tone: "cyan",
    },
    { label: "文本分块", value: stats?.chunks ?? null, icon: Layers, tone: "violet" },
    { label: "知识概念", value: stats?.concepts ?? null, icon: Sparkles, tone: "amber" },
    { label: "图谱连接", value: stats?.links ?? null, icon: Waypoints, tone: "mint" },
  ] as const;

  return (
    <div className="page-scroll">
      <div className="page">
        <div className="page-header">
          <div>
            <h1 className="page-heading">知识库</h1>
            <p className="page-sub">导入文档、笔记与网页，让知脉把它们织成一张可对话的知识网络。</p>
          </div>
          <div className="page-header-actions">
            <button type="button" className="btn btn-sm" onClick={() => setNoteOpen(true)}>
              <FileText size={14} />
              添加笔记
            </button>
            <button type="button" className="btn btn-sm" onClick={() => setUrlOpen(true)}>
              <Globe size={14} />
              添加网页
            </button>
            <button type="button" className="btn btn-primary btn-sm" onClick={openFilePicker}>
              <CloudUpload size={14} />
              上传文档
            </button>
          </div>
        </div>

        {/* 统计条 */}
        <div className="stats-bar">
          {statCards.map((card) => {
            const Icon = card.icon;
            return (
              <div key={card.label} className={`stat-card card stat-${card.tone}`}>
                <div className="stat-icon">
                  <Icon size={16} />
                </div>
                <div className="stat-body">
                  {card.value == null ? (
                    <Skeleton width={38} height={20} radius={6} />
                  ) : (
                    <span className="stat-value mono">{formatNumber(card.value)}</span>
                  )}
                  <span className="stat-label">{card.label}</span>
                </div>
              </div>
            );
          })}
          {statsError ? (
            <button type="button" className="stats-error" onClick={() => void refreshStats()}>
              <RefreshCw size={13} />
              统计加载失败，点击重试
            </button>
          ) : null}
        </div>

        {/* 上传区 */}
        <div
          className={`upload-zone${dragging ? " dragging" : ""}${uploadProgress ? " uploading" : ""}`}
          onDragEnter={handleDragEnter}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          onClick={openFilePicker}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              openFilePicker();
            }
          }}
          role="button"
          tabIndex={0}
          aria-label="上传文档：拖拽文件到这里，或点击选择文件"
        >
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept=".md,.markdown,.txt,.pdf,.docx,.html,.htm"
            className="sr-only"
            tabIndex={-1}
            onChange={(event) => {
              if (event.target.files) void processFiles(event.target.files);
              event.target.value = "";
            }}
          />
          <div className="upload-zone-icon">
            <CloudUpload size={26} />
            <span className="upload-orb" aria-hidden="true" />
          </div>
          <div className="upload-zone-text">
            <div className="upload-title">
              {uploadProgress ? (
                <>
                  <span className="spin-inline" aria-hidden="true" />
                  正在解析 {uploadProgress.total} 个文件…
                </>
              ) : (
                <>拖拽文件到这里，或点击选择文件</>
              )}
            </div>
            <div className="upload-hint">
              支持 Markdown / TXT / PDF / Word / HTML，单个文件不超过 25 MB，可一次上传多个
            </div>
            {uploadProgress ? (
              <div className="upload-progress">
                <div className="upload-progress-bar" />
                <span className="upload-progress-names">
                  {uploadProgress.names.slice(0, 3).join("、")}
                  {uploadProgress.names.length > 3 ? ` 等 ${uploadProgress.names.length} 个文件` : ""}
                </span>
              </div>
            ) : (
              <span className={`upload-cta${dragging ? " active" : ""}`}>选择文件</span>
            )}
          </div>
        </div>

        {/* 筛选栏 */}
        <div className="filter-bar">
          <div className="input-wrap filter-search">
            <Search size={15} />
            <input
              className="input"
              placeholder="搜索文档标题、摘要或标签…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              aria-label="搜索文档"
            />
            {search ? (
              <span className="input-suffix">
                <button
                  type="button"
                  className="btn btn-icon"
                  style={{ width: 28, height: 28 }}
                  onClick={() => setSearch("")}
                  aria-label="清空搜索"
                >
                  <X size={13} />
                </button>
              </span>
            ) : null}
          </div>
          <div className="filter-tags">
            <button
              type="button"
              className={`tag-chip${activeTag == null ? " active" : ""}`}
              onClick={() => setActiveTag(null)}
            >
              全部
            </button>
            {visibleTags.map((tag) => (
              <button
                key={tag.name}
                type="button"
                className={`tag-chip${activeTag === tag.name ? " active" : ""}`}
                onClick={() => setActiveTag(activeTag === tag.name ? null : tag.name)}
              >
                {tag.name}
                <span className="tag-count">{tag.count}</span>
              </button>
            ))}
            {visibleTags.length === 0 && !statsLoading ? (
              <span className="dim" style={{ fontSize: 12 }}>
                <Filter size={12} style={{ display: "inline", marginRight: 4 }} />
                导入文档后会出现标签筛选
              </span>
            ) : null}
          </div>
          <div className="filter-meta">
            <span>
              共 <b className="mono">{formatNumber(filtered ? docs.length : total)}</b> 份文档
            </span>
            {filtered ? (
              <button
                type="button"
                className="filter-clear"
                onClick={() => {
                  setSearch("");
                  setActiveTag(null);
                }}
              >
                清除筛选
              </button>
            ) : null}
            <button
              type="button"
              className="btn btn-icon"
              onClick={refreshLibrary}
              aria-label="刷新文档列表"
              data-tip="刷新"
            >
              <RefreshCw size={14} />
            </button>
          </div>
        </div>

        {error && docs.length > 0 ? (
          <div className="page-error" style={{ marginBottom: 16 }}>
            <RefreshCw size={16} />
            <div style={{ flex: 1 }}>
              <div style={{ color: "var(--text)", fontWeight: 600, marginBottom: 3 }}>
                刷新文档列表失败，当前展示的是上一次的数据
              </div>
              <div>{error}</div>
            </div>
            <button type="button" className="btn btn-sm" onClick={refreshLibrary}>
              <RefreshCw size={12} /> 重试
            </button>
          </div>
        ) : null}

        {/* 文档网格 */}
        {loading && docs.length === 0 ? (
          <div className="doc-grid">
            {Array.from({ length: 6 }).map((_, index) => (
              <SkeletonDocCard key={index} />
            ))}
          </div>
        ) : error && docs.length === 0 ? (
          <div className="page-error">
            <RefreshCw size={17} />
            <div style={{ flex: 1 }}>
              <div style={{ color: "var(--text)", fontWeight: 600, marginBottom: 3 }}>
                无法加载知识库
              </div>
              <div>{error}</div>
              <button
                type="button"
                className="btn btn-sm"
                style={{ marginTop: 12 }}
                onClick={refreshLibrary}
              >
                <RefreshCw size={12} /> 重试
              </button>
            </div>
          </div>
        ) : docs.length === 0 ? (
          <div className="card">
            {filtered ? (
              <EmptyState
                variant="search"
                title="没有找到匹配的文档"
                description="试试换一个关键词，或清除标签筛选后再看看。"
                actions={
                  <button
                    type="button"
                    className="btn"
                    onClick={() => {
                      setSearch("");
                      setActiveTag(null);
                    }}
                  >
                    清除筛选
                  </button>
                }
              />
            ) : (
              <EmptyState
                variant="docs"
                title="知识库还是空的"
                description="导入第一份文档或写一条笔记，知脉会为你建立索引与知识图谱。"
                actions={
                  <>
                    <button type="button" className="btn btn-primary" onClick={openFilePicker}>
                      <CloudUpload size={14} />
                      上传文档
                    </button>
                    <button type="button" className="btn" onClick={() => setNoteOpen(true)}>
                      <FileText size={14} />
                      添加笔记
                    </button>
                  </>
                }
              />
            )}
          </div>
        ) : (
          <>
            <div className="doc-grid">
              {docs.map((document, index) => (
                <DocumentCard
                  key={document.id}
                  document={document}
                  index={index}
                  onOpen={setDrawerDoc}
                  onAsk={handleAsk}
                  onDelete={setDeleteTarget}
                />
              ))}
            </div>
            {loading ? (
              <div className="doc-list-loading">
                <RefreshCw size={13} className="spin" /> 正在更新…
              </div>
            ) : null}
          </>
        )}
      </div>

      <DocumentDrawer
        open={Boolean(drawerDoc)}
        document={drawerDoc}
        onClose={() => {
          setDrawerDoc(null);
          if (query.doc) navigate("/library", { replace: true });
        }}
        onAsk={handleAsk}
        onDelete={setDeleteTarget}
      />

      <DocumentModals
        noteOpen={noteOpen}
        urlOpen={urlOpen}
        onCloseNote={() => setNoteOpen(false)}
        onCloseUrl={() => setUrlOpen(false)}
        onSaved={handleSaved}
      />

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="删除这份文档？"
        description={
          <span>
            确定删除 <strong>《{deleteTarget?.title}》</strong> 吗？它的{" "}
            {deleteTarget?.chunk_count ?? 0} 个分块、概念与图谱连接会一并移除，此操作不可撤销。
          </span>
        }
        confirmLabel="删除文档"
        loading={deleting}
        icon={<Trash2 size={16} />}
        onConfirm={() => void confirmDelete()}
        onClose={() => setDeleteTarget(null)}
      />
    </div>
  );
}
