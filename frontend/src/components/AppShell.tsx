import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  CalendarCheck,
  ChevronLeft,
  ChevronRight,
  LibraryBig,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  Settings,
  Waypoints,
} from "lucide-react";
import { Logo } from "./Logo";
import { CommandPalette } from "./CommandPalette";
import { useApp } from "../state/AppContext";
import { navigate, type Route } from "../lib/router";

interface NavEntry {
  key: "chat" | "plans" | "library" | "graph" | "settings";
  label: string;
  icon: typeof MessageSquare;
  path: string;
}

const NAV_ITEMS: NavEntry[] = [
  { key: "chat", label: "对话", icon: MessageSquare, path: "/chat" },
  { key: "plans", label: "计划", icon: CalendarCheck, path: "/plans" },
  { key: "library", label: "知识库", icon: LibraryBig, path: "/library" },
  { key: "graph", label: "神经图谱", icon: Waypoints, path: "/graph" },
  { key: "settings", label: "设置", icon: Settings, path: "/settings" },
];

const PAGE_META: Record<
  NavEntry["key"],
  { label: string; context: string }
> = {
  chat: { label: "对话", context: "与你的知识库交流" },
  plans: { label: "计划", context: "规划习惯与日常打卡" },
  library: { label: "知识库", context: "管理文档、笔记与网页" },
  graph: { label: "神经图谱", context: "漫游你的知识网络" },
  settings: { label: "设置", context: "模型接入与数据管理" },
};

const SIDEBAR_STORAGE_KEY = "synapse.sidebar.collapsed";

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

interface SidebarProps {
  route: Route;
  collapsed: boolean;
  onToggle: () => void;
}

function Sidebar({ route, collapsed, onToggle }: SidebarProps) {
  const { health, healthError } = useApp();

  const activeKey: NavEntry["key"] | null =
    route.name === "notfound" ? null : (route.name as NavEntry["key"]);

  const llmConfigured = Boolean(health?.llm.configured);
  const statusLabel = healthError
    ? "服务未连接"
    : llmConfigured
      ? "AI 已接入"
      : "本地检索";
  const statusDetail = healthError
    ? "请检查后端"
    : llmConfigured
      ? health?.llm.model || "已配置"
      : "离线可用";
  const dotClass = healthError ? "is-off" : "pulse";

  return (
    <aside className={`sidebar${collapsed ? " collapsed" : ""}`} aria-label="主导航">
      <div className="sidebar-brand">
        <Logo size={34} />
        <div className="brand-text">
          <span className="brand-name">Synapse</span>
          <span className="brand-sub">知脉</span>
        </div>
      </div>

      {!collapsed ? <div className="sidebar-section-label">工作台</div> : null}

      <nav className="sidebar-nav">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          const active = activeKey === item.key;
          return (
            <a
              key={item.key}
              href={`#${item.path}`}
              className={`nav-item${active ? " active" : ""}`}
              aria-current={active ? "page" : undefined}
              data-tip={collapsed ? item.label : undefined}
              data-tip-side={collapsed ? "right" : undefined}
            >
              <Icon size={18} strokeWidth={1.9} />
              <span className="nav-label">{item.label}</span>
            </a>
          );
        })}
      </nav>

      <div className="sidebar-spacer" />

      <div className="sidebar-footer">
        <button
          type="button"
          className="model-capsule"
          onClick={() => navigate("/settings")}
          data-tip={collapsed ? `${statusLabel} · ${statusDetail}` : undefined}
          data-tip-side={collapsed ? "right" : undefined}
          aria-label={`模型状态：${statusLabel}，前往设置`}
        >
          <span className={`status-dot ${dotClass}`} />
          <span className="model-capsule-text">
            <span className="model-capsule-label">{statusLabel}</span>
            <span className="model-capsule-model">{statusDetail}</span>
          </span>
        </button>

        <button
          type="button"
          className="sidebar-collapse"
          onClick={onToggle}
          aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"}
        >
          {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
          <span>收起侧栏</span>
        </button>

        <div className="sidebar-version">
          <span className="version-label">Synapse · 知脉</span>
          <span>v1.0</span>
        </div>
      </div>
    </aside>
  );
}

interface TopBarProps {
  route: Route;
  collapsed: boolean;
  onToggle: () => void;
  onOpenPalette: () => void;
}

function TopBar({ route, collapsed, onToggle, onOpenPalette }: TopBarProps) {
  const { health, healthError } = useApp();
  const meta = route.name === "notfound" ? PAGE_META.chat : PAGE_META[route.name];
  const Icon =
    NAV_ITEMS.find((item) => item.key === (route.name === "notfound" ? "chat" : route.name))?.icon ??
    MessageSquare;

  const isApple =
    typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

  const statusText = healthError
    ? "服务未连接"
    : health?.llm.configured
      ? health.llm.model || "模型已配置"
      : "本地检索";

  return (
    <header className="topbar">
      <button
        type="button"
        className="btn btn-icon topbar-toggle"
        onClick={onToggle}
        aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"}
      >
        {collapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}
      </button>
      <div className="topbar-divider" />
      <div className="topbar-crumb">
        <Icon size={15} className="crumb-icon" />
        <span className="crumb-current">{meta.label}</span>
        <span className="crumb-sep">/</span>
        <span className="topbar-context">{meta.context}</span>
      </div>
      <div className="topbar-spacer" />
      <button type="button" className="command-trigger" onClick={onOpenPalette} aria-label="打开命令面板">
        <Search size={14} />
        <span className="ct-label">搜索或提问…</span>
        <span className="kbd-group">
          {isApple ? <span className="kbd">⌘</span> : <span className="kbd">Ctrl</span>}
          <span className="kbd">K</span>
        </span>
      </button>
      <div className="topbar-status" title={healthError ?? undefined}>
        <span
          className={`status-dot ${healthError ? "is-off" : health?.llm.configured ? "pulse" : "is-warn"}`}
        />
        <span>{statusText}</span>
        {health ? <span className="mono">v{health.version.slice(0, 3)}</span> : null}
      </div>
    </header>
  );
}

interface AppShellProps {
  route: Route;
  children: ReactNode;
}

export function AppShell({ route, children }: AppShellProps) {
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [paletteOpen, setPaletteOpen] = useState(false);

  const toggle = (): void => {
    setCollapsed((value) => {
      const next = !value;
      try {
        window.localStorage.setItem(SIDEBAR_STORAGE_KEY, next ? "1" : "0");
      } catch {
        /* localStorage 不可用时忽略 */
      }
      return next;
    });
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      const meta = event.metaKey || event.ctrlKey;
      if (meta && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const shellClass = useMemo(
    () => `app-shell${collapsed ? " sidebar-collapsed" : ""}`,
    [collapsed],
  );

  return (
    <div className={shellClass}>
      <Sidebar route={route} collapsed={collapsed} onToggle={toggle} />
      <div className="app-main">
        <TopBar
          route={route}
          collapsed={collapsed}
          onToggle={toggle}
          onOpenPalette={() => setPaletteOpen(true)}
        />
        <div className="app-content">{children}</div>
      </div>
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  );
}
