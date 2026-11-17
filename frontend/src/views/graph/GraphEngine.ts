import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";
import type { GraphLink, GraphNode, GraphNodeType } from "../../types";

export interface EngineNode extends GraphNode, SimulationNodeDatum {
  x: number;
  y: number;
  vx: number;
  vy: number;
  fx: number | null;
  fy: number | null;
  radius: number;
  pinned: boolean;
  sx: number;
  sy: number;
  sr: number;
  labelWidth: number;
}

export interface EngineLink extends SimulationLinkDatum<EngineNode> {
  id: string;
  type: string;
  weight: number;
  source: EngineNode | string;
  target: EngineNode | string;
  curve: number;
}

export interface EngineOptions {
  canvas: HTMLCanvasElement;
  onSelect: (node: GraphNode | null) => void;
  reducedMotion: boolean;
}

interface Pulse {
  link: EngineLink;
  t: number;
  speed: number;
  color: string;
}

interface ViewTransform {
  scale: number;
  tx: number;
  ty: number;
}

const TYPE_COLORS: Record<GraphNodeType, string> = {
  document: "#4FD8FF",
  concept: "#A78BFA",
  tag: "#FBBF24",
};

const TYPE_RADIUS: Record<GraphNodeType, [number, number]> = {
  document: [7, 14],
  concept: [3.5, 9],
  tag: [4.5, 8],
};

const LINK_COLORS: Record<string, string> = {
  mentions: "120, 180, 255",
  tagged: "251, 191, 36",
  related: "79, 216, 255",
  cooccur: "167, 139, 250",
};

const LINK_BASE_ALPHA: Record<string, number> = {
  mentions: 0.24,
  tagged: 0.18,
  related: 0.32,
  cooccur: 0.16,
};

const FONT_FAMILY =
  '-apple-system, BlinkMacSystemFont, "SF Pro SC", "PingFang SC", "Noto Sans SC", sans-serif';

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function hashString(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) / 4294967295;
}

function hexToRgb(hex: string): [number, number, number] {
  const value = hex.replace("#", "");
  return [
    parseInt(value.slice(0, 2), 16),
    parseInt(value.slice(2, 4), 16),
    parseInt(value.slice(4, 6), 16),
  ];
}

function rgba(hex: string, alpha: number): string {
  const [red, green, blue] = hexToRgb(hex);
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}

function mixColor(hex: string, target: [number, number, number], ratio: number): string {
  const [red, green, blue] = hexToRgb(hex);
  return `rgb(${Math.round(red + (target[0] - red) * ratio)}, ${Math.round(
    green + (target[1] - green) * ratio,
  )}, ${Math.round(blue + (target[2] - blue) * ratio)})`;
}

function createGlowSprite(color: string, size = 96): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const context = canvas.getContext("2d");
  if (context) {
    const center = size / 2;
    const gradient = context.createRadialGradient(center, center, 0, center, center, center);
    gradient.addColorStop(0, rgba(color, 0.62));
    gradient.addColorStop(0.28, rgba(color, 0.26));
    gradient.addColorStop(0.6, rgba(color, 0.08));
    gradient.addColorStop(1, rgba(color, 0));
    context.fillStyle = gradient;
    context.fillRect(0, 0, size, size);
  }
  return canvas;
}

function createCoreSprite(color: string, radius: number, size = 96): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const context = canvas.getContext("2d");
  if (context) {
    const center = size / 2;
    const gradient = context.createRadialGradient(
      center - radius * 0.24,
      center - radius * 0.3,
      radius * 0.1,
      center,
      center,
      radius,
    );
    gradient.addColorStop(0, mixColor(color, [255, 255, 255], 0.86));
    gradient.addColorStop(0.5, mixColor(color, [255, 255, 255], 0.16));
    gradient.addColorStop(1, mixColor(color, [8, 14, 26], 0.4));
    context.fillStyle = gradient;
    context.beginPath();
    context.arc(center, center, radius, 0, Math.PI * 2);
    context.fill();
  }
  return canvas;
}

export class GraphEngine {
  private readonly canvas: HTMLCanvasElement;
  private readonly context: CanvasRenderingContext2D;
  private readonly onSelect: (node: GraphNode | null) => void;
  private reducedMotion: boolean;

  private nodes: EngineNode[] = [];
  private links: EngineLink[] = [];
  private nodeById = new Map<string, EngineNode>();
  private simulation: Simulation<EngineNode, EngineLink> | null = null;

  private width = 1000;
  private height = 700;
  private dpr = 1;

  private view: ViewTransform = { scale: 1, tx: 0, ty: 0 };
  private viewTarget: ViewTransform | null = null;

  private hoveredId: string | null = null;
  private selectedId: string | null = null;
  private focusIds = new Set<string>();
  private lastFocusKey = "";
  private searchMatches = new Set<string>();

  private dragNode: EngineNode | null = null;
  private dragMoved = false;
  private panning = false;
  private pointerStart = { x: 0, y: 0 };
  private viewStart: ViewTransform = { scale: 1, tx: 0, ty: 0 };

  private rafId: number | null = null;
  private lastFrame = 0;
  private dirty = true;

  private pulses: Pulse[] = [];
  private glowSprites: Record<GraphNodeType, HTMLCanvasElement>;
  private coreSprites: Record<GraphNodeType, HTMLCanvasElement>;

  private disposed = false;

  constructor(options: EngineOptions) {
    this.canvas = options.canvas;
    const context = this.canvas.getContext("2d");
    if (!context) throw new Error("当前浏览器不支持 Canvas 2D");
    this.context = context;
    this.onSelect = options.onSelect;
    this.reducedMotion = options.reducedMotion;

    this.glowSprites = {
      document: createGlowSprite(TYPE_COLORS.document),
      concept: createGlowSprite(TYPE_COLORS.concept),
      tag: createGlowSprite(TYPE_COLORS.tag),
    };
    this.coreSprites = {
      document: createCoreSprite(TYPE_COLORS.document, 30),
      concept: createCoreSprite(TYPE_COLORS.concept, 30),
      tag: createCoreSprite(TYPE_COLORS.tag, 30),
    };

    this.canvas.addEventListener("pointerdown", this.handlePointerDown);
    this.canvas.addEventListener("pointermove", this.handlePointerMove);
    this.canvas.addEventListener("pointerup", this.handlePointerUp);
    this.canvas.addEventListener("pointercancel", this.handlePointerUp);
    this.canvas.addEventListener("pointerleave", this.handlePointerLeave);
    this.canvas.addEventListener("wheel", this.handleWheel, { passive: false });
    this.canvas.addEventListener("dblclick", this.handleDoubleClick);

    this.lastFrame = performance.now();
    this.rafId = requestAnimationFrame(this.loop);
  }

  /* ------------------------------- 数据与布局 ------------------------------ */

  setData(nodes: GraphNode[], links: GraphLink[]): void {
    if (this.disposed) return;
    this.simulation?.stop();
    this.nodeById.clear();

    const maxMetric: Record<GraphNodeType, number> = { document: 1, concept: 1, tag: 1 };
    for (const node of nodes) {
      const metric = Math.max(node.weight ?? 1, (node.degree ?? 1) * 0.9);
      maxMetric[node.type] = Math.max(maxMetric[node.type], metric);
    }

    const centerX = this.width / 2;
    const centerY = this.height / 2;
    this.nodes = nodes.map((node, index) => {
      const [minRadius, maxRadius] = TYPE_RADIUS[node.type];
      const metric = Math.max(node.weight ?? 1, (node.degree ?? 1) * 0.9);
      const ratio = clamp(Math.sqrt(metric / maxMetric[node.type]), 0, 1);
      const radius = minRadius + (maxRadius - minRadius) * ratio;
      const angle = index * 2.399963229728653 + hashString(node.id) * 1.8;
      const distance = 26 + Math.sqrt(index + 1) * 34;
      const engineNode: EngineNode = {
        ...node,
        radius,
        pinned: false,
        sx: 0,
        sy: 0,
        sr: radius,
        labelWidth: 0,
        x: centerX + Math.cos(angle) * distance * 1.05,
        y: centerY + Math.sin(angle) * distance * 0.85,
        vx: 0,
        vy: 0,
        fx: null,
        fy: null,
      };
      this.nodeById.set(engineNode.id, engineNode);
      return engineNode;
    });

    this.links = links.map((link) => ({
      ...link,
      source: link.source,
      target: link.target,
      curve: (hashString(link.id) - 0.5) * 1.7,
    }));

    this.buildPulses();

    const resolve = (endpoint: EngineNode | string): EngineNode | undefined =>
      typeof endpoint === "string" ? this.nodeById.get(endpoint) : endpoint;

    const linkDistance = (link: EngineLink): number => {
      const source = resolve(link.source);
      const target = resolve(link.target);
      const base =
        link.type === "related"
          ? 178
          : link.type === "tagged"
            ? 80
            : link.type === "cooccur"
              ? 70
              : 94;
      return base + (source?.radius ?? 5) * 2.4 + (target?.radius ?? 5) * 2.4;
    };

    const linkStrength = (link: EngineLink): number => {
      const weightFactor = clamp(link.weight ?? 0.5, 0.15, 1);
      const base = link.type === "related" ? 0.22 : link.type === "cooccur" ? 0.2 : 0.5;
      return base * (0.55 + weightFactor * 0.65);
    };

    const simulation = forceSimulation<EngineNode>(this.nodes)
      .force(
        "link",
        forceLink<EngineNode, EngineLink>(this.links)
          .id((node) => node.id)
          .distance(linkDistance)
          .strength(linkStrength),
      )
      .force(
        "charge",
        forceManyBody<EngineNode>()
          .strength((node) =>
            node.type === "document" ? -380 : node.type === "tag" ? -165 : -105,
          )
          .distanceMax(600),
      )
      .force("center", forceCenter(this.width / 2, this.height / 2))
      .force(
        "collide",
        forceCollide<EngineNode>((node) => node.radius + 9).strength(0.92),
      )
      .force("x", forceX<EngineNode>(this.width / 2).strength(0.03))
      .force("y", forceY<EngineNode>(this.height / 2).strength(0.05))
      .alphaDecay(0.022)
      .velocityDecay(0.34)
      .on("tick", () => {
        this.dirty = true;
      });

    simulation.stop();
    // 先预跑若干步，首屏就呈现清晰结构，随后再以较低 alpha 继续微调。
    for (let step = 0; step < 140; step += 1) simulation.tick();
    this.simulation = simulation;
    this.dirty = true;
    this.resetView();
    simulation.alpha(0.6).restart();
  }

  setReducedMotion(value: boolean): void {
    this.reducedMotion = value;
    this.dirty = true;
  }

  setSearchMatches(ids: string[]): void {
    this.searchMatches = new Set(ids);
    this.dirty = true;
  }

  selectById(id: string | null): void {
    this.selectedId = id;
    this.dirty = true;
  }

  focusNode(id: string): void {
    const node = this.nodeById.get(id);
    if (!node) return;
    const scale = Math.max(this.view.scale, 1.25);
    this.animateView({
      scale,
      tx: this.width / 2 - (node.x ?? 0) * scale,
      ty: this.height / 2 - (node.y ?? 0) * scale,
    });
  }

  focusFirstMatch(): void {
    for (const node of this.nodes) {
      if (this.searchMatches.has(node.id)) {
        this.selectById(node.id);
        this.focusNode(node.id);
        return;
      }
    }
  }

  relayout(): void {
    const centerX = this.width / 2;
    const centerY = this.height / 2;
    this.nodes.forEach((node, index) => {
      const angle = Math.random() * Math.PI * 2;
      const distance = 40 + Math.sqrt(index + 1) * 46;
      node.x = centerX + Math.cos(angle) * distance;
      node.y = centerY + Math.sin(angle) * distance * 0.8;
      node.vx = 0;
      node.vy = 0;
      node.fx = null;
      node.fy = null;
      node.pinned = false;
    });
    if (this.simulation) {
      this.simulation.alpha(1).restart();
    }
    this.dirty = true;
  }

  resize(width: number, height: number, dpr: number): void {
    this.width = Math.max(320, width);
    this.height = Math.max(320, height);
    this.dpr = dpr;
    this.canvas.width = Math.round(this.width * dpr);
    this.canvas.height = Math.round(this.height * dpr);
    this.canvas.style.width = `${this.width}px`;
    this.canvas.style.height = `${this.height}px`;
    if (this.simulation) {
      this.simulation.force("center", forceCenter(this.width / 2, this.height / 2));
      this.simulation.force("x", forceX<EngineNode>(this.width / 2).strength(0.03));
      this.simulation.force("y", forceY<EngineNode>(this.height / 2).strength(0.05));
      this.simulation.alpha(0.3).restart();
    }
    this.dirty = true;
  }

  resetView(): void {
    if (this.nodes.length === 0) {
      this.view = { scale: 1, tx: this.width / 2, ty: this.height / 2 };
      this.viewTarget = null;
      this.dirty = true;
      return;
    }
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const node of this.nodes) {
      minX = Math.min(minX, (node.x ?? 0) - node.radius);
      minY = Math.min(minY, (node.y ?? 0) - node.radius);
      maxX = Math.max(maxX, (node.x ?? 0) + node.radius);
      maxY = Math.max(maxY, (node.y ?? 0) + node.radius);
    }
    const boundsWidth = Math.max(60, maxX - minX);
    const boundsHeight = Math.max(60, maxY - minY);
    const scale = clamp(
      Math.min((this.width - 140) / boundsWidth, (this.height - 150) / boundsHeight),
      0.55,
      1.5,
    );
    const centerX = (minX + maxX) / 2;
    const centerY = (minY + maxY) / 2;
    this.view = {
      scale,
      tx: this.width / 2 - centerX * scale,
      ty: this.height / 2 - centerY * scale,
    };
    this.viewTarget = null;
    this.dirty = true;
  }

  zoomBy(factor: number): void {
    const nextScale = clamp(this.view.scale * factor, 0.35, 2.5);
    const ratio = nextScale / this.view.scale;
    const centerX = this.width / 2;
    const centerY = this.height / 2;
    this.viewTarget = null;
    this.view = {
      scale: nextScale,
      tx: centerX - (centerX - this.view.tx) * ratio,
      ty: centerY - (centerY - this.view.ty) * ratio,
    };
    this.dirty = true;
  }

  getZoom(): number {
    return this.view.scale;
  }

  getCounts(): { nodes: number; links: number } {
    return { nodes: this.nodes.length, links: this.links.length };
  }

  destroy(): void {
    this.disposed = true;
    if (this.rafId != null) cancelAnimationFrame(this.rafId);
    this.simulation?.stop();
    this.simulation = null;
    this.canvas.removeEventListener("pointerdown", this.handlePointerDown);
    this.canvas.removeEventListener("pointermove", this.handlePointerMove);
    this.canvas.removeEventListener("pointerup", this.handlePointerUp);
    this.canvas.removeEventListener("pointercancel", this.handlePointerUp);
    this.canvas.removeEventListener("pointerleave", this.handlePointerLeave);
    this.canvas.removeEventListener("wheel", this.handleWheel);
    this.canvas.removeEventListener("dblclick", this.handleDoubleClick);
  }

  /* -------------------------------- 交互事件 ------------------------------- */

  private toLocalPoint(event: PointerEvent | WheelEvent | MouseEvent): { x: number; y: number } {
    const rect = this.canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  private screenToWorld(x: number, y: number): { x: number; y: number } {
    return {
      x: (x - this.view.tx) / this.view.scale,
      y: (y - this.view.ty) / this.view.scale,
    };
  }

  private hitTest(x: number, y: number): EngineNode | null {
    let closest: EngineNode | null = null;
    let closestDistance = Infinity;
    for (const node of this.nodes) {
      const dx = x - node.sx;
      const dy = y - node.sy;
      const distance = Math.hypot(dx, dy);
      const threshold = Math.max(node.sr + 10, 14);
      if (distance <= threshold && distance < closestDistance) {
        closest = node;
        closestDistance = distance;
      }
    }
    return closest;
  }

  private updateFocus(): void {
    this.focusIds.clear();
    const focusId = this.hoveredId ?? this.selectedId;
    if (!focusId) return;
    this.focusIds.add(focusId);
    for (const link of this.links) {
      const source = this.resolveNode(link.source);
      const target = this.resolveNode(link.target);
      if (!source || !target) continue;
      if (source.id === focusId) this.focusIds.add(target.id);
      if (target.id === focusId) this.focusIds.add(source.id);
    }
  }

  private updateCursor(): void {
    if (this.dragNode || this.panning) {
      this.canvas.style.cursor = "grabbing";
    } else if (this.hoveredId) {
      this.canvas.style.cursor = "pointer";
    } else {
      this.canvas.style.cursor = "grab";
    }
  }

  private handlePointerDown = (event: PointerEvent): void => {
    if (event.button !== 0) return;
    const point = this.toLocalPoint(event);
    this.canvas.setPointerCapture(event.pointerId);
    this.pointerStart = point;
    this.viewStart = { ...this.view };
    const node = this.hitTest(point.x, point.y);
    if (node) {
      this.dragNode = node;
      this.dragMoved = false;
      node.fx = node.x;
      node.fy = node.y;
      node.pinned = true;
      this.simulation?.alphaTarget(0.16).restart();
    } else {
      this.panning = true;
      this.dragMoved = false;
    }
    this.updateCursor();
  };

  private handlePointerMove = (event: PointerEvent): void => {
    const point = this.toLocalPoint(event);
    if (this.dragNode) {
      const world = this.screenToWorld(point.x, point.y);
      const dx = point.x - this.pointerStart.x;
      const dy = point.y - this.pointerStart.y;
      if (Math.hypot(dx, dy) > 4) this.dragMoved = true;
      this.dragNode.fx = world.x;
      this.dragNode.fy = world.y;
      this.dragNode.x = world.x;
      this.dragNode.y = world.y;
      this.dirty = true;
      return;
    }
    if (this.panning) {
      const dx = point.x - this.pointerStart.x;
      const dy = point.y - this.pointerStart.y;
      if (Math.hypot(dx, dy) > 3) this.dragMoved = true;
      this.view = {
        ...this.view,
        tx: this.viewStart.tx + dx,
        ty: this.viewStart.ty + dy,
      };
      this.dirty = true;
      return;
    }
    const node = this.hitTest(point.x, point.y);
    const nextHovered = node?.id ?? null;
    if (nextHovered !== this.hoveredId) {
      this.hoveredId = nextHovered;
      this.updateFocus();
      this.updateCursor();
      this.dirty = true;
    }
  };

  private handlePointerUp = (event: PointerEvent): void => {
    if (this.canvas.hasPointerCapture(event.pointerId)) {
      this.canvas.releasePointerCapture(event.pointerId);
    }
    if (this.dragNode) {
      const node = this.dragNode;
      this.simulation?.alphaTarget(0);
      if (!this.dragMoved) {
        this.onSelect(node);
      }
    } else if (this.panning && !this.dragMoved) {
      const point = this.toLocalPoint(event);
      if (!this.hitTest(point.x, point.y)) this.onSelect(null);
    }
    this.dragNode = null;
    this.panning = false;
    this.updateCursor();
    this.updateFocus();
    this.dirty = true;
  };

  private handlePointerLeave = (): void => {
    if (this.hoveredId && !this.dragNode) {
      this.hoveredId = null;
      this.updateFocus();
      this.dirty = true;
    }
  };

  private handleWheel = (event: WheelEvent): void => {
    event.preventDefault();
    const point = this.toLocalPoint(event);
    const factor = Math.exp(-event.deltaY * 0.0014);
    const nextScale = clamp(this.view.scale * factor, 0.35, 2.5);
    const ratio = nextScale / this.view.scale;
    this.viewTarget = null;
    this.view = {
      scale: nextScale,
      tx: point.x - (point.x - this.view.tx) * ratio,
      ty: point.y - (point.y - this.view.ty) * ratio,
    };
    this.dirty = true;
  };

  private handleDoubleClick = (event: MouseEvent): void => {
    const point = this.toLocalPoint(event);
    const node = this.hitTest(point.x, point.y);
    if (!node) return;
    node.fx = null;
    node.fy = null;
    node.pinned = false;
    this.simulation?.alpha(0.45).restart();
    this.dirty = true;
  };

  private animateView(target: ViewTransform): void {
    this.viewTarget = target;
    this.dirty = true;
  }

  /* --------------------------------- 粒子 --------------------------------- */

  private buildPulses(): void {
    this.pulses = [];
    if (this.links.length === 0) return;
    const candidates = this.links
      .filter((link) => link.type === "mentions" || link.type === "related" || link.type === "tagged")
      .sort((a, b) => (b.weight ?? 0) - (a.weight ?? 0))
      .slice(0, 110);
    const colorByType: Record<string, string> = {
      mentions: "#8CE8FF",
      related: "#7DD3FC",
      tagged: "#FCD34D",
      cooccur: "#C4B5FD",
    };
    this.pulses = candidates.map((link, index) => ({
      link,
      t: hashString(link.id + index),
      speed: 0.04 + hashString(link.id) * 0.07,
      color: colorByType[link.type] ?? "#8CE8FF",
    }));
  }

  /* -------------------------------- 渲染循环 ------------------------------- */

  private loop = (time: number): void => {
    if (this.disposed) return;
    this.rafId = requestAnimationFrame(this.loop);
    const delta = Math.min(48, time - (this.lastFrame || time));
    this.lastFrame = time;
    const moving = this.simulation != null && this.simulation.alpha() > 0.001;
    const animatingPulses = !this.reducedMotion && this.pulses.length > 0;
    const animatingView = this.viewTarget != null;
    if (!this.dirty && !moving && !animatingPulses && !animatingView) return;
    this.dirty = false;
    this.draw(time, delta);
  };

  private resolveNode(endpoint: EngineNode | string | number): EngineNode | undefined {
    if (typeof endpoint === "object") return endpoint;
    return this.nodeById.get(String(endpoint));
  }

  private draw(time: number, delta: number): void {
    const context = this.context;
    const { width, height, dpr, view } = this;
    if (width <= 0 || height <= 0) return;

    if (this.viewTarget) {
      const target = this.viewTarget;
      const ease = 0.16;
      this.view = {
        scale: this.view.scale + (target.scale - this.view.scale) * ease,
        tx: this.view.tx + (target.tx - this.view.tx) * ease,
        ty: this.view.ty + (target.ty - this.view.ty) * ease,
      };
      if (
        Math.abs(target.scale - this.view.scale) < 0.002 &&
        Math.abs(target.tx - this.view.tx) < 0.5 &&
        Math.abs(target.ty - this.view.ty) < 0.5
      ) {
        this.view = target;
        this.viewTarget = null;
      }
      this.dirty = true;
    }

    const { scale, tx, ty } = this.view;
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, width, height);

    // 计算屏幕坐标
    const radiusFactor = clamp(Math.pow(scale, 0.62), 0.72, 2.4);
    for (const node of this.nodes) {
      node.sx = (node.x ?? 0) * scale + tx;
      node.sy = (node.y ?? 0) * scale + ty;
      node.sr = node.radius * radiusFactor;
    }

    const focusId = this.hoveredId ?? this.selectedId;
    const focusKey = `${this.hoveredId ?? ""}|${this.selectedId ?? ""}`;
    if (focusKey !== this.lastFocusKey) {
      this.lastFocusKey = focusKey;
      this.updateFocus();
    }
    const hasFocus = Boolean(focusId);

    this.drawLinks(context, hasFocus, focusId);
    if (!this.reducedMotion) this.drawPulses(context, delta);
    this.drawNodes(context, time, hasFocus, focusId);
  }

  private drawLinks(
    context: CanvasRenderingContext2D,
    hasFocus: boolean,
    focusId: string | null,
  ): void {
    const { width, height } = this;
    const paths = new Map<string, Path2D>();
    const margin = 80;

    for (const link of this.links) {
      const source = this.resolveNode(link.source);
      const target = this.resolveNode(link.target);
      if (!source || !target) continue;
      if (
        (source.sx < -margin && target.sx < -margin) ||
        (source.sx > width + margin && target.sx > width + margin) ||
        (source.sy < -margin && target.sy < -margin) ||
        (source.sy > height + margin && target.sy > height + margin)
      ) {
        continue;
      }

      const highlighted =
        hasFocus &&
        (source.id === focusId || target.id === focusId);
      const key = `${link.type}:${highlighted ? "lit" : "base"}`;
      let path = paths.get(key);
      if (!path) {
        path = new Path2D();
        paths.set(key, path);
      }

      const midX = (source.sx + target.sx) / 2;
      const midY = (source.sy + target.sy) / 2;
      const dx = target.sx - source.sx;
      const dy = target.sy - source.sy;
      const length = Math.max(1, Math.hypot(dx, dy));
      const offset = clamp(length * 0.13, 4, 42) * link.curve;
      const controlX = midX - (dy / length) * offset;
      const controlY = midY + (dx / length) * offset;

      path.moveTo(source.sx, source.sy);
      path.quadraticCurveTo(controlX, controlY, target.sx, target.sy);
    }

    for (const [key, path] of paths) {
      const [type, state] = key.split(":");
      const baseAlpha = LINK_BASE_ALPHA[type] ?? 0.15;
      const alpha = state === "lit" ? Math.min(0.78, baseAlpha * 3.2) : baseAlpha * (hasFocus ? 0.32 : 1);
      const color = LINK_COLORS[type] ?? "120, 190, 255";
      context.strokeStyle = `rgba(${color}, ${alpha})`;
      context.lineWidth = state === "lit" ? 1.45 : 0.9;
      context.stroke(path);
    }
  }

  private drawPulses(context: CanvasRenderingContext2D, delta: number): void {
    if (delta <= 0) return;
    for (const pulse of this.pulses) {
      const source = this.resolveNode(pulse.link.source);
      const target = this.resolveNode(pulse.link.target);
      if (!source || !target) continue;
      pulse.t += (pulse.speed * delta) / 1000;
      if (pulse.t > 1) pulse.t -= 1;

      const midX = (source.sx + target.sx) / 2;
      const midY = (source.sy + target.sy) / 2;
      const dx = target.sx - source.sx;
      const dy = target.sy - source.sy;
      const length = Math.max(1, Math.hypot(dx, dy));
      const offset = clamp(length * 0.13, 4, 42) * pulse.link.curve;
      const controlX = midX - (dy / length) * offset;
      const controlY = midY + (dx / length) * offset;
      const t = pulse.t;
      const inv = 1 - t;
      const px = inv * inv * source.sx + 2 * inv * t * controlX + t * t * target.sx;
      const py = inv * inv * source.sy + 2 * inv * t * controlY + t * t * target.sy;
      if (px < -20 || px > this.width + 20 || py < -20 || py > this.height + 20) continue;

      const glow = 14;
      context.globalAlpha = Math.sin(t * Math.PI) * 0.75;
      context.drawImage(
        this.glowSpriteFor(pulse.color),
        px - glow / 2,
        py - glow / 2,
        glow,
        glow,
      );
      context.globalAlpha = 1;
      context.fillStyle = pulse.color;
      context.beginPath();
      context.arc(px, py, 1.9, 0, Math.PI * 2);
      context.fill();
    }
  }

  private glowSpriteFor(color: string): HTMLCanvasElement {
    if (!this.extraGlowSprites) this.extraGlowSprites = new Map();
    let sprite = this.extraGlowSprites.get(color);
    if (!sprite) {
      sprite = createGlowSprite(color, 32);
      this.extraGlowSprites.set(color, sprite);
    }
    return sprite;
  }

  private extraGlowSprites: Map<string, HTMLCanvasElement> | null = null;
  private labelWidths = new Map<string, number>();

  private labelWidth(text: string, font: string): number {
    const key = `${font}|${text}`;
    let width = this.labelWidths.get(key);
    if (width == null) {
      this.context.font = font;
      width = this.context.measureText(text).width;
      this.labelWidths.set(key, width);
    }
    return width;
  }

  private drawNodes(
    context: CanvasRenderingContext2D,
    time: number,
    hasFocus: boolean,
    focusId: string | null,
  ): void {
    const { width, height } = this;
    const scale = this.view.scale;
    const focusSet = this.focusIds;

    for (const node of this.nodes) {
      if (node.sx < -60 || node.sx > width + 60 || node.sy < -60 || node.sy > height + 60) {
        continue;
      }
      const isFocus = node.id === focusId;
      const isNeighbor = hasFocus && focusSet.has(node.id) && !isFocus;
      const isMatch = this.searchMatches.has(node.id);
      const dimmed = hasFocus && !isFocus && !isNeighbor;
      const alpha = dimmed ? 0.13 : 1;
      const hoverScale = isFocus ? 1.32 : isNeighbor ? 1.12 : 1;
      const sr = node.sr * hoverScale;
      const typeColor = TYPE_COLORS[node.type];

      // 光晕
      const glowSize = sr * (node.type === "document" ? 9 : node.type === "concept" ? 7.4 : 7);
      context.globalAlpha = alpha * (isFocus ? 1 : node.type === "document" ? 0.95 : 0.82);
      context.drawImage(
        this.glowSprites[node.type],
        node.sx - glowSize / 2,
        node.sy - glowSize / 2,
        glowSize,
        glowSize,
      );

      // 文档外圈呼吸光环
      if (node.type === "document") {
        const breathing = this.reducedMotion ? 0 : Math.sin(time / 1300 + node.radius) * 1.8;
        context.globalAlpha = alpha * 0.62;
        context.strokeStyle = rgba(typeColor, 0.52);
        context.lineWidth = 1.2;
        context.beginPath();
        context.arc(node.sx, node.sy, sr + 5 + breathing, 0, Math.PI * 2);
        context.stroke();
        context.globalAlpha = alpha * 0.22;
        context.lineWidth = 0.8;
        context.beginPath();
        context.arc(node.sx, node.sy, sr + 10.5 + breathing * 0.6, 0, Math.PI * 2);
        context.stroke();
      }

      // 主体
      context.globalAlpha = alpha;
      context.drawImage(
        this.coreSprites[node.type],
        node.sx - sr,
        node.sy - sr,
        sr * 2,
        sr * 2,
      );

      // 选中环
      if (this.selectedId === node.id) {
        context.globalAlpha = alpha;
        context.strokeStyle = rgba(typeColor, 0.85);
        context.lineWidth = 1.6;
        context.beginPath();
        context.arc(node.sx, node.sy, sr + 7, 0, Math.PI * 2);
        context.stroke();
        const rotation = this.reducedMotion ? 0 : time / 2600;
        context.strokeStyle = rgba(typeColor, 0.5);
        context.lineWidth = 1;
        context.setLineDash([3, 5]);
        context.beginPath();
        context.arc(node.sx, node.sy, sr + 12, rotation, rotation + Math.PI * 1.6);
        context.stroke();
        context.setLineDash([]);
      } else if (isFocus) {
        context.globalAlpha = alpha;
        context.strokeStyle = rgba(typeColor, 0.75);
        context.lineWidth = 1.3;
        context.beginPath();
        context.arc(node.sx, node.sy, sr + 6, 0, Math.PI * 2);
        context.stroke();
      }

      // 搜索命中标记
      if (isMatch) {
        const pulse = this.reducedMotion ? 0 : Math.sin(time / 320) * 1.5;
        context.globalAlpha = alpha * 0.9;
        context.strokeStyle = "rgba(251, 191, 36, 0.85)";
        context.lineWidth = 1.2;
        context.setLineDash([2.5, 4]);
        context.beginPath();
        context.arc(node.sx, node.sy, sr + 9 + pulse, 0, Math.PI * 2);
        context.stroke();
        context.setLineDash([]);
      }

      // 固定节点标记
      if (node.pinned) {
        context.globalAlpha = alpha * 0.6;
        context.fillStyle = "rgba(232, 244, 255, 0.65)";
        context.beginPath();
        context.arc(node.sx + sr + 4, node.sy - sr - 4, 1.5, 0, Math.PI * 2);
        context.fill();
      }

      context.globalAlpha = 1;
    }

    // 标签：按优先级排序并做简单的碰撞检测，避免密集区域文字互相压盖
    const labelCandidates = this.nodes
      .filter((node) => {
        if (node.sx < -40 || node.sx > width + 40 || node.sy < -40 || node.sy > height + 40) {
          return false;
        }
        const isFocus = node.id === focusId;
        const isNeighbor = hasFocus && focusSet.has(node.id) && !isFocus;
        return (
          isFocus ||
          isNeighbor ||
          this.searchMatches.has(node.id) ||
          (node.type === "document" && scale >= 0.36) ||
          (node.type === "tag" && scale >= 0.5) ||
          (node.type === "concept" && scale >= 0.78)
        );
      })
      .sort((first, second) => {
        const priority = (node: EngineNode): number => {
          if (node.id === focusId) return 0;
          if (hasFocus && focusSet.has(node.id)) return 1;
          if (node.type === "document") return 2;
          if (node.type === "tag") return 3;
          return 4;
        };
        const difference = priority(first) - priority(second);
        if (difference !== 0) return difference;
        return (second.degree ?? 0) - (first.degree ?? 0);
      });

    const placedLabels: { x1: number; y1: number; x2: number; y2: number }[] = [];
    for (const node of labelCandidates) {
      const isFocus = node.id === focusId;
      const isNeighbor = hasFocus && focusSet.has(node.id) && !isFocus;
      const dimmed = hasFocus && !isFocus && !isNeighbor;
      const alpha = dimmed ? 0.16 : 1;
      const fontSize = node.type === "document" ? 12.5 : 11;
      const fontWeight = node.type === "document" ? 650 : node.type === "tag" ? 600 : 500;
      const font = `${fontWeight} ${fontSize}px ${FONT_FAMILY}`;
      const color =
        node.type === "document"
          ? "rgba(222, 247, 255, 0.98)"
          : node.type === "tag"
            ? "rgba(253, 228, 158, 0.96)"
            : "rgba(224, 213, 255, 0.94)";
      const label = node.label.length > 16 ? `${node.label.slice(0, 15)}…` : node.label;
      const sr = node.sr * (isFocus ? 1.32 : isNeighbor ? 1.12 : 1);
      const labelX = node.sx;
      const labelY = node.sy + sr + 6;
      const labelWidth = this.labelWidth(label, font);
      const box = {
        x1: labelX - labelWidth / 2 - 4,
        y1: labelY - 2,
        x2: labelX + labelWidth / 2 + 4,
        y2: labelY + fontSize + 4,
      };
      const forceShow = isFocus || isNeighbor || this.searchMatches.has(node.id);
      const collides = placedLabels.some(
        (placed) =>
          !(box.x2 < placed.x1 || box.x1 > placed.x2 || box.y2 < placed.y1 || box.y1 > placed.y2),
      );
      if (!forceShow && collides) continue;
      if (!forceShow) placedLabels.push(box);

      context.globalAlpha = alpha;
      context.font = font;
      context.textAlign = "center";
      context.textBaseline = "top";
      context.lineWidth = 3;
      context.strokeStyle = "rgba(4, 8, 15, 0.82)";
      context.strokeText(label, labelX, labelY);
      context.fillStyle = color;
      context.fillText(label, labelX, labelY);

      if (node.type === "document") {
        context.globalAlpha = alpha * 0.55;
        context.fillStyle = rgba(TYPE_COLORS.document, 0.8);
        context.fillRect(labelX - 10, labelY + fontSize + 4, 20, 1);
      }
    }

    context.globalAlpha = 1;
    context.textAlign = "start";
    context.textBaseline = "alphabetic";
  }
}
