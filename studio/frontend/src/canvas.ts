// Konva-based tile map renderer with RAF-driven animation and
// incremental paint/erase patches.

import Konva from "konva";
import type { MapState, TileAsset, ToolMode } from "./types";
import { SpriteCache } from "./sprites";

interface AnimHandle {
  node: Konva.Image;
  frames: HTMLImageElement[];
  cumulative: number[]; // running sum of durations (ms)
  total: number;
  layer: Konva.Layer;
  current: number;
}

interface LayerHandle {
  name: string;
  konvaLayer: Konva.Layer;
  grid: (Konva.Image | null)[][]; // [y][x] -> node (null if empty)
  anims: Map<Konva.Image, AnimHandle>;
}

interface ObjectGroupHandle {
  name: string;
  konvaLayer: Konva.Layer;
  nodes: Map<number, Konva.Image>; // obj.id -> node
  anims: Map<Konva.Image, AnimHandle>;
}

type CursorCb = (tileX: number, tileY: number) => void;
type PaintCb = (tileX: number, tileY: number, dragging: boolean) => void;
type SelectCb = (
  x0: number, y0: number, x1: number, y1: number, finalized: boolean,
) => void;

export class MapRenderer {
  private stage: Konva.Stage;
  private host: HTMLDivElement;
  private sprites: SpriteCache;
  private state: MapState | null = null;

  private tileLayers = new Map<string, LayerHandle>();
  private objectGroups = new Map<string, ObjectGroupHandle>();
  private rafId: number | null = null;
  private startTime = 0;
  private fpsSamples: number[] = [];
  private lastFrameTs = 0;

  private cursorCb: CursorCb = () => {};
  private paintCb: PaintCb = () => {};
  private selectCb: SelectCb = () => {};

  private mode: ToolMode = "view";
  private painting = false;

  // Selection overlay state
  private overlayLayer: Konva.Layer | null = null;
  private selectionRect: Konva.Rect | null = null;
  private selectionAnchor: { tx: number; ty: number } | null = null;
  private currentSelection:
    { x0: number; y0: number; x1: number; y1: number } | null = null;

  constructor(host: HTMLDivElement, sprites: SpriteCache) {
    this.host = host;
    this.sprites = sprites;
    this.stage = new Konva.Stage({
      container: host,
      width: host.clientWidth,
      height: host.clientHeight,
      draggable: true,
    });

    new ResizeObserver(() => {
      this.stage.width(this.host.clientWidth);
      this.stage.height(this.host.clientHeight);
    }).observe(this.host);

    this.bindZoom();
    this.bindPointer();
  }

  onCursor(cb: CursorCb): void {
    this.cursorCb = cb;
  }

  onPaint(cb: PaintCb): void {
    this.paintCb = cb;
  }

  onSelect(cb: SelectCb): void {
    this.selectCb = cb;
  }

  setMode(m: ToolMode): void {
    this.mode = m;
    // Only "view" mode allows stage drag (pan).
    this.stage.draggable(m === "view");
    this.host.style.cursor =
      m === "view" ? "grab" :
      m === "erase" ? "not-allowed" :
      m === "select" ? "crosshair" : "crosshair";
  }

  setSelectionRect(
    sel: { x0: number; y0: number; x1: number; y1: number } | null,
  ): void {
    this.currentSelection = sel;
    this.drawSelectionOverlay();
  }

  private ensureOverlayLayer(): Konva.Layer {
    if (!this.overlayLayer) {
      this.overlayLayer = new Konva.Layer({ listening: false });
      this.stage.add(this.overlayLayer);
    } else {
      this.overlayLayer.moveToTop();
    }
    return this.overlayLayer;
  }

  private drawSelectionOverlay(): void {
    const layer = this.ensureOverlayLayer();
    if (this.selectionRect) {
      this.selectionRect.destroy();
      this.selectionRect = null;
    }
    if (!this.currentSelection || !this.state) {
      layer.batchDraw();
      return;
    }
    const { x0, y0, x1, y1 } = this.currentSelection;
    const tw = this.state.tile_w, th = this.state.tile_h;
    this.selectionRect = new Konva.Rect({
      x: Math.min(x0, x1) * tw,
      y: Math.min(y0, y1) * th,
      width: (Math.abs(x1 - x0) + 1) * tw,
      height: (Math.abs(y1 - y0) + 1) * th,
      stroke: "#5aa0ff",
      strokeWidth: 2,
      strokeScaleEnabled: false,
      dash: [6, 4],
      fill: "rgba(90,160,255,0.08)",
      listening: false,
    });
    layer.add(this.selectionRect);
    layer.batchDraw();
  }

  getMode(): ToolMode {
    return this.mode;
  }

  render(state: MapState): void {
    this.state = state;
    this.stage.destroyChildren();
    this.tileLayers.clear();
    this.objectGroups.clear();
    // Overlay layer lives under stage; destroyChildren wiped it — drop ref.
    this.overlayLayer = null;
    this.selectionRect = null;

    if (state.empty || !state.width || !state.height) {
      this.stop();
      return;
    }

    // Background
    const bg = new Konva.Layer({ listening: false });
    bg.add(new Konva.Rect({
      x: 0, y: 0,
      width: state.width * state.tile_w,
      height: state.height * state.tile_h,
      fill: "#14171c",
      stroke: "#2a2f38",
      strokeWidth: 1,
    }));
    this.stage.add(bg);

    // Tile layers
    for (const layer of state.layers) {
      const kl = new Konva.Layer({
        listening: false,
        visible: layer.visible,
        opacity: layer.opacity,
      });
      const grid: (Konva.Image | null)[][] = [];
      const anims = new Map<Konva.Image, AnimHandle>();
      for (let y = 0; y < state.height; y++) {
        const row = layer.data[y] || [];
        const outRow: (Konva.Image | null)[] = [];
        for (let x = 0; x < state.width; x++) {
          const key = row[x] ?? null;
          outRow.push(
            key ? this.makeTileNode(state, key, x, y, kl, anims) : null
          );
        }
        grid.push(outRow);
      }
      this.stage.add(kl);
      this.tileLayers.set(layer.name, {
        name: layer.name, konvaLayer: kl, grid, anims,
      });
    }

    // Object groups
    for (const og of state.object_groups) {
      const kl = new Konva.Layer({
        listening: false,
        visible: og.visible,
        opacity: og.opacity,
      });
      const nodes = new Map<number, Konva.Image>();
      const anims = new Map<Konva.Image, AnimHandle>();
      for (const obj of og.objects) {
        const node = this.makeObjectNode(state, obj.key, obj, kl, anims);
        if (node) nodes.set(obj.id, node);
      }
      this.stage.add(kl);
      this.objectGroups.set(og.name, {
        name: og.name, konvaLayer: kl, nodes, anims,
      });
    }

    this.fit();
    this.start();
    this.updateAnimCount();
  }

  // ------------------------------------------------------------------
  // Node creation helpers
  // ------------------------------------------------------------------

  private makeTileNode(
    state: MapState,
    key: string,
    x: number,
    y: number,
    layer: Konva.Layer,
    anims: Map<Konva.Image, AnimHandle>,
  ): Konva.Image | null {
    const asset = state.tiles[key];
    const img = this.sprites.get(key);
    if (!asset || !img) return null;
    const node = new Konva.Image({
      x: x * state.tile_w,
      y: y * state.tile_h,
      width: state.tile_w,
      height: state.tile_h,
      image: img,
      perfectDrawEnabled: false,
    });
    layer.add(node);
    const a = this.buildAnim(asset, node, layer);
    if (a) anims.set(node, a);
    return node;
  }

  private makeObjectNode(
    state: MapState,
    key: string,
    obj: { x: number; y: number; w: number; h: number },
    layer: Konva.Layer,
    anims: Map<Konva.Image, AnimHandle>,
  ): Konva.Image | null {
    const asset = state.tiles[key];
    const img = this.sprites.get(key);
    if (!asset || !img) return null;
    const w = obj.w || asset.w;
    const h = obj.h || asset.h;
    const node = new Konva.Image({
      x: obj.x, y: obj.y - h, width: w, height: h,
      image: img, perfectDrawEnabled: false,
    });
    layer.add(node);
    const a = this.buildAnim(asset, node, layer);
    if (a) anims.set(node, a);
    return node;
  }

  private buildAnim(
    asset: TileAsset,
    node: Konva.Image,
    layer: Konva.Layer,
  ): AnimHandle | null {
    if (!asset.animation || asset.animation.length === 0) return null;
    const frames: HTMLImageElement[] = [];
    const cumulative: number[] = [];
    let total = 0;
    for (const fr of asset.animation) {
      const im = this.sprites.get(fr.key);
      if (!im) continue;
      frames.push(im);
      total += Math.max(1, fr.duration);
      cumulative.push(total);
    }
    if (frames.length === 0) return null;
    return { node, frames, cumulative, total, layer, current: -1 };
  }

  // ------------------------------------------------------------------
  // Incremental paint patch
  // ------------------------------------------------------------------

  applyPaintPatch(
    layer: string,
    cells: { x: number; y: number; key: string | null }[],
  ): void {
    if (!this.state) return;
    const lh = this.tileLayers.get(layer);
    if (!lh) return;
    // Also mutate the state object so sprite palette / counts stay accurate.
    const stLayer = this.state.layers.find((l) => l.name === layer);

    for (const c of cells) {
      const { x, y, key } = c;
      if (y < 0 || y >= lh.grid.length) continue;
      if (x < 0 || x >= lh.grid[y].length) continue;
      if (stLayer) {
        if (!stLayer.data[y]) stLayer.data[y] = [];
        stLayer.data[y][x] = key;
      }
      const old = lh.grid[y][x];
      if (old) {
        lh.anims.delete(old);
        old.destroy();
        lh.grid[y][x] = null;
      }
      if (key) {
        const node = this.makeTileNode(
          this.state, key, x, y, lh.konvaLayer, lh.anims,
        );
        lh.grid[y][x] = node;
      }
    }
    lh.konvaLayer.batchDraw();
    this.updateAnimCount();
  }

  // ------------------------------------------------------------------
  // Animation loop
  // ------------------------------------------------------------------

  private start(): void {
    if (this.rafId !== null) return;
    this.startTime = performance.now();
    const tick = (ts: number) => {
      this.rafId = requestAnimationFrame(tick);
      this.tickAnim(ts - this.startTime);
      this.tickFps(ts);
    };
    this.rafId = requestAnimationFrame(tick);
  }

  private stop(): void {
    if (this.rafId !== null) {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
  }

  private tickAnim(elapsed: number): void {
    const dirty = new Set<Konva.Layer>();
    for (const lh of this.tileLayers.values()) {
      if (!lh.konvaLayer.visible()) continue;
      for (const a of lh.anims.values()) {
        if (this.advanceAnim(a, elapsed)) dirty.add(lh.konvaLayer);
      }
    }
    for (const og of this.objectGroups.values()) {
      if (!og.konvaLayer.visible()) continue;
      for (const a of og.anims.values()) {
        if (this.advanceAnim(a, elapsed)) dirty.add(og.konvaLayer);
      }
    }
    for (const kl of dirty) kl.batchDraw();
  }

  private advanceAnim(a: AnimHandle, elapsed: number): boolean {
    const pos = elapsed % a.total;
    let idx = 0;
    while (idx < a.cumulative.length - 1 && pos >= a.cumulative[idx]) idx++;
    if (idx === a.current) return false;
    a.current = idx;
    a.node.image(a.frames[idx]);
    return true;
  }

  private tickFps(ts: number): void {
    if (this.lastFrameTs > 0) {
      this.fpsSamples.push(ts - this.lastFrameTs);
      if (this.fpsSamples.length > 60) this.fpsSamples.shift();
    }
    this.lastFrameTs = ts;
    if (Math.floor(ts / 500) !== Math.floor((ts - 16) / 500)) {
      const avg = this.fpsSamples.reduce((a, b) => a + b, 0) /
        Math.max(1, this.fpsSamples.length);
      const fps = avg > 0 ? 1000 / avg : 0;
      const el = document.getElementById("fps");
      if (el) el.textContent = `${fps.toFixed(0)} fps`;
    }
  }

  private updateAnimCount(): void {
    let n = 0;
    for (const lh of this.tileLayers.values()) n += lh.anims.size;
    for (const og of this.objectGroups.values()) n += og.anims.size;
    const el = document.getElementById("anim-count");
    if (el) el.textContent = `${n} anim`;
  }

  // ------------------------------------------------------------------
  // View controls
  // ------------------------------------------------------------------

  fit(): void {
    if (!this.state) return;
    const mapW = this.state.width * this.state.tile_w;
    const mapH = this.state.height * this.state.tile_h;
    if (mapW === 0 || mapH === 0) return;
    const pad = 24;
    const sx = (this.stage.width() - pad * 2) / mapW;
    const sy = (this.stage.height() - pad * 2) / mapH;
    const s = Math.min(sx, sy);
    this.stage.scale({ x: s, y: s });
    this.stage.position({
      x: (this.stage.width() - mapW * s) / 2,
      y: (this.stage.height() - mapH * s) / 2,
    });
    this.stage.batchDraw();
  }

  private bindZoom(): void {
    const minScale = 0.05;
    const maxScale = 8;
    this.stage.on("wheel", (e) => {
      e.evt.preventDefault();
      const oldScale = this.stage.scaleX();
      const pointer = this.stage.getPointerPosition();
      if (!pointer) return;
      const mousePointTo = {
        x: (pointer.x - this.stage.x()) / oldScale,
        y: (pointer.y - this.stage.y()) / oldScale,
      };
      const dir = e.evt.deltaY > 0 ? 1 / 1.12 : 1.12;
      let newScale = oldScale * dir;
      newScale = Math.max(minScale, Math.min(maxScale, newScale));
      this.stage.scale({ x: newScale, y: newScale });
      this.stage.position({
        x: pointer.x - mousePointTo.x * newScale,
        y: pointer.y - mousePointTo.y * newScale,
      });
      this.stage.batchDraw();
    });
  }

  private bindPointer(): void {
    const toTile = () => {
      if (!this.state) return null;
      const p = this.stage.getPointerPosition();
      if (!p) return null;
      const s = this.stage.scaleX();
      const wx = (p.x - this.stage.x()) / s;
      const wy = (p.y - this.stage.y()) / s;
      const tx = Math.floor(wx / this.state.tile_w);
      const ty = Math.floor(wy / this.state.tile_h);
      return { tx, ty };
    };

    this.stage.on("mousemove", () => {
      const t = toTile();
      if (!t) return;
      this.cursorCb(t.tx, t.ty);
      if (this.mode === "select" && this.selectionAnchor) {
        const { tx, ty } = this.selectionAnchor;
        const x0 = Math.min(tx, t.tx), y0 = Math.min(ty, t.ty);
        const x1 = Math.max(tx, t.tx), y1 = Math.max(ty, t.ty);
        this.currentSelection = { x0, y0, x1, y1 };
        this.drawSelectionOverlay();
        this.selectCb(x0, y0, x1, y1, false);
      } else if (this.painting &&
                 (this.mode === "paint" || this.mode === "erase")) {
        this.paintCb(t.tx, t.ty, true);
      }
    });

    this.stage.on("mousedown", (e) => {
      if (this.mode === "view") return;
      if (e.evt.button !== 0) return; // left only
      const t = toTile();
      if (!t) return;
      if (this.mode === "select") {
        this.selectionAnchor = { tx: t.tx, ty: t.ty };
        this.currentSelection = { x0: t.tx, y0: t.ty, x1: t.tx, y1: t.ty };
        this.drawSelectionOverlay();
      } else {
        this.painting = true;
        this.paintCb(t.tx, t.ty, false);
      }
    });

    const stopPaint = () => { this.painting = false; };
    const endSelect = () => {
      if (this.mode === "select" && this.selectionAnchor &&
          this.currentSelection) {
        const s = this.currentSelection;
        this.selectCb(s.x0, s.y0, s.x1, s.y1, true);
      }
      this.selectionAnchor = null;
    };
    this.stage.on("mouseup", () => { stopPaint(); endSelect(); });
    this.stage.on("mouseleave", () => { stopPaint(); endSelect(); });
    window.addEventListener("blur", () => { stopPaint(); endSelect(); });
  }

  // ------------------------------------------------------------------
  // Layer visibility / opacity
  // ------------------------------------------------------------------

  setLayerVisible(name: string, visible: boolean): void {
    const lh = this.tileLayers.get(name);
    if (lh) { lh.konvaLayer.visible(visible); lh.konvaLayer.batchDraw(); }
  }

  setLayerOpacity(name: string, opacity: number): void {
    const lh = this.tileLayers.get(name);
    if (lh) { lh.konvaLayer.opacity(opacity); lh.konvaLayer.batchDraw(); }
  }

  setObjectGroupVisible(name: string, visible: boolean): void {
    const og = this.objectGroups.get(name);
    if (og) { og.konvaLayer.visible(visible); og.konvaLayer.batchDraw(); }
  }

  setObjectGroupOpacity(name: string, opacity: number): void {
    const og = this.objectGroups.get(name);
    if (og) { og.konvaLayer.opacity(opacity); og.konvaLayer.batchDraw(); }
  }

  // Test/debug helper: does the Konva grid carry a node at (layer, x, y)?
  inspectCell(layer: string, x: number, y: number): boolean {
    const lh = this.tileLayers.get(layer);
    if (!lh) return false;
    if (y < 0 || y >= lh.grid.length) return false;
    if (x < 0 || x >= lh.grid[y].length) return false;
    return lh.grid[y][x] !== null;
  }
}
