import type { CSSProperties } from "react";
import { CalendarClock, Check, Clock, MapPin } from "lucide-react";
import type { Plan } from "../../types";
import {
  formatScheduleDate,
  formatTimeRange,
  scheduleStatus,
  scheduleStatusLabel,
} from "../../lib/plans";

interface ScheduleCardProps {
  plan: Plan;
  today: string;
  onOpen: (plan: Plan) => void;
  onComplete: (plan: Plan) => void;
  onUndo: (plan: Plan) => void;
}

export function ScheduleCard({ plan, today, onOpen, onComplete, onUndo }: ScheduleCardProps) {
  const status = scheduleStatus(plan, today);
  const badge = scheduleStatusLabel(plan, today);
  const style = { "--plan-color": plan.color } as CSSProperties;
  const overdue = status === "overdue";
  const completed = status === "completed";
  const upcoming = status === "upcoming";

  return (
    <article
      className={`schedule-card card is-${status}`}
      style={style}
      onClick={() => onOpen(plan)}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen(plan);
        }
      }}
      role="button"
      tabIndex={0}
      aria-label={`查看行程 ${plan.title}`}
    >
      <div className="schedule-time-block" aria-hidden="true">
        <span className="schedule-date">{formatScheduleDate(plan.plan_date, today)}</span>
        <span className="schedule-time mono">
          <Clock size={10} />
          {formatTimeRange(plan.start_time, plan.end_time)}
        </span>
      </div>
      <div className="schedule-main">
        <div className="schedule-head">
          <h3 className="schedule-title" title={plan.title}>
            {plan.title}
          </h3>
          <span className={`schedule-badge tone-${badge.tone}`}>{badge.label}</span>
          {plan.group_name && plan.group_id ? (
            <span className="schedule-group">
              <span className="plan-color-dot" style={{ background: plan.group_color }} />
              {plan.group_name}
            </span>
          ) : null}
        </div>
        {plan.description ? (
          <p className="schedule-desc" title={plan.description}>
            {plan.description}
          </p>
        ) : null}
        <div className="schedule-meta">
          <span className="schedule-meta-item">
            <CalendarClock size={12} />
            {formatScheduleDate(plan.plan_date, today)}
            {plan.plan_date ? ` · ${plan.plan_date}` : ""}
          </span>
          {plan.location ? (
            <span className="schedule-meta-item">
              <MapPin size={12} />
              {plan.location}
            </span>
          ) : null}
        </div>
      </div>
      <div className="schedule-actions">
        {completed ? (
          <button
            type="button"
            className="schedule-completed-btn"
            title="点击撤销完成"
            onClick={(event) => {
              event.stopPropagation();
              onUndo(plan);
            }}
          >
            <Check size={14} />
            已完成
          </button>
        ) : upcoming ? (
          <span className="schedule-not-started">未开始</span>
        ) : (
          <button
            type="button"
            className="schedule-complete-btn"
            onClick={(event) => {
              event.stopPropagation();
              onComplete(plan);
            }}
          >
            {overdue ? "补完成" : "完成"}
          </button>
        )}
      </div>
    </article>
  );
}
