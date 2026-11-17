import type { HeatmapDay, Plan, PlanFrequency } from "../types";

export const PLAN_COLORS = [
  "#4FD8FF",
  "#A78BFA",
  "#FBBF24",
  "#34D399",
  "#FB7185",
  "#60A5FA",
  "#F472B6",
  "#2DD4BF",
] as const;

export const PLAN_TITLE_MAX = 40;
export const PLAN_DESC_MAX = 200;
export const PLAN_NOTE_MAX = 120;

export interface PlanTemplate {
  key: string;
  title: string;
  description: string;
  color: string;
  kind: "recurring" | "once";
  frequency?: "daily" | "weekly";
  weekly_target?: number;
  start_offset_days?: number;
  plan_offset_days?: number;
  start_time?: string;
  end_time?: string;
  location?: string;
}

export const PLAN_TEMPLATES: PlanTemplate[] = [
  {
    key: "reading",
    title: "每日阅读 30 分钟",
    description: "睡前读几页非虚构类书籍，不追求速度，坚持每天一点点。",
    color: "#4FD8FF",
    kind: "recurring",
    frequency: "daily",
    weekly_target: 7,
  },
  {
    key: "exercise",
    title: "每周运动 3 次",
    description: "跑步、游泳或力量训练均可，每次至少 30 分钟。",
    color: "#34D399",
    kind: "recurring",
    frequency: "weekly",
    weekly_target: 3,
  },
  {
    key: "review",
    title: "每日复盘 5 分钟",
    description: "睡前记录今天完成的事、卡住的事和明天最重要的一件事。",
    color: "#A78BFA",
    kind: "recurring",
    frequency: "daily",
    weekly_target: 7,
  },
  {
    key: "checkup",
    title: "明天 09:00 体检",
    description: "空腹，带好身份证和医保卡，提前 15 分钟到达。",
    color: "#2DD4BF",
    kind: "once",
    plan_offset_days: 1,
    start_time: "09:00",
    end_time: "10:00",
    location: "市体检中心",
  },
];

export const WEEKDAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"];

export function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

/** 本地日期 → YYYY-MM-DD。 */
export function toIsoDate(date: Date): string {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

/** YYYY-MM-DD → 本地当日 00:00 的 Date；非法返回 null。 */
export function parseIsoDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim());
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  if (
    date.getFullYear() !== Number(match[1]) ||
    date.getMonth() !== Number(match[2]) - 1 ||
    date.getDate() !== Number(match[3])
  ) {
    return null;
  }
  return date;
}

export function addDaysIso(value: string, days: number): string {
  const date = parseIsoDate(value);
  if (!date) return value;
  date.setDate(date.getDate() + days);
  return toIsoDate(date);
}

/** 0 = 周一 … 6 = 周日。 */
export function weekdayIndex(value: string): number {
  const date = parseIsoDate(value);
  if (!date) return 0;
  return (date.getDay() + 6) % 7;
}

export function compareIso(first: string, second: string): number {
  return first < second ? -1 : first > second ? 1 : 0;
}

export function formatShortDate(value: string): string {
  const date = parseIsoDate(value);
  if (!date) return value;
  return `${date.getMonth() + 1}月${date.getDate()}日`;
}

export function formatFullDate(value: string): string {
  const date = parseIsoDate(value);
  if (!date) return value;
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 周${WEEKDAY_LABELS[weekdayIndex(value)]}`;
}

export function formatMonthDay(value: string): string {
  const date = parseIsoDate(value);
  if (!date) return value;
  return `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

export function formatFrequency(plan: Pick<Plan, "frequency" | "weekly_target">): string {
  return plan.frequency === "daily" ? "每天" : `每周 ${plan.weekly_target} 次`;
}

export function streakUnit(frequency: PlanFrequency): string {
  return frequency === "daily" ? "天" : "周";
}

export function weekProgress(plan: Plan): { filled: number; total: number } {
  const total = plan.frequency === "daily" ? 7 : Math.max(1, plan.weekly_target);
  return { filled: Math.min(total, Math.max(0, plan.week_checked)), total };
}

export function planStarted(plan: Plan, today: string): boolean {
  return compareIso(plan.start_date, today) <= 0;
}

/** 月历数据：返回从周一开始的 6×7 单元格，非本月为 null。 */
export function getMonthCells(year: number, month: number): (string | null)[] {
  const first = new Date(year, month, 1);
  const firstWeekday = (first.getDay() + 6) % 7;
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const cells: (string | null)[] = [];
  for (let index = 0; index < firstWeekday; index += 1) cells.push(null);
  for (let day = 1; day <= daysInMonth; day += 1) {
    cells.push(`${year}-${pad2(month + 1)}-${pad2(day)}`);
  }
  while (cells.length % 7 !== 0) cells.push(null);
  while (cells.length < 42) cells.push(null);
  return cells;
}

export function monthTitle(year: number, month: number): string {
  return `${year} 年 ${month + 1} 月`;
}

export function monthKey(year: number, month: number): string {
  return `${year}-${pad2(month + 1)}`;
}

/** 把热力图连续日期按「周列 × 周一~周日行」对齐。 */
export function buildHeatmapWeeks(days: HeatmapDay[]): { date: string; count: number }[][] {
  if (days.length === 0) return [];
  const sorted = [...days].sort((first, second) => compareIso(first.date, second.date));
  const firstDate = sorted[0].date;
  const leading = weekdayIndex(firstDate);
  const cells: { date: string; count: number }[] = [];
  for (let index = 0; index < leading; index += 1) {
    cells.push({ date: "", count: 0 });
  }
  cells.push(...sorted);
  while (cells.length % 7 !== 0) cells.push({ date: "", count: 0 });
  const weeks: { date: string; count: number }[][] = [];
  for (let index = 0; index < cells.length; index += 7) {
    weeks.push(cells.slice(index, index + 7));
  }
  return weeks;
}

export function heatmapLevel(count: number): 0 | 1 | 2 | 3 | 4 {
  if (count <= 0) return 0;
  if (count === 1) return 1;
  if (count === 2) return 2;
  if (count === 3) return 3;
  return 4;
}

export function streakText(plan: Plan): string {
  if (plan.current_streak <= 0) return "尚未连续";
  return `连续 ${plan.current_streak} ${streakUnit(plan.frequency)}`;
}


/* ------------------------- 单日行程（once schedule） ------------------------ */

export function isOncePlan(
  plan: Pick<Plan, "frequency"> | null | undefined,
): boolean {
  return plan?.frequency === "once";
}

export type ScheduleStatus = "completed" | "today" | "overdue" | "upcoming";

export function scheduleStatus(plan: Plan, today: string): ScheduleStatus {
  if (plan.checked) return "completed";
  if (!plan.plan_date) return "today";
  const diff = compareIso(plan.plan_date, today);
  if (diff > 0) return "upcoming";
  if (diff === 0) return "today";
  return "overdue";
}

/** 两个日期相差的自然日（second - first）。 */
export function daysBetweenIso(first: string, second: string): number {
  const a = parseIsoDate(first);
  const b = parseIsoDate(second);
  if (!a || !b) return 0;
  const utcA = Date.UTC(a.getFullYear(), a.getMonth(), a.getDate());
  const utcB = Date.UTC(b.getFullYear(), b.getMonth(), b.getDate());
  return Math.round((utcB - utcA) / 86_400_000);
}

export function formatTimeRange(
  start: string | null,
  end: string | null,
): string {
  if (!start) return "全天";
  if (end && end !== start) return `${start}–${end}`;
  return start;
}

export function formatScheduleDate(date: string | null, today: string): string {
  if (!date) return "";
  const diff = daysBetweenIso(today, date);
  if (diff === 0) return "今天";
  if (diff === 1) return "明天";
  if (diff === -1) return "昨天";
  if (diff > 1 && diff < 7) return `周${WEEKDAY_LABELS[weekdayIndex(date)]}`;
  return formatShortDate(date);
}

export function scheduleStatusLabel(
  plan: Plan,
  today: string,
): { label: string; tone: "done" | "today" | "overdue" | "upcoming" } {
  const status = scheduleStatus(plan, today);
  if (status === "completed") return { label: "已完成", tone: "done" };
  if (status === "overdue") return { label: "已过期", tone: "overdue" };
  if (status === "upcoming") {
    const diff = plan.plan_date ? daysBetweenIso(today, plan.plan_date) : 0;
    return { label: diff === 1 ? "明天" : "未开始", tone: "upcoming" };
  }
  return { label: "今天", tone: "today" };
}

export function planKindLabel(plan: Pick<Plan, "frequency">): string {
  if (plan.frequency === "daily") return "每天";
  if (plan.frequency === "weekly") return "每周";
  return "单日行程";
}
