import { useEffect, useState } from "react";
import {
  CalendarCheck,
  CalendarClock,
  CalendarDays,
  Check,
  Clock,
  LoaderCircle,
  MapPin,
  Minus,
  Plus,
  Repeat,
} from "lucide-react";
import { Modal } from "../../components/Modal";
import { createPlan, toUserMessage, updatePlan } from "../../lib/api";
import {
  PLAN_COLORS,
  PLAN_DESC_MAX,
  PLAN_TITLE_MAX,
  addDaysIso,
  parseIsoDate,
  type PlanTemplate,
} from "../../lib/plans";
import { useApp } from "../../state/AppContext";
import type { Plan, PlanFrequency, PlanGroup } from "../../types";

type FormKind = "recurring" | "once";

interface PlanFormDialogProps {
  open: boolean;
  mode: "create" | "edit";
  initialPlan: Plan | null;
  template: PlanTemplate | null;
  groups: PlanGroup[];
  defaultGroupId: string | null;
  today: string;
  onClose: () => void;
  onSaved: (plan: Plan, mode: "create" | "edit") => void;
}

function isValidDate(value: string): boolean {
  return Boolean(value) && parseIsoDate(value) !== null;
}

function isValidTime(value: string): boolean {
  return /^([01]\d|2[0-3]):[0-5]\d$/.test(value);
}

export function PlanFormDialog({
  open,
  mode,
  initialPlan,
  template,
  groups,
  defaultGroupId,
  today,
  onClose,
  onSaved,
}: PlanFormDialogProps) {
  const { toast } = useApp();
  const [kind, setKind] = useState<FormKind>("recurring");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [frequency, setFrequency] = useState<PlanFrequency>("daily");
  const [weeklyTarget, setWeeklyTarget] = useState(3);
  const [color, setColor] = useState<string>(PLAN_COLORS[0]);
  const [startDate, setStartDate] = useState(today);
  const [planDate, setPlanDate] = useState(today);
  const [startTime, setStartTime] = useState("");
  const [endTime, setEndTime] = useState("");
  const [location, setLocation] = useState("");
  const [groupId, setGroupId] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setSaving(false);
    setError(null);
    if (mode === "edit" && initialPlan) {
      const plan = initialPlan;
      setKind(plan.frequency === "once" ? "once" : "recurring");
      setTitle(plan.title);
      setDescription(plan.description ?? "");
      setFrequency(plan.frequency === "weekly" ? "weekly" : "daily");
      setWeeklyTarget(plan.frequency === "weekly" ? plan.weekly_target : 3);
      setColor(plan.color);
      setStartDate(plan.start_date);
      setPlanDate(plan.plan_date ?? plan.start_date);
      setStartTime(plan.start_time ?? "");
      setEndTime(plan.end_time ?? "");
      setLocation(plan.location ?? "");
      setGroupId(plan.group_id ?? "");
      return;
    }
    if (template) {
      setKind(template.kind);
      setTitle(template.title);
      setDescription(template.description);
      setFrequency(template.frequency ?? "daily");
      setWeeklyTarget(template.weekly_target ?? 7);
      setColor(template.color);
      setStartDate(addDaysIso(today, template.start_offset_days ?? 0));
      setPlanDate(addDaysIso(today, template.plan_offset_days ?? 0));
      setStartTime(template.start_time ?? "");
      setEndTime(template.end_time ?? "");
      setLocation(template.location ?? "");
      setGroupId(defaultGroupId ?? "");
      return;
    }
    setKind("recurring");
    setTitle("");
    setDescription("");
    setFrequency("daily");
    setWeeklyTarget(3);
    setColor(PLAN_COLORS[0]);
    setStartDate(today);
    setPlanDate(today);
    setStartTime("");
    setEndTime("");
    setLocation("");
    setGroupId(defaultGroupId ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, mode, initialPlan?.id, template?.key, defaultGroupId, today]);

  const switchKind = (next: FormKind): void => {
    if (next === kind) return;
    setKind(next);
    setError(null);
    if (next === "once") {
      setPlanDate((prev) => prev || today);
      setStartTime("");
      setEndTime("");
      setLocation("");
    } else {
      setPlanDate(today);
      setStartTime("");
      setEndTime("");
      setLocation("");
    }
  };

  const switchFrequency = (next: PlanFrequency): void => {
    setFrequency(next);
    if (next === "daily") setWeeklyTarget(7);
    else if (weeklyTarget === 7) setWeeklyTarget(3);
  };

  const submit = async (): Promise<void> => {
    if (saving) return;
    const trimmedTitle = title.trim();
    if (!trimmedTitle) {
      setError("请填写计划标题。");
      return;
    }
    if (trimmedTitle.length > PLAN_TITLE_MAX) {
      setError(`标题最多 ${PLAN_TITLE_MAX} 个字符。`);
      return;
    }
    if (description.trim().length > PLAN_DESC_MAX) {
      setError(`描述最多 ${PLAN_DESC_MAX} 个字符。`);
      return;
    }
    if (kind === "once") {
      if (!isValidDate(planDate)) {
        setError("请选择合法的行程日期。");
        return;
      }
      if (startTime && !isValidTime(startTime)) {
        setError("开始时间格式应为 HH:MM。");
        return;
      }
      if (endTime && !isValidTime(endTime)) {
        setError("结束时间格式应为 HH:MM。");
        return;
      }
      if (startTime && endTime && endTime <= startTime) {
        setError("结束时间需要晚于开始时间。");
        return;
      }
      if (location.trim().length > 60) {
        setError("地点最多 60 个字符。");
        return;
      }
    } else {
      if (!isValidDate(startDate)) {
        setError("请选择合法的开始日期。");
        return;
      }
      if (frequency === "weekly" && (weeklyTarget < 1 || weeklyTarget > 7)) {
        setError("每周目标次数需要在 1–7 之间。");
        return;
      }
    }

    setSaving(true);
    setError(null);
    const groupValue = groupId || null;
    const shared = {
      title: trimmedTitle,
      description: description.trim() || null,
      color,
      group_id: groupValue,
    };
    try {
      if (kind === "once") {
        const input = {
          ...shared,
          frequency: "once" as const,
          weekly_target: 0,
          plan_date: planDate,
          start_time: startTime || null,
          end_time: endTime || null,
          location: location.trim() || null,
        };
        if (mode === "edit" && initialPlan) {
          const result = await updatePlan(initialPlan.id, input);
          toast("success", "行程已更新", `《${result.plan.title}》的修改已保存。`);
          onSaved(result.plan, "edit");
        } else {
          const result = await createPlan(input);
          toast("success", "行程已创建", `《${result.plan.title}》已加入行程。`);
          onSaved(result.plan, "create");
        }
      } else {
        const input = {
          ...shared,
          frequency,
          weekly_target: frequency === "daily" ? 7 : weeklyTarget,
          start_date: startDate,
        };
        if (mode === "edit" && initialPlan) {
          const result = await updatePlan(initialPlan.id, input);
          toast("success", "计划已更新", `《${result.plan.title}》的修改已保存。`);
          onSaved(result.plan, "edit");
        } else {
          const result = await createPlan(input);
          toast("success", "计划已创建", `《${result.plan.title}》已加入今日计划。`);
          onSaved(result.plan, "create");
        }
      }
      onClose();
    } catch (err) {
      setError(toUserMessage(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={mode === "edit" ? "编辑计划" : "新建计划"}
      description={
        mode === "edit"
          ? "调整计划内容、类型或所属分组。"
          : "循环计划用于长期习惯，单日行程用于特定日期的一次安排。"
      }
      icon={kind === "once" ? <CalendarClock size={17} /> : <CalendarCheck size={17} />}
      width={620}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={saving}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void submit()}
            disabled={saving}
          >
            {saving ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}
            {saving ? "保存中…" : mode === "edit" ? "保存修改" : kind === "once" ? "创建行程" : "创建计划"}
          </button>
        </>
      }
    >
      <form
        className="plan-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <div className="field">
          <span className="field-label">
            <span>计划类型</span>
          </span>
          <div className="plan-kind-switch" role="group" aria-label="计划类型">
            <button
              type="button"
              className={`plan-kind-option${kind === "recurring" ? " active" : ""}`}
              onClick={() => switchKind("recurring")}
              aria-pressed={kind === "recurring"}
            >
              <Repeat size={14} />
              循环计划
            </button>
            <button
              type="button"
              className={`plan-kind-option${kind === "once" ? " active" : ""}`}
              onClick={() => switchKind("once")}
              aria-pressed={kind === "once"}
            >
              <CalendarClock size={14} />
              单日行程
            </button>
          </div>
        </div>

        <label className="field">
          <span className="field-label">
            <span>标题</span>
            <span className="field-hint">
              {title.length} / {PLAN_TITLE_MAX}
            </span>
          </span>
          <input
            className="input"
            value={title}
            maxLength={PLAN_TITLE_MAX + 10}
            placeholder={kind === "once" ? "例如：19:00 瑜伽课" : "例如：每日阅读 30 分钟"}
            onChange={(event) => setTitle(event.target.value)}
            autoFocus
          />
        </label>

        <label className="field">
          <span className="field-label">
            <span>描述</span>
            <span className="field-hint">
              {description.length} / {PLAN_DESC_MAX}（可选）
            </span>
          </span>
          <textarea
            className="textarea"
            rows={2}
            maxLength={PLAN_DESC_MAX + 20}
            value={description}
            placeholder="补充说明这个计划或行程的内容"
            onChange={(event) => setDescription(event.target.value)}
          />
        </label>

        {kind === "recurring" ? (
          <>
            <div className="field">
              <span className="field-label">
                <span>频率</span>
              </span>
              <div className="plan-frequency-switch" role="group" aria-label="计划频率">
                <button
                  type="button"
                  className={`plan-frequency-option${frequency === "daily" ? " active" : ""}`}
                  onClick={() => switchFrequency("daily")}
                  aria-pressed={frequency === "daily"}
                >
                  每天
                </button>
                <button
                  type="button"
                  className={`plan-frequency-option${frequency === "weekly" ? " active" : ""}`}
                  onClick={() => switchFrequency("weekly")}
                  aria-pressed={frequency === "weekly"}
                >
                  每周
                </button>
              </div>
              {frequency === "weekly" ? (
                <div className="plan-target-row">
                  <span className="field-hint">每周目标次数</span>
                  <div className="plan-stepper">
                    <button
                      type="button"
                      className="btn btn-icon"
                      onClick={() => setWeeklyTarget((value) => Math.max(1, value - 1))}
                      disabled={weeklyTarget <= 1}
                      aria-label="减少每周目标次数"
                    >
                      <Minus size={13} />
                    </button>
                    <span className="plan-stepper-value mono">{weeklyTarget}</span>
                    <button
                      type="button"
                      className="btn btn-icon"
                      onClick={() => setWeeklyTarget((value) => Math.min(7, value + 1))}
                      disabled={weeklyTarget >= 7}
                      aria-label="增加每周目标次数"
                    >
                      <Plus size={13} />
                    </button>
                    <span className="field-hint">次 / 周</span>
                  </div>
                </div>
              ) : null}
            </div>

            <label className="field">
              <span className="field-label">
                <span>开始日期</span>
                <span className="field-hint">只能从这一天开始打卡</span>
              </span>
              <div className="input-wrap plan-date-wrap">
                <CalendarDays size={15} />
                <input
                  className="input"
                  type="date"
                  value={startDate}
                  onChange={(event) => setStartDate(event.target.value)}
                />
              </div>
            </label>
          </>
        ) : (
          <>
            <div className="plan-form-row">
              <label className="field">
                <span className="field-label">
                  <span>行程日期</span>
                  <span className="field-hint">必填</span>
                </span>
                <div className="input-wrap plan-date-wrap">
                  <CalendarDays size={15} />
                  <input
                    className="input"
                    type="date"
                    value={planDate}
                    onChange={(event) => setPlanDate(event.target.value)}
                  />
                </div>
              </label>
              <label className="field">
                <span className="field-label">
                  <span>地点</span>
                  <span className="field-hint">可选</span>
                </span>
                <div className="input-wrap plan-date-wrap">
                  <MapPin size={15} />
                  <input
                    className="input"
                    value={location}
                    maxLength={70}
                    placeholder="例如：市体检中心"
                    onChange={(event) => setLocation(event.target.value)}
                  />
                </div>
              </label>
            </div>
            <div className="plan-form-row">
              <label className="field">
                <span className="field-label">
                  <span>开始时间</span>
                  <span className="field-hint">可选</span>
                </span>
                <div className="input-wrap plan-date-wrap">
                  <Clock size={15} />
                  <input
                    className="input"
                    type="time"
                    value={startTime}
                    onChange={(event) => setStartTime(event.target.value)}
                  />
                </div>
              </label>
              <label className="field">
                <span className="field-label">
                  <span>结束时间</span>
                  <span className="field-hint">可选</span>
                </span>
                <div className="input-wrap plan-date-wrap">
                  <Clock size={15} />
                  <input
                    className="input"
                    type="time"
                    value={endTime}
                    onChange={(event) => setEndTime(event.target.value)}
                  />
                </div>
              </label>
            </div>
            <div className="field-hint">
              行程日期当天及之后可以完成；未来行程显示「未开始」，过期未完成可补完成。
            </div>
          </>
        )}

        <div className="plan-form-row">
          <label className="field">
            <span className="field-label">
              <span>所属分组</span>
            </span>
            <select
              className="select-input"
              value={groupId}
              onChange={(event) => setGroupId(event.target.value)}
            >
              <option value="">未分组</option>
              {groups.map((group) => (
                <option key={group.id} value={group.id}>
                  {group.name}
                </option>
              ))}
            </select>
          </label>
          <div className="field">
            <span className="field-label">
              <span>颜色</span>
            </span>
            <div className="plan-color-row">
              {PLAN_COLORS.map((item) => (
                <button
                  key={item}
                  type="button"
                  className={`plan-color-swatch${color === item ? " active" : ""}`}
                  style={{ background: item }}
                  onClick={() => setColor(item)}
                  aria-label={`选择颜色 ${item}`}
                  aria-pressed={color === item}
                >
                  {color === item ? <Check size={11} /> : null}
                </button>
              ))}
            </div>
          </div>
        </div>

        {error ? <div className="field-error">{error}</div> : null}
      </form>
    </Modal>
  );
}
