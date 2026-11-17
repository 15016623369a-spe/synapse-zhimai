interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  /** 无障碍标签，也是自动化测试定位依据。 */
  label: string;
  disabled?: boolean;
  size?: "sm" | "md";
}

/** 通用开关（设置页 V2 卡片复用）。 */
export function Switch({ checked, onChange, label, disabled = false, size = "md" }: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={label}
      className={`switch${checked ? " on" : ""}${size === "sm" ? " switch-sm" : ""}`}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <span className="switch-thumb" aria-hidden="true" />
    </button>
  );
}
