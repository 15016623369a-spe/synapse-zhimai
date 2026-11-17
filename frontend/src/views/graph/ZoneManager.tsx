import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Check,
  FolderPlus,
  Layers,
  LoaderCircle,
  Pencil,
  RefreshCw,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { ConfirmDialog, Modal } from "../../components/Modal";
import { SkeletonRows } from "../../components/Skeleton";
import { EmptyState } from "../../components/EmptyState";
import {
  assignDocumentsZone,
  createZone,
  deleteZone,
  listDocuments,
  toUserMessage,
  updateZone,
} from "../../lib/api";
import { ZONE_COLOR_PALETTE } from "../../lib/zones";
import { useApp } from "../../state/AppContext";
import type { DocumentItem, Zone, ZoneList } from "../../types";

const ZONE_NAME_MAX = 30;

type ManagerTab = "zones" | "docs";

interface ZoneManagerProps {
  open: boolean;
  onClose: () => void;
  zoneList: ZoneList | null;
  zonesLoading: boolean;
  zonesError: string | null;
  onRefreshZones: () => void;
  onChanged: () => void;
}

export function ZoneManager({
  open,
  onClose,
  zoneList,
  zonesLoading,
  zonesError,
  onRefreshZones,
  onChanged,
}: ZoneManagerProps) {
  const { toast } = useApp();
  const zones = zoneList?.items ?? [];
  const unassignedCount = zoneList?.unassigned_count ?? 0;

  const [tab, setTab] = useState<ManagerTab>("zones");

  const [createName, setCreateName] = useState("");
  const [createColor, setCreateColor] = useState<string>(ZONE_COLOR_PALETTE[0]);
  const [createDescription, setCreateDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameName, setRenameName] = useState("");
  const [renameSaving, setRenameSaving] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Zone | null>(null);
  const [deleting, setDeleting] = useState(false);

  const [docs, setDocs] = useState<DocumentItem[] | null>(null);
  const [docsLoading, setDocsLoading] = useState(false);
  const [docsError, setDocsError] = useState<string | null>(null);
  const [docsSeed, setDocsSeed] = useState(0);
  const [docSearch, setDocSearch] = useState("");
  const [selectedDocIds, setSelectedDocIds] = useState<Set<string>>(new Set());
  const [archiveTarget, setArchiveTarget] = useState<string>("unassigned");
  const [assigning, setAssigning] = useState(false);

  useEffect(() => {
    if (!open) return;
    setTab("zones");
    setCreateName("");
    setCreateColor(ZONE_COLOR_PALETTE[zones.length % ZONE_COLOR_PALETTE.length]);
    setCreateDescription("");
    setCreateError(null);
    setRenamingId(null);
    setSelectedDocIds(new Set());
    setDocSearch("");
    setDocsSeed(0);
    setDocs(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setDocsLoading(true);
    setDocsError(null);
    listDocuments({ limit: 100 })
      .then((data) => {
        if (!cancelled) setDocs(data.items);
      })
      .catch((error) => {
        if (!cancelled) setDocsError(toUserMessage(error));
      })
      .finally(() => {
        if (!cancelled) setDocsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, tab, docsSeed]);

  useEffect(() => {
    if (!open) return;
    setArchiveTarget((current) => {
      if (current !== "unassigned" && zones.some((zone) => zone.id === current)) {
        return current;
      }
      return zones.length > 0 ? zones[0].id : "unassigned";
    });
  }, [open, zones]);

  const filteredDocs = useMemo(() => {
    if (!docs) return [];
    const keyword = docSearch.trim().toLowerCase();
    if (!keyword) return docs;
    return docs.filter(
      (doc) =>
        doc.title.toLowerCase().includes(keyword) ||
        (doc.filename ?? "").toLowerCase().includes(keyword),
    );
  }, [docs, docSearch]);

  const allFilteredSelected =
    filteredDocs.length > 0 && filteredDocs.every((doc) => selectedDocIds.has(doc.id));

  const handleCreate = async (): Promise<void> => {
    if (creating) return;
    const name = createName.trim();
    if (!name) {
      setCreateError("请填写分区名称。");
      return;
    }
    if (name.length > ZONE_NAME_MAX) {
      setCreateError(`分区名称最多 ${ZONE_NAME_MAX} 个字符。`);
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const result = await createZone({
        name,
        color: createColor,
        description: createDescription.trim() || null,
      });
      toast("success", "分区已创建", `《${result.zone.name}》已建立，可以把相关文档归档进去。`);
      setCreateName("");
      setCreateDescription("");
      onChanged();
    } catch (error) {
      setCreateError(toUserMessage(error));
    } finally {
      setCreating(false);
    }
  };

  const startRename = (zone: Zone): void => {
    setRenamingId(zone.id);
    setRenameName(zone.name);
    setRenameError(null);
  };

  const saveRename = async (): Promise<void> => {
    if (!renamingId || renameSaving) return;
    const name = renameName.trim();
    if (!name) {
      setRenameError("请填写分区名称。");
      return;
    }
    if (name.length > ZONE_NAME_MAX) {
      setRenameError(`分区名称最多 ${ZONE_NAME_MAX} 个字符。`);
      return;
    }
    setRenameSaving(true);
    setRenameError(null);
    try {
      const result = await updateZone(renamingId, { name });
      toast("success", "分区已重命名", `新名称：《${result.zone.name}》。`);
      setRenamingId(null);
      onChanged();
    } catch (error) {
      setRenameError(toUserMessage(error));
    } finally {
      setRenameSaving(false);
    }
  };

  const confirmDelete = async (): Promise<void> => {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    try {
      const result = await deleteZone(deleteTarget.id);
      toast(
        "success",
        "分区已删除",
        `《${deleteTarget.name}》已删除，其 ${result.unassigned_count} 篇文档已变为未分区。`,
      );
      setDeleteTarget(null);
      onChanged();
      setDocsSeed((seed) => seed + 1);
    } catch (error) {
      toast("error", "删除分区失败", toUserMessage(error));
    } finally {
      setDeleting(false);
    }
  };

  const toggleDoc = (docId: string): void => {
    setSelectedDocIds((prev) => {
      const next = new Set(prev);
      if (next.has(docId)) next.delete(docId);
      else next.add(docId);
      return next;
    });
  };

  const toggleAllFiltered = (): void => {
    setSelectedDocIds((prev) => {
      const next = new Set(prev);
      if (allFilteredSelected) {
        for (const doc of filteredDocs) next.delete(doc.id);
      } else {
        for (const doc of filteredDocs) next.add(doc.id);
      }
      return next;
    });
  };

  const applyArchive = async (): Promise<void> => {
    if (assigning || selectedDocIds.size === 0) return;
    const targetId = archiveTarget === "unassigned" ? null : archiveTarget;
    const targetZone = targetId ? zones.find((zone) => zone.id === targetId) : null;
    setAssigning(true);
    try {
      const result = await assignDocumentsZone({
        doc_ids: [...selectedDocIds],
        zone_id: targetId,
      });
      toast(
        "success",
        targetId ? "文档已移入分区" : "文档已移出分区",
        targetId
          ? `已将 ${result.updated} 篇文档移入《${targetZone?.name ?? "分区"}》。`
          : `已将 ${result.updated} 篇文档移出分区，变为未分区。`,
      );
      setSelectedDocIds(new Set());
      setDocsSeed((seed) => seed + 1);
      onChanged();
    } catch (error) {
      toast("error", "归档失败", toUserMessage(error));
    } finally {
      setAssigning(false);
    }
  };

  const docZoneLabel = (doc: DocumentItem): string => {
    if (!doc.zone_id) return "未分区";
    return doc.zone_name ?? zones.find((zone) => zone.id === doc.zone_id)?.name ?? "分区";
  };

  const docZoneColor = (doc: DocumentItem): string => {
    if (!doc.zone_id) return "#5B6F8C";
    return zones.find((zone) => zone.id === doc.zone_id)?.color ?? "#4FD8FF";
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="管理分区"
      description="把相关的文档放进同一个分区，分区图谱会为它们生成独立的 3D 知识岛。"
      icon={<Layers size={17} />}
      width={760}
      footer={
        <>
          <span className="zone-manager-foot-hint">
            {zoneList ? `共 ${zones.length} 个分区 · ${unassignedCount} 篇未分区` : ""}
          </span>
          <button type="button" className="btn" onClick={onClose}>
            完成
          </button>
        </>
      }
    >
      <div className="zone-manager-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "zones"}
          className={`zone-tab${tab === "zones" ? " active" : ""}`}
          onClick={() => setTab("zones")}
        >
          分区管理
          <span className="zone-tab-count">{zones.length}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "docs"}
          className={`zone-tab${tab === "docs" ? " active" : ""}`}
          onClick={() => setTab("docs")}
        >
          文档归档
          <span className="zone-tab-count">{docs?.length ?? "…"}</span>
        </button>
      </div>

      {tab === "zones" ? (
        <div className="zone-manager-pane">
          <div className="zone-create-card">
            <div className="zone-create-title">
              <FolderPlus size={14} />
              新建分区
            </div>
            <div className="zone-create-row">
              <input
                className="input"
                value={createName}
                maxLength={ZONE_NAME_MAX}
                placeholder="分区名称，例如：AI 与大模型"
                onChange={(event) => setCreateName(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    void handleCreate();
                  }
                }}
                aria-label="新建分区名称"
              />
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void handleCreate()}
                disabled={creating}
              >
                {creating ? <LoaderCircle size={14} className="spin" /> : <FolderPlus size={14} />}
                创建
              </button>
            </div>
            <div className="zone-color-row">
              <span className="zone-field-label">颜色</span>
              <div className="zone-color-swatches">
                {ZONE_COLOR_PALETTE.map((color) => (
                  <button
                    key={color}
                    type="button"
                    className={`zone-color-swatch${createColor === color ? " active" : ""}`}
                    style={{ background: color }}
                    onClick={() => setCreateColor(color)}
                    aria-label={`选择颜色 ${color}`}
                    aria-pressed={createColor === color}
                  >
                    {createColor === color ? <Check size={11} /> : null}
                  </button>
                ))}
              </div>
            </div>
            <input
              className="input zone-create-desc"
              value={createDescription}
              maxLength={120}
              placeholder="可选：一句话描述这个分区"
              onChange={(event) => setCreateDescription(event.target.value)}
              aria-label="分区描述"
            />
            {createError ? <div className="field-error">{createError}</div> : null}
          </div>

          <div className="zone-list-head">
            <span className="zone-field-label">已有分区</span>
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={onRefreshZones}
              disabled={zonesLoading}
            >
              <RefreshCw size={12} className={zonesLoading ? "spin" : undefined} />
              刷新
            </button>
          </div>

          {zonesLoading && zones.length === 0 ? (
            <SkeletonRows rows={3} />
          ) : zonesError && zones.length === 0 ? (
            <div className="page-error">
              <X size={15} />
              <div style={{ flex: 1 }}>
                <div>{zonesError}</div>
                <button
                  type="button"
                  className="btn btn-sm"
                  style={{ marginTop: 10 }}
                  onClick={onRefreshZones}
                >
                  <RefreshCw size={12} /> 重试
                </button>
              </div>
            </div>
          ) : zones.length === 0 ? (
            <div className="zone-empty-hint">
              还没有分区。给相关的文档建一个分区，它们会在分区图谱里形成独立的知识岛。
            </div>
          ) : (
            <div className="zone-list">
              {zones.map((zone) => (
                <div key={zone.id} className="zone-list-item">
                  <span className="zone-dot" style={{ background: zone.color }} />
                  <div className="zone-list-main">
                    {renamingId === zone.id ? (
                      <div className="zone-rename-row">
                        <input
                          className="input input-sm"
                          value={renameName}
                          maxLength={ZONE_NAME_MAX}
                          autoFocus
                          onChange={(event) => setRenameName(event.target.value)}
                          onKeyDown={(event) => {
                            if (event.key === "Enter") {
                              event.preventDefault();
                              void saveRename();
                            } else if (event.key === "Escape") {
                              setRenamingId(null);
                            }
                          }}
                          aria-label={`重命名分区 ${zone.name}`}
                        />
                        <button
                          type="button"
                          className="btn btn-sm btn-primary"
                          onClick={() => void saveRename()}
                          disabled={renameSaving}
                        >
                          {renameSaving ? <LoaderCircle size={12} className="spin" /> : <Check size={12} />}
                          保存
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() => setRenamingId(null)}
                        >
                          取消
                        </button>
                      </div>
                    ) : (
                      <>
                        <span className="zone-list-name">{zone.name}</span>
                        {zone.description ? (
                          <span className="zone-list-desc">{zone.description}</span>
                        ) : null}
                      </>
                    )}
                    {renamingId === zone.id && renameError ? (
                      <div className="field-error">{renameError}</div>
                    ) : null}
                  </div>
                  <span className="zone-list-count mono">{zone.document_count} 篇</span>
                  <div className="zone-list-actions">
                    <button
                      type="button"
                      className="btn btn-icon"
                      onClick={() => startRename(zone)}
                      aria-label={`重命名 ${zone.name}`}
                      data-tip="重命名"
                    >
                      <Pencil size={13} />
                    </button>
                    <button
                      type="button"
                      className="btn btn-icon zone-delete-btn"
                      onClick={() => setDeleteTarget(zone)}
                      aria-label={`删除分区 ${zone.name}`}
                      data-tip="删除分区"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                </div>
              ))}
              {unassignedCount > 0 ? (
                <div className="zone-list-item unassigned">
                  <span className="zone-dot" style={{ background: "#5B6F8C" }} />
                  <div className="zone-list-main">
                    <span className="zone-list-name">未分区</span>
                    <span className="zone-list-desc">尚未归档的文档</span>
                  </div>
                  <span className="zone-list-count mono">{unassignedCount} 篇</span>
                </div>
              ) : null}
            </div>
          )}

          <button
            type="button"
            className="zone-goto-docs"
            onClick={() => setTab("docs")}
          >
            去归档文档 →
          </button>
        </div>
      ) : (
        <div className="zone-manager-pane">
          <div className="zone-doc-toolbar">
            <div className="input-wrap zone-doc-search">
              <Search size={14} />
              <input
                className="input"
                placeholder="搜索文档标题或文件名…"
                value={docSearch}
                onChange={(event) => setDocSearch(event.target.value)}
                aria-label="搜索待归档文档"
              />
              {docSearch ? (
                <span className="input-suffix">
                  <button
                    type="button"
                    className="btn btn-icon"
                    style={{ width: 28, height: 28 }}
                    onClick={() => setDocSearch("")}
                    aria-label="清空文档搜索"
                  >
                    <X size={12} />
                  </button>
                </span>
              ) : null}
            </div>
            <button
              type="button"
              className="btn btn-sm"
              onClick={toggleAllFiltered}
              disabled={filteredDocs.length === 0}
            >
              {allFilteredSelected ? "清空选择" : "全选"}
            </button>
            <span className="zone-doc-selected mono">
              已选 {selectedDocIds.size} 篇
            </span>
          </div>

          {docsLoading && !docs ? (
            <SkeletonRows rows={4} />
          ) : docsError && !docs ? (
            <div className="page-error">
              <X size={15} />
              <div style={{ flex: 1 }}>
                <div>{docsError}</div>
                <button
                  type="button"
                  className="btn btn-sm"
                  style={{ marginTop: 10 }}
                  onClick={() => setDocsSeed((seed) => seed + 1)}
                >
                  <RefreshCw size={12} /> 重试
                </button>
              </div>
            </div>
          ) : filteredDocs.length === 0 ? (
            <EmptyState
              variant="docs"
              title={docSearch ? "没有匹配的文档" : "知识库里还没有文档"}
              description={docSearch ? "换一个关键词试试。" : "先到知识库导入文档，再来归档。"}
              compact
            />
          ) : (
            <div className="zone-doc-list">
              {filteredDocs.map((doc) => {
                const selected = selectedDocIds.has(doc.id);
                return (
                  <label key={doc.id} className={`zone-doc-item${selected ? " selected" : ""}`}>
                    <input
                      type="checkbox"
                      checked={selected}
                      onChange={() => toggleDoc(doc.id)}
                      aria-label={`选择文档 ${doc.title}`}
                    />
                    <span className="zone-doc-main">
                      <span className="zone-doc-title">{doc.title}</span>
                      <span className="zone-doc-meta">
                        <span className="zone-dot" style={{ background: docZoneColor(doc) }} />
                        {docZoneLabel(doc)}
                      </span>
                    </span>
                  </label>
                );
              })}
            </div>
          )}

          <div className="zone-archive-bar">
            <span className="zone-field-label">将所选文档移入</span>
            <select
              className="select zone-archive-select"
              value={archiveTarget}
              onChange={(event) => setArchiveTarget(event.target.value)}
              aria-label="选择目标分区"
            >
              <option value="unassigned">未分区（移出分区）</option>
              {zones.map((zone) => (
                <option key={zone.id} value={zone.id}>
                  {zone.name}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void applyArchive()}
              disabled={assigning || selectedDocIds.size === 0}
            >
              {assigning ? <LoaderCircle size={14} className="spin" /> : null}
              应用（{selectedDocIds.size} 篇）
            </button>
          </div>
          <div className="zone-manager-foot-hint">
            支持多选：勾选文档后可批量移入或移出分区，不会删除文档。
          </div>
        </div>
      )}

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="删除这个分区？"
        description={
          <span>
            确定删除 <strong>《{deleteTarget?.name}》</strong> 吗？删除后该分区的{" "}
            {deleteTarget?.document_count ?? 0} 篇文档将变为未分区，
            <strong>不会删除文档</strong>。此操作不可撤销。
          </span>
        }
        confirmLabel="删除分区"
        loading={deleting}
        icon={<Trash2 size={16} />}
        onConfirm={() => void confirmDelete()}
        onClose={() => setDeleteTarget(null)}
      />
    </Modal>
  );
}
