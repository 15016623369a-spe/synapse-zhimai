import {
  AdditiveBlending,
  BufferAttribute,
  BufferGeometry,
  CanvasTexture,
  Color,
  Group,
  LinearFilter,
  LineBasicMaterial,
  LineSegments,
  PerspectiveCamera,
  Points,
  PointsMaterial,
  Scene,
  Sprite,
  SpriteMaterial,
  SRGBColorSpace,
  Vector3,
  WebGLRenderer,
  type Material,
  type Texture,
} from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import type { GraphLink, GraphNode, GraphNodeType, GraphZoneMeta } from "../../types";

export interface Graph3DHoverPoint {
  x: number;
  y: number;
}

export interface Graph3DEngineOptions {
  container: HTMLElement;
  onSelect: (node: GraphNode | null) => void;
  onHover?: (node: GraphNode | null, point: Graph3DHoverPoint | null) => void;
  onSelectZone?: (zoneId: string | null) => void;
  reducedMotion: boolean;
}

export type GraphLayoutMode = "global" | "zones";

interface ZoneGroup {
  zoneId: string | null;
  key: string;
  name: string;
  color: string;
  documentCount: number;
  nodes: Node3D[];
  center: Vector3;
  radius: number;
  labelEl: HTMLDivElement | null;
  ringSprites: Sprite[];
}

interface ZoneGroupSource {
  zoneId: string | null;
  key: string;
  nodes: Node3D[];
  edges: Edge3D[];
  nodeSet: Set<Node3D>;
}

const TYPE_COLORS: Record<GraphNodeType, string> = {
  document: "#4FD8FF",
  concept: "#A78BFA",
  tag: "#FBBF24",
};

const TYPE_COLOR_OBJECTS: Record<GraphNodeType, Color> = {
  document: new Color(TYPE_COLORS.document),
  concept: new Color(TYPE_COLORS.concept),
  tag: new Color(TYPE_COLORS.tag),
};

const TYPE_RADIUS: Record<GraphNodeType, [number, number]> = {
  document: [5.6, 10.2],
  concept: [2.6, 4.8],
  tag: [3.4, 6],
};

const LINK_REST_LENGTH: Record<string, number> = {
  mentions: 112,
  tagged: 98,
  related: 152,
  cooccur: 84,
};

const LINK_SPRING: Record<string, number> = {
  mentions: 0.05,
  tagged: 0.045,
  related: 0.028,
  cooccur: 0.026,
};

const FOV = 50;
const TARGET_LAYOUT_RADIUS = 320;
const PULSE_COUNT = 160;
const FIT_FILL = 0.88;
const LABEL_TOP_PADDING = 8;
const LABEL_DOCUMENT_MAX = 24;
const LABEL_TAG_MAX = 20;
const LABEL_CONCEPT_MAX = 16;
const CONCEPT_LABEL_MIN_DEGREE = 3;
const MAX_CONCEPT_LABELS = 15;

let glowTextureCache: Texture | null = null;
let coreTextureCache: Texture | null = null;
let ringTextureCache: Texture | null = null;
let softTextureCache: Texture | null = null;

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function makeCanvas(size: number): { canvas: HTMLCanvasElement; context: CanvasRenderingContext2D } {
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Canvas 2D 上下文不可用");
  return { canvas, context };
}

function toTexture(canvas: HTMLCanvasElement): CanvasTexture {
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  texture.minFilter = LinearFilter;
  texture.magFilter = LinearFilter;
  texture.generateMipmaps = false;
  texture.needsUpdate = true;
  return texture;
}

/** 柔和的径向光晕（用于外层光晕 / 星云）。 */
function getGlowTexture(): Texture {
  if (glowTextureCache) return glowTextureCache;
  const { canvas, context } = makeCanvas(128);
  const gradient = context.createRadialGradient(64, 64, 0, 64, 64, 64);
  gradient.addColorStop(0, "rgba(255,255,255,0.95)");
  gradient.addColorStop(0.16, "rgba(255,255,255,0.62)");
  gradient.addColorStop(0.38, "rgba(255,255,255,0.24)");
  gradient.addColorStop(0.7, "rgba(255,255,255,0.06)");
  gradient.addColorStop(1, "rgba(255,255,255,0)");
  context.fillStyle = gradient;
  context.fillRect(0, 0, 128, 128);
  glowTextureCache = toTexture(canvas);
  return glowTextureCache;
}

/** 亮核：中心接近纯白，边缘柔和收束。 */
function getCoreTexture(): Texture {
  if (coreTextureCache) return coreTextureCache;
  const { canvas, context } = makeCanvas(96);
  const gradient = context.createRadialGradient(48, 48, 0, 48, 48, 48);
  gradient.addColorStop(0, "rgba(255,255,255,1)");
  gradient.addColorStop(0.3, "rgba(255,255,255,0.94)");
  gradient.addColorStop(0.56, "rgba(255,255,255,0.5)");
  gradient.addColorStop(0.8, "rgba(255,255,255,0.12)");
  gradient.addColorStop(1, "rgba(255,255,255,0)");
  context.fillStyle = gradient;
  context.beginPath();
  context.arc(48, 48, 48, 0, Math.PI * 2);
  context.fill();
  coreTextureCache = toTexture(canvas);
  return coreTextureCache;
}

/** 细圆环（文档光环 / 选中 / 搜索命中）。 */
function getRingTexture(): Texture {
  if (ringTextureCache) return ringTextureCache;
  const { canvas, context } = makeCanvas(128);
  const gradient = context.createRadialGradient(64, 64, 0, 64, 64, 64);
  gradient.addColorStop(0, "rgba(255,255,255,0)");
  gradient.addColorStop(0.56, "rgba(255,255,255,0)");
  gradient.addColorStop(0.66, "rgba(255,255,255,0.55)");
  gradient.addColorStop(0.72, "rgba(255,255,255,0.95)");
  gradient.addColorStop(0.79, "rgba(255,255,255,0.5)");
  gradient.addColorStop(0.9, "rgba(255,255,255,0)");
  gradient.addColorStop(1, "rgba(255,255,255,0)");
  context.fillStyle = gradient;
  context.fillRect(0, 0, 128, 128);
  ringTextureCache = toTexture(canvas);
  return ringTextureCache;
}

/** 超大星云柔光。 */
function getSoftTexture(): Texture {
  if (softTextureCache) return softTextureCache;
  const { canvas, context } = makeCanvas(256);
  const gradient = context.createRadialGradient(128, 128, 0, 128, 128, 128);
  gradient.addColorStop(0, "rgba(255,255,255,0.5)");
  gradient.addColorStop(0.4, "rgba(255,255,255,0.18)");
  gradient.addColorStop(0.72, "rgba(255,255,255,0.05)");
  gradient.addColorStop(1, "rgba(255,255,255,0)");
  context.fillStyle = gradient;
  context.fillRect(0, 0, 256, 256);
  softTextureCache = toTexture(canvas);
  return softTextureCache;
}

function roundRectPath(
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
): void {
  context.beginPath();
  context.moveTo(x + radius, y);
  context.lineTo(x + width - radius, y);
  context.quadraticCurveTo(x + width, y, x + width, y + radius);
  context.lineTo(x + width, y + height - radius);
  context.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
  context.lineTo(x + radius, y + height);
  context.quadraticCurveTo(x, y + height, x, y + height - radius);
  context.lineTo(x, y + radius);
  context.quadraticCurveTo(x, y, x + radius, y);
  context.closePath();
}

/** 生成文字标签文本（带暗色底 + 描边，保证在太空中可读）。 */
function truncateLabel(label: string, max: number): string {
  const trimmed = label.trim();
  return trimmed.length > max ? `${trimmed.slice(0, max)}…` : trimmed;
}

interface NodeLayer {
  sprite: Sprite;
  baseScale: number;
  baseOpacity: number;
}

interface Node3D {
  data: GraphNode;
  pos: Vector3;
  vel: Vector3;
  radius: number;
  neighbors: Set<string>;
  group: Group;
  glow: NodeLayer;
  mid: NodeLayer;
  core: NodeLayer;
  halo: Sprite | null;
  selectRing: Sprite;
  searchRing: Sprite;
  labelEl: HTMLDivElement | null;
  labelKind: "document" | "tag" | "concept" | null;
  labelWidth: number;
  labelHeight: number;
  hoverBoost: number;
}

interface Edge3D {
  source: Node3D;
  target: Node3D;
  type: string;
  weight: number;
  color: Color;
}

interface Pulse3D {
  edge: Edge3D;
  t: number;
  speed: number;
  sprite: Sprite;
}

export class Graph3DEngine {
  private readonly container: HTMLElement;
  private readonly onSelect: (node: GraphNode | null) => void;
  private readonly onHover?: (node: GraphNode | null, point: Graph3DHoverPoint | null) => void;
  private readonly onSelectZone?: (zoneId: string | null) => void;
  private layoutMode: GraphLayoutMode = "global";
  private zoneMeta: GraphZoneMeta[] = [];
  private zoneGroups: ZoneGroup[] = [];

  private readonly renderer: WebGLRenderer;
  private readonly scene = new Scene();
  private readonly camera: PerspectiveCamera;
  private readonly controls: OrbitControls;

  private readonly graphRoot = new Group();
  private readonly starLayers: { points: Points; spin: number }[] = [];
  private readonly nebulae: Sprite[] = [];
  private readonly labelLayer: HTMLDivElement;

  private nodes: Node3D[] = [];
  private nodeById = new Map<string, Node3D>();
  private edges: Edge3D[] = [];
  private pulses: Pulse3D[] = [];

  private edgeLines: LineSegments | null = null;
  private edgeMaterial: LineBasicMaterial | null = null;
  private edgeGeometry: BufferGeometry | null = null;
  private highlightLines: LineSegments | null = null;
  private highlightMaterial: LineBasicMaterial | null = null;
  private highlightGeometry: BufferGeometry | null = null;

  private width = 800;
  private height = 600;
  private dpr = 1;
  private referenceDistance = 760;

  private hovered: Node3D | null = null;
  private selected: Node3D | null = null;
  private searchIds = new Set<string>();

  private reducedMotion: boolean;
  private autoRotateEnabled = true;
  private autoRotateActive = false;

  private rafId: number | null = null;
  private autoRotateResumeTimer: number | null = null;
  private lastTime = 0;
  private disposed = false;
  private running = true;

  private viewTween: {
    fromPosition: Vector3;
    toPosition: Vector3;
    fromTarget: Vector3;
    toTarget: Vector3;
    elapsed: number;
    duration: number;
  } | null = null;

  private pointerDown = { x: 0, y: 0, time: 0 };

  private readonly disposableMaterials: Material[] = [];
  private readonly disposableTextures: Texture[] = [];
  private readonly nodeMaterials: Material[] = [];
  private readonly nodeTextures: Texture[] = [];
  private readonly disposableGeometries: BufferGeometry[] = [];

  private readonly tmpVec = new Vector3();
  private readonly tmpVec2 = new Vector3();

  constructor(options: Graph3DEngineOptions) {
    this.container = options.container;
    this.onSelect = options.onSelect;
    this.onHover = options.onHover;
    this.onSelectZone = options.onSelectZone;
    this.reducedMotion = options.reducedMotion;
    this.autoRotateEnabled = !options.reducedMotion;

    this.renderer = new WebGLRenderer({
      antialias: true,
      alpha: true,
      powerPreference: "high-performance",
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.domElement.className = "graph-canvas graph-canvas-3d";
    this.renderer.domElement.setAttribute("aria-hidden", "true");
    this.container.appendChild(this.renderer.domElement);

    this.labelLayer = document.createElement("div");
    this.labelLayer.className = "graph3d-label-layer";
    this.container.appendChild(this.labelLayer);

    this.camera = new PerspectiveCamera(FOV, 1, 5, 6000);
    this.camera.position.set(0, 80, 700);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.075;
    this.controls.enablePan = false;
    this.controls.rotateSpeed = 0.55;
    this.controls.zoomSpeed = 0.85;
    this.controls.minDistance = 70;
    this.controls.maxDistance = 2200;
    this.controls.minPolarAngle = Math.PI * 0.12;
    this.controls.maxPolarAngle = Math.PI * 0.88;
    this.controls.autoRotateSpeed = 0.38;
    this.controls.target.set(0, 0, 0);
    this.controls.update();
    this.controls.addEventListener("start", this.handleControlsStart);
    this.controls.addEventListener("end", this.handleControlsEnd);

    this.createStarLayers();
    this.createNebulae();
    this.scene.add(this.graphRoot);
    this.controls.autoRotate = this.autoRotateEnabled && !this.reducedMotion;
    this.autoRotateActive = this.controls.autoRotate;

    this.attachEvents();
    this.lastTime = performance.now();
    this.rafId = requestAnimationFrame(this.animate);
  }

  /* ------------------------------- 公共 API ------------------------------- */

  setData(nodes: GraphNode[], links: GraphLink[]): void {
    if (this.disposed) return;
    this.clearGraphObjects();
    this.hovered = null;
    this.selected = null;
    this.searchIds.clear();

    this.buildNodes(nodes);
    this.buildEdges(links);
    this.layoutAndArrange();
    this.applyNodeTransforms();
    this.rebuildEdgeGeometry();
    this.buildPulses();
    this.buildZoneVisuals();
    this.applySearchRings();
    this.applySelectionRings();
    this.rebuildHighlightGeometry();
    this.updateFocus();
    this.resetView();
  }

  setLayoutMode(mode: GraphLayoutMode): void {
    if (this.layoutMode === mode) return;
    this.layoutMode = mode;
  }

  setZoneMeta(zones: GraphZoneMeta[]): void {
    this.zoneMeta = zones;
  }

  setReducedMotion(value: boolean): void {
    this.reducedMotion = value;
    this.controls.autoRotate = this.autoRotateEnabled && !value;
    this.autoRotateActive = this.controls.autoRotate;
    for (const pulse of this.pulses) pulse.sprite.visible = !value;
  }

  setAutoRotate(value: boolean): void {
    this.autoRotateEnabled = value;
    if (this.autoRotateResumeTimer != null) {
      window.clearTimeout(this.autoRotateResumeTimer);
      this.autoRotateResumeTimer = null;
    }
    this.controls.autoRotate = value && !this.reducedMotion;
    this.autoRotateActive = this.controls.autoRotate;
  }

  setSearchMatches(ids: string[]): void {
    this.searchIds = new Set(ids);
    this.applySearchRings();
  }

  selectById(id: string | null): void {
    const node = id ? (this.nodeById.get(id) ?? null) : null;
    this.selected = node;
    this.applySelectionRings();
    this.rebuildHighlightGeometry();
    this.updateFocus();
  }

  focusFirstMatch(): void {
    this.selectFirstSearchMatch();
  }

  selectFirstSearchMatch(): void {
    for (const node of this.nodes) {
      if (this.searchIds.has(node.data.id)) {
        this.selectById(node.data.id);
        this.focusNode(node.data.id);
        return;
      }
    }
  }

  focusNode(id: string): void {
    const node = this.nodeById.get(id);
    if (!node) return;
    const target = node.pos.clone();
    const direction = this.tmpVec.copy(this.camera.position).sub(this.controls.target);
    if (direction.lengthSq() < 1) direction.set(0, 0.25, 1);
    direction.normalize();
    const distance = clamp(node.radius * 42, 420, 620);
    const position = target.clone().addScaledVector(direction, distance);
    this.tweenView(position, target, 0.7);
  }

  relayout(): void {
    if (this.nodes.length === 0) return;
    for (const node of this.nodes) {
      node.pos.copy(this.randomSpherePosition(node.radius));
      node.vel.set(0, 0, 0);
      node.hoverBoost = 0;
    }
    this.clearZoneVisuals();
    this.layoutAndArrange();
    this.applyNodeTransforms();
    this.rebuildEdgeGeometry();
    this.buildPulses();
    this.buildZoneVisuals();
    this.rebuildHighlightGeometry();
    this.updateFocus();
    this.resetView();
  }

  resetView(): void {
    this.fitView(0.6);
  }

  private fitView(duration: number): void {
    if (this.nodes.length === 0) {
      this.tweenView(new Vector3(0, 90, 780), new Vector3(0, 0, 0), duration);
      return;
    }

    const center = new Vector3();
    for (const node of this.nodes) center.add(node.pos);
    center.multiplyScalar(1 / this.nodes.length);

    const direction = new Vector3(0, 0.3, 1).normalize();
    const zonesMode = this.layoutMode === "zones";
    const fill = zonesMode ? 0.9 : FIT_FILL;
    const topInset = zonesMode ? 128 : 0;
    const tanHalf = Math.tan((FOV * Math.PI) / 360);
    const originalPosition = this.camera.position.clone();
    const originalTarget = this.controls.target.clone();

    let distance = 760;
    let lookOffsetY = 0;
    for (let iteration = 0; iteration < 6; iteration += 1) {
      lookOffsetY = zonesMode ? distance * tanHalf * (topInset / this.height) : 0;
      this.camera.position
        .copy(center)
        .add(new Vector3(0, lookOffsetY, 0))
        .addScaledVector(direction, distance);
      this.camera.lookAt(center.clone().add(new Vector3(0, lookOffsetY, 0)));
      this.camera.updateMatrixWorld(true);
      let maxX = 0;
      let maxY = 0;
      if (zonesMode && this.zoneGroups.length > 0) {
        const aspect = this.width / this.height;
        for (const group of this.zoneGroups) {
          const projected = this.tmpVec.copy(group.center).project(this.camera);
          if (projected.z > 1 || projected.z < -1) continue;
          const cameraDistance = this.camera.position.distanceTo(group.center);
          const radiusNdc =
            (group.radius * 1.26) / Math.max(1, cameraDistance * tanHalf);
          maxX = Math.max(maxX, Math.abs(projected.x) + radiusNdc / aspect);
          maxY = Math.max(maxY, Math.abs(projected.y) + radiusNdc);
        }
      } else {
        for (const node of this.nodes) {
          const projected = this.tmpVec.copy(node.pos).project(this.camera);
          if (projected.z > 1 || projected.z < -1) continue;
          maxX = Math.max(maxX, Math.abs(projected.x));
          maxY = Math.max(maxY, Math.abs(projected.y));
        }
      }
      const extent = Math.max(maxX, maxY);
      if (extent < 0.001) break;
      const ratio = extent / fill;
      if (Math.abs(ratio - 1) < 0.025) break;
      distance *= ratio;
    }

    this.camera.position.copy(originalPosition);
    this.controls.target.copy(originalTarget);
    this.camera.lookAt(originalTarget);

    const lookTarget = center.clone().add(new Vector3(0, lookOffsetY, 0));
    const position = lookTarget.clone().addScaledVector(direction, distance);
    this.referenceDistance = distance;
    this.controls.minDistance = Math.max(80, distance * 0.2);
    this.controls.maxDistance = distance * 2.6;
    this.tweenView(position, lookTarget, duration);
  }

  zoomBy(factor: number): void {
    const offset = this.tmpVec.copy(this.camera.position).sub(this.controls.target);
    const length = clamp(
      offset.length() * factor,
      this.controls.minDistance,
      this.controls.maxDistance,
    );
    offset.setLength(length);
    this.camera.position.copy(this.controls.target).add(offset);
    this.viewTween = null;
  }

  resize(width: number, height: number, dpr = 1): void {
    if (this.disposed) return;
    this.width = Math.max(240, width);
    this.height = Math.max(240, height);
    this.dpr = clamp(dpr, 1, 2);
    this.renderer.setPixelRatio(this.dpr);
    this.renderer.setSize(this.width, this.height, true);
    this.camera.aspect = this.width / this.height;
    this.camera.updateProjectionMatrix();
    if (this.nodes.length > 0) this.fitView(0.35);
  }

  getZoom(): number {
    return this.camera.position.distanceTo(this.controls.target);
  }

  destroy(): void {
    if (this.disposed) return;
    this.disposed = true;
    if (this.rafId != null) cancelAnimationFrame(this.rafId);
    this.detachEvents();
    this.clearGraphObjects();
    this.controls.removeEventListener("start", this.handleControlsStart);
    this.controls.removeEventListener("end", this.handleControlsEnd);
    this.controls.dispose();
    if (this.autoRotateResumeTimer != null) window.clearTimeout(this.autoRotateResumeTimer);
    for (const layer of this.starLayers) {
      layer.points.geometry.dispose();
      if (layer.points.material instanceof PointsMaterial) layer.points.material.dispose();
    }
    for (const nebula of this.nebulae) {
      const material = nebula.material;
      material.dispose();
    }
    this.labelLayer.remove();
    for (const material of this.disposableMaterials) material.dispose();
    for (const geometry of this.disposableGeometries) geometry.dispose();
    for (const texture of this.disposableTextures) texture.dispose();
    const sharedGlow = getGlowTexture();
    void sharedGlow;
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }

  /* ------------------------------ 场景构建 ------------------------------- */

  private createStarLayers(): void {
    const layers = [
      {
        count: 420,
        minRadius: 1050,
        maxRadius: 1950,
        size: 1.5,
        opacity: 0.36,
        spin: 0.0015,
        color: "#b9d4ff",
      },
      {
        count: 330,
        minRadius: 780,
        maxRadius: 1280,
        size: 2.3,
        opacity: 0.52,
        spin: 0.0028,
        color: "#cfe4ff",
      },
      {
        count: 220,
        minRadius: 620,
        maxRadius: 960,
        size: 3.2,
        opacity: 0.62,
        spin: 0.0042,
        color: "#e6f2ff",
      },
    ];
    for (const layer of layers) {
      const positions = new Float32Array(layer.count * 3);
      for (let index = 0; index < layer.count; index += 1) {
        const theta = Math.random() * Math.PI * 2;
        const phi = Math.acos(2 * Math.random() - 1);
        const radius =
          layer.minRadius + Math.random() * (layer.maxRadius - layer.minRadius);
        positions[index * 3] = Math.sin(phi) * Math.cos(theta) * radius;
        positions[index * 3 + 1] = Math.cos(phi) * radius * 0.82;
        positions[index * 3 + 2] = Math.sin(phi) * Math.sin(theta) * radius;
      }
      const geometry = new BufferGeometry();
      geometry.setAttribute("position", new BufferAttribute(positions, 3));
      const material = new PointsMaterial({
        color: new Color(layer.color),
        size: layer.size,
        sizeAttenuation: true,
        transparent: true,
        opacity: layer.opacity,
        depthWrite: false,
        blending: AdditiveBlending,
      });
      const points = new Points(geometry, material);
      points.renderOrder = -20;
      this.scene.add(points);
      this.starLayers.push({ points, spin: layer.spin });
    }
  }

  private createNebulae(): void {
    const definitions = [
      { color: "#4FD8FF", position: new Vector3(-680, 380, -1300), scale: 2400, opacity: 0.14 },
      { color: "#A78BFA", position: new Vector3(820, -340, -1550), scale: 2800, opacity: 0.12 },
      { color: "#5B8CFF", position: new Vector3(200, 660, -1150), scale: 2000, opacity: 0.1 },
      { color: "#4FD8FF", position: new Vector3(60, 40, -760), scale: 1700, opacity: 0.06 },
      { color: "#A78BFA", position: new Vector3(-120, -180, -980), scale: 2100, opacity: 0.05 },
    ];
    for (const definition of definitions) {
      const material = new SpriteMaterial({
        map: getSoftTexture(),
        color: new Color(definition.color),
        transparent: true,
        opacity: definition.opacity,
        blending: AdditiveBlending,
        depthWrite: false,
        depthTest: false,
      });
      this.disposableMaterials.push(material);
      const sprite = new Sprite(material);
      sprite.position.copy(definition.position);
      sprite.scale.set(definition.scale, definition.scale, 1);
      sprite.renderOrder = -30;
      this.scene.add(sprite);
      this.nebulae.push(sprite);
    }
  }

  private randomSpherePosition(radius: number): Vector3 {
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.acos(2 * Math.random() - 1);
    const distance =
      TARGET_LAYOUT_RADIUS * (0.35 + Math.random() * 0.75) + radius * 2;
    return new Vector3(
      Math.sin(phi) * Math.cos(theta) * distance,
      Math.cos(phi) * distance,
      Math.sin(phi) * Math.sin(theta) * distance,
    );
  }

  private buildNodes(nodes: GraphNode[]): void {
    const maxMetric: Record<GraphNodeType, number> = { document: 1, concept: 1, tag: 1 };
    for (const node of nodes) {
      const metric = Math.max(node.weight ?? 1, (node.degree ?? 1) * 0.9);
      maxMetric[node.type] = Math.max(maxMetric[node.type], metric);
    }

    const total = Math.max(1, nodes.length);
    const goldenAngle = Math.PI * (3 - Math.sqrt(5));

    nodes.forEach((node, index) => {
      const [minRadius, maxRadius] = TYPE_RADIUS[node.type];
      const metric = Math.max(node.weight ?? 1, (node.degree ?? 1) * 0.9);
      const ratio = clamp(Math.sqrt(metric / maxMetric[node.type]), 0, 1);
      const radius = minRadius + (maxRadius - minRadius) * ratio;
      const y = total === 1 ? 0 : 1 - (index / (total - 1)) * 2;
      const ringRadius = Math.sqrt(Math.max(0, 1 - y * y));
      const theta = goldenAngle * index;
      const shell =
        TARGET_LAYOUT_RADIUS *
        (0.4 + 0.75 * Math.cbrt((index + 1) / total)) *
        (0.75 + Math.random() * 0.45);
      const position = new Vector3(
        Math.cos(theta) * ringRadius * shell,
        y * shell,
        Math.sin(theta) * ringRadius * shell,
      );
      position.x += (Math.random() - 0.5) * 26;
      position.y += (Math.random() - 0.5) * 26;
      position.z += (Math.random() - 0.5) * 26;

      const visual = this.createNodeVisual(node, radius, position);
      this.nodes.push(visual);
      this.nodeById.set(node.id, visual);
    });
  }

  private createNodeVisual(data: GraphNode, radius: number, position: Vector3): Node3D {
    const group = new Group();
    group.position.copy(position);
    this.graphRoot.add(group);

    const isDocument = data.type === "document";
    const isTag = data.type === "tag";
    const labelKind: Node3D["labelKind"] = isDocument
      ? "document"
      : isTag
        ? "tag"
        : (data.degree ?? 0) >= CONCEPT_LABEL_MIN_DEGREE
          ? "concept"
          : null;
    const color = TYPE_COLOR_OBJECTS[data.type];
    const lightColor = new Color().copy(color).lerp(new Color("#ffffff"), 0.34);
    const glowColor = new Color().copy(color).lerp(new Color("#ffffff"), 0.06);

    const glow = this.createSpriteLayer(
      getGlowTexture(),
      color,
      radius * (isDocument ? 11 : isTag ? 8.6 : 6.8),
      isDocument ? 0.42 : isTag ? 0.52 : 0.48,
      group,
      0,
    );
    const mid = this.createSpriteLayer(
      getGlowTexture(),
      glowColor,
      radius * (isDocument ? 4 : isTag ? 3.6 : 3.1),
      isDocument ? 0.66 : 0.74,
      group,
      1,
    );
    const core = this.createSpriteLayer(
      getCoreTexture(),
      lightColor,
      radius * (isDocument ? 2.6 : isTag ? 2.45 : 2.2),
      1,
      group,
      3,
    );

    let halo: Sprite | null = null;
    if (isDocument) {
      const haloMaterial = new SpriteMaterial({
        map: getRingTexture(),
        color,
        transparent: true,
        opacity: 0.5,
        blending: AdditiveBlending,
        depthWrite: false,
      });
      this.disposableMaterials.push(haloMaterial);
      this.nodeMaterials.push(haloMaterial);
      halo = new Sprite(haloMaterial);
      halo.scale.set(radius * 5, radius * 5, 1);
      halo.renderOrder = 2;
      group.add(halo);
    }

    const selectRing = this.createRingSprite(radius * 3.8, "#9FE8FF", 0.72, group, 5);
    selectRing.visible = false;
    const searchRing = this.createRingSprite(radius * 4.4, "#E6FBFF", 0.5, group, 6);
    searchRing.visible = false;

    let labelEl: HTMLDivElement | null = null;
    let labelWidth = 0;
    let labelHeight = 0;
    if (labelKind) {
      labelEl = document.createElement("div");
      labelEl.className = `graph3d-label${labelKind === "tag" ? " is-tag" : ""}${
        labelKind === "concept" ? " is-concept" : ""
      }`;
      const maxLength =
        labelKind === "document"
          ? LABEL_DOCUMENT_MAX
          : labelKind === "tag"
            ? LABEL_TAG_MAX
            : LABEL_CONCEPT_MAX;
      labelEl.textContent = truncateLabel(data.label, maxLength);
      labelEl.title = data.label;
      labelEl.style.visibility = "hidden";
      this.labelLayer.appendChild(labelEl);
      labelWidth = labelEl.offsetWidth;
      labelHeight = labelEl.offsetHeight;
    }

    return {
      data,
      pos: position,
      vel: new Vector3(),
      radius,
      neighbors: new Set<string>(),
      group,
      glow,
      mid,
      core,
      halo,
      selectRing,
      searchRing,
      labelEl,
      labelKind,
      labelWidth,
      labelHeight,
      hoverBoost: 0,
    };
  }

  private createSpriteLayer(
    texture: Texture,
    color: Color,
    scale: number,
    opacity: number,
    group: Group,
    renderOrder: number,
  ): NodeLayer {
    const material = new SpriteMaterial({
      map: texture,
      color,
      transparent: true,
      opacity,
      blending: AdditiveBlending,
      depthWrite: false,
      depthTest: true,
    });
    this.disposableMaterials.push(material);
    this.nodeMaterials.push(material);
    const sprite = new Sprite(material);
    sprite.scale.set(scale, scale, 1);
    sprite.renderOrder = renderOrder;
    group.add(sprite);
    return { sprite, baseScale: scale, baseOpacity: opacity };
  }

  private createRingSprite(
    scale: number,
    color: string,
    opacity: number,
    group: Group,
    renderOrder: number,
  ): Sprite {
    const material = new SpriteMaterial({
      map: getRingTexture(),
      color: new Color(color),
      transparent: true,
      opacity,
      blending: AdditiveBlending,
      depthWrite: false,
    });
    this.disposableMaterials.push(material);
    this.nodeMaterials.push(material);
    const sprite = new Sprite(material);
    sprite.scale.set(scale, scale, 1);
    sprite.renderOrder = renderOrder;
    group.add(sprite);
    return sprite;
  }

  private buildEdges(links: GraphLink[]): void {
    for (const link of links) {
      const source = this.nodeById.get(link.source);
      const target = this.nodeById.get(link.target);
      if (!source || !target) continue;
      source.neighbors.add(target.data.id);
      target.neighbors.add(source.data.id);
      const colorSource = TYPE_COLOR_OBJECTS[source.data.type];
      const colorTarget = TYPE_COLOR_OBJECTS[target.data.type];
      const color = new Color().copy(colorSource).lerp(colorTarget, 0.5);
      const tint =
        link.type === "tagged"
          ? new Color("#FBBF24")
          : link.type === "related"
            ? new Color("#4FD8FF")
            : link.type === "cooccur"
              ? new Color("#A78BFA")
              : new Color("#7FB8FF");
      color.lerp(tint, 0.22);
      this.edges.push({
        source,
        target,
        type: link.type,
        weight: link.weight ?? 0.5,
        color,
      });
    }
  }

  /* ------------------------------- 力导向布局 ------------------------------ */

  private runSimulation(nodes: Node3D[], edges: Edge3D[], iterations: number): void {
    if (nodes.length === 0) return;
    const repulsion = 6000;
    const centerStrength = 0.0026;
    const gravityZ = 0.0014;
    const damping = 0.84;
    const maxSpeed = 17;

    for (let step = 0; step < iterations; step += 1) {
      for (const node of nodes) {
        node.vel.x -= node.pos.x * centerStrength;
        node.vel.y -= node.pos.y * (centerStrength * 1.05);
        node.vel.z -= node.pos.z * gravityZ;
      }

      for (let i = 0; i < nodes.length; i += 1) {
        const first = nodes[i];
        for (let j = i + 1; j < nodes.length; j += 1) {
          const second = nodes[j];
          const dx = first.pos.x - second.pos.x;
          const dy = first.pos.y - second.pos.y;
          const dz = first.pos.z - second.pos.z;
          const distanceSq = Math.max(64, dx * dx + dy * dy + dz * dz);
          const distance = Math.sqrt(distanceSq);
          const force = repulsion / distanceSq;
          const fx = (dx / distance) * force;
          const fy = (dy / distance) * force;
          const fz = (dz / distance) * force;
          first.vel.x += fx;
          first.vel.y += fy;
          first.vel.z += fz;
          second.vel.x -= fx;
          second.vel.y -= fy;
          second.vel.z -= fz;
        }
      }

      for (const edge of edges) {
        const dx = edge.target.pos.x - edge.source.pos.x;
        const dy = edge.target.pos.y - edge.source.pos.y;
        const dz = edge.target.pos.z - edge.source.pos.z;
        const distance = Math.max(1, Math.sqrt(dx * dx + dy * dy + dz * dz));
        const rest =
          (LINK_REST_LENGTH[edge.type] ?? 70) +
          edge.source.radius * 1.6 +
          edge.target.radius * 1.6;
        const spring = LINK_SPRING[edge.type] ?? 0.04;
        const strength = spring * (0.6 + clamp(edge.weight, 0.15, 1) * 0.9);
        const scale = (distance - rest) / distance;
        const fx = dx * scale * strength;
        const fy = dy * scale * strength;
        const fz = dz * scale * strength;
        edge.source.vel.x += fx;
        edge.source.vel.y += fy;
        edge.source.vel.z += fz;
        edge.target.vel.x -= fx;
        edge.target.vel.y -= fy;
        edge.target.vel.z -= fz;
      }

      for (const node of nodes) {
        node.vel.multiplyScalar(damping);
        const speed = node.vel.length();
        if (speed > maxSpeed) node.vel.multiplyScalar(maxSpeed / speed);
        node.pos.add(node.vel);
      }
    }
  }

  private normalizeGroup(nodes: Node3D[], targetRadius: number): void {
    if (nodes.length === 0) return;
    const centroid = new Vector3();
    for (const node of nodes) centroid.add(node.pos);
    centroid.multiplyScalar(1 / nodes.length);
    for (const node of nodes) node.pos.sub(centroid);

    const distances = nodes
      .map((node) => node.pos.length())
      .sort((first, second) => first - second);
    const typical = distances[Math.floor(distances.length * 0.9)] || 1;
    const clampRadius = typical * 1.45;
    const scale = targetRadius / typical;
    for (const node of nodes) {
      const distance = node.pos.length();
      if (distance > clampRadius) node.pos.multiplyScalar(clampRadius / distance);
      node.pos.multiplyScalar(scale);
      node.pos.x *= 1.16;
      node.pos.y *= 0.96;
      node.pos.z *= 0.86;
      node.vel.set(0, 0, 0);
    }
  }

  private groupNodesByZone(): ZoneGroupSource[] {
    const map = new Map<string, ZoneGroupSource>();
    for (const node of this.nodes) {
      const zoneId = node.data.zone_id ?? null;
      const key = zoneId ?? "__unassigned__";
      let group = map.get(key);
      if (!group) {
        group = { zoneId, key, nodes: [], edges: [], nodeSet: new Set<Node3D>() };
        map.set(key, group);
      }
      group.nodes.push(node);
      group.nodeSet.add(node);
    }
    for (const edge of this.edges) {
      for (const group of map.values()) {
        if (group.nodeSet.has(edge.source) && group.nodeSet.has(edge.target)) {
          group.edges.push(edge);
          break;
        }
      }
    }
    const order = new Map<string, number>();
    this.zoneMeta.forEach((zone, index) => order.set(zone.id ?? "__unassigned__", index));
    return [...map.values()].sort(
      (first, second) =>
        (order.get(first.key) ?? 999) - (order.get(second.key) ?? 999),
    );
  }

  private createZoneGroupInfo(
    source: ZoneGroupSource,
    center: Vector3,
    radius: number,
  ): ZoneGroup {
    const meta = this.zoneMeta.find((zone) => (zone.id ?? null) === source.zoneId);
    const sample = source.nodes[0]?.data;
    const documentCount =
      meta?.document_count ??
      source.nodes.filter((node) => node.data.type === "document").length;
    return {
      zoneId: source.zoneId,
      key: source.key,
      name: meta?.name ?? sample?.zone_name ?? (source.zoneId === null ? "未分区" : "分区"),
      color:
        meta?.color ??
        sample?.zone_color ??
        (source.zoneId === null ? "#5B6F8C" : "#4FD8FF"),
      documentCount,
      nodes: source.nodes,
      center,
      radius,
      labelEl: null,
      ringSprites: [],
    };
  }

  private layoutAndArrange(): void {
    if (this.nodes.length === 0) {
      this.zoneGroups = [];
      return;
    }
    if (this.layoutMode !== "zones") {
      this.runSimulation(this.nodes, this.edges, 350);
      this.normalizeGroup(this.nodes, TARGET_LAYOUT_RADIUS);
      this.zoneGroups = [];
      return;
    }

    const groups = this.groupNodesByZone();
    if (groups.length <= 1) {
      this.runSimulation(this.nodes, this.edges, 350);
      this.normalizeGroup(this.nodes, TARGET_LAYOUT_RADIUS);
      this.zoneGroups =
        groups.length === 1
          ? [
              this.createZoneGroupInfo(
                groups[0],
                new Vector3(0, 0, 0),
                TARGET_LAYOUT_RADIUS * 1.04,
              ),
            ]
          : [];
      return;
    }

    const prepared: { source: ZoneGroupSource; radius: number }[] = [];
    for (const source of groups) {
      this.runSimulation(source.nodes, source.edges, 320);
      const radius = clamp(112 + 20 * Math.sqrt(source.nodes.length), 130, 310);
      this.normalizeGroup(source.nodes, radius);
      prepared.push({ source, radius });
    }

    const count = prepared.length;
    // 岛视觉半径：节点范围 + 光晕/区域标签所需留白
    const visualRadius = (radius: number): number => radius * 1.18 + 52;
    const maxVisual = Math.max(...prepared.map((entry) => visualRadius(entry.radius)));
    const requiredDistance = (first: number, second: number): number =>
      visualRadius(first) + visualRadius(second) + 100;
    const adjacent = requiredDistance(
      Math.max(...prepared.map((entry) => entry.radius)),
      Math.max(...prepared.map((entry) => entry.radius)),
    );
    const ringRadius = count >= 2 ? adjacent / (2 * Math.sin(Math.PI / count)) : 0;

    const centers = prepared.map((entry, index) => {
      const angle = (index / count) * Math.PI * 2 + Math.PI * 0.16;
      return new Vector3(
        Math.cos(angle) * ringRadius,
        Math.sin(angle) * ringRadius * 0.72,
        (index % 2 === 0 ? 1 : -1) * Math.max(30, ringRadius * 0.06),
      );
    });

    // 迭代推开重叠（仅考虑 x/y 投影，确保 halo / 标签互不重叠）
    for (let iteration = 0; iteration < 160; iteration += 1) {
      let moved = false;
      for (let first = 0; first < count; first += 1) {
        for (let second = first + 1; second < count; second += 1) {
          const distanceNeed = requiredDistance(
            prepared[first].radius,
            prepared[second].radius,
          );
          const dx = centers[second].x - centers[first].x;
          const dy = centers[second].y - centers[first].y;
          const distance = Math.max(1, Math.hypot(dx, dy));
          if (distance >= distanceNeed) continue;
          const push = (distanceNeed - distance) / 2;
          const ux = dx / distance;
          const uy = dy / distance;
          centers[first].x -= ux * push;
          centers[first].y -= uy * push;
          centers[second].x += ux * push;
          centers[second].y += uy * push;
          moved = true;
        }
      }
      if (!moved) break;
    }

    // 重新居中，保持整体居中
    const centroid = new Vector3();
    for (const center of centers) centroid.add(center);
    centroid.multiplyScalar(1 / count);
    for (const center of centers) center.sub(centroid);

    this.zoneGroups = prepared.map((entry, index) => {
      const center = centers[index];
      for (const node of entry.source.nodes) node.pos.add(center);
      return this.createZoneGroupInfo(entry.source, center, entry.radius);
    });
  }

  private createZoneSprite(
    texture: Texture,
    color: string,
    opacity: number,
    size: number,
    center: Vector3,
    renderOrder: number,
  ): Sprite {
    const material = new SpriteMaterial({
      map: texture,
      color: new Color(color),
      transparent: true,
      opacity,
      blending: AdditiveBlending,
      depthWrite: false,
      depthTest: false,
    });
    this.disposableMaterials.push(material);
    this.nodeMaterials.push(material);
    const sprite = new Sprite(material);
    sprite.position.copy(center);
    sprite.scale.set(size, size, 1);
    sprite.renderOrder = renderOrder;
    this.graphRoot.add(sprite);
    return sprite;
  }

  private buildZoneVisuals(): void {
    const groups = this.zoneGroups;
    this.clearZoneVisuals();
    this.zoneGroups = groups;
    for (const group of groups) {
      const element = document.createElement("div");
      element.className = `graph3d-zone-label${group.zoneId === null ? " unassigned" : ""}`;
      element.style.setProperty("--zone-color", group.color);
      element.dataset.zoneId = group.zoneId ?? "unassigned";
      element.title = `${group.name} · ${group.documentCount} 篇文档`;

      const dot = document.createElement("span");
      dot.className = "zone-label-dot";
      dot.style.background = group.color;
      const name = document.createElement("span");
      name.className = "zone-label-name";
      name.textContent = group.name;
      const count = document.createElement("span");
      count.className = "zone-label-count";
      count.textContent = `${group.documentCount} 篇`;
      element.append(dot, name, count);
      element.addEventListener("click", (event) => {
        event.stopPropagation();
        const token = (event.currentTarget as HTMLElement).dataset.zoneId;
        this.onSelectZone?.(!token || token === "unassigned" ? null : token);
      });
      this.labelLayer.appendChild(element);
      group.labelEl = element;

      const isUnassigned = group.zoneId === null;
      const ring = this.createZoneSprite(
        getRingTexture(),
        group.color,
        isUnassigned ? 0.08 : 0.17,
        group.radius * 2.35,
        group.center,
        0,
      );
      const aura = this.createZoneSprite(
        getGlowTexture(),
        group.color,
        isUnassigned ? 0.02 : 0.05,
        group.radius * 3.6,
        group.center,
        -1,
      );
      group.ringSprites = [ring, aura];
    }
  }

  private clearZoneVisuals(): void {
    for (const group of this.zoneGroups) {
      group.labelEl?.remove();
      group.labelEl = null;
      for (const sprite of group.ringSprites) {
        this.graphRoot.remove(sprite);
        sprite.material.dispose();
      }
      group.ringSprites = [];
    }
    this.zoneGroups = [];
  }

  private updateZoneLabels(): void {
    if (this.zoneGroups.length === 0) return;
    const depthReference = Math.max(120, this.referenceDistance);
    for (const group of this.zoneGroups) {
      const element = group.labelEl;
      if (!element) continue;
      const anchor = this.tmpVec.copy(group.center);
      anchor.y += group.radius * 1.18 + 18;
      const projected = this.tmpVec2.copy(anchor).project(this.camera);
      if (projected.z > 1 || projected.z < -1) {
        element.style.visibility = "hidden";
        continue;
      }
      const x = (projected.x * 0.5 + 0.5) * this.width;
      const y = (-projected.y * 0.5 + 0.5) * this.height;
      const width = element.offsetWidth || 120;
      if (x < -width || x > this.width + width || y < -40 || y > this.height + 40) {
        element.style.visibility = "hidden";
        continue;
      }
      const cameraDistance = this.camera.position.distanceTo(group.center);
      const depthFactor = clamp(
        1.3 - (cameraDistance / depthReference - 0.55) * 0.9,
        0.5,
        1,
      );
      const safeTop = this.layoutMode === "zones" ? 168 : 8;
      const clampedY = clamp(y, safeTop, Math.max(safeTop, this.height - 34));
      element.style.visibility = "visible";
      element.style.transform = `translate3d(${Math.round(
        clamp(x - width / 2, 8, Math.max(8, this.width - width - 8)),
      )}px, ${Math.round(clampedY)}px, 0)`;
      element.style.opacity = String(depthFactor);
    }
  }

  private applyNodeTransforms(): void {
    for (const node of this.nodes) node.group.position.copy(node.pos);
  }

  /* --------------------------------- 连线 --------------------------------- */

  private rebuildEdgeGeometry(): void {
    this.disposeEdgeObjects();
    if (this.edges.length === 0) return;

    const positions = new Float32Array(this.edges.length * 6);
    const colors = new Float32Array(this.edges.length * 6);

    this.edges.forEach((edge, index) => {
      const offset = index * 6;
      positions[offset] = edge.source.pos.x;
      positions[offset + 1] = edge.source.pos.y;
      positions[offset + 2] = edge.source.pos.z;
      positions[offset + 3] = edge.target.pos.x;
      positions[offset + 4] = edge.target.pos.y;
      positions[offset + 5] = edge.target.pos.z;
      colors[offset] = edge.color.r;
      colors[offset + 1] = edge.color.g;
      colors[offset + 2] = edge.color.b;
      colors[offset + 3] = edge.color.r;
      colors[offset + 4] = edge.color.g;
      colors[offset + 5] = edge.color.b;
    });

    const geometry = new BufferGeometry();
    geometry.setAttribute("position", new BufferAttribute(positions, 3));
    geometry.setAttribute("color", new BufferAttribute(colors, 3));
    this.disposableGeometries.push(geometry);
    const material = new LineBasicMaterial({
      vertexColors: true,
      transparent: true,
      opacity: 0.4,
      blending: AdditiveBlending,
      depthWrite: false,
    });
    this.disposableMaterials.push(material);
    this.edgeGeometry = geometry;
    this.edgeMaterial = material;
    this.edgeLines = new LineSegments(geometry, material);
    this.edgeLines.renderOrder = 0;
    this.graphRoot.add(this.edgeLines);
  }

  private rebuildHighlightGeometry(): void {
    if (this.highlightLines) {
      this.graphRoot.remove(this.highlightLines);
      this.highlightLines.geometry.dispose();
      (this.highlightLines.material as LineBasicMaterial).dispose();
      this.highlightLines = null;
      this.highlightGeometry = null;
      this.highlightMaterial = null;
    }
    const focus = this.hovered ?? this.selected;
    if (!focus) return;
    const incident = this.edges.filter(
      (edge) => edge.source === focus || edge.target === focus,
    );
    if (incident.length === 0) return;

    const positions = new Float32Array(incident.length * 6);
    const colors = new Float32Array(incident.length * 6);
    incident.forEach((edge, index) => {
      const offset = index * 6;
      positions[offset] = edge.source.pos.x;
      positions[offset + 1] = edge.source.pos.y;
      positions[offset + 2] = edge.source.pos.z;
      positions[offset + 3] = edge.target.pos.x;
      positions[offset + 4] = edge.target.pos.y;
      positions[offset + 5] = edge.target.pos.z;
      const bright = new Color().copy(edge.color).lerp(new Color("#ffffff"), 0.35);
      colors[offset] = bright.r;
      colors[offset + 1] = bright.g;
      colors[offset + 2] = bright.b;
      colors[offset + 3] = bright.r;
      colors[offset + 4] = bright.g;
      colors[offset + 5] = bright.b;
    });
    const geometry = new BufferGeometry();
    geometry.setAttribute("position", new BufferAttribute(positions, 3));
    geometry.setAttribute("color", new BufferAttribute(colors, 3));
    const material = new LineBasicMaterial({
      vertexColors: true,
      transparent: true,
      opacity: 1,
      blending: AdditiveBlending,
      depthWrite: false,
    });
    this.disposableGeometries.push(geometry);
    this.disposableMaterials.push(material);
    this.highlightGeometry = geometry;
    this.highlightMaterial = material;
    this.highlightLines = new LineSegments(geometry, material);
    this.highlightLines.renderOrder = 1;
    this.graphRoot.add(this.highlightLines);
    this.updateEdgeOpacity();
  }

  private updateEdgeOpacity(): void {
    if (!this.edgeLines || !this.edgeMaterial) return;
    this.edgeMaterial.opacity = this.hovered || this.selected ? 0.13 : 0.4;
  }

  /* --------------------------------- 粒子 --------------------------------- */

  private buildPulses(): void {
    for (const pulse of this.pulses) {
      this.graphRoot.remove(pulse.sprite);
    }
    this.pulses = [];
    if (this.edges.length === 0) return;
    const colorCyan = new Color("#8CE8FF");
    const colorViolet = new Color("#C4B5FD");
    for (let index = 0; index < PULSE_COUNT; index += 1) {
      const edge = this.edges[Math.floor(Math.random() * this.edges.length)];
      const material = new SpriteMaterial({
        map: getGlowTexture(),
        color: Math.random() > 0.5 ? colorCyan : colorViolet,
        transparent: true,
        opacity: 0.6 + Math.random() * 0.35,
        blending: AdditiveBlending,
        depthWrite: false,
      });
      this.disposableMaterials.push(material);
      const sprite = new Sprite(material);
      const size = 5 + Math.random() * 4;
      sprite.scale.set(size, size, 1);
      sprite.visible = !this.reducedMotion;
      sprite.renderOrder = 3;
      this.graphRoot.add(sprite);
      this.pulses.push({
        edge,
        t: Math.random(),
        speed: 0.02 + Math.random() * 0.045,
        sprite,
      });
    }
  }

  /* ----------------------------- 选中 / 搜索环 ---------------------------- */

  private applySelectionRings(): void {
    for (const node of this.nodes) {
      const visible = this.selected === node;
      node.selectRing.visible = visible;
      if (visible) {
        const scale = node.radius * 4.6;
        node.selectRing.scale.set(scale, scale, 1);
      }
    }
  }

  private applySearchRings(): void {
    for (const node of this.nodes) {
      node.searchRing.visible = this.searchIds.has(node.data.id);
    }
  }

  /* ------------------------------- 交互事件 ------------------------------- */

  private attachEvents(): void {
    const canvas = this.renderer.domElement;
    canvas.addEventListener("pointermove", this.handlePointerMove);
    canvas.addEventListener("pointerdown", this.handlePointerDown);
    canvas.addEventListener("pointerup", this.handlePointerUp);
    canvas.addEventListener("pointerleave", this.handlePointerLeave);
    canvas.addEventListener("dblclick", this.handleDoubleClick);
    document.addEventListener("visibilitychange", this.handleVisibilityChange);
    this.running = true;
  }

  private detachEvents(): void {
    const canvas = this.renderer.domElement;
    canvas.removeEventListener("pointermove", this.handlePointerMove);
    canvas.removeEventListener("pointerdown", this.handlePointerDown);
    canvas.removeEventListener("pointerup", this.handlePointerUp);
    canvas.removeEventListener("pointerleave", this.handlePointerLeave);
    canvas.removeEventListener("dblclick", this.handleDoubleClick);
    document.removeEventListener("visibilitychange", this.handleVisibilityChange);
  }

  private handlePointerMove = (event: PointerEvent): void => {
    const point = this.toLocalPoint(event);
    const node = this.pickNode(point.x, point.y);
    if (node !== this.hovered) {
      this.hovered = node;
      this.updateFocus();
      this.rebuildHighlightGeometry();
      this.updateEdgeOpacity();
      this.renderer.domElement.style.cursor = node ? "pointer" : "grab";
    }
    this.onHover?.(node ? node.data : null, node ? point : null);
  };

  private handlePointerDown = (event: PointerEvent): void => {
    const point = this.toLocalPoint(event);
    this.pointerDown = { x: point.x, y: point.y, time: performance.now() };
    this.renderer.domElement.style.cursor = "grabbing";
  };

  private handlePointerUp = (event: PointerEvent): void => {
    const point = this.toLocalPoint(event);
    const moved = Math.hypot(point.x - this.pointerDown.x, point.y - this.pointerDown.y);
    const duration = performance.now() - this.pointerDown.time;
    this.renderer.domElement.style.cursor = this.hovered ? "pointer" : "grab";
    if (moved > 6 || duration > 700) return;
    const node = this.pickNode(point.x, point.y);
    if (node) {
      this.onSelect(node.data);
    } else {
      this.onSelect(null);
    }
  };

  private handlePointerLeave = (): void => {
    if (this.hovered) {
      this.hovered = null;
      this.updateFocus();
      this.rebuildHighlightGeometry();
      this.updateEdgeOpacity();
    }
    this.onHover?.(null, null);
    this.renderer.domElement.style.cursor = "grab";
  };

  private handleDoubleClick = (event: MouseEvent): void => {
    event.preventDefault();
    this.resetView();
  };

  private handleVisibilityChange = (): void => {
    if (this.disposed) return;
    if (document.hidden) {
      this.running = false;
      if (this.rafId != null) {
        cancelAnimationFrame(this.rafId);
        this.rafId = null;
      }
    } else if (!this.running) {
      this.running = true;
      this.lastTime = performance.now();
      this.rafId = requestAnimationFrame(this.animate);
    }
  };

  private toLocalPoint(event: PointerEvent | MouseEvent): Graph3DHoverPoint {
    const rect = this.renderer.domElement.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  private pickNode(x: number, y: number): Node3D | null {
    if (this.nodes.length === 0) return null;
    let picked: Node3D | null = null;
    let pickedDistance = Infinity;
    const halfHeight = this.height / 2;
    const tanHalfFov = Math.tan((FOV * Math.PI) / 360);
    for (const node of this.nodes) {
      const projected = this.tmpVec.copy(node.pos).project(this.camera);
      if (projected.z > 1 || projected.z < -1) continue;
      const screenX = (projected.x * 0.5 + 0.5) * this.width;
      const screenY = (-projected.y * 0.5 + 0.5) * this.height;
      const cameraDistance = this.camera.position.distanceTo(node.pos);
      if (cameraDistance <= 0) continue;
      const screenRadius =
        (node.radius * 2 * halfHeight) / (cameraDistance * tanHalfFov);
      const threshold = Math.max(15, screenRadius * 1.9);
      const delta = Math.hypot(x - screenX, y - screenY);
      if (delta <= threshold && cameraDistance < pickedDistance) {
        picked = node;
        pickedDistance = cameraDistance;
      }
    }
    return picked;
  }

  /* -------------------------------- 焦点状态 ------------------------------ */

  private updateFocus(): void {
    this.updateEdgeOpacity();
    for (const node of this.nodes) {
      node.selectRing.visible = this.selected === node;
    }
  }

  /* --------------------------------- 帧循环 ------------------------------- */

  private animate = (): void => {
    if (this.disposed || !this.running) return;
    this.rafId = requestAnimationFrame(this.animate);
    const now = performance.now();
    const deltaSeconds = Math.min(0.06, (now - this.lastTime) / 1000);
    this.lastTime = now;

    this.updateViewTween(deltaSeconds);
    this.controls.update();
    if (!this.reducedMotion) {
      for (const layer of this.starLayers) {
        layer.points.rotation.y += deltaSeconds * layer.spin;
        layer.points.rotation.x += deltaSeconds * layer.spin * 0.35;
      }
    }

    const focus = this.hovered ?? this.selected;
    const depthReference = Math.max(120, this.referenceDistance);
    for (const node of this.nodes) {
      const isFocus = focus === node;
      const isNeighbor = focus ? focus.neighbors.has(node.data.id) : false;
      const targetBoost = isFocus ? 1 : isNeighbor ? 0.3 : 0;
      node.hoverBoost += (targetBoost - node.hoverBoost) * Math.min(1, deltaSeconds * 14);

      const distance = this.camera.position.distanceTo(node.pos);
      const depthFactor = clamp(
        1.3 - (distance / depthReference - 0.55) * 0.9,
        0.45,
        1,
      );
      const dimension = focus ? (isFocus || isNeighbor ? 1 : 0.26) : 1;
      const boost = 1 + node.hoverBoost * 0.35;

      for (const layer of [node.glow, node.mid, node.core]) {
        layer.sprite.scale.set(layer.baseScale * boost, layer.baseScale * boost, 1);
        const material = layer.sprite.material;
        material.opacity = clamp(
          layer.baseOpacity * depthFactor * dimension * (1 + node.hoverBoost * 0.45),
          0,
          1,
        );
      }
      if (node.halo) {
        const pulse = this.reducedMotion
          ? 0
          : Math.sin(now / 1500 + node.radius) * 0.035;
        const scale = node.radius * 5 * (1 + pulse);
        node.halo.scale.set(scale, scale, 1);
        node.halo.material.opacity = 0.5 * depthFactor * dimension;
      }
      if (node.selectRing.visible) {
        const base = node.radius * 3.8;
        const pulse = this.reducedMotion ? 0 : Math.sin(now / 460) * 0.22;
        const scale = base + pulse;
        node.selectRing.scale.set(scale, scale, 1);
        node.selectRing.material.opacity = 0.62 * depthFactor;
      }
      if (node.searchRing.visible) {
        const base = node.radius * 4.4;
        const pulse = this.reducedMotion ? 1 : 1 + Math.sin(now / 320) * 0.08;
        node.searchRing.scale.set(base * pulse, base * pulse, 1);
        node.searchRing.material.opacity = 0.42 * depthFactor;
      }
    }

    this.updateLabels();
    this.updateZoneLabels();

    if (!this.reducedMotion) {
      for (const pulse of this.pulses) {
        pulse.t += pulse.speed * deltaSeconds;
        if (pulse.t > 1) pulse.t -= 1;
        const { source, target } = pulse.edge;
        pulse.sprite.position.set(
          source.pos.x + (target.pos.x - source.pos.x) * pulse.t,
          source.pos.y + (target.pos.y - source.pos.y) * pulse.t,
          source.pos.z + (target.pos.z - source.pos.z) * pulse.t,
        );
      }
    }

    this.renderer.render(this.scene, this.camera);
  };

  private updateViewTween(deltaSeconds: number): void {
    if (!this.viewTween) return;
    const tween = this.viewTween;
    tween.elapsed += deltaSeconds;
    const progress = clamp(tween.elapsed / tween.duration, 0, 1);
    const eased = progress < 0.5 ? 2 * progress * progress : 1 - Math.pow(-2 * progress + 2, 2) / 2;
    this.camera.position.lerpVectors(tween.fromPosition, tween.toPosition, eased);
    this.controls.target.lerpVectors(tween.fromTarget, tween.toTarget, eased);
    this.camera.lookAt(this.controls.target);
    if (progress >= 1) this.viewTween = null;
  }

  private tweenView(position: Vector3, target: Vector3, duration: number): void {
    this.viewTween = {
      fromPosition: this.camera.position.clone(),
      toPosition: position.clone(),
      fromTarget: this.controls.target.clone(),
      toTarget: target.clone(),
      elapsed: 0,
      duration,
    };
  }

  private updateLabels(): void {
    const focus = this.hovered ?? this.selected;
    const depthReference = Math.max(120, this.referenceDistance);
    const tanHalfFov = Math.tan((FOV * Math.PI) / 360);
    interface LabelItem {
      node: Node3D;
      x: number;
      y: number;
      screenRadius: number;
      depthFactor: number;
      dimension: number;
      priority: number;
      forceShow: boolean;
    }
    interface Rect {
      x1: number;
      y1: number;
      x2: number;
      y2: number;
      node?: Node3D;
    }
    const items: LabelItem[] = [];

    for (const node of this.nodes) {
      const projected = this.tmpVec.copy(node.pos).project(this.camera);
      if (projected.z > 1 || projected.z < -1) {
        if (node.labelEl) node.labelEl.style.visibility = "hidden";
        continue;
      }
      const x = (projected.x * 0.5 + 0.5) * this.width;
      const y = (-projected.y * 0.5 + 0.5) * this.height;
      const cameraDistance = this.camera.position.distanceTo(node.pos);
      const screenRadius =
        (node.radius * this.height) / (2 * cameraDistance * tanHalfFov);
      if (!node.labelEl) continue;
      if (x < -140 || x > this.width + 140 || y < -60 || y > this.height + 60) {
        node.labelEl.style.visibility = "hidden";
        continue;
      }
      const depthFactor = clamp(
        1.3 - (cameraDistance / depthReference - 0.55) * 0.9,
        0.4,
        1,
      );
      const isFocus = focus === node;
      const isNeighbor = focus ? focus.neighbors.has(node.data.id) : false;
      const dimension = focus ? (isFocus || isNeighbor ? 1 : 0.32) : 1;
      const kindBoost =
        node.labelKind === "document" ? 12000 : node.labelKind === "tag" ? 9000 : 0;
      const priority =
        (isFocus ? 100000 : 0) +
        (isNeighbor ? 50000 : 0) +
        kindBoost +
        (node.data.degree ?? 0) * 100 +
        (node.data.weight ?? 0);
      items.push({
        node,
        x,
        y,
        screenRadius,
        depthFactor,
        dimension,
        priority,
        forceShow: isFocus || isNeighbor,
      });
    }

    items.sort((first, second) => second.priority - first.priority);
    const placed: Rect[] = [];

    const collides = (rect: Rect, obstacles: Rect[]): boolean =>
      obstacles.some(
        (other) =>
          !(
            rect.x2 < other.x1 ||
            rect.x1 > other.x2 ||
            rect.y2 < other.y1 ||
            rect.y1 > other.y2
          ),
      );

    let conceptShown = 0;
    for (const item of items) {
      const element = item.node.labelEl;
      if (!element) continue;
      if (
        item.node.labelKind === "concept" &&
        !item.forceShow &&
        conceptShown >= MAX_CONCEPT_LABELS
      ) {
        element.style.visibility = "hidden";
        continue;
      }
      const width = item.node.labelWidth;
      const height = item.node.labelHeight;
      const gap = item.screenRadius + LABEL_TOP_PADDING;
      const candidates = [
        { left: item.x - width / 2, top: item.y + gap },
        { left: item.x - width / 2, top: item.y - gap - height },
        { left: item.x + item.screenRadius + 12, top: item.y - height / 2 },
        { left: item.x - item.screenRadius - 12 - width, top: item.y - height / 2 },
      ];
      const obstacles = placed;
      let chosen: { left: number; top: number } | null = null;
      for (const candidate of candidates) {
        const left = clamp(candidate.left, 6, Math.max(6, this.width - width - 6));
        const top = candidate.top;
        if (top < 4 || top + height > this.height - 4) continue;
        const rect: Rect = { x1: left - 4, y1: top - 3, x2: left + width + 4, y2: top + height + 3 };
        if (!collides(rect, obstacles)) {
          chosen = { left, top };
          break;
        }
      }
      if (!chosen) {
        if (!item.forceShow) {
          element.style.visibility = "hidden";
          continue;
        }
        chosen = {
          left: clamp(item.x - width / 2, 6, Math.max(6, this.width - width - 6)),
          top: item.y + gap,
        };
      }
      placed.push({
        x1: chosen.left - 4,
        y1: chosen.top - 3,
        x2: chosen.left + width + 4,
        y2: chosen.top + height + 3,
      });
      element.style.visibility = "visible";
      element.style.transform = `translate3d(${Math.round(chosen.left)}px, ${Math.round(chosen.top)}px, 0)`;
      element.style.opacity = String(
        clamp(0.97 * item.depthFactor * item.dimension, 0.34, 1),
      );
      if (item.node.labelKind === "concept" && !item.forceShow) conceptShown += 1;
    }
  }

  private handleControlsStart = (): void => {
    this.controls.autoRotate = false;
    if (this.autoRotateResumeTimer != null) {
      window.clearTimeout(this.autoRotateResumeTimer);
      this.autoRotateResumeTimer = null;
    }
  };

  private handleControlsEnd = (): void => {
    if (this.autoRotateResumeTimer != null) {
      window.clearTimeout(this.autoRotateResumeTimer);
    }
    this.autoRotateResumeTimer = window.setTimeout(() => {
      this.autoRotateResumeTimer = null;
      this.controls.autoRotate = this.autoRotateEnabled && !this.reducedMotion;
    }, 1800);
  };

  /* --------------------------------- 清理 --------------------------------- */

  private clearGraphObjects(): void {
    this.clearZoneVisuals();
    for (const pulse of this.pulses) {
      this.graphRoot.remove(pulse.sprite);
      pulse.sprite.material.dispose();
    }
    for (const node of this.nodes) {
      this.graphRoot.remove(node.group);
      node.group.clear();
      node.labelEl?.remove();
    }
    for (const material of this.nodeMaterials) material.dispose();
    for (const texture of this.nodeTextures) texture.dispose();
    this.nodeMaterials.length = 0;
    this.nodeTextures.length = 0;

    this.nodes = [];
    this.nodeById.clear();
    this.edges = [];
    this.pulses = [];

    if (this.edgeLines) {
      this.graphRoot.remove(this.edgeLines);
      this.edgeLines.geometry.dispose();
      (this.edgeLines.material as LineBasicMaterial).dispose();
      this.edgeLines = null;
    }
    if (this.highlightLines) {
      this.graphRoot.remove(this.highlightLines);
      this.highlightLines.geometry.dispose();
      (this.highlightLines.material as LineBasicMaterial).dispose();
      this.highlightLines = null;
    }
    this.edgeGeometry = null;
    this.edgeMaterial = null;
    this.highlightGeometry = null;
    this.highlightMaterial = null;
    this.hovered = null;
    this.selected = null;
    this.searchIds.clear();
  }

  private disposeEdgeObjects(): void {
    if (this.edgeLines) {
      this.graphRoot.remove(this.edgeLines);
      this.edgeLines.geometry.dispose();
      (this.edgeLines.material as LineBasicMaterial).dispose();
      this.edgeLines = null;
    }
    if (this.highlightLines) {
      this.graphRoot.remove(this.highlightLines);
      this.highlightLines.geometry.dispose();
      (this.highlightLines.material as LineBasicMaterial).dispose();
      this.highlightLines = null;
    }
    this.edgeGeometry = null;
    this.edgeMaterial = null;
    this.highlightGeometry = null;
    this.highlightMaterial = null;
  }
}
