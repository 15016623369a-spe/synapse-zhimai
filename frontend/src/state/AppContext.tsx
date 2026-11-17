import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { getHealth, getSettings, getStats, toUserMessage } from "../lib/api";
import { navigate } from "../lib/router";
import type { Health, SettingsData, Stats } from "../types";

export type ToastKind = "success" | "error" | "info";

export interface ToastItem {
  id: number;
  kind: ToastKind;
  title: string;
  message?: string;
}

export interface PendingChat {
  question: string;
  docIds: string[] | null;
  docTitle: string | null;
  nonce: number;
}

interface AppContextValue {
  health: Health | null;
  healthError: string | null;
  loadingHealth: boolean;
  refreshHealth: () => Promise<void>;
  stats: Stats | null;
  statsError: string | null;
  statsLoading: boolean;
  refreshStats: () => Promise<void>;
  settings: SettingsData | null;
  refreshSettings: () => Promise<SettingsData | null>;
  graphRevision: number;
  bumpGraphRevision: () => void;
  libraryRevision: number;
  bumpLibraryRevision: () => void;
  toasts: ToastItem[];
  toast: (kind: ToastKind, title: string, message?: string) => void;
  dismissToast: (id: number) => void;
  pendingChat: PendingChat | null;
  startChat: (input?: {
    question?: string;
    docIds?: string[] | null;
    docTitle?: string | null;
  }) => void;
  consumePendingChat: () => void;
}

const AppContext = createContext<AppContextValue | null>(null);

let toastSeed = 0;

export function AppProvider({ children }: { children: ReactNode }) {
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [loadingHealth, setLoadingHealth] = useState(true);
  const [stats, setStats] = useState<Stats | null>(null);
  const [statsError, setStatsError] = useState<string | null>(null);
  const [statsLoading, setStatsLoading] = useState(true);
  const [settings, setSettings] = useState<SettingsData | null>(null);
  const [graphRevision, setGraphRevision] = useState(0);
  const [libraryRevision, setLibraryRevision] = useState(0);
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const [pendingChat, setPendingChat] = useState<PendingChat | null>(null);
  const timers = useRef<Map<number, number>>(new Map());

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((item) => item.id !== id));
    const timer = timers.current.get(id);
    if (timer != null) {
      window.clearTimeout(timer);
      timers.current.delete(id);
    }
  }, []);

  const toast = useCallback(
    (kind: ToastKind, title: string, message?: string) => {
      toastSeed += 1;
      const id = toastSeed;
      setToasts((prev) => [...prev.slice(-3), { id, kind, title, message }]);
      const timer = window.setTimeout(() => {
        setToasts((prev) => prev.filter((item) => item.id !== id));
        timers.current.delete(id);
      }, 4200);
      timers.current.set(id, timer);
    },
    [],
  );

  const refreshHealth = useCallback(async () => {
    setLoadingHealth(true);
    try {
      const data = await getHealth();
      setHealth(data);
      setHealthError(null);
    } catch (error) {
      setHealthError(toUserMessage(error));
    } finally {
      setLoadingHealth(false);
    }
  }, []);

  const refreshStats = useCallback(async () => {
    setStatsLoading(true);
    try {
      const data = await getStats();
      setStats(data);
      setStatsError(null);
    } catch (error) {
      setStatsError(toUserMessage(error));
    } finally {
      setStatsLoading(false);
    }
  }, []);

  const refreshSettings = useCallback(async (): Promise<SettingsData | null> => {
    try {
      const data = await getSettings();
      setSettings(data);
      return data;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    void refreshHealth();
    void refreshStats();
    void refreshSettings();
    const timersMap = timers.current;
    return () => {
      for (const timer of timersMap.values()) window.clearTimeout(timer);
      timersMap.clear();
    };
  }, [refreshHealth, refreshStats, refreshSettings]);

  const bumpGraphRevision = useCallback(() => setGraphRevision((value) => value + 1), []);

  const bumpLibraryRevision = useCallback(() => setLibraryRevision((value) => value + 1), []);

  const startChat = useCallback(
    (input?: { question?: string; docIds?: string[] | null; docTitle?: string | null }) => {
      setPendingChat({
        question: input?.question ?? "",
        docIds: input?.docIds ?? null,
        docTitle: input?.docTitle ?? null,
        nonce: Date.now(),
      });
      navigate("/chat");
    },
    [],
  );

  const consumePendingChat = useCallback(() => setPendingChat(null), []);

  const value = useMemo<AppContextValue>(
    () => ({
      health,
      healthError,
      loadingHealth,
      refreshHealth,
      stats,
      statsError,
      statsLoading,
      refreshStats,
      settings,
      refreshSettings,
      graphRevision,
      bumpGraphRevision,
      libraryRevision,
      bumpLibraryRevision,
      toasts,
      toast,
      dismissToast,
      pendingChat,
      startChat,
      consumePendingChat,
    }),
    [
      health,
      healthError,
      loadingHealth,
      refreshHealth,
      stats,
      statsError,
      statsLoading,
      refreshStats,
      settings,
      refreshSettings,
      graphRevision,
      bumpGraphRevision,
      libraryRevision,
      bumpLibraryRevision,
      toasts,
      toast,
      dismissToast,
      pendingChat,
      startChat,
      consumePendingChat,
    ],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp(): AppContextValue {
  const context = useContext(AppContext);
  if (!context) throw new Error("useApp 必须在 AppProvider 内部使用");
  return context;
}
