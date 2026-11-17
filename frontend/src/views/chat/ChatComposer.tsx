import { useEffect, useRef } from "react";
import { Send, Square, X, Sparkles } from "lucide-react";

const MAX_LENGTH = 4000;

interface ChatComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
  streaming: boolean;
  disabled?: boolean;
  docChip: { id: string; title: string } | null;
  onClearDocChip: () => void;
}

export function ChatComposer({
  value,
  onChange,
  onSend,
  onStop,
  streaming,
  disabled,
  docChip,
  onClearDocChip,
}: ChatComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const overLimit = value.length > MAX_LENGTH;
  const canSend = value.trim().length > 0 && !overLimit && !streaming && !disabled;

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 190)}px`;
  }, [value]);

  useEffect(() => {
    if (!streaming) {
      const timer = window.setTimeout(() => textareaRef.current?.focus(), 60);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [streaming]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>): void => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      if (canSend) onSend();
    }
  };

  return (
    <div className="composer-wrap">
      <div className="composer">
        {docChip ? (
          <div className="composer-doc-chip">
            <Sparkles size={12} />
            <span className="doc-chip-text">限定文档：{docChip.title}</span>
            <button type="button" onClick={onClearDocChip} aria-label="取消限定文档">
              <X size={11} />
            </button>
          </div>
        ) : null}
        <textarea
          ref={textareaRef}
          className="composer-textarea"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="向你的知识库提问…（Enter 发送，Shift+Enter 换行）"
          rows={1}
          aria-label="提问输入框"
        />
        <div className="composer-bar">
          <div className="composer-hint">
            <span>
              <span className="kbd">Enter</span> 发送
            </span>
            <span className="hint-sep">·</span>
            <span>
              <span className="kbd">Shift</span> + <span className="kbd">Enter</span> 换行
            </span>
            {overLimit ? (
              <>
                <span className="hint-sep">·</span>
                <span style={{ color: "var(--rose)" }}>内容过长，最多 {MAX_LENGTH} 字</span>
              </>
            ) : null}
          </div>
          {value.length > 0 ? (
            <span className={`composer-count${overLimit ? " over" : ""}`}>
              {value.length} / {MAX_LENGTH}
            </span>
          ) : null}
          {streaming ? (
            <button type="button" className="composer-send stop" onClick={onStop} aria-label="停止生成">
              <Square size={13} fill="currentColor" />
            </button>
          ) : (
            <button
              type="button"
              className="composer-send"
              onClick={onSend}
              disabled={!canSend}
              aria-label="发送消息"
            >
              <Send size={15} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
