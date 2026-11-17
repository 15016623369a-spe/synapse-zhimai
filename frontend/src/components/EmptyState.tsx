import type { ReactNode } from "react";

export type EmptyArtVariant = "docs" | "graph" | "chat" | "search" | "citations" | "settings";

function EmptyArt({ variant }: { variant: EmptyArtVariant }) {
  if (variant === "graph") {
    return (
      <svg width="104" height="104" viewBox="0 0 104 104" fill="none" aria-hidden="true">
        <circle cx="52" cy="52" r="42" stroke="rgba(120,190,255,.14)" strokeDasharray="2 7" />
        <path
          d="M33 36 L62 28 L78 50 L64 74 L38 68 Z M33 36 L64 74 M62 28 L38 68 M62 28 L78 50"
          stroke="rgba(120,190,255,.22)"
          strokeWidth="1"
        />
        <circle cx="33" cy="36" r="4" fill="#4FD8FF" opacity=".9" />
        <circle cx="62" cy="28" r="3" fill="#A78BFA" opacity=".85" />
        <circle cx="78" cy="50" r="3.4" fill="#FBBF24" opacity=".85" />
        <circle cx="64" cy="74" r="2.6" fill="#4FD8FF" opacity=".7" />
        <circle cx="38" cy="68" r="2.4" fill="#A78BFA" opacity=".7" />
        <circle cx="52" cy="52" r="2.2" fill="#34D399" opacity=".8" />
      </svg>
    );
  }

  if (variant === "chat") {
    return (
      <svg width="104" height="104" viewBox="0 0 104 104" fill="none" aria-hidden="true">
        <rect x="18" y="24" width="68" height="46" rx="13" stroke="rgba(120,190,255,.26)" />
        <path d="M35 70 L30 82 L48 70" stroke="rgba(120,190,255,.26)" />
        <circle cx="38" cy="47" r="3" fill="#4FD8FF" opacity=".85" />
        <circle cx="52" cy="47" r="3" fill="#A78BFA" opacity=".85" />
        <circle cx="66" cy="47" r="3" fill="#FBBF24" opacity=".8" />
        <path d="M38 47 L52 47 L66 47" stroke="rgba(120,190,255,.2)" />
        <path
          d="M82 20 L88 26 L82 32 L76 26 Z"
          stroke="rgba(79,216,255,.4)"
          fill="rgba(79,216,255,.08)"
        />
      </svg>
    );
  }

  if (variant === "search") {
    return (
      <svg width="104" height="104" viewBox="0 0 104 104" fill="none" aria-hidden="true">
        <circle cx="47" cy="46" r="24" stroke="rgba(120,190,255,.3)" strokeWidth="1.2" />
        <path d="M64 63 L82 81" stroke="rgba(120,190,255,.38)" strokeWidth="2.4" strokeLinecap="round" />
        <circle cx="39" cy="40" r="2.6" fill="#4FD8FF" opacity=".8" />
        <circle cx="52" cy="51" r="2" fill="#A78BFA" opacity=".8" />
        <circle cx="46" cy="58" r="1.6" fill="#FBBF24" opacity=".7" />
      </svg>
    );
  }

  if (variant === "citations") {
    return (
      <svg width="104" height="104" viewBox="0 0 104 104" fill="none" aria-hidden="true">
        <rect x="26" y="22" width="52" height="62" rx="10" stroke="rgba(120,190,255,.24)" />
        <path
          d="M38 40 h28 M38 50 h28 M38 60 h18"
          stroke="rgba(120,190,255,.22)"
          strokeLinecap="round"
        />
        <circle cx="76" cy="76" r="12" fill="rgba(251,191,36,.08)" stroke="rgba(251,191,36,.4)" />
        <path d="M72 72 h8 M72 76 h8 M72 80 h5" stroke="rgba(251,191,36,.7)" strokeLinecap="round" />
      </svg>
    );
  }

  if (variant === "settings") {
    return (
      <svg width="104" height="104" viewBox="0 0 104 104" fill="none" aria-hidden="true">
        <circle cx="52" cy="52" r="16" stroke="rgba(120,190,255,.3)" />
        <circle cx="52" cy="52" r="5" fill="rgba(79,216,255,.35)" />
        <path
          d="M52 22 v8 M52 74 v8 M22 52 h8 M74 52 h8 M31 31 l6 6 M67 67 l6 6 M73 31 l-6 6 M37 67 l-6 6"
          stroke="rgba(120,190,255,.23)"
          strokeLinecap="round"
        />
      </svg>
    );
  }

  return (
    <svg width="104" height="104" viewBox="0 0 104 104" fill="none" aria-hidden="true">
      <rect x="24" y="30" width="42" height="52" rx="9" stroke="rgba(120,190,255,.22)" />
      <rect x="40" y="20" width="42" height="52" rx="9" stroke="rgba(120,190,255,.34)" fill="rgba(13,20,34,.6)" />
      <path
        d="M50 34 h22 M50 44 h22 M50 54 h14"
        stroke="rgba(120,190,255,.28)"
        strokeLinecap="round"
      />
      <circle cx="78" cy="76" r="3.4" fill="#4FD8FF" opacity=".85" />
      <circle cx="88" cy="66" r="2.2" fill="#A78BFA" opacity=".8" />
      <path d="M78 76 L88 66" stroke="rgba(79,216,255,.35)" />
    </svg>
  );
}

interface EmptyStateProps {
  title: string;
  description?: ReactNode;
  variant?: EmptyArtVariant;
  actions?: ReactNode;
  compact?: boolean;
}

export function EmptyState({ title, description, variant = "docs", actions, compact }: EmptyStateProps) {
  return (
    <div className="empty" style={compact ? { padding: "30px 20px" } : undefined}>
      <div className="empty-art">
        <EmptyArt variant={variant} />
      </div>
      <div className="empty-title">{title}</div>
      {description ? <div className="empty-desc">{description}</div> : null}
      {actions ? <div className="empty-actions">{actions}</div> : null}
    </div>
  );
}
