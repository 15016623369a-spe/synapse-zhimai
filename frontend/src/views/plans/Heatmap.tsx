import { useMemo } from "react";
import type { HeatmapDay } from "../../types";
import {
  WEEKDAY_LABELS,
  buildHeatmapWeeks,
  formatFullDate,
  heatmapLevel,
} from "../../lib/plans";

interface HeatmapProps {
  days: HeatmapDay[];
  today: string;
}

export function Heatmap({ days, today }: HeatmapProps) {
  const weeks = useMemo(() => buildHeatmapWeeks(days), [days]);
  const total = useMemo(() => days.reduce((sum, day) => sum + day.count, 0), [days]);

  return (
    <div className="plan-heatmap">
      <div className="plan-heatmap-head">
        <span>最近 {days.length} 天</span>
        <span className="mono">{total} 次打卡</span>
      </div>
      <div className="heatmap-scroll">
        <div className="heatmap-grid">
          <div className="heatmap-weekdays" aria-hidden="true">
            {WEEKDAY_LABELS.map((label, index) => (
              <span key={label} className="heatmap-weekday">
                {index % 2 === 0 ? label : ""}
              </span>
            ))}
          </div>
          <div className="heatmap-weeks" role="img" aria-label={`最近 ${days.length} 天打卡热力图`}>
            {weeks.map((week, weekIndex) => (
              <div className="heatmap-week" key={`week-${weekIndex}`}>
                {week.map((cell, dayIndex) => {
                  if (!cell.date) {
                    return (
                      <span
                        className="heatmap-cell empty"
                        key={`empty-${weekIndex}-${dayIndex}`}
                        aria-hidden="true"
                      />
                    );
                  }
                  const level = heatmapLevel(cell.count);
                  const isToday = cell.date === today;
                  return (
                    <span
                      key={cell.date}
                      className={`heatmap-cell level-${level}${isToday ? " today" : ""}`}
                      data-tip={`${formatFullDate(cell.date)} · ${cell.count} 次打卡`}
                      aria-label={`${cell.date}，${cell.count} 次打卡`}
                    />
                  );
                })}
              </div>
            ))}
          </div>
        </div>
      </div>
      <div className="heatmap-legend">
        <span>少</span>
        {[0, 1, 2, 3, 4].map((level) => (
          <span key={level} className={`heatmap-cell level-${level}`} aria-hidden="true" />
        ))}
        <span>多</span>
        <span className="heatmap-today-hint">
          <span className="heatmap-cell today-swatch" aria-hidden="true" />
          今天
        </span>
      </div>
    </div>
  );
}
