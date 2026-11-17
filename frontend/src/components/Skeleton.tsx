import type { CSSProperties } from "react";

export function Skeleton({
  width,
  height = 12,
  radius,
  className = "",
  style,
}: {
  width?: number | string;
  height?: number | string;
  radius?: number | string;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <div
      className={`skeleton ${className}`}
      aria-hidden="true"
      style={{ width, height, borderRadius: radius, ...style }}
    />
  );
}

export function SkeletonText({ lines = 3, gap = 9 }: { lines?: number; gap?: number }) {
  return (
    <div
      aria-hidden="true"
      style={{ display: "flex", flexDirection: "column", gap, width: "100%" }}
    >
      {Array.from({ length: lines }).map((_, index) => (
        <div
          key={index}
          className="skeleton skeleton-text"
          style={{ width: index === lines - 1 ? "62%" : `${92 - index * 6}%` }}
        />
      ))}
    </div>
  );
}

export function SkeletonDocCard() {
  return (
    <div className="card doc-skeleton" aria-hidden="true">
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <Skeleton width={34} height={34} radius={10} />
        <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 8 }}>
          <Skeleton width="72%" height={14} radius={6} />
          <Skeleton width="42%" height={10} radius={5} />
        </div>
      </div>
      <div style={{ marginTop: 16 }}>
        <SkeletonText lines={2} />
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
        <Skeleton width={58} height={22} radius={99} />
        <Skeleton width={48} height={22} radius={99} />
      </div>
    </div>
  );
}

export function SkeletonRows({ rows = 4 }: { rows?: number }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }} aria-hidden="true">
      {Array.from({ length: rows }).map((_, index) => (
        <Skeleton key={index} height={54} radius={12} />
      ))}
    </div>
  );
}
