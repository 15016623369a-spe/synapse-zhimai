import { CircleAlert, CircleCheck, Info, X } from "lucide-react";
import { useApp } from "../state/AppContext";

export function ToastViewport() {
  const { toasts, dismissToast } = useApp();

  if (toasts.length === 0) return null;

  return (
    <div className="toast-viewport" role="region" aria-label="通知">
      {toasts.map((item) => (
        <div
          key={item.id}
          className={`toast toast-${item.kind}`}
          role={item.kind === "error" ? "alert" : "status"}
        >
          <span className="toast-icon">
            {item.kind === "success" ? (
              <CircleCheck size={17} />
            ) : item.kind === "error" ? (
              <CircleAlert size={17} />
            ) : (
              <Info size={17} />
            )}
          </span>
          <div className="toast-content">
            <div className="toast-title">{item.title}</div>
            {item.message ? <div className="toast-message">{item.message}</div> : null}
          </div>
          <button
            type="button"
            className="toast-close"
            onClick={() => dismissToast(item.id)}
            aria-label="关闭通知"
          >
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}
