/** 通用格式化工具：时间、字节、数字、百分比。 */

export function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return "未知时间";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "未知时间";
  const diff = Date.now() - date.getTime();
  if (diff < 0) return "刚刚";
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const day = 24 * hour;

  if (diff < minute) return "刚刚";
  if (diff < hour) return `${Math.floor(diff / minute)} 分钟前`;
  if (diff < day) return `${Math.floor(diff / hour)} 小时前`;
  if (diff < 2 * day) return "昨天";
  if (diff < 7 * day) return `${Math.floor(diff / day)} 天前`;
  return formatDate(iso);
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return `${date.getFullYear()}/${String(date.getMonth() + 1).padStart(2, "0")}/${String(
    date.getDate(),
  ).padStart(2, "0")}`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return `${formatDate(iso)} ${String(date.getHours()).padStart(2, "0")}:${String(
    date.getMinutes(),
  ).padStart(2, "0")}`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatNumber(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toLocaleString("zh-CN");
}

export function formatPercent(value: number, digits = 0): string {
  if (!Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatScore(value: number | null | undefined): string {
  if (!Number.isFinite(value ?? NaN)) return "—";
  return `${Math.round((value as number) * 100)}%`;
}

const SOURCE_TYPE_LABELS: Record<string, string> = {
  md: "Markdown",
  markdown: "Markdown",
  txt: "纯文本",
  text: "纯文本",
  pdf: "PDF",
  docx: "Word",
  html: "网页",
  htm: "网页",
  url: "网页",
  web: "网页",
  note: "笔记",
};

export function sourceTypeLabel(type: string | null | undefined): string {
  if (!type) return "文档";
  return SOURCE_TYPE_LABELS[type.toLowerCase()] ?? type.toUpperCase();
}

export function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max)}…`;
}

/** 复制文本到剪贴板，返回是否成功。 */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* 回退到 execCommand */
  }
  try {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(textarea);
    return ok;
  } catch {
    return false;
  }
}

/**
 * 把文档分块 / 引用的原始 Markdown 片段清洗成适合展示的纯文本，
 * 去掉标题井号、加粗、行内代码、表格分隔线等标记。
 */
export function formatExcerpt(text: string): string {
  return text
    .replace(/\r\n/g, "\n")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/^\s*---\s*$/gm, "")
    .replace(/^\s*#{1,6}\s+/gm, "")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/(^|[\s（(])\*([^*\n]+)\*(?=[\s）)。，,！!？?]|$)/g, "$1$2")
    .replace(/`{1,3}([^`]*)`{1,3}/g, "$1")
    .replace(/^\s*\|?[\s:|-]+\|[\s:|-]*$/gm, "")
    .replace(/^\s*\|(.+)\|\s*$/gm, (_match, row: string) =>
      row
        .split("|")
        .map((cell) => cell.trim())
        .filter(Boolean)
        .join(" · "),
    )
    .replace(/^\s*[-*+]\s+/gm, "· ")
    .replace(/^\s*\d+\.\s+/gm, "")
    .replace(/\*\*/g, "")
    .replace(/#{1,6}\s?/g, "")
    .replace(/[`~]/g, "")
    .replace(/\|/g, " · ")
    .replace(/^\s*>\s?/gm, "")
    .replace(/\*/g, "")
    .replace(/[ \t]{2,}/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** 多轮上下文提示文案：context_summary 已含「已结合上文」时直接使用，否则补前缀。 */
export function contextHintLabel(summary: string | null | undefined): string {
  const text = (summary ?? "").trim();
  if (!text) return "已结合上文";
  return text.includes("已结合上文") ? text : `已结合上文 · ${text}`;
}
