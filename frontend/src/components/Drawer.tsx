import { useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";

interface DrawerProps {
  open: boolean;
  onClose: () => void;
  label: string;
  children: ReactNode;
  width?: number;
  overlay?: boolean;
}

/** 右侧滑出抽屉（Esc / 点击遮罩关闭）。 */
export function Drawer({ open, onClose, label, children, width, overlay = true }: DrawerProps) {
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  return createPortal(
    <>
      {overlay ? <div className="drawer-backdrop" onClick={onClose} /> : null}
      <aside
        className="drawer"
        role="dialog"
        aria-modal={overlay ? "true" : undefined}
        aria-label={label}
        style={width ? { width: `min(${width}px, 100vw)` } : undefined}
      >
        {children}
      </aside>
    </>,
    document.body,
  );
}
