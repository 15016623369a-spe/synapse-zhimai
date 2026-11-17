import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  CalendarCheck,
  FileText,
  LibraryBig,
  LoaderCircle,
  MessageSquare,
  Search,
  Settings,
  SquarePen,
  Waypoints,
  type LucideIcon,
} from "lucide-react";
import { listDocuments } from "../lib/api";
import { navigate } from "../lib/router";
import { useApp } from "../state/AppContext";
import type { DocumentItem } from "../types";

interface CommandItem {
  id: string;
  title: string;
  subtitle?: string;
  group: string;
  icon: LucideIcon;
  run: () => void;
}

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
}

export function CommandPalette({ open, onClose }: CommandPaletteProps) {
  const { startChat } = useApp();
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const [docs, setDocs] = useState<DocumentItem[]>([]);
  const [searching, setSearching] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActiveIndex(0);
    setDocs([]);
    const timer = window.setTimeout(() => inputRef.current?.focus(), 40);
    return () => window.clearTimeout(timer);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const trimmed = query.trim();
    if (trimmed.length < 1) {
      setDocs([]);
      setSearching(false);
      return;
    }
    let cancelled = false;
    setSearching(true);
    const timer = window.setTimeout(() => {
      listDocuments({ q: trimmed, limit: 6 })
        .then((data) => {
          if (!cancelled) setDocs(data.items);
        })
        .catch(() => {
          if (!cancelled) setDocs([]);
        })
        .finally(() => {
          if (!cancelled) setSearching(false);
        });
    }, 220);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [query, open]);

  const items = useMemo<CommandItem[]>(() => {
    const trimmed = query.trim();
    const navigation: CommandItem[] = [
      {
        id: "nav-chat",
        title: "对话",
        subtitle: "与你的知识库交流",
        group: "导航",
        icon: MessageSquare,
        run: () => navigate("/chat"),
      },
      {
        id: "nav-plans",
        title: "计划",
        subtitle: "规划习惯与日常打卡",
        group: "导航",
        icon: CalendarCheck,
        run: () => navigate("/plans"),
      },
      {
        id: "nav-library",
        title: "知识库",
        subtitle: "管理文档、笔记与网页",
        group: "导航",
        icon: LibraryBig,
        run: () => navigate("/library"),
      },
      {
        id: "nav-graph",
        title: "神经图谱",
        subtitle: "漫游知识网络",
        group: "导航",
        icon: Waypoints,
        run: () => navigate("/graph"),
      },
      {
        id: "nav-settings",
        title: "设置",
        subtitle: "模型接入与数据",
        group: "导航",
        icon: Settings,
        run: () => navigate("/settings"),
      },
    ];

    const actions: CommandItem[] = [
      {
        id: "action-new-chat",
        title: "新建对话",
        subtitle: "从空白开始提问",
        group: "操作",
        icon: SquarePen,
        run: () => startChat(),
      },
      {
        id: "action-library-search",
        title: trimmed ? `在知识库中搜索「${trimmed}」` : "前往知识库",
        group: "操作",
        icon: Search,
        run: () =>
          trimmed ? navigate("/library", { query: { q: trimmed } }) : navigate("/library"),
      },
    ];

    const documentItems: CommandItem[] = docs.map((doc) => ({
      id: `doc-${doc.id}`,
      title: doc.title,
      subtitle: doc.summary ?? `${doc.chunk_count} 个分块 · ${doc.source_type.toUpperCase()}`,
      group: "文档",
      icon: FileText,
      run: () => navigate("/library", { query: { doc: doc.id } }),
    }));

    if (!trimmed) return [...navigation, ...actions];
    return [...actions, ...documentItems, ...navigation];
  }, [docs, query, startChat]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query, docs.length]);

  if (!open) return null;

  const runItem = (item: CommandItem): void => {
    onClose();
    item.run();
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>): void => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((index) => (index + 1) % Math.max(items.length, 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((index) => (index - 1 + items.length) % Math.max(items.length, 1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      const item = items[activeIndex];
      if (item) runItem(item);
    } else if (event.key === "Escape") {
      event.preventDefault();
      onClose();
    }
  };

  let lastGroup = "";

  return createPortal(
    <div
      className="palette-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="palette" role="dialog" aria-modal="true" aria-label="命令面板">
        <div className="palette-input-row">
          <Search size={17} />
          <input
            ref={inputRef}
            className="palette-input"
            placeholder="搜索页面、文档或输入问题…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onKeyDown}
            aria-label="命令面板搜索"
            autoComplete="off"
            spellCheck={false}
          />
          <span className="kbd">ESC</span>
        </div>
        <div className="palette-list" ref={listRef}>
          {searching ? (
            <div className="palette-empty">
              <LoaderCircle size={14} className="spin" /> 正在搜索文档…
            </div>
          ) : null}
          {items.map((item, index) => {
            const showGroup = item.group !== lastGroup;
            lastGroup = item.group;
            const Icon = item.icon;
            return (
              <div key={item.id}>
                {showGroup ? <div className="palette-group-label">{item.group}</div> : null}
                <button
                  type="button"
                  className={`palette-item${index === activeIndex ? " active" : ""}`}
                  onMouseEnter={() => setActiveIndex(index)}
                  onClick={() => runItem(item)}
                >
                  <Icon size={15} />
                  <span className="palette-item-text">
                    <span className="palette-item-title">{item.title}</span>
                    {item.subtitle ? (
                      <span className="palette-item-sub">{item.subtitle}</span>
                    ) : null}
                  </span>
                  {index === activeIndex ? <span className="kbd">↵</span> : null}
                </button>
              </div>
            );
          })}
          {!searching && items.length === 0 ? (
            <div className="palette-empty">没有匹配的页面或文档</div>
          ) : null}
        </div>
        <div className="palette-foot">
          <span className="foot-item">
            <span className="kbd">↑</span>
            <span className="kbd">↓</span> 选择
          </span>
          <span className="foot-item">
            <span className="kbd">↵</span> 打开
          </span>
          <span className="foot-item">
            <span className="kbd">ESC</span> 关闭
          </span>
          <span style={{ marginLeft: "auto" }} className="mono">
            Synapse 命令栏
          </span>
        </div>
      </div>
    </div>,
    document.body,
  );
}

export type { CommandItem };
