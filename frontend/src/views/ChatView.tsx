import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDown,
  BookOpen,
  ChevronDown,
  History,
  LoaderCircle,
  RefreshCw,
  SquarePen,
  Trash2,
  WifiOff,
} from "lucide-react";
import {
  deleteConversation,
  getConversation,
  listConversations,
  streamChat,
  toUserMessage,
} from "../lib/api";
import { copyToClipboard, formatRelativeTime } from "../lib/format";
import { navigate } from "../lib/router";
import { useApp } from "../state/AppContext";
import type { ChatMode, ConversationSummary, Message, WebSourceCandidate } from "../types";
import { ConfirmDialog } from "../components/Modal";
import { Skeleton, SkeletonRows } from "../components/Skeleton";
import { CitationPanel } from "./chat/CitationPanel";
import { ExtensionPanel } from "./chat/ExtensionPanel";
import { ChatComposer } from "./chat/ChatComposer";
import { MessageBubble } from "./chat/MessageBubble";
import { WelcomePanel } from "./chat/WelcomePanel";
import type { ChatToolCall, StreamState, UiMessage } from "./chat/chatTypes";

const MAX_INPUT_LENGTH = 4000;

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function toUiMessage(message: Message): UiMessage {
  return {
    id: message.id,
    role: message.role,
    content: message.content,
    citations: Array.isArray(message.citations) ? message.citations : [],
    createdAt: message.created_at,
  };
}

interface ChatViewProps {
  conversationId: string | null;
}

export function ChatView({ conversationId }: ChatViewProps) {
  const {
    health,
    healthError,
    pendingChat,
    consumePendingChat,
    toast,
    refreshStats,
    refreshHealth,
    bumpGraphRevision,
    bumpLibraryRevision,
  } = useApp();

  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [title, setTitle] = useState("");
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [conversationError, setConversationError] = useState<string | null>(null);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationsError, setConversationsError] = useState(false);
  const [conversationsLoading, setConversationsLoading] = useState(true);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [docChip, setDocChip] = useState<{ id: string; title: string } | null>(null);
  const [panelOpen, setPanelOpen] = useState(false);
  const [panelMessageId, setPanelMessageId] = useState<string | null>(null);
  const [activeCitationIndex, setActiveCitationIndex] = useState<number | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [showScrollBottom, setShowScrollBottom] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ConversationSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [extension, setExtension] = useState<{
    open: boolean;
    question: string;
    candidates?: WebSourceCandidate[];
    queries?: string[];
  }>({ open: false, question: "" });

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const atBottomRef = useRef(true);
  const streamRef = useRef<StreamState | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const skipLoadRef = useRef<string | null>(null);
  const mountedRef = useRef(true);
  const copiedTimerRef = useRef<number | null>(null);
  const scrollFrameRef = useRef<number | null>(null);
  const lastRequestRef = useRef<{ text: string; docIds: string[] | null } | null>(null);
  const autoOpenedPanelRef = useRef(false);
  const historyRef = useRef<HTMLDivElement | null>(null);
  const docChipRef = useRef(docChip);
  docChipRef.current = docChip;

  /* ---------------------------- 生命周期 / 滚动 ---------------------------- */

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
      const state = streamRef.current;
      if (state?.timer != null) {
        window.clearInterval(state.timer);
        state.timer = null;
      }
      if (copiedTimerRef.current != null) window.clearTimeout(copiedTimerRef.current);
      if (scrollFrameRef.current != null) window.cancelAnimationFrame(scrollFrameRef.current);
    };
  }, []);

  const scrollToBottom = useCallback((smooth = false) => {
    const element = scrollRef.current;
    if (!element) return;
    element.scrollTo({ top: element.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }, []);

  const scheduleScroll = useCallback(() => {
    if (scrollFrameRef.current != null) return;
    scrollFrameRef.current = window.requestAnimationFrame(() => {
      scrollFrameRef.current = null;
      if (atBottomRef.current) scrollToBottom(false);
    });
  }, [scrollToBottom]);

  const handleScroll = (): void => {
    const element = scrollRef.current;
    if (!element) return;
    const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
    atBottomRef.current = distance < 120;
    setShowScrollBottom(distance > 240);
  };

  useEffect(() => {
    if (atBottomRef.current) scheduleScroll();
  }, [messages, scheduleScroll]);

  /* ------------------------------- 会话列表 -------------------------------- */

  const loadConversations = useCallback(async () => {
    setConversationsLoading(true);
    try {
      const data = await listConversations();
      if (!mountedRef.current) return;
      setConversations(data.items);
      setConversationsError(false);
    } catch {
      if (mountedRef.current) setConversationsError(true);
    } finally {
      if (mountedRef.current) setConversationsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadConversations();
  }, [loadConversations]);

  /* -------------------------------- 载入会话 ------------------------------- */

  useEffect(() => {
    if (conversationId && skipLoadRef.current === conversationId) {
      skipLoadRef.current = null;
      return;
    }
    if (conversationId && streamRef.current?.conversationId === conversationId) {
      return;
    }
    if (!conversationId) {
      setMessages([]);
      setTitle("");
      setConversationError(null);
      setLoadingConversation(false);
      setPanelMessageId(null);
      setActiveCitationIndex(null);
      atBottomRef.current = true;
      return;
    }

    let cancelled = false;
    setLoadingConversation(true);
    setConversationError(null);
    setMessages([]);
    setTitle("");
    atBottomRef.current = true;

    getConversation(conversationId)
      .then((data) => {
        if (cancelled || !mountedRef.current) return;
        setMessages(data.messages.map(toUiMessage));
        setTitle(data.title);
        setLoadingConversation(false);
        const lastWithCitations = [...data.messages]
          .reverse()
          .find((message) => message.role === "assistant" && message.citations.length > 0);
        setPanelMessageId(lastWithCitations?.id ?? null);
        setActiveCitationIndex(null);
        window.requestAnimationFrame(() => scrollToBottom(false));
      })
      .catch((error) => {
        if (cancelled || !mountedRef.current) return;
        setConversationError(toUserMessage(error));
        setLoadingConversation(false);
      });

    return () => {
      cancelled = true;
    };
  }, [conversationId, scrollToBottom]);

  // 切换会话时中止上一个会话仍在进行的生成
  useEffect(() => {
    const state = streamRef.current;
    if (state && state.conversationId && state.conversationId !== conversationId) {
      state.stopped = true;
      abortRef.current?.abort();
    }
  }, [conversationId]);

  /* ------------------------------ 待处理提问 ------------------------------- */

  useEffect(() => {
    if (!pendingChat) return;
    if (pendingChat.question) setInput(pendingChat.question);
    if (pendingChat.docIds && pendingChat.docIds.length > 0 && pendingChat.docTitle) {
      setDocChip({ id: pendingChat.docIds[0], title: pendingChat.docTitle });
    }
    consumePendingChat();
  }, [pendingChat, consumePendingChat]);

  /* -------------------------------- 发送消息 ------------------------------- */

  const updateAssistant = useCallback((assistantId: string, patch: Partial<UiMessage>) => {
    setMessages((prev) =>
      prev.map((message) => (message.id === assistantId ? { ...message, ...patch } : message)),
    );
  }, []);

  const send = useCallback(
    async (overrideText?: string, overrideDocIds?: string[] | null) => {
      const text = (overrideText ?? input).trim();
      if (!text || streamRef.current) return;
      if (text.length > MAX_INPUT_LENGTH) {
        toast("error", "内容过长", `单次提问最多 ${MAX_INPUT_LENGTH} 字，请精简后再发送。`);
        return;
      }

      const docIds =
        overrideDocIds !== undefined
          ? overrideDocIds
          : docChipRef.current
            ? [docChipRef.current.id]
            : null;

      setInput("");
      setDocChip(null);
      setConversationError(null);
      atBottomRef.current = true;
      setShowScrollBottom(false);

      const userMessage: UiMessage = {
        id: `local-user-${Date.now()}`,
        role: "user",
        content: text,
        citations: [],
        createdAt: new Date().toISOString(),
      };
      const assistantId = `local-assistant-${Date.now()}`;
      const assistantMessage: UiMessage = {
        id: assistantId,
        role: "assistant",
        content: "",
        citations: [],
        createdAt: new Date().toISOString(),
        streaming: true,
        mode: null,
      };

      setMessages((prev) => [...prev, userMessage, assistantMessage]);
      setStreaming(true);
      setPanelMessageId(assistantId);
      setActiveCitationIndex(null);
      setHistoryOpen(false);
      lastRequestRef.current = { text, docIds };

      let toolSeq = 0;
      const controller = new AbortController();
      abortRef.current = controller;
      const state: StreamState = {
        assistantId,
        conversationId,
        buffer: "",
        content: "",
        timer: null,
        pendingDone: null,
        pendingError: null,
        notice: null,
        mode: null,
        gap: null,
        webSources: [],
        contextUsed: false,
        contextSummary: null,
        tool: null,
        tools: [],
        stopped: false,
        finalized: false,
        eof: false,
        userText: text,
        docIds,
      };
      streamRef.current = state;

      const cleanup = (): void => {
        if (state.timer != null) {
          window.clearInterval(state.timer);
          state.timer = null;
        }
        if (abortRef.current === controller) abortRef.current = null;
        if (streamRef.current === state) streamRef.current = null;
      };

      const flush = (): void => {
        if (state.buffer) {
          state.content += state.buffer;
          state.buffer = "";
        }
      };

      const finalizeDone = (done: NonNullable<StreamState["pendingDone"]>): void => {
        if (state.finalized) return;
        state.finalized = true;
        flush();
        cleanup();
        if (!mountedRef.current) return;
        let doneTools: ChatToolCall[] = state.tools;
        if (doneTools.length === 0 && (done.mcp_results?.length ?? 0) > 0) {
          doneTools = (done.mcp_results ?? []).map((item, index) => ({
            id: `mcp-result-${index}`,
            name: item.name,
            args: item.args ?? null,
            status: item.ok === false ? "error" : "ok",
            summary: item.ok === false ? "调用失败" : "已返回结果",
          }));
        }
        setMessages((prev) =>
          prev.map((message) =>
            message.id === assistantId
              ? {
                  ...message,
                  content: state.content,
                  citations: done.citations ?? [],
                  mode: done.mode,
                  gap: done.gap ?? state.gap ?? null,
                  webSources: done.web_sources ?? state.webSources ?? [],
                  reference: done.reference ?? message.reference ?? null,
                  contextUsed: done.context_used ?? state.contextUsed,
                  contextSummary: done.context_summary ?? state.contextSummary,
                  tool: null,
                  tools: doneTools,
                  streaming: false,
                }
              : message,
          ),
        );
        setStreaming(false);
        setTitle(done.title);
        setPanelMessageId(assistantId);
        setActiveCitationIndex(null);
        if (
          (done.citations?.length ?? 0) > 0 &&
          !autoOpenedPanelRef.current &&
          window.matchMedia("(min-width: 1181px)").matches
        ) {
          autoOpenedPanelRef.current = true;
          setPanelOpen(true);
        }
        void loadConversations();
      };

      const finalizeError = (message: string): void => {
        if (state.finalized) return;
        state.finalized = true;
        flush();
        cleanup();
        if (!mountedRef.current) return;
        setMessages((prev) =>
          prev.map((item) =>
            item.id === assistantId
              ? { ...item, content: state.content, streaming: false, error: message }
              : item,
          ),
        );
        setStreaming(false);
        void loadConversations();
      };

      const finalizeStopped = (): void => {
        if (state.finalized) return;
        state.finalized = true;
        flush();
        cleanup();
        if (!mountedRef.current) return;
        setMessages((prev) =>
          prev.map((item) =>
            item.id === assistantId
              ? { ...item, content: state.content, streaming: false, stopped: true }
              : item,
          ),
        );
        setStreaming(false);
        void loadConversations();
      };

      const tick = (): void => {
        if (streamRef.current !== state || state.finalized) return;
        if (state.buffer) {
          const take = Math.max(2, Math.ceil(state.buffer.length / 14));
          const chunk = state.buffer.slice(0, take);
          state.buffer = state.buffer.slice(take);
          state.content += chunk;
          updateAssistant(assistantId, { content: state.content });
          scheduleScroll();
        }
        if (!state.buffer) {
          if (state.pendingDone) {
            finalizeDone(state.pendingDone);
            return;
          }
          if (state.pendingError) {
            finalizeError(state.pendingError);
            return;
          }
          if (state.eof) finalizeStopped();
        }
      };

      state.timer = window.setInterval(tick, 24);

      try {
        await streamChat(
          {
            message: text,
            conversation_id: conversationId,
            doc_ids: docIds,
            top_k: 6,
          },
          (event) => {
            if (state.finalized) return;
            switch (event.type) {
              case "meta": {
                state.conversationId = event.data.conversation_id;
                state.mode = event.data.mode;
                state.contextUsed = Boolean(event.data.context_used);
                state.contextSummary = event.data.context_summary ?? null;
                if (!state.notice) {
                  updateAssistant(assistantId, {
                    mode: event.data.mode,
                    reference: event.data.reference ?? null,
                    contextUsed: state.contextUsed,
                    contextSummary: state.contextSummary,
                  });
                }
                if (!conversationId) {
                  skipLoadRef.current = event.data.conversation_id;
                  navigate(`/chat/${event.data.conversation_id}`, { replace: true });
                }
                break;
              }
              case "sources": {
                updateAssistant(assistantId, { citations: event.data.citations });
                break;
              }
              case "gap": {
                state.gap = event.data;
                updateAssistant(assistantId, { gap: event.data });
                break;
              }
              case "tool": {
                const data = event.data;
                let nextTools = state.tools;
                if (data.phase === "start") {
                  toolSeq += 1;
                  nextTools = [
                    ...nextTools,
                    {
                      id: `tool-${toolSeq}`,
                      name: data.name,
                      args: data.args ?? null,
                      query: data.query,
                      status: "running",
                    },
                  ];
                } else {
                  const ok = data.ok !== false;
                  const index = nextTools
                    .map((item, position) => ({ item, position }))
                    .reverse()
                    .find(
                      (entry) =>
                        entry.item.name === data.name && entry.item.status === "running",
                    )?.position;
                  if (index != null) {
                    nextTools = nextTools.slice();
                    nextTools[index] = {
                      ...nextTools[index],
                      args: nextTools[index].args ?? data.args ?? null,
                      query: nextTools[index].query ?? data.query,
                      status: ok ? "ok" : "error",
                      summary: data.summary,
                      count: data.count,
                      error: data.error ?? undefined,
                    };
                  } else {
                    toolSeq += 1;
                    nextTools = [
                      ...nextTools,
                      {
                        id: `tool-${toolSeq}`,
                        name: data.name,
                        args: data.args ?? null,
                        query: data.query,
                        status: ok ? "ok" : "error",
                        summary: data.summary,
                        count: data.count,
                        error: data.error ?? undefined,
                      },
                    ];
                  }
                }
                state.tool = data;
                state.tools = nextTools;
                updateAssistant(assistantId, { tool: data, tools: nextTools });
                break;
              }
              case "web_sources": {
                state.webSources = event.data.candidates ?? [];
                updateAssistant(assistantId, { webSources: event.data.candidates ?? [] });
                break;
              }
              case "delta": {
                state.buffer += event.data.text;
                break;
              }
              case "notice": {
                state.notice = event.data.message;
                state.mode = event.data.mode;
                updateAssistant(assistantId, {
                  notice: event.data.message,
                  mode: event.data.mode,
                });
                break;
              }
              case "done": {
                state.pendingDone = event.data;
                break;
              }
              case "error": {
                state.pendingError = event.data.message;
                break;
              }
            }
          },
          controller.signal,
        );
        if (!state.finalized) {
          state.eof = true;
          if (!state.buffer && !state.pendingDone && !state.pendingError) finalizeStopped();
        }
      } catch (error) {
        if (isAbortError(error)) {
          finalizeStopped();
        } else {
          finalizeError(toUserMessage(error));
        }
      }
    },
    [
      conversationId,
      input,
      loadConversations,
      scheduleScroll,
      toast,
      updateAssistant,
    ],
  );

  const stopGeneration = useCallback(() => {
    const state = streamRef.current;
    if (!state) return;
    state.stopped = true;
    abortRef.current?.abort();
  }, []);

  const retryLastAnswer = useCallback(() => {
    const lastRequest = lastRequestRef.current;
    if (!lastRequest || streamRef.current) return;
    setMessages((prev) => {
      const next = [...prev];
      const trailing = next[next.length - 1];
      if (trailing && trailing.role === "assistant") next.pop();
      const maybeUser = next[next.length - 1];
      if (maybeUser && maybeUser.role === "user") next.pop();
      return next;
    });
    void send(lastRequest.text, lastRequest.docIds);
  }, [send]);

  /* ------------------------------ 引用 / 复制 ------------------------------ */

  const activeCitationsMessage = useMemo(() => {
    if (panelMessageId) {
      const found = messages.find((message) => message.id === panelMessageId);
      if (found) return found;
    }
    return [...messages].reverse().find((message) => message.role === "assistant" && message.citations.length > 0) ?? null;
  }, [messages, panelMessageId]);

  const panelCitations = activeCitationsMessage?.citations ?? [];

  const questionByAssistant = useMemo(() => {
    const map = new Map<string, string>();
    let lastUser = "";
    for (const message of messages) {
      if (message.role === "user") lastUser = message.content;
      else map.set(message.id, lastUser);
    }
    return map;
  }, [messages]);

  const openExtension = useCallback(
    (question: string, candidates?: WebSourceCandidate[], queries?: string[]): void => {
      if (!question.trim()) {
        toast("error", "缺少问题", "没有找到这一轮对应的用户问题。");
        return;
      }
      setExtension({ open: true, question, candidates, queries });
    },
    [toast],
  );

  const closeExtension = useCallback((): void => {
    setExtension((prev) => ({ ...prev, open: false }));
  }, []);

  const handleExtensionCommitted = useCallback((): void => {
    void refreshStats();
    void refreshHealth();
    bumpGraphRevision();
    bumpLibraryRevision();
  }, [refreshStats, refreshHealth, bumpGraphRevision, bumpLibraryRevision]);

  const openCitations = (message: UiMessage, index?: number): void => {
    setPanelMessageId(message.id);
    setActiveCitationIndex(index ?? null);
    setPanelOpen(true);
  };

  const handleCitationClick = (message: UiMessage, index: number): void => {
    openCitations(message, index);
  };

  const handleCopy = async (message: UiMessage): Promise<void> => {
    const ok = await copyToClipboard(message.content);
    if (!ok) {
      toast("error", "复制失败", "当前浏览器不支持自动复制，请手动选择文本。");
      return;
    }
    setCopiedId(message.id);
    if (copiedTimerRef.current != null) window.clearTimeout(copiedTimerRef.current);
    copiedTimerRef.current = window.setTimeout(() => setCopiedId(null), 1800);
  };

  /* -------------------------------- 会话操作 ------------------------------- */

  const handleNewChat = (): void => {
    if (streamRef.current) {
      streamRef.current.stopped = true;
      abortRef.current?.abort();
    }
    setHistoryOpen(false);
    if (!conversationId) {
      setMessages([]);
      setTitle("");
      setInput("");
      setDocChip(null);
      setPanelMessageId(null);
      setPanelOpen(false);
      return;
    }
    navigate("/chat");
  };

  const handleOpenConversation = (id: string): void => {
    setHistoryOpen(false);
    if (id === conversationId) return;
    navigate(`/chat/${id}`);
  };

  const confirmDelete = async (): Promise<void> => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await deleteConversation(deleteTarget.id);
      toast("success", "会话已删除", `《${deleteTarget.title}》已从历史记录中移除。`);
      setDeleteTarget(null);
      void loadConversations();
      if (deleteTarget.id === conversationId) {
        navigate("/chat", { replace: true });
      }
    } catch (error) {
      toast("error", "删除失败", toUserMessage(error));
    } finally {
      setDeleting(false);
    }
  };

  /* ------------------------------- 交互细节 -------------------------------- */

  useEffect(() => {
    if (!historyOpen) return;
    const onMouseDown = (event: MouseEvent): void => {
      if (historyRef.current && !historyRef.current.contains(event.target as Node)) {
        setHistoryOpen(false);
      }
    };
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, [historyOpen]);

  useEffect(() => {
    setHistoryOpen(false);
  }, [conversationId]);

  const lastMode: ChatMode | null = useMemo(() => {
    const lastAssistant = [...messages].reverse().find((message) => message.role === "assistant");
    return lastAssistant?.mode ?? null;
  }, [messages]);

  const configuredLabel = health?.llm.configured
    ? `AI 模式 · ${health.llm.model || "已接入模型"}`
    : healthError
      ? "服务未连接"
      : "本地检索模式 · 完全离线";

  const headerTitle = conversationId ? title || "对话" : "新对话";
  const showWelcome = messages.length === 0 && !loadingConversation && !conversationError;

  return (
    <div className="chat-view">
      <div className="chat-main">
        <div className="chat-header">
          <div className="chat-header-title">
            <h2 title={headerTitle}>{headerTitle}</h2>
            <span className="chat-header-sub">
              {conversationId
                ? lastMode === "llm"
                  ? "AI 生成 · 附引用来源"
                  : lastMode === "extractive"
                    ? "本地检索模式"
                    : "历史会话"
                : configuredLabel}
            </span>
          </div>
          <div className="chat-header-actions">
            <button type="button" className="btn btn-sm" onClick={handleNewChat}>
              <SquarePen size={13} />
              <span className="btn-label">新对话</span>
            </button>
            <div className="dropdown" ref={historyRef}>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => setHistoryOpen((open) => !open)}
                aria-haspopup="menu"
                aria-expanded={historyOpen}
              >
                <History size={13} />
                <span className="btn-label">历史</span>
                <ChevronDown size={12} />
              </button>
              {historyOpen ? (
                <div className="dropdown-panel" role="menu" aria-label="历史会话">
                  <div className="dropdown-head">
                    <span>历史会话</span>
                    <span>{conversations.length} 个</span>
                  </div>
                  {conversationsLoading ? (
                    <div style={{ padding: "4px 8px 10px" }}>
                      <Skeleton width="100%" height={42} radius={10} />
                      <div style={{ height: 6 }} />
                      <Skeleton width="100%" height={42} radius={10} />
                    </div>
                  ) : conversationsError ? (
                    <div className="history-empty">
                      <div>历史会话加载失败</div>
                      <button
                        type="button"
                        className="btn btn-sm"
                        style={{ marginTop: 10 }}
                        onClick={() => void loadConversations()}
                      >
                        <RefreshCw size={12} /> 重试
                      </button>
                    </div>
                  ) : conversations.length === 0 ? (
                    <div className="history-empty">还没有历史会话，发送第一条提问吧。</div>
                  ) : (
                    conversations.map((conversation) => (
                      <div
                        key={conversation.id}
                        className={`history-item${conversation.id === conversationId ? " active" : ""}`}
                        role="none"
                      >
                        <button
                          type="button"
                          className="history-item-main"
                          role="menuitem"
                          onClick={() => handleOpenConversation(conversation.id)}
                        >
                          <span className="history-item-title">{conversation.title}</span>
                          <span className="history-item-meta">
                            {conversation.message_count} 条消息 · {formatRelativeTime(conversation.updated_at)}
                          </span>
                        </button>
                        <button
                          type="button"
                          className="history-delete"
                          aria-label={`删除会话 ${conversation.title}`}
                          onClick={() => setDeleteTarget(conversation)}
                        >
                          <Trash2 size={13} />
                        </button>
                      </div>
                    ))
                  )}
                </div>
              ) : null}
            </div>
            <button
              type="button"
              className={`btn btn-icon chat-panel-toggle${panelOpen ? " active" : ""}`}
              onClick={() => setPanelOpen((open) => !open)}
              aria-label={panelOpen ? "收起引用来源" : "展开引用来源"}
              data-tip={panelOpen ? "收起引用来源" : "引用来源"}
            >
              <BookOpen size={16} />
              {panelCitations.length > 0 ? (
                <span className="panel-count">{panelCitations.length}</span>
              ) : null}
            </button>
          </div>
        </div>

        <div className="chat-scroll" ref={scrollRef} onScroll={handleScroll}>
          {conversationError ? (
            <div className="chat-inner">
              <div className="page-error" style={{ marginTop: 20 }}>
                <WifiOff size={18} />
                <div style={{ flex: 1 }}>
                  <div style={{ color: "var(--text)", fontWeight: 600, marginBottom: 3 }}>
                    会话加载失败
                  </div>
                  <div>{conversationError}</div>
                  <button
                    type="button"
                    className="btn btn-sm"
                    style={{ marginTop: 12 }}
                    onClick={() => navigate("/chat", { replace: true })}
                  >
                    <RefreshCw size={12} /> 返回新对话
                  </button>
                </div>
              </div>
            </div>
          ) : loadingConversation ? (
            <div className="chat-inner">
              <div style={{ marginBottom: 22 }}>
                <Skeleton width="62%" height={16} radius={6} />
                <div style={{ marginTop: 10 }}>
                  <Skeleton width="88%" height={12} radius={5} />
                </div>
              </div>
              <SkeletonRows rows={3} />
            </div>
          ) : showWelcome ? (
            <div className="chat-inner is-welcome">
              <WelcomePanel
                onAsk={(question) => void send(question)}
                disabled={streaming}
              />
            </div>
          ) : (
            <div className="chat-inner">
              {messages.map((message) => (
                <MessageBubble
                  key={message.id}
                  message={message}
                  copied={copiedId === message.id}
                  onCopy={(item) => void handleCopy(item)}
                  onRetry={retryLastAnswer}
                  onCitationClick={handleCitationClick}
                  onOpenCitations={(item, index) => openCitations(item, index)}
                  question={questionByAssistant.get(message.id)}
                  onExtend={openExtension}
                />
              ))}
              {streaming ? (
                <div className="answer-progress" aria-hidden="true">
                  <LoaderCircle size={12} className="spin" />
                  <span>正在从知识库中检索并组织回答…</span>
                </div>
              ) : null}
            </div>
          )}
        </div>

        {showScrollBottom ? (
          <button
            type="button"
            className="scroll-bottom"
            onClick={() => {
              atBottomRef.current = true;
              scrollToBottom(true);
            }}
          >
            <ArrowDown size={12} />
            回到底部
          </button>
        ) : null}

        <ChatComposer
          value={input}
          onChange={setInput}
          onSend={() => void send()}
          onStop={stopGeneration}
          streaming={streaming}
          disabled={Boolean(conversationError)}
          docChip={docChip}
          onClearDocChip={() => setDocChip(null)}
        />
      </div>

      <ExtensionPanel
        open={extension.open}
        question={extension.question}
        initialCandidates={extension.candidates}
        initialQueries={extension.queries}
        conversationId={conversationId}
        onClose={closeExtension}
        onCommitted={handleExtensionCommitted}
      />

      <CitationPanel
        open={panelOpen}
        citations={panelCitations}
        activeIndex={activeCitationIndex}
        onSelect={setActiveCitationIndex}
        onClose={() => setPanelOpen(false)}
      />

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="删除这个会话？"
        description={
          <span>
            确定删除会话 <strong>《{deleteTarget?.title}》</strong> 吗？会话中的全部消息将被移除，
            此操作不可撤销。
          </span>
        }
        confirmLabel="删除会话"
        loading={deleting}
        icon={<Trash2 size={16} />}
        onConfirm={() => void confirmDelete()}
        onClose={() => setDeleteTarget(null)}
      />

      <span className="sr-only" aria-live="polite">
        {streaming ? "正在生成回答" : ""}
      </span>
    </div>
  );
}
