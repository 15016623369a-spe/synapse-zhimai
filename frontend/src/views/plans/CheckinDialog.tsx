import { useEffect, useState } from "react";
import { CalendarCheck, Check, LoaderCircle } from "lucide-react";
import { Modal } from "../../components/Modal";
import { addCheckin, toUserMessage } from "../../lib/api";
import { PLAN_NOTE_MAX, formatFullDate, formatTimeRange } from "../../lib/plans";
import { useApp } from "../../state/AppContext";
import type { Plan } from "../../types";

interface CheckinDialogProps {
  open: boolean;
  plan: Plan | null;
  date: string;
  isToday: boolean;
  variant?: "recurring" | "schedule";
  onClose: () => void;
  onChecked: (plan: Plan) => void;
}

export function CheckinDialog({
  open,
  plan,
  date,
  isToday,
  variant = "recurring",
  onClose,
  onChecked,
}: CheckinDialogProps) {
  const { toast } = useApp();
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setNote("");
    setError(null);
    setSubmitting(false);
  }, [open, plan?.id, date]);

  if (!plan) return null;

  const submit = async (): Promise<void> => {
    if (submitting) return;
    if (note.length > PLAN_NOTE_MAX) {
      setError(`备注最多 ${PLAN_NOTE_MAX} 个字符。`);
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const result = await addCheckin(plan.id, {
        date,
        note: note.trim() ? note.trim() : null,
      });
      if (variant === "schedule") {
        toast(
          "success",
          isToday ? "行程已完成" : "行程已补完成",
          `《${plan.title}》已完成，记录已计入累计与热力图。`,
        );
      } else {
        const streak = result.plan.current_streak;
        const unit = result.plan.frequency === "daily" ? "天" : "周";
        toast(
          "success",
          isToday ? "打卡成功" : "补记打卡成功",
          streak > 0
            ? `《${plan.title}》连续 ${streak} ${unit}，继续保持。`
            : `《${plan.title}》已记录，继续加油。`,
        );
      }
      onChecked(result.plan);
      onClose();
    } catch (err) {
      setError(toUserMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={
        variant === "schedule"
          ? isToday
            ? "完成行程"
            : "补完成行程"
          : isToday
            ? "今日打卡"
            : "补记打卡"
      }
      description={
        variant === "schedule"
          ? `${formatFullDate(date)}${plan.start_time ? ` · ${formatTimeRange(plan.start_time, plan.end_time)}` : ""}${plan.location ? ` · ${plan.location}` : ""}`
          : `${plan.title} · ${formatFullDate(date)}`
      }
      icon={<CalendarCheck size={17} />}
      width={480}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void submit()}
            disabled={submitting}
          >
            {submitting ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}
            {submitting
              ? "记录中…"
              : variant === "schedule"
                ? isToday
                  ? "确认完成"
                  : "确认补完成"
                : isToday
                  ? "确认打卡"
                  : "确认补记"}
          </button>
        </>
      }
    >
      <div className="checkin-dialog">
        <div className="checkin-dialog-plan">
          <span className="plan-color-dot" style={{ background: plan.color }} />
          <span className="checkin-dialog-title">{plan.title}</span>
          <span className="checkin-dialog-freq">
            {variant === "schedule"
              ? "单日行程"
              : plan.frequency === "daily"
                ? "每天"
                : `每周 ${plan.weekly_target} 次`}
          </span>
        </div>
        <label className="field">
          <span className="field-label">
            <span>备注</span>
            <span className="field-hint">
              {note.length} / {PLAN_NOTE_MAX}
            </span>
          </span>
          <textarea
            className="textarea"
            rows={3}
            maxLength={PLAN_NOTE_MAX + 20}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder={
              variant === "schedule"
                ? "可选：记录一下这次行程的完成情况"
                : isToday
                  ? "可选：今天完成得怎么样？"
                  : "可选：补记一下那天的完成情况"
            }
            autoFocus
          />
        </label>
        {variant === "schedule" ? (
          <div className="field-hint">行程完成后会计入累计打卡与热力图，撤销后可重新完成。</div>
        ) : isToday ? (
          <div className="field-hint">每天每个计划只能打卡一次，之后可以在日历中补记过去日期。</div>
        ) : null}
        {error ? <div className="field-error">{error}</div> : null}
      </div>
    </Modal>
  );
}
