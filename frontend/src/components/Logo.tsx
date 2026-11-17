import { useId } from "react";

interface LogoProps {
  size?: number;
  className?: string;
}

/** 内联 SVG 神经节点 Logo。 */
export function Logo({ size = 34, className }: LogoProps) {
  const id = useId().replace(/[:]/g, "");
  const glowId = `logo-glow-${id}`;
  const gradientId = `logo-grad-${id}`;

  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 40 40"
      fill="none"
      role="img"
      aria-label="Synapse 知脉标志"
    >
      <defs>
        <linearGradient id={gradientId} x1="4" y1="4" x2="36" y2="36" gradientUnits="userSpaceOnUse">
          <stop stopColor="#4FD8FF" />
          <stop offset="0.55" stopColor="#7AA8FF" />
          <stop offset="1" stopColor="#A78BFA" />
        </linearGradient>
        <filter id={glowId} x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation="1.4" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      <g stroke={`url(#${gradientId})`} strokeWidth="1.05" strokeLinecap="round" opacity="0.72">
        <path d="M20 20 L9.5 11.5" />
        <path d="M20 20 L30.8 9.6" />
        <path d="M20 20 L33 23.4" />
        <path d="M20 20 L14.2 32" />
        <path d="M20 20 L27.2 31.2" />
        <path d="M9.5 11.5 L30.8 9.6" opacity="0.4" />
        <path d="M14.2 32 L27.2 31.2" opacity="0.4" />
        <path d="M33 23.4 L27.2 31.2" opacity="0.35" />
      </g>

      <g filter={`url(#${glowId})`}>
        <circle cx="20" cy="20" r="6.2" fill="#4FD8FF" opacity="0.12" />
        <circle cx="20" cy="20" r="3.35" fill="#DFF7FF" />
        <circle cx="9.5" cy="11.5" r="2.1" fill="#A78BFA" />
        <circle cx="30.8" cy="9.6" r="1.85" fill="#4FD8FF" />
        <circle cx="33" cy="23.4" r="1.7" fill="#FBBF24" />
        <circle cx="14.2" cy="32" r="1.9" fill="#4FD8FF" />
        <circle cx="27.2" cy="31.2" r="2.05" fill="#A78BFA" />
      </g>

      <circle cx="20" cy="20" r="6.4" stroke={`url(#${gradientId})`} strokeWidth="0.8" opacity="0.55" />
    </svg>
  );
}

interface NeuralOrbProps {
  size?: number;
}

/** 对话欢迎态中央的神经核心装饰。 */
export function NeuralOrb({ size = 104 }: NeuralOrbProps) {
  const id = useId().replace(/[:]/g, "");
  const gradientId = `orb-grad-${id}`;
  const glowId = `orb-glow-${id}`;

  const satellites = [
    { x: 96, y: 34, r: 3.2, fill: "#4FD8FF", delay: "0s" },
    { x: 142, y: 74, r: 2.4, fill: "#A78BFA", delay: "-1.2s" },
    { x: 122, y: 128, r: 2.8, fill: "#FBBF24", delay: "-2.1s" },
    { x: 56, y: 122, r: 2.2, fill: "#4FD8FF", delay: "-0.6s" },
    { x: 34, y: 78, r: 3, fill: "#A78BFA", delay: "-1.7s" },
    { x: 72, y: 40, r: 1.8, fill: "#34D399", delay: "-2.6s" },
  ];

  return (
    <div className="neural-orb" style={{ width: size, height: size }} aria-hidden="true">
      <svg viewBox="0 0 176 176" fill="none" className="neural-orb-svg">
        <defs>
          <linearGradient id={gradientId} x1="24" y1="20" x2="156" y2="160" gradientUnits="userSpaceOnUse">
            <stop stopColor="#4FD8FF" stopOpacity="0.9" />
            <stop offset="1" stopColor="#A78BFA" stopOpacity="0.75" />
          </linearGradient>
          <filter id={glowId} x="-80%" y="-80%" width="260%" height="260%">
            <feGaussianBlur stdDeviation="3" result="b" />
            <feMerge>
              <feMergeNode in="b" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        <circle cx="88" cy="88" r="70" stroke="rgba(120,190,255,.14)" strokeWidth="0.8" />
        <circle
          cx="88"
          cy="88"
          r="54"
          stroke={`url(#${gradientId})`}
          strokeWidth="0.9"
          strokeDasharray="2 7"
          opacity="0.5"
          className="orb-ring-slow"
        />
        <circle
          cx="88"
          cy="88"
          r="76"
          stroke="rgba(167,139,250,.16)"
          strokeWidth="0.7"
          strokeDasharray="1 10"
          className="orb-ring-slower"
        />

        <g filter={`url(#${glowId})`} opacity="0.95">
          {satellites.map((dot, index) => (
            <g key={index}>
              <line
                x1="88"
                y1="88"
                x2={dot.x}
                y2={dot.y}
                stroke="rgba(120,190,255,.25)"
                strokeWidth="0.7"
              />
              <circle
                cx={dot.x}
                cy={dot.y}
                r={dot.r}
                fill={dot.fill}
                className="orb-satellite"
                style={{ animationDelay: dot.delay }}
              />
            </g>
          ))}
        </g>

        <circle cx="88" cy="88" r="26" fill="rgba(79,216,255,.06)" />
        <circle cx="88" cy="88" r="26" stroke="rgba(79,216,255,.3)" strokeWidth="0.9" />
        <circle
          cx="88"
          cy="88"
          r="17"
          stroke="rgba(167,139,250,.36)"
          strokeWidth="0.8"
          strokeDasharray="1 5"
          className="orb-ring-slow"
        />
        <circle cx="88" cy="88" r="9" fill="rgba(79,216,255,.16)" />
        <circle cx="88" cy="88" r="4.6" fill="#DFF7FF" filter={`url(#${glowId})`} className="orb-core" />
      </svg>
    </div>
  );
}
