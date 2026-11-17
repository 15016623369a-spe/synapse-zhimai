import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type ForwardedRef,
} from "react";
import { GraphEngine } from "./GraphEngine";
import { Graph3DEngine, type GraphLayoutMode } from "./Graph3DEngine";
import type { GraphData, GraphNode, GraphNodeType, GraphZoneMeta } from "../../types";

export interface GraphCanvasHandle {
  resetView: () => void;
  zoomIn: () => void;
  zoomOut: () => void;
  relayout: () => void;
  focusNode: (id: string) => void;
  focusFirstMatch: () => void;
  selectNode: (id: string | null) => void;
}

interface GraphCanvasProps {
  data: GraphData | null;
  reducedMotion: boolean;
  selectedId: string | null;
  searchMatches: string[];
  autoRotate?: boolean;
  layoutMode?: GraphLayoutMode;
  zoneMeta?: GraphZoneMeta[];
  onSelect: (node: GraphNode | null) => void;
  onSelectZone?: (zoneId: string | null) => void;
}

interface GraphEngineLike {
  setData: (nodes: GraphNode[], links: GraphData["links"]) => void;
  setReducedMotion: (value: boolean) => void;
  setSearchMatches: (ids: string[]) => void;
  selectById: (id: string | null) => void;
  focusNode: (id: string) => void;
  focusFirstMatch: () => void;
  relayout: () => void;
  resize: (width: number, height: number, dpr: number) => void;
  resetView: () => void;
  zoomBy: (factor: number) => void;
  setAutoRotate?: (value: boolean) => void;
  setLayoutMode?: (mode: GraphLayoutMode) => void;
  setZoneMeta?: (zones: GraphZoneMeta[]) => void;
  destroy: () => void;
}

const TYPE_LABELS: Record<GraphNodeType, string> = {
  document: "文档",
  concept: "概念",
  tag: "标签",
};

function supportsWebGL(): boolean {
  try {
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
    return Boolean(context);
  } catch {
    return false;
  }
}

function GraphCanvasInner(
  {
    data,
    reducedMotion,
    selectedId,
    searchMatches,
    autoRotate = true,
    layoutMode = "global",
    zoneMeta,
    onSelect,
    onSelectZone,
  }: GraphCanvasProps,
  ref: ForwardedRef<GraphCanvasHandle>,
) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const fallbackCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const engineRef = useRef<GraphEngineLike | null>(null);
  const [mode, setMode] = useState<"pending" | "3d" | "2d">("pending");
  const [hoverNode, setHoverNode] = useState<GraphNode | null>(null);

  const tooltipRef = useRef<HTMLDivElement | null>(null);
  const hoverIdRef = useRef<string | null>(null);
  const hoverPointRef = useRef<{ x: number; y: number } | null>(null);

  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;
  const reducedMotionRef = useRef(reducedMotion);
  reducedMotionRef.current = reducedMotion;
  const autoRotateRef = useRef(autoRotate);
  autoRotateRef.current = autoRotate;
  const layoutModeRef = useRef(layoutMode);
  layoutModeRef.current = layoutMode;
  const zoneMetaRef = useRef<GraphZoneMeta[]>(zoneMeta ?? []);
  zoneMetaRef.current = zoneMeta ?? [];
  const onSelectZoneRef = useRef(onSelectZone);
  onSelectZoneRef.current = onSelectZone;

  const positionTooltip = useCallback((point: { x: number; y: number } | null) => {
    const element = tooltipRef.current;
    if (!element || !point) return;
    const container = containerRef.current;
    const width = container?.clientWidth ?? 0;
    const height = container?.clientHeight ?? 0;
    const tooltipWidth = element.offsetWidth || 210;
    const tooltipHeight = element.offsetHeight || 74;
    const x = Math.min(Math.max(8, point.x + 18), Math.max(8, width - tooltipWidth - 10));
    const y = Math.min(Math.max(8, point.y + 16), Math.max(8, height - tooltipHeight - 10));
    element.style.transform = `translate3d(${Math.round(x)}px, ${Math.round(y)}px, 0)`;
  }, []);

  const handleHover = useCallback(
    (node: GraphNode | null, point: { x: number; y: number } | null) => {
      const nextId = node?.id ?? null;
      if (nextId !== hoverIdRef.current) {
        hoverIdRef.current = nextId;
        setHoverNode(node);
        hoverPointRef.current = point;
        window.requestAnimationFrame(() => positionTooltip(point));
      } else if (point) {
        hoverPointRef.current = point;
        positionTooltip(point);
      }
    },
    [positionTooltip],
  );

  useEffect(() => {
    if (!hoverNode) return undefined;
    const frame = window.requestAnimationFrame(() =>
      positionTooltip(hoverPointRef.current),
    );
    return () => window.cancelAnimationFrame(frame);
  }, [hoverNode, positionTooltip]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    let engine: GraphEngineLike | null = null;
    let actualMode: "3d" | "2d" = "3d";

    if (supportsWebGL()) {
      try {
        engine = new Graph3DEngine({
          container,
          onSelect: (node) => onSelectRef.current(node),
          onHover: handleHover,
          onSelectZone: (zoneId) => onSelectZoneRef.current?.(zoneId),
          reducedMotion: reducedMotionRef.current,
        });
      } catch {
        engine = null;
      }
    }

    if (!engine) {
      actualMode = "2d";
      const canvas = fallbackCanvasRef.current;
      if (canvas) {
        engine = new GraphEngine({
          canvas,
          onSelect: (node) => onSelectRef.current(node),
          reducedMotion: reducedMotionRef.current,
        });
      }
    }

    engineRef.current = engine;
    setMode(actualMode);

    if (engine) {
      engine.setAutoRotate?.(autoRotateRef.current);
      engine.setLayoutMode?.(layoutModeRef.current);
      engine.setZoneMeta?.(zoneMetaRef.current);
      const applySize = (): void => {
        const rect = container.getBoundingClientRect();
        engine?.resize(
          Math.max(240, rect.width),
          Math.max(240, rect.height),
          window.devicePixelRatio || 1,
        );
      };
      applySize();
      const observer = new ResizeObserver(() => applySize());
      observer.observe(container);

      return () => {
        observer.disconnect();
        engine?.destroy();
        engineRef.current = null;
      };
    }

    return () => {
      engineRef.current = null;
    };
  }, [handleHover]);

  useEffect(() => {
    const engine = engineRef.current;
    if (!engine || !data) return;
    engine.setLayoutMode?.(layoutMode);
    engine.setZoneMeta?.(zoneMeta ?? []);
    engine.setData(data.nodes, data.links);
    engine.selectById(selectedId ?? null);
    engine.setSearchMatches(searchMatches);
    engine.setAutoRotate?.(autoRotate);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, layoutMode, zoneMeta]);

  useEffect(() => {
    engineRef.current?.setReducedMotion(reducedMotion);
  }, [reducedMotion]);

  useEffect(() => {
    engineRef.current?.selectById(selectedId);
  }, [selectedId]);

  useEffect(() => {
    engineRef.current?.setSearchMatches(searchMatches);
  }, [searchMatches]);

  useEffect(() => {
    engineRef.current?.setAutoRotate?.(autoRotate);
  }, [autoRotate]);

  useImperativeHandle(
    ref,
    () => ({
      resetView: () => engineRef.current?.resetView(),
      zoomIn: () => engineRef.current?.zoomBy(1.28),
      zoomOut: () => engineRef.current?.zoomBy(1 / 1.28),
      relayout: () => engineRef.current?.relayout(),
      focusNode: (id: string) => engineRef.current?.focusNode(id),
      focusFirstMatch: () => engineRef.current?.focusFirstMatch(),
      selectNode: (id: string | null) => engineRef.current?.selectById(id),
    }),
    [],
  );

  return (
    <div
      className="graph-canvas-wrap"
      ref={containerRef}
      role="application"
      aria-label="知识图谱画布"
    >
      <canvas
        ref={fallbackCanvasRef}
        className="graph-canvas"
        style={{ display: mode === "2d" ? "block" : "none" }}
        aria-hidden={mode !== "2d"}
      />
      {hoverNode ? (
        <div className="graph3d-tooltip" ref={tooltipRef} role="tooltip">
          <div className="graph3d-tooltip-row">
            <span className={`node-type-pill ${hoverNode.type}`}>{TYPE_LABELS[hoverNode.type]}</span>
            <span className="graph3d-tooltip-degree">
              <b>{hoverNode.degree}</b> 连接
            </span>
          </div>
          <div className="graph3d-tooltip-title">{hoverNode.label}</div>
          {hoverNode.zone_name ? (
            <div className="graph3d-tooltip-zone">
              <span
                className="zone-dot"
                style={{ background: hoverNode.zone_color ?? "#5B6F8C" }}
              />
              {hoverNode.zone_name}
            </div>
          ) : null}
          <div className="graph3d-tooltip-meta">权重 {hoverNode.weight.toFixed(1)}</div>
        </div>
      ) : null}
    </div>
  );
}

export const GraphCanvas = forwardRef<GraphCanvasHandle, GraphCanvasProps>(GraphCanvasInner);
