import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  CalendarCheck,
  CalendarClock,
  CalendarDays,
  Check,
  ChevronDown,
  ChevronRight,
  Clock,
  Flame,
  FolderCog,
  LoaderCircle,
  MapPin,
  Plus,
  RefreshCw,
  Sparkles,
  Target,
  TrendingUp,
} from "lucide-react";
import { ConfirmDialog } from "../components/Modal";
import { Skeleton, SkeletonRows } from "../components/Skeleton";
import {
  getPlansOverview,
  listPlanGroups,
  listPlans,
  removeCheckin,
  toUserMessage,
} from "../lib/api";
import {
  PLAN_TEMPLATES,
  addDaysIso,
  formatFrequency,
  formatFullDate,
  formatMonthDay,
  formatScheduleDate,
  formatTimeRange,
  isOncePlan,
  scheduleStatus,
  streakUnit,
  toIsoDate,
  type PlanTemplate,
} from "../lib/plans";
import { useApp } from "../state/AppContext";
import type {
  Plan,
  PlanGroup,
  PlanGroupsResponse,
  PlansListResponse,
  PlansOverview,
} from "../types";
import { CheckinDialog } from "./plans/CheckinDialog";
import { Heatmap } from "./plans/Heatmap";
import { PlanCard } from "./plans/PlanCard";
import { PlanDrawer } from "./plans/PlanDrawer";
import { PlanFormDialog } from "./plans/PlanFormDialog";
import { PlanGroupManager } from "./plans/PlanGroupManager";
import { ScheduleCard } from "./plans/ScheduleCard";

interface FormState {
  open: boolean;
  mode: "create" | "edit";
  plan: Plan | null;
  template: PlanTemplate | null;
  defaultGroupId: string | null;
}

interface CheckinTarget {
  plan: Plan;
  date: string;
  isToday: boolean;
  variant: "recurring" | "schedule";
}

interface UndoTarget {
  plan: Plan;
  date: string;
  variant: "recurring" | "schedule";
}

const EMPTY_FORM: FormState = {
  open: false,
  mode: "create",
  plan: null,
  template: null,
  defaultGroupId: null,
};

const COLLAPSE_KEY = "synapse.plans.collapsedGroups";
const UNGROUPED_KEY = "__ungrouped__";

function readCollapsed(): Record<string, boolean> {
  try {
    const raw = window.localStorage.getItem(COLLAPSE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object") return {};
    return parsed as Record<string, boolean>;
  } catch {
    return {};
  }
}

function scheduleSort(first: Plan, second: Plan): number {
  const firstDate = first.plan_date ?? "";
  const secondDate = second.plan_date ?? "";
  if (firstDate !== secondDate) return firstDate < secondDate ? -1 : 1;
  const firstTime = first.start_time ?? "99:99";
  const secondTime = second.start_time ?? "99:99";
  return firstTime < secondTime ? -1 : firstTime > secondTime ? 1 : 0;
}

export function PlansView() {
  const { toast } = useApp();
  const [plansData, setPlansData] = useState<PlansListResponse | null>(null);
  const [overview, setOverview] = useState<PlansOverview | null>(null);
  const [groupsData, setGroupsData] = useState<PlanGroupsResponse | null>(null);
  const [groupsLoading, setGroupsLoading] = useState(true);
  const [groupsError, setGroupsError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshSeed, setRefreshSeed] = useState(0);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [checkinTarget, setCheckinTarget] = useState<CheckinTarget | null>(null);
  const [undoTarget, setUndoTarget] = useState<UndoTarget | null>(null);
  const [undoLoading, setUndoLoading] = useState(false);
  const [drawerPlanId, setDrawerPlanId] = useState<string | null>(null);
  const [groupManagerOpen, setGroupManagerOpen] = useState(false);
  const [celebratingId, setCelebratingId] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(readCollapsed);
  const celebrateTimer = useRef<number | null>(null);

  const today = plansData?.today ?? overview?.today ?? "";
  const todayValue = today || toIsoDate(new Date());
  const plans = plansData?.items ?? [];
  const stats = overview?.stats ?? null;

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    setGroupsLoading(true);
    try {
      const [plansResponse, overviewResponse] = await Promise.all([
        listPlans(false),
        getPlansOverview(120),
      ]);
      setPlansData(plansResponse);
      setOverview(overviewResponse);
      setError(null);
      try {
        const groupsResponse = await listPlanGroups();
        setGroupsData(groupsResponse);
        setGroupsError(null);
      } catch (err) {
        setGroupsData(null);
        setGroupsError(toUserMessage(err));
      }
    } catch (err) {
      setError(toUserMessage(err));
    } finally {
      setLoading(false);
      setGroupsLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, refreshSeed]);

  useEffect(() => {
    return () => {
      if (celebrateTimer.current != null) window.clearTimeout(celebrateTimer.current);
    };
  }, []);

  const refresh = useCallback((): void => {
    setRefreshSeed((seed) => seed + 1);
  }, []);

  const refreshOverview = useCallback(async (): Promise<void> => {
    try {
      const response = await getPlansOverview(120);
      setOverview(response);
    } catch {
      /* 统计刷新失败不打断主流程 */
    }
  }, []);

  const applyPlan = useCallback((updated: Plan): void => {
    setPlansData((prev) =>
      prev
        ? {
            ...prev,
            items: prev.items.map((item) => (item.id === updated.id ? updated : item)),
          }
        : prev,
    );
  }, []);

  const toggleGroup = (key: string): void => {
    setCollapsed((prev) => {
      const next = { ...prev, [key]: !prev[key] };
      try {
        window.localStorage.setItem(COLLAPSE_KEY, JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  const celebrate = (planId: string): void => {
    setCelebratingId(planId);
    if (celebrateTimer.current != null) window.clearTimeout(celebrateTimer.current);
    celebrateTimer.current = window.setTimeout(() => setCelebratingId(null), 1300);
  };

  const openCreate = (template: PlanTemplate | null = null, defaultGroupId: string | null = null): void => {
    setForm({ open: true, mode: "create", plan: null, template, defaultGroupId });
  };

  const openEdit = (plan: Plan): void => {
    setDrawerPlanId(null);
    setForm({
      open: true,
      mode: "edit",
      plan,
      template: null,
      defaultGroupId: plan.group_id ?? null,
    });
  };

  const handleSaved = (saved: Plan): void => {
    applyPlan(saved);
    refresh();
  };

  const handleChecked = (updated: Plan): void => {
    applyPlan(updated);
    setCheckinTarget(null);
    celebrate(updated.id);
    void refreshOverview();
  };

  const confirmUndo = async (): Promise<void> => {
    if (!undoTarget || undoLoading) return;
    const { plan, date, variant } = undoTarget;
    setUndoLoading(true);
    try {
      const result = await removeCheckin(plan.id, date);
      applyPlan(result.plan);
      toast(
        "success",
        variant === "schedule" ? "已撤销完成" : "已撤销今日打卡",
        variant === "schedule"
          ? `《${plan.title}》的完成记录已移除。`
          : `《${plan.title}》的今日记录已移除。`,
      );
      setUndoTarget(null);
      void refreshOverview();
    } catch (err) {
      toast("error", "撤销失败", toUserMessage(err));
    } finally {
      setUndoLoading(false);
    }
  };

  const openRecurringCheckin = (plan: Plan): void => {
    setCheckinTarget({ plan, date: todayValue, isToday: true, variant: "recurring" });
  };

  const openScheduleComplete = (plan: Plan): void => {
    if (!plan.plan_date) return;
    setCheckinTarget({
      plan,
      date: plan.plan_date,
      isToday: plan.plan_date === todayValue,
      variant: "schedule",
    });
  };

  const openRecurringUndo = (plan: Plan): void => {
    setUndoTarget({ plan, date: todayValue, variant: "recurring" });
  };

  const openScheduleUndo = (plan: Plan): void => {
    if (!plan.plan_date) return;
    setUndoTarget({ plan, date: plan.plan_date, variant: "schedule" });
  };

  const handleDrawerChanged = (updated: Plan): void => {
    applyPlan(updated);
    void refreshOverview();
    refresh();
  };

  const handleDrawerDeleted = (): void => {
    setDrawerPlanId(null);
    refresh();
  };

  /* ------------------------------ 派生数据 ------------------------------ */

  const oncePlans = useMemo(() => plans.filter(isOncePlan), [plans]);
  const recurringPlans = useMemo(() => plans.filter((plan) => !isOncePlan(plan)), [plans]);

  const todaySchedules = useMemo(() => {
    return oncePlans
      .filter((plan) => {
        if (!plan.plan_date) return false;
        if (plan.plan_date === todayValue) return true;
        return plan.plan_date < todayValue && !plan.checked;
      })
      .sort(scheduleSort);
  }, [oncePlans, todayValue]);

  const upcomingSchedules = useMemo(() => {
    const horizon = addDaysIso(todayValue, 30);
    return oncePlans
      .filter(
        (plan) =>
          Boolean(plan.plan_date) &&
          plan.plan_date! > todayValue &&
          plan.plan_date! <= horizon,
      )
      .sort(scheduleSort);
  }, [oncePlans, todayValue]);

  const recurringTotal =
    stats?.recurring_today_total ?? recurringPlans.filter((plan) => plan.start_date <= todayValue).length;
  const recurringChecked =
    stats?.recurring_today_checked ?? recurringPlans.filter((plan) => plan.today_checked).length;
  const scheduleTotal = stats?.today_schedule_total ?? todaySchedules.filter((plan) => plan.plan_date === todayValue).length;
  const scheduleChecked =
    stats?.today_schedule_checked ?? todaySchedules.filter((plan) => plan.plan_date === todayValue && plan.checked).length;
  const upcomingTotal = stats?.upcoming_schedule_total ?? upcomingSchedules.length;

  const sortedGroups = useMemo(
    () => [...(groupsData?.items ?? [])].sort((first, second) => first.sort_order - second.sort_order),
    [groupsData],
  );

  const plansByGroup = useMemo(() => {
    const map = new Map<string, Plan[]>();
    for (const group of sortedGroups) map.set(group.id, []);
    for (const plan of recurringPlans) {
      if (plan.group_id && map.has(plan.group_id)) {
        map.get(plan.group_id)!.push(plan);
      }
    }
    return map;
  }, [sortedGroups, recurringPlans]);

  const ungroupedPlans = useMemo(
    () => recurringPlans.filter((plan) => !plan.group_id),
    [recurringPlans],
  );

  const bestUnit = useMemo(() => {
    if (!stats?.best_plan_title) return "天";
    const matched = plans.find((plan) => plan.title === stats.best_plan_title);
    return matched ? streakUnit(matched.frequency) : "天";
  }, [stats, plans]);

  const weekPercent = useMemo(() => {
    if (!stats || stats.week_target_total <= 0) return 0;
    return Math.min(100, Math.round((stats.week_checked_total / stats.week_target_total) * 100));
  }, [stats]);

  const totalCheckins = stats?.total_checkins ?? 0;
  const isEmpty = !loading && !error && plans.length === 0;

  const statsCards = [
    {
      key: "today",
      icon: CalendarCheck,
      tone: "cyan",
      label: "今日打卡",
      value: `${recurringChecked}/${recurringTotal}`,
      suffix: "个循环计划",
      detail:
        scheduleTotal > 0
          ? `行程 ${scheduleChecked}/${scheduleTotal} 已完成`
          : recurringTotal === 0
            ? "还没有进行中的计划"
            : recurringChecked >= recurringTotal
              ? "循环计划全部完成"
              : "循环计划还差一点",
    },
    {
      key: "streak",
      icon: Flame,
      tone: "amber",
      label: "当前最佳连续",
      value: stats && stats.best_current_streak > 0 ? `${stats.best_current_streak} ${bestUnit}` : "—",
      suffix: "",
      detail: stats?.best_plan_title ?? "还没有连续记录",
    },
    {
      key: "week",
      icon: TrendingUp,
      tone: "violet",
      label: "本周完成",
      value: `${stats?.week_checked_total ?? 0}/${stats?.week_target_total ?? 0}`,
      suffix: "次",
      detail: `${weekPercent}% 的周目标`,
      progress: weekPercent,
    },
    {
      key: "total",
      icon: Target,
      tone: "mint",
      label: "累计打卡",
      value: String(totalCheckins),
      suffix: "次",
      detail: "循环与单日行程都计入",
    },
  ] as const;

  const favoriteTemplateMeta = (template: PlanTemplate): string => {
    if (template.kind === "once") {
      const date = addDaysIso(todayValue, template.plan_offset_days ?? 0);
      return `单日行程 · ${formatScheduleDate(date, todayValue)}${template.start_time ? ` ${template.start_time}` : ""}`;
    }
    return template.frequency === "weekly" ? `循环计划 · 每周 ${template.weekly_target} 次` : "循环计划 · 每天";
  };

  const renderGroupSection = (
    key: string,
    name: string,
    color: string,
    count: number,
    groupPlans: Plan[],
    group: PlanGroup | null,
  ): React.ReactNode => {
    const isCollapsed = Boolean(collapsed[key]);
    const groupStyle = { "--group-color": color } as CSSProperties;
    return (
      <div className={`plan-group${isCollapsed ? " collapsed" : ""}`} key={key} style={groupStyle}>
        <div className="plan-group-head">
          <button
            type="button"
            className="plan-group-toggle"
            onClick={() => toggleGroup(key)}
            aria-expanded={!isCollapsed}
            aria-label={isCollapsed ? `展开分组 ${name}` : `折叠分组 ${name}`}
          >
            {isCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
          </button>
          <span className="plan-group-dot" style={{ background: color }} aria-hidden="true" />
          <span className="plan-group-name">{name}</span>
          <span className="plan-group-count mono">{count} 个计划</span>
          <span className="plan-group-spacer" />
          <button
            type="button"
            className="plan-group-add"
            onClick={() => openCreate(null, group?.id ?? null)}
            title={group ? `在「${name}」中新建计划` : "新建未分组计划"}
          >
            <Plus size={12} />
            添加计划
          </button>
        </div>
        {isCollapsed ? null : (
          <div className="plan-group-body">
            {groupPlans.length === 0 ? (
              <div className="plan-group-empty">
                这个分组还没有循环计划，点击右上角「添加计划」开始。
              </div>
            ) : (
              <div className="plans-list">
                {groupPlans.map((plan) => (
                  <PlanCard
                    key={plan.id}
                    plan={plan}
                    today={todayValue}
                    celebrating={celebratingId === plan.id}
                    onOpen={(item) => setDrawerPlanId(item.id)}
                    onCheckin={openRecurringCheckin}
                    onUndo={openRecurringUndo}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="page-scroll">
      <div className="page plans-page plans2-page">
        <div className="page-header">
          <div>
            <h1 className="page-heading">计划与打卡</h1>
            <p className="page-sub">按分组规划习惯，用打卡记录每一天。</p>
          </div>
          <div className="page-header-actions">
            <button type="button" className="btn" onClick={() => setGroupManagerOpen(true)}>
              <FolderCog size={15} />
              管理分组
            </button>
            <button type="button" className="btn btn-primary" onClick={() => openCreate(null, null)}>
              <Plus size={15} />
              新建计划
            </button>
          </div>
        </div>

        {error && !plansData ? (
          <div className="page-error">
            <RefreshCw size={17} />
            <div style={{ flex: 1 }}>
              <div style={{ color: "var(--text)", fontWeight: 600, marginBottom: 3 }}>
                无法加载计划
              </div>
              <div>{error}</div>
              <button type="button" className="btn btn-sm" style={{ marginTop: 12 }} onClick={refresh}>
                <RefreshCw size={12} /> 重试
              </button>
            </div>
          </div>
        ) : (
          <>
            <div className="plans-stats">
              {statsCards.map((card) => {
                const Icon = card.icon;
                return (
                  <div key={card.key} className={`plans-stat card stat-tone-${card.tone}`}>
                    <div className="plans-stat-icon">
                      <Icon size={16} />
                    </div>
                    <div className="plans-stat-body">
                      {loading && !plansData ? (
                        <Skeleton width={68} height={22} radius={6} />
                      ) : (
                        <div className="plans-stat-value">
                          <span className="mono">{card.value}</span>
                          {card.suffix ? <span className="plans-stat-suffix">{card.suffix}</span> : null}
                        </div>
                      )}
                      <div className="plans-stat-label">{card.label}</div>
                      <div className="plans-stat-detail" title={card.detail}>
                        {card.detail}
                      </div>
                      {"progress" in card && card.progress != null ? (
                        <div className="plans-stat-bar">
                          <span style={{ width: `${card.progress}%` }} />
                        </div>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </div>

            {error && plansData ? (
              <div className="plans-error-banner">
                <RefreshCw size={13} />
                <span>刷新失败：{error}</span>
                <button type="button" onClick={refresh}>
                  重试
                </button>
              </div>
            ) : null}
            {groupsError ? (
              <div className="plans-error-banner groups-banner">
                <FolderCog size={13} />
                <span>分组加载失败：{groupsError}</span>
                <button type="button" onClick={() => setGroupManagerOpen(true)}>
                  重试
                </button>
              </div>
            ) : null}

            {loading && !plansData ? (
              <div className="plans-layout">
                <div className="plans-main">
                  <Skeleton width={140} height={18} radius={6} />
                  <div className="plans-list" style={{ marginTop: 14 }}>
                    {[0, 1, 2].map((index) => (
                      <div className="card plan-card-skeleton" key={index}>
                        <Skeleton width={34} height={34} radius={10} />
                        <div style={{ flex: 1 }}>
                          <Skeleton width="56%" height={15} radius={6} />
                          <div style={{ marginTop: 10 }}>
                            <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
                              <Skeleton width="88%" height={10} radius={5} />
                              <Skeleton width="66%" height={10} radius={5} />
                            </div>
                          </div>
                        </div>
                        <Skeleton width={64} height={34} radius={10} />
                      </div>
                    ))}
                  </div>
                </div>
                <aside className="plans-rail">
                  <div className="card plans-rail-card">
                    <Skeleton width="100%" height={140} radius={10} />
                  </div>
                  <div className="card plans-rail-card">
                    <SkeletonRows rows={4} />
                  </div>
                </aside>
              </div>
            ) : isEmpty ? (
              <section className="card plans-empty">
                <div className="plans-empty-art">
                  <svg width="120" height="120" viewBox="0 0 120 120" fill="none" aria-hidden="true">
                    <rect x="28" y="24" width="64" height="76" rx="14" stroke="rgba(120,190,255,.3)" />
                    <path d="M40 44h40M40 58h40M40 72h24" stroke="rgba(120,190,255,.26)" strokeLinecap="round" />
                    <circle cx="88" cy="88" r="16" fill="rgba(79,216,255,.1)" stroke="rgba(79,216,255,.5)" />
                    <path d="M81 88l5 5 9-10" stroke="#4FD8FF" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
                    <circle cx="34" cy="30" r="3" fill="#A78BFA" />
                    <circle cx="94" cy="34" r="2" fill="#FBBF24" />
                  </svg>
                </div>
                <h2 className="plans-empty-title">还没有计划，从一个每天能做到的小目标开始</h2>
                <p className="plans-empty-desc">
                  循环计划用来培养长期习惯，单日行程用来安排特定日期的具体事项。选一个模板快速开始，或者创建你自己的计划。
                </p>
                <div className="plans-template-grid">
                  {PLAN_TEMPLATES.map((template) => (
                    <button
                      key={template.key}
                      type="button"
                      className="plan-template-card"
                      onClick={() => openCreate(template, null)}
                    >
                      <span
                        className="plan-template-icon"
                        style={{ color: template.color, background: `${template.color}1a`, borderColor: `${template.color}44` }}
                      >
                        {template.kind === "once" ? <CalendarClock size={15} /> : <Sparkles size={15} />}
                      </span>
                      <span className="plan-template-body">
                        <span className="plan-template-title">{template.title}</span>
                        <span className="plan-template-desc">{template.description}</span>
                        <span className="plan-template-meta">{favoriteTemplateMeta(template)}</span>
                      </span>
                    </button>
                  ))}
                </div>
                <button type="button" className="btn" onClick={() => openCreate(null, null)}>
                  <Plus size={14} /> 自定义计划
                </button>
              </section>
            ) : (
              <div className="plans-layout">
                <div className="plans-main">
                  {todaySchedules.length > 0 ? (
                    <section className="schedule-section">
                      <div className="plans-section-head">
                        <div>
                          <h2 className="plans-section-title">
                            <CalendarClock size={15} />
                            今日行程
                          </h2>
                          <span className="plans-section-sub">
                            {formatFullDate(todayValue)} · 今日与过期未完成 · {todaySchedules.length} 项
                          </span>
                        </div>
                        <span className="plans-section-note">
                          <Clock size={13} />
                          点击行程可查看详情
                        </span>
                      </div>
                      <div className="schedule-list">
                        {todaySchedules.map((plan) => (
                          <ScheduleCard
                            key={plan.id}
                            plan={plan}
                            today={todayValue}
                            onOpen={(item) => setDrawerPlanId(item.id)}
                            onComplete={openScheduleComplete}
                            onUndo={openScheduleUndo}
                          />
                        ))}
                      </div>
                    </section>
                  ) : null}

                  <section className="plans-groups-section">
                    <div className="plans-section-head">
                      <div>
                        <h2 className="plans-section-title">今日计划</h2>
                        <span className="plans-section-sub">
                          {formatFullDate(todayValue)} · 循环计划已完成 {recurringChecked}/{recurringTotal}
                        </span>
                      </div>
                      <span className="plans-section-note">
                        <CalendarDays size={13} />
                        点击卡片查看详情与月历
                      </span>
                    </div>

                    <div className="plan-groups">
                      {sortedGroups.map((group) =>
                        renderGroupSection(
                          group.id,
                          group.name,
                          group.color,
                          group.plan_count,
                          plansByGroup.get(group.id) ?? [],
                          group,
                        ),
                      )}
                      {ungroupedPlans.length > 0 || sortedGroups.length === 0
                        ? renderGroupSection(
                            UNGROUPED_KEY,
                            "未分组",
                            "#5B6F8C",
                            ungroupedPlans.length,
                            ungroupedPlans,
                            null,
                          )
                        : null}
                      {sortedGroups.length === 0 && ungroupedPlans.length === 0 ? (
                        <div className="card plan-groups-empty">
                          <CalendarCheck size={18} />
                          <div>
                            <div className="plan-groups-empty-title">还没有循环计划</div>
                            <div className="plan-groups-empty-desc">
                              创建每天/每周循环计划，或用「新建计划 → 单日行程」安排特定日期的事项。
                            </div>
                          </div>
                          <button type="button" className="btn btn-sm" onClick={() => openCreate(null, null)}>
                            <Plus size={12} /> 新建计划
                          </button>
                        </div>
                      ) : null}
                    </div>
                  </section>

                  {plans.every((plan) => (isOncePlan(plan) ? plan.checked : plan.today_checked)) ? (
                    <div className="plans-all-done">
                      <Check size={14} />
                      今天的计划与行程都完成了，给自己一点掌声。
                    </div>
                  ) : null}
                </div>

                <aside className="plans-rail">
                  <section className="card plans-rail-card">
                    {overview ? (
                      <Heatmap days={overview.heatmap} today={todayValue} />
                    ) : (
                      <Skeleton width="100%" height={150} radius={10} />
                    )}
                  </section>

                  {upcomingSchedules.length > 0 ? (
                    <section className="card plans-rail-card upcoming-card">
                      <div className="plans-recent-head">
                        <span>即将到来</span>
                        <span className="mono dim">
                          {upcomingTotal} 项 · 未来 30 天
                        </span>
                      </div>
                      <div className="upcoming-list">
                        {upcomingSchedules.slice(0, 5).map((plan) => (
                          <button
                            key={plan.id}
                            type="button"
                            className="upcoming-item"
                            onClick={() => setDrawerPlanId(plan.id)}
                          >
                            <span className="upcoming-date">
                              <span className="upcoming-date-day">
                                {formatScheduleDate(plan.plan_date, todayValue)}
                              </span>
                              <span className="mono upcoming-date-raw">
                                {plan.plan_date ? formatMonthDay(plan.plan_date) : ""}
                              </span>
                            </span>
                            <span className="upcoming-main">
                              <span className="upcoming-title">
                                <span className="plan-color-dot" style={{ background: plan.color }} />
                                {plan.title}
                              </span>
                              <span className="upcoming-meta">
                                <Clock size={10} />
                                {formatTimeRange(plan.start_time, plan.end_time)}
                                {plan.location ? (
                                  <>
                                    <MapPin size={10} />
                                    {plan.location}
                                  </>
                                ) : null}
                              </span>
                            </span>
                          </button>
                        ))}
                      </div>
                      {upcomingSchedules.length > 5 ? (
                        <div className="upcoming-more">还有 {upcomingSchedules.length - 5} 项未来行程</div>
                      ) : null}
                    </section>
                  ) : null}

                  <section className="card plans-rail-card plans-recent-card">
                    <div className="plans-recent-head">
                      <span>最近打卡</span>
                      <span className="mono dim">{overview?.recent.length ?? 0} 条</span>
                    </div>
                    {overview && overview.recent.length > 0 ? (
                      <div className="plans-recent-list">
                        {overview.recent.slice(0, 8).map((item) => (
                          <button
                            key={`${item.plan_id}-${item.date}-${item.created_at}`}
                            type="button"
                            className="plan-recent-item"
                            onClick={() => setDrawerPlanId(item.plan_id)}
                          >
                            <span className="plan-color-dot" style={{ background: item.color }} />
                            <span className="plan-recent-main">
                              <span className="plan-recent-title">{item.plan_title}</span>
                              <span className="plan-recent-note">{item.note ? item.note : "无备注"}</span>
                            </span>
                            <span className="plan-recent-date mono">{formatMonthDay(item.date)}</span>
                          </button>
                        ))}
                      </div>
                    ) : (
                      <div className="plans-recent-empty">还没有打卡记录，完成今天的计划或行程试试。</div>
                    )}
                  </section>
                </aside>
              </div>
            )}
          </>
        )}
      </div>

      <PlanFormDialog
        open={form.open}
        mode={form.mode}
        initialPlan={form.plan}
        template={form.template}
        groups={groupsData?.items ?? []}
        defaultGroupId={form.defaultGroupId}
        today={todayValue}
        onClose={() => setForm((prev) => ({ ...prev, open: false }))}
        onSaved={handleSaved}
      />

      <CheckinDialog
        open={Boolean(checkinTarget)}
        plan={checkinTarget?.plan ?? null}
        date={checkinTarget?.date ?? todayValue}
        isToday={Boolean(checkinTarget?.isToday)}
        variant={checkinTarget?.variant ?? "recurring"}
        onClose={() => setCheckinTarget(null)}
        onChecked={handleChecked}
      />

      <PlanDrawer
        open={Boolean(drawerPlanId)}
        planId={drawerPlanId}
        today={todayValue}
        onClose={() => setDrawerPlanId(null)}
        onEdit={openEdit}
        onChanged={handleDrawerChanged}
        onDeleted={handleDrawerDeleted}
      />

      <PlanGroupManager
        open={groupManagerOpen}
        groups={groupsData}
        loading={groupsLoading}
        error={groupsError}
        onClose={() => setGroupManagerOpen(false)}
        onRefresh={refresh}
        onChanged={refresh}
      />

      <ConfirmDialog
        open={Boolean(undoTarget)}
        title={undoTarget?.variant === "schedule" ? "撤销这次完成？" : "撤销今日打卡？"}
        description={
          <span>
            {undoTarget?.variant === "schedule"
              ? `将移除《${undoTarget?.plan.title}》的完成记录，累计与热力图会重新计算。`
              : `将移除《${undoTarget?.plan.title}》今天的打卡记录，连续天数与统计会重新计算。`}
          </span>
        }
        confirmLabel={undoTarget?.variant === "schedule" ? "撤销完成" : "撤销打卡"}
        loading={undoLoading}
        icon={<CalendarDays size={16} />}
        onConfirm={() => void confirmUndo()}
        onClose={() => setUndoTarget(null)}
      />
    </div>
  );
}
