import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  FileText,
  FolderPlus,
  Globe2,
  Layers,
  LoaderCircle,
  LocateFixed,
  MessageSquare,
  RefreshCw,
  RotateCcw,
  RotateCw,
  Search,
  Settings2,
  Sparkles,
  Tag,
  Waypoints,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { EmptyState } from "../components/EmptyState";
import { Skeleton, SkeletonRows } from "../components/Skeleton";
import { getGraph, getGraphNode, listZones, toUserMessage } from "../lib/api";
import { formatExcerpt, formatNumber } from "../lib/format";
import { navigate, parseHash } from "../lib/router";
import { useReducedMotion } from "../lib/hooks";
import { useApp } from "../state/AppContext";
import type {
  GraphData,
  GraphNode,
  GraphNodeDetail,
  GraphNodeType,
  GraphZoneMeta,
  ZoneList,
} from "../types";
import { GraphCanvas, type GraphCanvasHandle } from "./graph/GraphCanvas";
import { ZoneManager } from "./graph/ZoneManager";
import {
  UNASSIGNED_ZONE_COLOR,
  UNASSIGNED_ZONE_NAME,
  UNASSIGNED_ZONE_ID,
  zoneSuffixFromNodeId,
  zoneToken,
} from "../lib/zones";

const TYPE_META: Record<
  GraphNodeType,
  { label: string; color: string; icon: typeof FileText }
> = {
  document: { label: "文档", color: "#4FD8FF", icon: FileText },
  concept: { label: "概念", color: "#A78BFA", icon: Sparkles },
  tag: { label: "关键词/标签", color: "#FBBF24", icon: Tag },
};

const NODE_TYPE_LABEL: Record<GraphNodeType, string> = {
  document: "文档",
  concept: "概念",
  tag: "关键词/标签",
};

const GRAPH_MODE_KEY = "synapse.graphMode";
type GraphMode = "global" | "zones";
const ALL_ZONES = "all";

interface TypeFilterState {
  document: boolean;
  concept: boolean;
  tag: boolean;
}

function readStoredMode(): GraphMode {
  try {
    return window.localStorage.getItem(GRAPH_MODE_KEY) === "zones" ? "zones" : "global";
  } catch {
    return "global";
  }
}

function readInitialGraphState(): { mode: GraphMode; zone: string } {
  const query = parseHash(window.location.hash || "#/graph").query;
  const stored = readStoredMode();
  const mode: GraphMode =
    query.mode === "zones" ? "zones" : query.mode === "global" ? "global" : stored;
  const zone = mode === "zones" && query.zone ? query.zone : ALL_ZONES;
  return { mode, zone };
}

export function GraphView() {
  const { graphRevision, bumpGraphRevision, startChat, toast } = useApp();
  const reducedMotion = useReducedMotion();
  const canvasRef = useRef<GraphCanvasHandle | null>(null);
  const initialRef = useRef<{ mode: GraphMode; zone: string } | null>(null);
  if (!initialRef.current) initialRef.current = readInitialGraphState();

  const [graphMode, setGraphMode] = useState<GraphMode>(initialRef.current.mode);
  const [activeZone, setActiveZone] = useState<string>(initialRef.current.zone);
  const [autoRotate, setAutoRotate] = useState(!reducedMotion);
  const [managerOpen, setManagerOpen] = useState(false);

  const [graph, setGraph] = useState<GraphData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshSeed, setRefreshSeed] = useState(0);

  const [zoneList, setZoneList] = useState<ZoneList | null>(null);
  const [zonesLoading, setZonesLoading] = useState(false);
  const [zonesError, setZonesError] = useState<string | null>(null);
  const [zonesSeed, setZonesSeed] = useState(0);

  const [typeFilter, setTypeFilter] = useState<TypeFilterState>({
    document: true,
    concept: true,
    tag: true,
  });
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [nodeDetail, setNodeDetail] = useState<GraphNodeDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailSeed, setDetailSeed] = useState(0);
  const matchCursorRef = useRef(0);
  const relayoutTimerRef = useRef<number | null>(null);
  const prevModeRef = useRef(graphMode);
  const prevZoneRef = useRef(activeZone);

  /* ------------------------------ URL / 模式同步 ----------------------------- */

  useEffect(() => {
    try {
      window.localStorage.setItem(GRAPH_MODE_KEY, graphMode);
    } catch {
      /* localStorage 不可用时忽略 */
    }
    const query =
      graphMode === "zones"
        ? {
            mode: "zones",
            zone: activeZone === ALL_ZONES ? null : activeZone,
          }
        : {};
    navigate("/graph", { query, replace: true });
  }, [graphMode, activeZone]);

  useEffect(() => {
    if (prevModeRef.current === graphMode) return;
    prevModeRef.current = graphMode;
    setSelectedId(null);
    setSearch("");
    setActiveZone(ALL_ZONES);
    setGraph(null);
    setLoading(true);
  }, [graphMode]);

  useEffect(() => {
    if (prevZoneRef.current === activeZone) return;
    prevZoneRef.current = activeZone;
    setSelectedId(null);
  }, [activeZone]);

  useEffect(() => {
    if (!zoneList) return;
    if (activeZone === ALL_ZONES || activeZone === UNASSIGNED_ZONE_ID) return;
    if (!zoneList.items.some((zone) => zone.id === activeZone)) {
      setActiveZone(ALL_ZONES);
    }
  }, [zoneList, activeZone]);

  /* -------------------------------- 加载图谱 ------------------------------- */

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getGraph({
      ...(graphMode === "zones" ? { mode: "zones" as const } : {}),
      max_concepts: 120,
      min_link_weight: 0.15,
      min_concept_degree: 2,
    })
      .then((data) => {
        if (cancelled) return;
        setGraph(data);
        setSelectedId(null);
        setDetailSeed(0);
      })
      .catch((err) => {
        if (!cancelled) setError(toUserMessage(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshSeed, graphRevision, graphMode]);

  /* -------------------------------- 加载分区 ------------------------------- */

  useEffect(() => {
    if (graphMode !== "zones" && !managerOpen) return;
    let cancelled = false;
    setZonesLoading(true);
    setZonesError(null);
    listZones()
      .then((data) => {
        if (!cancelled) setZoneList(data);
      })
      .catch((err) => {
        if (!cancelled) setZonesError(toUserMessage(err));
      })
      .finally(() => {
        if (!cancelled) setZonesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [graphMode, zonesSeed, graphRevision, managerOpen]);

  useEffect(() => {
    if (reducedMotion) setAutoRotate(false);
  }, [reducedMotion]);

  useEffect(() => {
    return () => {
      if (relayoutTimerRef.current != null) window.clearTimeout(relayoutTimerRef.current);
    };
  }, []);

  /* ----------------------------- 分区数据 / 过滤 ---------------------------- */

  const scopeNodes = useMemo<GraphNode[]>(() => {
    if (!graph) return [];
    if (graphMode !== "zones" || activeZone === ALL_ZONES) return graph.nodes;
    const zoneId = activeZone === UNASSIGNED_ZONE_ID ? null : activeZone;
    return graph.nodes.filter((node) => (node.zone_id ?? null) === zoneId);
  }, [graph, graphMode, activeZone]);

  const scopeLinks = useMemo(() => {
    if (!graph) return [];
    const ids = new Set(scopeNodes.map((node) => node.id));
    return graph.links.filter((link) => ids.has(link.source) && ids.has(link.target));
  }, [graph, scopeNodes]);

  const enabledTypes = useMemo(
    () => (Object.keys(typeFilter) as GraphNodeType[]).filter((type) => typeFilter[type]),
    [typeFilter],
  );

  const viewData = useMemo<GraphData | null>(() => {
    if (!graph) return null;
    const enabled = new Set(enabledTypes);
    const nodes = scopeNodes.filter((node) => enabled.has(node.type));
    const ids = new Set(nodes.map((node) => node.id));
    const links = scopeLinks.filter(
      (link) => ids.has(link.source) && ids.has(link.target),
    );
    return {
      nodes,
      links,
      meta: {
        ...graph.meta,
        node_count: nodes.length,
        link_count: links.length,
        document_count:
          graphMode === "zones"
            ? nodes.filter((node) => node.type === "document").length
            : graph.meta.document_count,
      },
      zones: graph.zones,
    };
  }, [graph, scopeNodes, scopeLinks, enabledTypes, graphMode]);

  const zoneMetaForLayout = useMemo<GraphZoneMeta[]>(() => {
    if (graphMode !== "zones" || !graph) return [];
    const available = graph.zones ?? [];
    if (activeZone === ALL_ZONES) return available;
    const zoneId = activeZone === UNASSIGNED_ZONE_ID ? null : activeZone;
    const matched = available.find((zone) => (zone.id ?? null) === zoneId);
    if (matched) return [matched];
    const documentCount = scopeNodes.filter((node) => node.type === "document").length;
    return [
      {
        id: zoneId,
        name: activeZone === UNASSIGNED_ZONE_ID ? UNASSIGNED_ZONE_NAME : "分区",
        color: activeZone === UNASSIGNED_ZONE_ID ? UNASSIGNED_ZONE_COLOR : "#4FD8FF",
        document_count: documentCount,
        node_count: scopeNodes.length,
        link_count: scopeLinks.length,
      },
    ];
  }, [graphMode, graph, activeZone, scopeNodes, scopeLinks]);

  const typeCounts = useMemo(() => {
    const counts: Record<GraphNodeType, number> = { document: 0, concept: 0, tag: 0 };
    for (const node of scopeNodes) counts[node.type] += 1;
    return counts;
  }, [scopeNodes]);

  const zoneChips = useMemo(() => zoneList?.items ?? [], [zoneList]);

  const visibleZoneCount = useMemo(() => {
    const keys = new Set<string>();
    for (const node of scopeNodes) keys.add(node.zone_id ?? "__unassigned__");
    return keys.size;
  }, [scopeNodes]);

  const unassignedCount = zoneList?.unassigned_count ?? 0;

  /* --------------------------------- 搜索 ---------------------------------- */

  const matches = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    if (!keyword) return [];
    return scopeNodes.filter((node) => node.label.toLowerCase().includes(keyword));
  }, [scopeNodes, search]);

  const matchIds = useMemo(() => matches.map((node) => node.id), [matches]);

  useEffect(() => {
    matchCursorRef.current = 0;
  }, [search]);

  const handleSearchKeyDown = (event: React.KeyboardEvent<HTMLInputElement>): void => {
    if (event.key === "Enter") {
      event.preventDefault();
      if (matches.length === 0) return;
      const index = matchCursorRef.current % matches.length;
      matchCursorRef.current = index + 1;
      const node = matches[index];
      setSelectedId(node.id);
      canvasRef.current?.focusNode(node.id);
    } else if (event.key === "Escape") {
      setSearch("");
    }
  };

  /* -------------------------------- 节点详情 ------------------------------- */

  const detailZoneId = useMemo(() => {
    if (graphMode !== "zones" || !selectedId) return undefined;
    const suffix = zoneSuffixFromNodeId(selectedId);
    if (suffix) return suffix;
    const node = graph?.nodes.find((item) => item.id === selectedId);
    return zoneToken(node?.zone_id ?? null);
  }, [graphMode, selectedId, graph]);

  useEffect(() => {
    if (!selectedId) {
      setNodeDetail(null);
      setDetailError(null);
      setDetailLoading(false);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    setDetailError(null);
    getGraphNode(selectedId, detailZoneId)
      .then((detail) => {
        if (!cancelled) setNodeDetail(detail);
      })
      .catch((err) => {
        if (!cancelled) {
          setNodeDetail(null);
          setDetailError(toUserMessage(err));
        }
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, detailSeed, detailZoneId]);

  useEffect(() => {
    if (!selectedId) return;
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") setSelectedId(null);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [selectedId]);

  const selectedNode: GraphNode | null = useMemo(() => {
    if (!selectedId) return null;
    const fromGraph = graph?.nodes.find((node) => node.id === selectedId);
    if (fromGraph) return fromGraph;
    return nodeDetail?.node ?? null;
  }, [graph, nodeDetail, selectedId]);

  /* -------------------------------- 交互动作 ------------------------------- */

  const handleSelect = useCallback((node: GraphNode | null) => {
    setSelectedId(node ? node.id : null);
  }, []);

  const handleSelectZone = useCallback((zoneId: string | null) => {
    const token = zoneId ? zoneId : UNASSIGNED_ZONE_ID;
    setActiveZone((current) => (current === token ? current : token));
  }, []);

  const toggleType = (type: GraphNodeType): void => {
    setTypeFilter((prev) => {
      const activeCount = Object.values(prev).filter(Boolean).length;
      if (prev[type] && activeCount === 1) return prev;
      return { ...prev, [type]: !prev[type] };
    });
  };

  const handleRelayout = (): void => {
    canvasRef.current?.relayout();
    if (relayoutTimerRef.current != null) window.clearTimeout(relayoutTimerRef.current);
    relayoutTimerRef.current = window.setTimeout(() => {
      canvasRef.current?.resetView();
    }, 620);
  };

  const askAboutSelected = (): void => {
    if (!selectedNode) return;
    const isDocument = selectedNode.type === "document";
    startChat({
      question: isDocument
        ? `请总结《${selectedNode.label}》的核心内容。`
        : `关于「${selectedNode.label}」，我的知识库中有哪些相关内容？`,
      docIds: isDocument && selectedNode.doc_id ? [selectedNode.doc_id] : null,
      docTitle: isDocument ? selectedNode.label : null,
    });
  };

  const handleRefresh = (): void => {
    setRefreshSeed((seed) => seed + 1);
    if (graphMode === "zones") setZonesSeed((seed) => seed + 1);
    toast("info", "正在刷新图谱", "重新读取最新的知识连接。");
  };

  const handleSwitchMode = (mode: GraphMode): void => {
    if (mode === graphMode) return;
    setGraphMode(mode);
    setManagerOpen(false);
  };

  const handleZoneChanged = useCallback(() => {
    bumpGraphRevision();
    setZonesSeed((seed) => seed + 1);
  }, [bumpGraphRevision]);

  const counts = viewData
    ? { nodes: viewData.nodes.length, links: viewData.links.length }
    : { nodes: 0, links: 0 };

  const isEmpty = graph != null && viewData != null && viewData.nodes.length === 0;
  const nodeDetailNode = nodeDetail?.node ?? null;
  const detailType = (selectedNode?.type ?? nodeDetailNode?.type ?? "concept") as GraphNodeType;
  const detailLabel = selectedNode?.label ?? nodeDetailNode?.label ?? "";
  const detailZoneName =
    selectedNode?.zone_name ??
    nodeDetailNode?.zone_name ??
    (graphMode === "zones" && selectedNode && (selectedNode.zone_id ?? null) === null
      ? UNASSIGNED_ZONE_NAME
      : null);
  const detailZoneColor =
    selectedNode?.zone_color ??
    nodeDetailNode?.zone_color ??
    (graphMode === "zones" ? UNASSIGNED_ZONE_COLOR : null);
  const showZoneGuide =
    graphMode === "zones" &&
    !zonesLoading &&
    !zonesError &&
    zoneList != null &&
    zoneList.items.length === 0;

  return (
    <div className={`graph-view${selectedId ? " drawer-open" : ""}`}>
      <GraphCanvas
        ref={canvasRef}
        data={viewData}
        reducedMotion={reducedMotion}
        selectedId={selectedId}
        searchMatches={matchIds}
        autoRotate={autoRotate}
        layoutMode={graphMode === "zones" ? "zones" : "global"}
        zoneMeta={zoneMetaForLayout}
        onSelect={handleSelect}
        onSelectZone={handleSelectZone}
      />

      {/* 悬浮顶栏：模式切换 / 分区 chips / 工具条 */}
      <div className="graph-toolbar-wrap">
        <div className="graph-mode-switch" role="tablist" aria-label="图谱模式">
          <button
            type="button"
            role="tab"
            aria-selected={graphMode === "global"}
            className={`graph-mode-tab${graphMode === "global" ? " active" : ""}`}
            onClick={() => handleSwitchMode("global")}
          >
            <Globe2 size={13} />
            全局图谱
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={graphMode === "zones"}
            className={`graph-mode-tab${graphMode === "zones" ? " active" : ""}`}
            onClick={() => handleSwitchMode("zones")}
          >
            <Layers size={13} />
            分区图谱
          </button>
        </div>

        {graphMode === "zones" ? (
          <div className="graph-zone-bar">
            <div className="graph-zone-chips" role="group" aria-label="分区筛选">
              <button
                type="button"
                className={`zone-chip${activeZone === ALL_ZONES ? " active" : ""}`}
                onClick={() => setActiveZone(ALL_ZONES)}
                aria-pressed={activeZone === ALL_ZONES}
              >
                全部区域
              </button>
              {zonesError && !zoneList ? (
                <button type="button" className="zone-chip error" onClick={() => setZonesSeed((seed) => seed + 1)}>
                  <RefreshCw size={11} />
                  分区加载失败，重试
                </button>
              ) : zonesLoading && !zoneList ? (
                <span className="zone-chip loading">
                  <LoaderCircle size={11} className="spin" />
                  加载分区…
                </span>
              ) : (
                zoneChips.map((zone) => (
                  <button
                    key={zone.id}
                    type="button"
                    className={`zone-chip${activeZone === zone.id ? " active" : ""}`}
                    onClick={() => setActiveZone(zone.id)}
                    aria-pressed={activeZone === zone.id}
                    title={zone.description ?? undefined}
                  >
                    <span className="zone-dot" style={{ background: zone.color }} />
                    <span className="zone-chip-name">{zone.name}</span>
                    <span className="zone-chip-count mono">{zone.document_count}</span>
                  </button>
                ))
              )}
              {unassignedCount > 0 || activeZone === UNASSIGNED_ZONE_ID ? (
                <button
                  type="button"
                  className={`zone-chip${activeZone === UNASSIGNED_ZONE_ID ? " active" : ""}`}
                  onClick={() => setActiveZone(UNASSIGNED_ZONE_ID)}
                  aria-pressed={activeZone === UNASSIGNED_ZONE_ID}
                >
                  <span className="zone-dot" style={{ background: UNASSIGNED_ZONE_COLOR }} />
                  <span className="zone-chip-name">{UNASSIGNED_ZONE_NAME}</span>
                  <span className="zone-chip-count mono">{unassignedCount}</span>
                </button>
              ) : null}
            </div>
            <button
              type="button"
              className="zone-chip manage zone-manage-btn"
              onClick={() => setManagerOpen(true)}
            >
              <Settings2 size={12} />
              管理分区
            </button>
          </div>
        ) : null}

        <div className="graph-toolbar">
          <div className="graph-search">
            <Search size={15} />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              onKeyDown={handleSearchKeyDown}
              placeholder="搜索节点…"
              aria-label="搜索图谱节点"
              spellCheck={false}
            />
            {search ? (
              <>
                <span className={`graph-search-count${matches.length === 0 ? " empty" : ""}`}>
                  {matches.length > 0 ? `${matches.length} 个匹配 · Enter 定位` : "无匹配"}
                </span>
                <button
                  type="button"
                  className="graph-search-clear"
                  onClick={() => setSearch("")}
                  aria-label="清空搜索"
                >
                  <X size={12} />
                </button>
              </>
            ) : null}
          </div>

          <div className="graph-toolbar-divider" />

          <div className="graph-filters" role="group" aria-label="节点类型筛选">
            {(Object.keys(TYPE_META) as GraphNodeType[]).map((type) => {
              const meta = TYPE_META[type];
              const active = typeFilter[type];
              const onlyActive =
                active && Object.values(typeFilter).filter(Boolean).length === 1;
              return (
                <button
                  key={type}
                  type="button"
                  className={`graph-filter-chip${active ? " active" : ""}`}
                  onClick={() => toggleType(type)}
                  aria-pressed={active}
                  disabled={onlyActive}
                  title={onlyActive ? "至少保留一种节点类型" : `切换${meta.label}显示`}
                >
                  <span className="filter-dot" style={{ background: meta.color }} />
                  {meta.label}
                  <span className="filter-count mono">{typeCounts[type]}</span>
                </button>
              );
            })}
          </div>

          <div className="graph-toolbar-divider" />

          <button
            type="button"
            className="graph-tool-btn"
            onClick={handleRelayout}
            data-tip="重新布局"
          >
            <RotateCcw size={14} />
            <span>重新布局</span>
          </button>
          <button
            type="button"
            className="graph-tool-btn"
            onClick={handleRefresh}
            data-tip="刷新图谱"
          >
            <RefreshCw size={14} className={loading ? "spin" : undefined} />
            <span>刷新</span>
          </button>
          <button
            type="button"
            className={`graph-tool-btn${autoRotate && !reducedMotion ? " active" : ""}`}
            onClick={() => setAutoRotate((value) => !value)}
            aria-pressed={autoRotate && !reducedMotion}
            disabled={reducedMotion}
            data-tip={reducedMotion ? "系统已开启减弱动效" : "自动旋转视角"}
          >
            <RotateCw size={14} className={autoRotate && !reducedMotion ? "auto-rotate-icon" : undefined} />
            <span>自动旋转</span>
          </button>
        </div>
      </div>

      {/* 左下角图例 */}
      <div className="graph-legend">
        <div className="legend-title">图例</div>
        {(Object.keys(TYPE_META) as GraphNodeType[]).map((type) => {
          const meta = TYPE_META[type];
          const Icon = meta.icon;
          return (
            <button
              key={type}
              type="button"
              className={`legend-item${typeFilter[type] ? "" : " off"}`}
              onClick={() => toggleType(type)}
              aria-pressed={typeFilter[type]}
            >
              <span className="legend-dot" style={{ background: meta.color, boxShadow: `0 0 8px ${meta.color}` }} />
              <Icon size={11} style={{ color: meta.color }} />
              <span>{meta.label}</span>
              <span className="legend-count mono">{typeCounts[type]}</span>
            </button>
          );
        })}
        <div className="legend-hint">
          拖拽旋转 · 滚轮缩放
          <br />
          {graphMode === "zones"
            ? "点击区域标签聚焦 · 双击复位"
            : "点击节点查看详情 · 双击复位"}
          <span className="legend-note">关键词来自文档标签与 AI 整理</span>
        </div>
      </div>

      {/* 右下角计数与缩放 */}
      <div className="graph-hud">
        <div className="graph-counts">
          {graphMode === "zones" ? (
            <>
              <span className="mono">{formatNumber(activeZone === ALL_ZONES ? visibleZoneCount : 1)}</span> 区域
              <span className="count-sep" />
            </>
          ) : null}
          <span className="mono">{formatNumber(counts.nodes)}</span> 节点
          <span className="count-sep" />
          <span className="mono">{formatNumber(counts.links)}</span> 连接
          {reducedMotion ? (
            <>
              <span className="count-sep" />
              <span className="reduced-motion-tag">已减弱动效</span>
            </>
          ) : null}
        </div>
        <div className="graph-zoom-controls">
          <button
            type="button"
            onClick={() => canvasRef.current?.zoomOut()}
            aria-label="缩小"
            data-tip="缩小"
          >
            <ZoomOut size={15} />
          </button>
          <button
            type="button"
            onClick={() => canvasRef.current?.resetView()}
            aria-label="重置视图"
            data-tip="重置视图"
          >
            <LocateFixed size={15} />
          </button>
          <button
            type="button"
            onClick={() => canvasRef.current?.zoomIn()}
            aria-label="放大"
            data-tip="放大"
          >
            <ZoomIn size={15} />
          </button>
        </div>
      </div>

      {/* 空分区引导 */}
      {showZoneGuide ? (
        <div className="graph-zone-empty-card">
          <div className="graph-zone-empty-icon">
            <FolderPlus size={17} />
          </div>
          <div className="graph-zone-empty-text">
            <div className="graph-zone-empty-title">还没有分区</div>
            <div className="graph-zone-empty-desc">
              创建分区，把相关的文档放进去，让图谱更清晰。
            </div>
          </div>
          <button type="button" className="btn btn-sm btn-primary" onClick={() => setManagerOpen(true)}>
            新建分区
          </button>
        </div>
      ) : null}

      {/* 加载 / 错误 / 空状态 */}
      {loading && !graph ? (
        <div className="graph-overlay">
          <LoaderCircle size={24} className="spin" />
          <span>正在编织知识网络…</span>
        </div>
      ) : null}

      {error && !graph ? (
        <div className="graph-overlay graph-overlay-error">
          <div className="graph-error-card">
            <Waypoints size={26} style={{ color: "var(--rose)" }} />
            <div className="graph-error-title">知识图谱加载失败</div>
            <div className="graph-error-desc">{error}</div>
            <button type="button" className="btn" onClick={() => setRefreshSeed((seed) => seed + 1)}>
              <RefreshCw size={13} /> 重新加载
            </button>
          </div>
        </div>
      ) : null}

      {isEmpty ? (
        <div className="graph-overlay">
          <div className="graph-empty-card card">
            <EmptyState
              variant="graph"
              title="暂时没有可展示的节点"
              description={
                graphMode === "zones"
                  ? "当前区域还没有节点，或筛选条件过滤掉了所有节点。"
                  : "知识库还没有文档，或当前筛选条件过滤掉了所有节点。"
              }
              actions={
                <>
                  {graphMode === "zones" ? (
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={() => setActiveZone(ALL_ZONES)}
                    >
                      查看全部区域
                    </button>
                  ) : (
                    <button type="button" className="btn btn-primary" onClick={() => navigate("/library")}>
                      去导入文档
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn"
                    onClick={() => setTypeFilter({ document: true, concept: true, tag: true })}
                  >
                    重置筛选
                  </button>
                </>
              }
            />
          </div>
        </div>
      ) : null}

      {error && graph ? (
        <div className="graph-error-banner">
          <RefreshCw size={13} />
          <span>{error}</span>
          <button type="button" onClick={() => setRefreshSeed((seed) => seed + 1)}>
            重试
          </button>
        </div>
      ) : null}

      {/* 右侧节点详情抽屉 */}
      {selectedId ? (
        <aside className="graph-drawer" aria-label="节点详情">
          <div className="graph-drawer-head">
            <div className="graph-drawer-head-badges">
              <span className={`node-type-pill ${detailType}`}>
                {(() => {
                  const Icon = TYPE_META[detailType].icon;
                  return <Icon size={12} />;
                })()}
                {NODE_TYPE_LABEL[detailType]}
              </span>
              {graphMode === "zones" && detailZoneName ? (
                <span className="graph-drawer-zone">
                  <span
                    className="zone-dot"
                    style={{ background: detailZoneColor ?? UNASSIGNED_ZONE_COLOR }}
                  />
                  {detailZoneName}
                </span>
              ) : null}
            </div>
            <button
              type="button"
              className="btn btn-icon"
              onClick={() => setSelectedId(null)}
              aria-label="关闭节点详情"
            >
              <X size={16} />
            </button>
          </div>

          <div className="graph-drawer-body">
            <div>
              <h2 className="graph-node-title">{detailLabel}</h2>
              <div className="graph-node-meta">
                <span>
                  <b className="mono">{formatNumber(selectedNode?.degree ?? nodeDetailNode?.degree ?? 0)}</b>{" "}
                  连接
                </span>
                <span className="node-meta-sep" />
                <span>
                  权重{" "}
                  <b className="mono">
                    {(selectedNode?.weight ?? nodeDetailNode?.weight ?? 0).toFixed(1)}
                  </b>
                </span>
              </div>
            </div>

            {detailLoading ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                <Skeleton width="100%" height={54} radius={12} />
                <SkeletonRows rows={3} />
              </div>
            ) : detailError ? (
              <div className="page-error">
                <X size={15} />
                <div style={{ flex: 1 }}>
                  <div>{detailError}</div>
                  <button
                    type="button"
                    className="btn btn-sm"
                    style={{ marginTop: 10 }}
                    onClick={() => setDetailSeed((seed) => seed + 1)}
                  >
                    <RefreshCw size={12} /> 重试
                  </button>
                </div>
              </div>
            ) : nodeDetail ? (
              <>
                {nodeDetail.node.summary ? (
                  <div className="graph-node-summary">{nodeDetail.node.summary}</div>
                ) : null}

                {nodeDetail.node.tags && nodeDetail.node.tags.length > 0 ? (
                  <div className="graph-node-tags">
                    {nodeDetail.node.tags.map((tag) => (
                      <span key={tag} className="tag-chip">
                        {tag}
                      </span>
                    ))}
                  </div>
                ) : null}

                {nodeDetail.related.length > 0 ? (
                  <section>
                    <div className="graph-section-title">相关节点</div>
                    <div className="graph-related">
                      {nodeDetail.related.map((related) => (
                        <button
                          key={related.id}
                          type="button"
                          className="related-chip"
                          onClick={() => {
                            setSelectedId(related.id);
                            canvasRef.current?.focusNode(related.id);
                          }}
                        >
                          <span
                            className="related-dot"
                            style={{ background: TYPE_META[related.type]?.color ?? "#A78BFA" }}
                          />
                          {related.label}
                        </button>
                      ))}
                    </div>
                  </section>
                ) : null}

                {nodeDetail.docs.length > 0 ? (
                  <section>
                    <div className="graph-section-title">关联文档 · {nodeDetail.docs.length}</div>
                    <div className="graph-doc-list">
                      {nodeDetail.docs.slice(0, 5).map((doc) => (
                        <button
                          key={doc.id}
                          type="button"
                          className="graph-doc-item"
                          onClick={() => navigate("/library", { query: { doc: doc.id } })}
                        >
                          <FileText size={13} />
                          <span className="graph-doc-text">
                            <span className="graph-doc-title">{doc.title}</span>
                            {doc.summary ? (
                              <span className="graph-doc-summary">{doc.summary}</span>
                            ) : null}
                          </span>
                        </button>
                      ))}
                    </div>
                  </section>
                ) : null}

                {nodeDetail.chunks.length > 0 ? (
                  <section>
                    <div className="graph-section-title">相关片段 · {nodeDetail.chunks.length}</div>
                    <div className="graph-chunk-list">
                      {nodeDetail.chunks.slice(0, 4).map((chunk) => (
                        <div key={chunk.id} className="graph-chunk">
                          <div className="graph-chunk-doc">{chunk.doc_title}</div>
                          <p className="graph-chunk-text">{formatExcerpt(chunk.text)}</p>
                        </div>
                      ))}
                    </div>
                  </section>
                ) : null}
              </>
            ) : null}
          </div>

          <div className="graph-drawer-foot">
            <button type="button" className="btn btn-primary btn-block" onClick={askAboutSelected}>
              <MessageSquare size={14} />
              就这个主题提问
            </button>
          </div>
        </aside>
      ) : null}

      <ZoneManager
        open={managerOpen}
        onClose={() => setManagerOpen(false)}
        zoneList={zoneList}
        zonesLoading={zonesLoading}
        zonesError={zonesError}
        onRefreshZones={() => setZonesSeed((seed) => seed + 1)}
        onChanged={handleZoneChanged}
      />

      <span className="sr-only" aria-live="polite">
        {graphMode === "zones" ? `分区图谱，当前 ${activeZone === ALL_ZONES ? "全部区域" : "单选区域"}，${counts.nodes} 个节点` : ""}
      </span>
    </div>
  );
}
