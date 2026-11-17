import { useEffect, useState } from "react";
import {
  Check,
  FolderPlus,
  LoaderCircle,
  Pencil,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import { Modal, ConfirmDialog } from "../../components/Modal";
import { SkeletonRows } from "../../components/Skeleton";
import {
  createPlanGroup,
  deletePlanGroup,
  toUserMessage,
  updatePlanGroup,
} from "../../lib/api";
import { PLAN_COLORS } from "../../lib/plans";
import { useApp } from "../../state/AppContext";
import type { PlanGroup, PlanGroupsResponse } from "../../types";

const GROUP_NAME_MAX = 20;
const GROUP_DESC_MAX = 120;

interface PlanGroupManagerProps {
  open: boolean;
  groups: PlanGroupsResponse | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
  onRefresh: () => void;
  onChanged: () => void;
}

function GroupColorPicker({ value, onChange }: { value: string; onChange: (color: string) => void }) {
  return (
    <div className="group-color-row">
      {PLAN_COLORS.map((item) => (
        <button
          key={item}
          type="button"
          className={`group-color-swatch${value === item ? " active" : ""}`}
          style={{ background: item }}
          onClick={() => onChange(item)}
          aria-label={`选择分组颜色 ${item}`}
          aria-pressed={value === item}
        >
          {value === item ? <Check size={9} /> : null}
        </button>
      ))}
    </div>
  );
}

export function PlanGroupManager({
  open,
  groups,
  loading,
  error,
  onClose,
  onRefresh,
  onChanged,
}: PlanGroupManagerProps) {
  const { toast } = useApp();
  const [name, setName] = useState("");
  const [color, setColor] = useState<string>(PLAN_COLORS[0]);
  const [description, setDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editColor, setEditColor] = useState<string>(PLAN_COLORS[0]);
  const [editDescription, setEditDescription] = useState("");
  const [savingEdit, setSavingEdit] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<PlanGroup | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (!open) return;
    setName("");
    setDescription("");
    setColor(PLAN_COLORS[0]);
    setCreateError(null);
    setEditingId(null);
    setEditError(null);
    setDeleteTarget(null);
  }, [open]);

  const resetEdit = (): void => {
    setEditingId(null);
    setEditError(null);
  };

  const validateName = (value: string): string | null => {
    const trimmed = value.trim();
    if (!trimmed) return "请填写分组名称。";
    if (trimmed.length > GROUP_NAME_MAX) return `分组名称最多 ${GROUP_NAME_MAX} 个字符。`;
    return null;
  };

  const create = async (): Promise<void> => {
    if (creating) return;
    const nameError = validateName(name);
    if (nameError) {
      setCreateError(nameError);
      return;
    }
    if (description.trim().length > GROUP_DESC_MAX) {
      setCreateError(`描述最多 ${GROUP_DESC_MAX} 个字符。`);
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const result = await createPlanGroup({
        name: name.trim(),
        color,
        description: description.trim() || null,
      });
      toast("success", "分组已创建", `「${result.group.name}」已加入计划分组。`);
      setName("");
      setDescription("");
      setColor(PLAN_COLORS[0]);
      onChanged();
    } catch (err) {
      setCreateError(toUserMessage(err));
    } finally {
      setCreating(false);
    }
  };

  const startEdit = (group: PlanGroup): void => {
    setEditingId(group.id);
    setEditName(group.name);
    setEditColor(group.color);
    setEditDescription(group.description ?? "");
    setEditError(null);
  };

  const saveEdit = async (group: PlanGroup): Promise<void> => {
    if (savingEdit) return;
    const nameError = validateName(editName);
    if (nameError) {
      setEditError(nameError);
      return;
    }
    if (editDescription.trim().length > GROUP_DESC_MAX) {
      setEditError(`描述最多 ${GROUP_DESC_MAX} 个字符。`);
      return;
    }
    setSavingEdit(true);
    setEditError(null);
    try {
      const result = await updatePlanGroup(group.id, {
        name: editName.trim(),
        color: editColor,
        description: editDescription.trim() || null,
      });
      toast("success", "分组已更新", `「${result.group.name}」已保存。`);
      setEditingId(null);
      onChanged();
    } catch (err) {
      setEditError(toUserMessage(err));
    } finally {
      setSavingEdit(false);
    }
  };

  const confirmDelete = async (): Promise<void> => {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    try {
      const result = await deletePlanGroup(deleteTarget.id);
      toast(
        "success",
        "分组已删除",
        `「${deleteTarget.name}」的计划已转为未分组（${deleteTarget.plan_count} 个），计划与打卡均保留。`,
      );
      if (editingId === deleteTarget.id) setEditingId(null);
      setDeleteTarget(null);
      onChanged();
      // result.ungrouped_count 可用来提示未分组数量，当前 toast 已足够
      void result;
    } catch (err) {
      toast("error", "删除分组失败", toUserMessage(err));
    } finally {
      setDeleting(false);
    }
  };

  const items = groups?.items ?? [];

  return (
    <>
      <Modal
        open={open}
        onClose={onClose}
        title="管理分组"
        description="把计划放进不同的分组，让每日安排更有条理。删除分组不会删除计划与打卡。"
        icon={<FolderPlus size={17} />}
        width={760}
        footer={
          <>
            <span className="group-manager-foot">
              {items.length} 个分组 · 未分组 {groups?.ungrouped_count ?? 0} 个计划
            </span>
            <button type="button" className="btn" onClick={onClose}>
              完成
            </button>
          </>
        }
      >
        <div className="group-manager">
          <section className="group-create-card">
            <div className="group-create-title">
              <FolderPlus size={14} />
              新建分组
            </div>
            <div className="group-create-grid">
              <label className="field group-create-name">
                <span className="field-label">
                  <span>名称</span>
                  <span className="field-hint">
                    {name.length} / {GROUP_NAME_MAX}
                  </span>
                </span>
                <input
                  className="input"
                  value={name}
                  maxLength={GROUP_NAME_MAX + 10}
                  placeholder="例如：运动健康"
                  onChange={(event) => setName(event.target.value)}
                />
              </label>
              <div className="field group-create-desc">
                <span className="field-label">
                  <span>描述</span>
                  <span className="field-hint">可选 · {description.length} / {GROUP_DESC_MAX}</span>
                </span>
                <input
                  className="input"
                  value={description}
                  maxLength={GROUP_DESC_MAX + 20}
                  placeholder="这个分组用来放什么计划？"
                  onChange={(event) => setDescription(event.target.value)}
                />
              </div>
            </div>
            <div className="group-create-bottom">
              <GroupColorPicker value={color} onChange={setColor} />
              <button type="button" className="btn btn-primary btn-sm" onClick={() => void create()} disabled={creating}>
                {creating ? <LoaderCircle size={13} className="spin" /> : <FolderPlus size={13} />}
                {creating ? "创建中…" : "创建分组"}
              </button>
            </div>
            {createError ? <div className="field-error">{createError}</div> : null}
          </section>

          {loading && !groups ? (
            <div className="group-manager-loading">
              <SkeletonRows rows={3} />
            </div>
          ) : error && !groups ? (
            <div className="page-error">
              <X size={15} />
              <div style={{ flex: 1 }}>
                <div>{error}</div>
                <button type="button" className="btn btn-sm" style={{ marginTop: 10 }} onClick={onRefresh}>
                  <RefreshCw size={12} /> 重试
                </button>
              </div>
            </div>
          ) : items.length === 0 ? (
            <div className="group-manager-empty">
              还没有分组。先创建一个分组，再把计划放进去。
            </div>
          ) : (
            <div className="group-list">
              {items.map((group) => {
                const isEditing = editingId === group.id;
                if (isEditing) {
                  return (
                    <div className="group-list-item editing" key={group.id}>
                      <div className="group-edit-grid">
                        <input
                          className="input"
                          value={editName}
                          maxLength={GROUP_NAME_MAX + 10}
                          onChange={(event) => setEditName(event.target.value)}
                          aria-label="分组名称"
                          autoFocus
                        />
                        <input
                          className="input"
                          value={editDescription}
                          maxLength={GROUP_DESC_MAX + 20}
                          placeholder="描述（可选）"
                          onChange={(event) => setEditDescription(event.target.value)}
                          aria-label="分组描述"
                        />
                      </div>
                      <div className="group-edit-bottom">
                        <GroupColorPicker value={editColor} onChange={setEditColor} />
                        <div className="group-edit-actions">
                          <button type="button" className="btn btn-sm" onClick={resetEdit} disabled={savingEdit}>
                            取消
                          </button>
                          <button
                            type="button"
                            className="btn btn-primary btn-sm"
                            onClick={() => void saveEdit(group)}
                            disabled={savingEdit}
                          >
                            {savingEdit ? <LoaderCircle size={12} className="spin" /> : <Check size={12} />}
                            保存
                          </button>
                        </div>
                      </div>
                      {editError ? <div className="field-error">{editError}</div> : null}
                    </div>
                  );
                }
                return (
                  <div className="group-list-item" key={group.id}>
                    <span className="group-color-dot" style={{ background: group.color }} />
                    <div className="group-list-main">
                      <div className="group-list-head">
                        <span className="group-list-name">{group.name}</span>
                        <span className="group-list-count mono">{group.plan_count} 个计划</span>
                      </div>
                      {group.description ? (
                        <p className="group-list-desc" title={group.description}>
                          {group.description}
                        </p>
                      ) : null}
                    </div>
                    <div className="group-list-actions">
                      <button
                        type="button"
                        className="btn btn-icon"
                        onClick={() => startEdit(group)}
                        aria-label={`编辑分组 ${group.name}`}
                      >
                        <Pencil size={14} />
                      </button>
                      <button
                        type="button"
                        className="btn btn-icon plan-delete-btn"
                        onClick={() => setDeleteTarget(group)}
                        aria-label={`删除分组 ${group.name}`}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </div>
                );
              })}
              {groups?.ungrouped_count ? (
                <div className="group-list-item ungrouped-hint">
                  <span className="group-color-dot" style={{ background: "var(--dim)" }} />
                  <div className="group-list-main">
                    <div className="group-list-head">
                      <span className="group-list-name">未分组</span>
                      <span className="group-list-count mono">{groups.ungrouped_count} 个计划</span>
                    </div>
                    <p className="group-list-desc">未分组的计划会显示在计划页最后一个区块。</p>
                  </div>
                </div>
              ) : null}
            </div>
          )}
        </div>
      </Modal>

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="删除这个分组？"
        description={
          <span>
            删除 <strong>「{deleteTarget?.name}」</strong> 后，组内{" "}
            <strong>{deleteTarget?.plan_count ?? 0} 个计划</strong>会变成「未分组」，计划与打卡记录<strong>不会删除</strong>。
          </span>
        }
        confirmLabel="删除分组"
        loading={deleting}
        icon={<Trash2 size={16} />}
        onConfirm={() => void confirmDelete()}
        onClose={() => setDeleteTarget(null)}
      />

    </>
  );
}
