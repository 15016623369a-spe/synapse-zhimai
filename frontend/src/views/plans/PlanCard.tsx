import type { CSSProperties } from "react";
import { Check, Flame } from "lucide-react";
import type { Plan } from "../../types";
import {
  formatFrequency,
  planStarted,
  streakUnit,
  weekProgress,
} from "../../lib/plans";

interface PlanCardProps {
  plan: Plan;
  today: string;
  celebrating: boolean;
  onOpen: (plan: Plan) => void;
  onCheckin: (plan: Plan) => void;
  onUndo: (plan: Plan) => void;
}

export function PlanCard({
  plan,
  today,
  celebrating,
  onOpen,
  onCheckin,
  onUndo,
}: PlanCardProps) {
  const week = weekProgress(plan);
  const started = planStarted(plan, today);
  const cardStyle = { "--plan-color": plan.color } as CSSProperties;

  return (
    <article
      className={`plan-card card${celebrating ? " celebrate" : ""}`}
      style={cardStyle}
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
      aria-label={`查看计划 ${plan.title} 的详情`}
    >
      <span className="plan-card-accent" aria-hidden="true" />
      <div className="plan-card-main">
        <div className="plan-card-head">
          <span className="plan-color-dot" aria-hidden="true" />
          <h3 className="plan-card-title" title={plan.title}>
            {plan.title}
          </h3>
          <span className="plan-freq-badge">{formatFrequency(plan)}</span>
          {plan.current_streak > 0 ? (
            <span className="plan-streak">
              <Flame size={12} />
              {plan.current_streak} {streakUnit(plan.frequency)}
            </span>
          ) : null}
        </div>
        {plan.description ? (
          <p className="plan-card-desc" title={plan.description}>
            {plan.description}
          </p>
        ) : null}
        <div className="plan-card-meta">
          <div className="plan-week">
            <span className="plan-week-text">
              本周{" "}
              <b className="mono">
                {week.filled}/{week.total}
              </b>
            </span>
            <span className="plan-week-dots" aria-hidden="true">
              {Array.from({ length: week.total }).map((_, index) => (
                <span
                  key={index}
                  className={`plan-week-dot${index < week.filled ? " filled" : ""}`}
                  style={index < week.filled ? { background: plan.color } : undefined}
                />
              ))}
            </span>
          </div>
          <span className="plan-total">
            累计 <b className="mono">{plan.total_checkins}</b> 次
          </span>
        </div>
      </div>
      <div className="plan-card-actions">
        {!started ? (
          <span className="plan-not-started">未开始</span>
        ) : plan.today_checked ? (
          <button
            type="button"
            className="plan-checkedin-btn"
            onClick={(event) => {
              event.stopPropagation();
              onUndo(plan);
            }}
            title="点击撤销今日打卡"
          >
            <Check size={14} />
            已打卡
          </button>
        ) : (
          <button
            type="button"
            className="plan-checkin-btn"
            onClick={(event) => {
              event.stopPropagation();
              onCheckin(plan);
            }}
          >
            打卡
          </button>
        )}
      </div>
    </article>
  );
}
