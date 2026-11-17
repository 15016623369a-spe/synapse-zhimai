import { useEffect, useMemo, useState } from "react";
import {
  CalendarClock,
  CalendarDays,
  Check,
  ChevronLeft,
  ChevronRight,
  Clock,
  Flame,
  LoaderCircle,
  MapPin,
  Pencil,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import { Drawer } from "../../components/Drawer";
import { ConfirmDialog } from "../../components/Modal";
import { Skeleton, SkeletonRows } from "../../components/Skeleton";
import {
  deletePlan,
  getPlan,
  listCheckins,
  removeCheckin,
  toUserMessage,
} from "../../lib/api";
import {
  WEEKDAY_LABELS,
  addDaysIso,
  compareIso,
  formatFrequency,
  formatFullDate,
  formatMonthDay,
  formatScheduleDate,
  formatTimeRange,
  getMonthCells,
  isOncePlan,
  monthKey,
  monthTitle,
  parseIsoDate,
  scheduleStatus,
  scheduleStatusLabel,
  streakUnit,
  weekdayIndex,
} from "../../lib/plans";
import { useApp } from "../../state/AppContext";
import type { Plan, PlanCheckin } from "../../types";
import { CheckinDialog } from "./CheckinDialog";

interface PlanDrawerProps {
  open: boolean;
  planId: string | null;
  today: string;
  onClose: () => void;
  onEdit: (plan: Plan) => void;
  onChanged: (plan: Plan) => void;
  onDeleted: () => void;
}

function monthFromDate(iso: string): { year: number; month: number } {
  const date = parseIsoDate(iso);
  const now = date ?? new Date();
  return { year: now.getFullYear(), month: now.getMonth() };
}

export function PlanDrawer({
  open,
  planId,
  today,
  onClose,
  onEdit,
  onChanged,
  onDeleted,
}: PlanDrawerProps) {
  const { toast } = useApp();
  const [plan, setPlan] = useState<Plan | null>(null);
  const [checkins, setCheckins] = useState<PlanCheckin[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadSeed, setReloadSeed] = useState(0);
  const [viewYear, setViewYear] = useState(() => monthFromDate(today).year);
  const [viewMonth, setViewMonth] = useState(() => monthFromDate(today).month);
  const [checkinDialog, setCheckinDialog] = useState<{
    date: string;
    isToday: boolean;
  } | null>(null);
  const [undoDate, setUndoDate] = useState<string | null>(null);
  const [undoLoading, setUndoLoading] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (!open || !planId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setPlan(null);
    setCheckins([]);
    const current = monthFromDate(today);
    setViewYear(current.year);
    setViewMonth(current.month);
    getPlan(planId)
      .then((response) => {
        if (cancelled) return;
        setPlan(response.plan);
        const once = response.plan.frequency === "once";
        const start = once
          ? (response.plan.plan_date ?? today)
          : compareIso(response.plan.start_date, addDaysIso(today, -365)) > 0
            ? response.plan.start_date
            : addDaysIso(today, -365);
        const end = once ? (response.plan.plan_date ?? today) : today;
        return listCheckins(planId, { start, end });
      })
      .then((response) => {
        if (!cancelled && response) setCheckins(response.items);
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
  }, [open, planId, today, reloadSeed]);

  const once = isOncePlan(plan);
  const checkinMap = useMemo(() => {
    const map = new Map<string, PlanCheckin>();
    for (const item of checkins) map.set(item.date, item);
    return map;
  }, [checkins]);
  const onceCheckin = checkins[0] ?? null;
  const onceStatus = plan && once ? scheduleStatus(plan, today) : null;
  const onceBadge = plan && once ? scheduleStatusLabel(plan, today) : null;

  const minDate = useMemo(() => {
    if (!plan) return today;
    const limit = addDaysIso(today, -365);
    return compareIso(plan.start_date, limit) > 0 ? plan.start_date : limit;
  }, [plan, today]);

  const cells = useMemo(() => getMonthCells(viewYear, viewMonth), [viewYear, viewMonth]);
  const currentMonthKey = monthKey(viewYear, viewMonth);
  const todayParts = parseIsoDate(today);
  const todayMonthKey = monthKey(
    todayParts?.getFullYear() ?? viewYear,
    todayParts?.getMonth() ?? viewMonth,
  );
  const monthCount = checkins.filter((item) => item.date.startsWith(currentMonthKey)).length;

  const prevDisabled =
    monthKey(viewYear, viewMonth) <=
    monthKey(
      parseIsoDate(minDate)?.getFullYear() ?? viewYear,
      parseIsoDate(minDate)?.getMonth() ?? viewMonth,
    );
  const nextDisabled = monthKey(viewYear, viewMonth) >= todayMonthKey;

  const goMonth = (delta: number): void => {
    const date = new Date(viewYear, viewMonth + delta, 1);
    setViewYear(date.getFullYear());
    setViewMonth(date.getMonth());
  };

  const refreshCheckins = (): void => {
    if (!plan) return;
    const start = once ? (plan.plan_date ?? today) : minDate;
    const end = once ? (plan.plan_date ?? today) : today;
    void listCheckins(plan.id, { start, end })
      .then((response) => setCheckins(response.items))
      .catch(() => undefined);
  };

  const handleChecked = (updated: Plan): void => {
    setPlan(updated);
    setCheckinDialog(null);
    onChanged(updated);
    refreshCheckins();
  };

  const handleDayClick = (date: string): void => {
    if (!plan || once) return;
    if (compareIso(date, today) > 0) return;
    if (compareIso(date, plan.start_date) < 0) return;
    if (checkinMap.has(date)) {
      setUndoDate(date);
    } else {
      setCheckinDialog({ date, isToday: date === today });
    }
  };

  const confirmUndo = async (): Promise<void> => {
    if (!plan || !undoDate || undoLoading) return;
    setUndoLoading(true);
    try {
      const result = await removeCheckin(plan.id, undoDate);
      setPlan(result.plan);
      setCheckins((prev) => prev.filter((item) => item.date !== undoDate));
      onChanged(result.plan);
      toast("success", "已撤销打卡", `${formatFullDate(undoDate)} 的记录已移除。`);
      setUndoDate(null);
    } catch (err) {
      toast("error", "撤销失败", toUserMessage(err));
    } finally {
      setUndoLoading(false);
    }
  };

  const confirmDelete = async (): Promise<void> => {
    if (!plan || deleting) return;
    setDeleting(true);
    try {
      const result = await deletePlan(plan.id);
      toast(
        "success",
        once ? "行程已删除" : "计划已删除",
        `《${plan.title}》及其 ${result.deleted_checkins} 条记录已删除。`,
      );
      setDeleteOpen(false);
      onDeleted();
      onClose();
    } catch (err) {
      toast("error", "删除失败", toUserMessage(err));
    } finally {
      setDeleting(false);
    }
  };

  const noteList = useMemo(
    () => [...checkins].sort((first, second) => compareIso(second.date, first.date)).slice(0, 8),
    [checkins],
  );

  const completeSchedule = (): void => {
    if (!plan || !plan.plan_date) return;
    setCheckinDialog({ date: plan.plan_date, isToday: plan.plan_date === today });
  };

  const undoOnce = (): void => {
    if (!plan?.plan_date) return;
    setUndoDate(plan.plan_date);
  };

  return (
    <>
      <Drawer open={open} onClose={onClose} label={once ? "行程详情" : "计划详情"} width={430}>
        {loading ? (
          <div className="plan-drawer-loading">
            <Skeleton width="60%" height={20} radius={6} />
            <Skeleton width="100%" height={72} radius={12} />
            <SkeletonRows rows={3} />
          </div>
        ) : error && !plan ? (
          <div className="plan-drawer-loading">
            <div className="page-error">
              <X size={15} />
              <div style={{ flex: 1 }}>
                <div>{error}</div>
                <button
                  type="button"
                  className="btn btn-sm"
                  style={{ marginTop: 10 }}
                  onClick={() => setReloadSeed((seed) => seed + 1)}
                >
                  <RefreshCw size={12} /> 重试
                </button>
              </div>
            </div>
            <button type="button" className="btn" onClick={onClose}>
              关闭
            </button>
          </div>
        ) : plan ? (
          <>
            <div className="drawer-head plan-drawer-head">
              <span className="plan-color-dot" style={{ background: plan.color }} aria-hidden="true" />
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="drawer-title">{plan.title}</div>
                <div className="drawer-sub">
                  {once
                    ? `单日行程 · ${formatScheduleDate(plan.plan_date, today)}${plan.start_time ? ` ${formatTimeRange(plan.start_time, plan.end_time)}` : ""}`
                    : `${formatFrequency(plan)} · 开始于 ${formatMonthDay(plan.start_date)}`}
                </div>
              </div>
              <div className="plan-drawer-actions">
                <button
                  type="button"
                  className="btn btn-icon"
                  onClick={() => onEdit(plan)}
                  aria-label={once ? "编辑行程" : "编辑计划"}
                  data-tip="编辑"
                >
                  <Pencil size={15} />
                </button>
                <button
                  type="button"
                  className="btn btn-icon plan-delete-btn"
                  onClick={() => setDeleteOpen(true)}
                  aria-label={once ? "删除行程" : "删除计划"}
                  data-tip="删除"
                >
                  <Trash2 size={15} />
                </button>
                <button type="button" className="btn btn-icon" onClick={onClose} aria-label="关闭详情">
                  <X size={16} />
                </button>
              </div>
            </div>

            <div className="drawer-body plan-drawer-body">
              {plan.description ? <div className="plan-drawer-desc">{plan.description}</div> : null}

              {once ? (
                <>
                  <div className="schedule-info-card">
                    <div className="schedule-info-row">
                      <CalendarDays size={14} />
                      <span className="schedule-info-label">日期</span>
                      <span className="schedule-info-value">{formatFullDate(plan.plan_date ?? "")}</span>
                    </div>
                    <div className="schedule-info-row">
                      <Clock size={14} />
                      <span className="schedule-info-label">时间</span>
                      <span className="schedule-info-value">
                        {formatTimeRange(plan.start_time, plan.end_time)}
                      </span>
                    </div>
                    {plan.location ? (
                      <div className="schedule-info-row">
                        <MapPin size={14} />
                        <span className="schedule-info-label">地点</span>
                        <span className="schedule-info-value">{plan.location}</span>
                      </div>
                    ) : null}
                    <div className="schedule-info-row">
                      <span className="plan-color-dot" style={{ background: plan.group_color }} />
                      <span className="schedule-info-label">分组</span>
                      <span className="schedule-info-value">{plan.group_name}</span>
                    </div>
                  </div>

                  <div className={`schedule-status-card tone-${onceBadge?.tone ?? "today"}`}>
                    <div className="schedule-status-main">
                      <span className="schedule-status-icon">
                        <CalendarClock size={16} />
                      </span>
                      <div>
                        <div className="schedule-status-title">
                          {onceStatus === "completed"
                            ? "行程已完成"
                            : onceStatus === "upcoming"
                              ? "行程还没有开始"
                              : onceStatus === "overdue"
                                ? "行程已过期，可以补完成"
                                : "今天到期，记得完成"}
                        </div>
                        <div className="schedule-status-desc">
                          {onceCheckin?.note
                            ? `完成备注：${onceCheckin.note}`
                            : onceStatus === "upcoming"
                              ? "到达行程日期后才可以完成。"
                              : "完成后会计入累计打卡与热力图。"}
                        </div>
                      </div>
                    </div>
                    {onceStatus === "completed" ? (
                      <button type="button" className="schedule-completed-btn" onClick={undoOnce}>
                        <Check size={14} /> 已完成 · 撤销
                      </button>
                    ) : onceStatus === "upcoming" ? (
                      <span className="schedule-not-started">未开始</span>
                    ) : (
                      <button type="button" className="schedule-complete-btn" onClick={completeSchedule}>
                        {onceStatus === "overdue" ? "补完成" : "完成行程"}
                      </button>
                    )}
                  </div>
                </>
              ) : (
                <>
                  <div className="plan-drawer-stats">
                    <div className="plan-drawer-stat">
                      <span className="plan-drawer-stat-value mono">{plan.current_streak}</span>
                      <span className="plan-drawer-stat-label">
                        <Flame size={11} /> 当前连续{streakUnit(plan.frequency)}
                      </span>
                    </div>
                    <div className="plan-drawer-stat">
                      <span className="plan-drawer-stat-value mono">{plan.longest_streak}</span>
                      <span className="plan-drawer-stat-label">最长连续{streakUnit(plan.frequency)}</span>
                    </div>
                    <div className="plan-drawer-stat">
                      <span className="plan-drawer-stat-value mono">{plan.total_checkins}</span>
                      <span className="plan-drawer-stat-label">累计打卡</span>
                    </div>
                    <div className="plan-drawer-stat">
                      <span className="plan-drawer-stat-value mono">
                        {plan.week_checked}/{plan.week_target}
                      </span>
                      <span className="plan-drawer-stat-label">本周进度</span>
                    </div>
                  </div>

                  <section className="plan-calendar-section">
                    <div className="plan-calendar-head">
                      <button
                        type="button"
                        className="btn btn-icon"
                        onClick={() => goMonth(-1)}
                        disabled={prevDisabled}
                        aria-label="上个月"
                      >
                        <ChevronLeft size={15} />
                      </button>
                      <span className="plan-calendar-title mono">{monthTitle(viewYear, viewMonth)}</span>
                      <button
                        type="button"
                        className="btn btn-icon"
                        onClick={() => goMonth(1)}
                        disabled={nextDisabled}
                        aria-label="下个月"
                      >
                        <ChevronRight size={15} />
                      </button>
                    </div>
                    <div className="plan-calendar-weekdays" aria-hidden="true">
                      {WEEKDAY_LABELS.map((label) => (
                        <span key={label}>{label}</span>
                      ))}
                    </div>
                    <div className="plan-calendar-grid">
                      {cells.map((date, index) => {
                        if (!date) {
                          return <span className="plan-calendar-day empty" key={`empty-${index}`} />;
                        }
                        const checked = checkinMap.has(date);
                        const isToday = date === today;
                        const future = compareIso(date, today) > 0;
                        const beforeStart = plan ? compareIso(date, plan.start_date) < 0 : false;
                        const disabled = future || beforeStart;
                        const dayNumber = Number(date.slice(8, 10));
                        return (
                          <button
                            key={date}
                            type="button"
                            className={`plan-calendar-day${checked ? " checked" : ""}${
                              isToday ? " today" : ""
                            }${disabled ? " disabled" : ""}`}
                            style={checked && plan ? { background: plan.color } : undefined}
                            disabled={disabled}
                            onClick={() => handleDayClick(date)}
                            aria-label={`${formatFullDate(date)}${checked ? "，已打卡" : ""}${
                              disabled ? "，不可打卡" : ""
                            }`}
                          >
                            {dayNumber}
                          </button>
                        );
                      })}
                    </div>
                    <div className="plan-calendar-foot">
                      本月打卡 <b className="mono">{monthCount}</b> 次 · 点击日期可补记或撤销
                    </div>
                  </section>
                </>
              )}

              <section>
                <div className="drawer-section-head">
                  <span>{once ? "最近完成与备注" : "最近打卡与备注"}</span>
                  <span className="mono dim">{checkins.length} 条</span>
                </div>
                {noteList.length === 0 ? (
                  <div className="drawer-empty">
                    {once ? "还没有完成记录，到日期后点击完成。" : "还没有打卡记录，从今天开始吧。"}
                  </div>
                ) : (
                  <div className="plan-note-list">
                    {noteList.map((item) => (
                      <div key={item.id || item.date} className="plan-note-item">
                        <div className="plan-note-head">
                          <span className="plan-note-date mono">{formatMonthDay(item.date)}</span>
                          <span className="plan-note-weekday">
                            周{WEEKDAY_LABELS[weekdayIndex(item.date)]}
                          </span>
                        </div>
                        <p className={`plan-note-text${item.note ? "" : " is-empty"}`}>
                          {item.note || "无备注"}
                        </p>
                      </div>
                    ))}
                  </div>
                )}
              </section>
            </div>
          </>
        ) : (
          <div className="plan-drawer-loading">
            <div className="drawer-empty">计划不存在或已被删除。</div>
            <button type="button" className="btn" onClick={onClose}>
              关闭
            </button>
          </div>
        )}
      </Drawer>

      {plan ? (
        <CheckinDialog
          open={Boolean(checkinDialog)}
          plan={checkinDialog ? plan : null}
          date={checkinDialog?.date ?? plan.plan_date ?? plan.start_date}
          isToday={Boolean(checkinDialog?.isToday)}
          variant={once ? "schedule" : "recurring"}
          onClose={() => setCheckinDialog(null)}
          onChecked={handleChecked}
        />
      ) : null}

      <ConfirmDialog
        open={Boolean(undoDate)}
        title={once ? "撤销这次完成？" : "撤销这次打卡？"}
        description={
          <span>
            将移除 {undoDate ? formatFullDate(undoDate) : ""} 的{once ? "完成" : "打卡"}记录，
            {once ? "累计与" : ""}统计会重新计算。
          </span>
        }
        confirmLabel={once ? "撤销完成" : "撤销打卡"}
        loading={undoLoading}
        icon={<CalendarDays size={16} />}
        onConfirm={() => void confirmUndo()}
        onClose={() => setUndoDate(null)}
      />

      <ConfirmDialog
        open={deleteOpen}
        title={once ? "删除这个行程？" : "删除这个计划？"}
        description={
          <span>
            确定删除 <strong>《{plan?.title}》</strong> 吗？该{once ? "行程" : "计划"}的{" "}
            <strong>{plan?.total_checkins ?? 0} 条记录</strong>会一并删除，此操作不可撤销。
          </span>
        }
        confirmLabel={once ? "删除行程" : "删除计划"}
        loading={deleting}
        icon={<Trash2 size={16} />}
        onConfirm={() => void confirmDelete()}
        onClose={() => setDeleteOpen(false)}
      />
    </>
  );
}
