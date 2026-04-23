// Entrypoint: wires WS <-> SpriteCache <-> MapRenderer <-> UI panels <-> ToolController.

import { StudioWS } from "./ws";
import { SpriteCache } from "./sprites";
import { MapRenderer } from "./canvas";
import { renderLayerPanel, renderInfoPanel, renderMeta } from "./layers";
import {
  ToolController, populateLayerSelect, populatePalette, populateWangPanel,
} from "./tools";
import type { MapState, WsEvent } from "./types";

async function boot() {
  const host = document.getElementById("canvas-host") as HTMLDivElement | null;
  if (!host) throw new Error("missing #canvas-host");

  const sprites = new SpriteCache();
  const renderer = new MapRenderer(host, sprites);
  const tools = new ToolController();

  // UI plumbing
  let wsRef: StudioWS | null = null;
  tools.bind(
    (m) => renderer.setMode(m === "wang" ? "paint" : m),
    () => { /* active layer change has no direct renderer hook yet */ },
    () => { /* palette selection — handled in paint callback */ },
    (sel) => {
      renderer.setSelectionRect(sel);
      // On explicit clear (Esc), tell the bridge so last_selection resets.
      if (sel === null && wsRef) wsRef.send({
        type: "selection", selection: null,
      });
    },
    () => { /* wang selection changes are displayed inline in tools.ts */ },
  );
  tools.setMode("view"); // initial

  // Cursor + paint callbacks
  const cursorEl = document.getElementById("cursor-tile");
  renderer.onCursor((tx, ty) => {
    if (cursorEl) cursorEl.textContent = `${tx}, ${ty}`;
  });

  const ws = new StudioWS("/ws");
  wsRef = ws;

  // Selection finalize: push to tool state + bridge
  renderer.onSelect((x0, y0, x1, y1, finalized) => {
    const { mode, activeLayer } = tools.state;
    if (mode !== "select" || !activeLayer) return;
    if (!finalized) {
      // Live drag preview — update tool state without broadcasting.
      tools.setSelection({ layer: activeLayer, x0, y0, x1, y1 });
      return;
    }
    const sel = { layer: activeLayer, x0, y0, x1, y1 };
    tools.setSelection(sel);
    ws.send({ type: "selection", selection: sel });
  });

  // Drag dedup: during a continuous drag, skip cells we just painted with
  // the same key so we don't spam the WS with redundant patches while the
  // cursor hovers inside one tile at 60 Hz.
  let lastPaintKey: string | null = null;
  let lastPaintX = -1, lastPaintY = -1;
  let lastPaintMode: string = "";
  renderer.onPaint((tx, ty, dragging) => {
    const { mode, activeLayer, selectedKey, wang } = tools.state;
    if (mode === "view" || mode === "select" || !activeLayer) return;

    // WANG MODE — delegate to bridge's autotile resolver.
    if (mode === "wang") {
      if (!wang) return;
      // Dedup on cell only (wang resolves its own 3x3 neighborhood).
      if (dragging && tx === lastPaintX && ty === lastPaintY &&
          mode === lastPaintMode) return;
      lastPaintKey = null; lastPaintX = tx; lastPaintY = ty;
      lastPaintMode = mode;
      ws.send({
        type: "wang_paint",
        layer: activeLayer,
        wangset_uid: wang.wangset_uid,
        color: wang.color,
        cells: [{ x: tx, y: ty }],
      });
      return;
    }

    let key: string | null;
    if (mode === "erase") key = null;
    else {
      if (!selectedKey) return;
      key = selectedKey;
    }
    if (dragging && tx === lastPaintX && ty === lastPaintY &&
        key === lastPaintKey && mode === lastPaintMode) return;
    lastPaintKey = key; lastPaintX = tx; lastPaintY = ty; lastPaintMode = mode;
    // Send as a single-cell patch. Bridge will broadcast and we'll apply
    // on receipt (so no local optimistic update needed — keeps state
    // single-source-of-truth).
    ws.send({
      type: "patch", op: "paint",
      layer: activeLayer,
      cells: [{ x: tx, y: ty, key }],
    });
  });

  // Header buttons
  document.getElementById("btn-fit")?.addEventListener("click", () => {
    renderer.fit();
  });
  document.getElementById("btn-reload")?.addEventListener("click", async () => {
    await loadFromHttp(renderer, sprites, tools);
  });

  // Undo / redo — bridge owns the history, so every click or shortcut
  // just POSTs and lets the WS broadcast apply the inverse patch back.
  const undoBtn = document.getElementById("btn-undo") as HTMLButtonElement
    | null;
  const redoBtn = document.getElementById("btn-redo") as HTMLButtonElement
    | null;

  async function refreshHistoryUI(): Promise<void> {
    try {
      const r = await fetch("/history");
      if (!r.ok) return;
      const h = await r.json() as {undo_depth: number; redo_depth: number};
      if (undoBtn) undoBtn.disabled = h.undo_depth === 0;
      if (redoBtn) redoBtn.disabled = h.redo_depth === 0;
    } catch { /* offline — leave buttons as-is */ }
  }

  async function doUndo(): Promise<void> {
    try {
      const r = await fetch("/undo", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "{}",
      });
      if (!r.ok) console.warn("[studio] undo failed:", await r.text());
    } catch (e) {
      console.warn("[studio] undo threw:", e);
    } finally {
      void refreshHistoryUI();
    }
  }
  async function doRedo(): Promise<void> {
    try {
      const r = await fetch("/redo", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "{}",
      });
      if (!r.ok) console.warn("[studio] redo failed:", await r.text());
    } catch (e) {
      console.warn("[studio] redo threw:", e);
    } finally {
      void refreshHistoryUI();
    }
  }

  undoBtn?.addEventListener("click", () => void doUndo());
  redoBtn?.addEventListener("click", () => void doRedo());

  // Ctrl/Cmd+Z = undo, Ctrl/Cmd+Shift+Z or Ctrl+Y = redo.
  // Skip when typing in an input/select/textarea.
  window.addEventListener("keydown", (e) => {
    const tgt = e.target as HTMLElement | null;
    if (tgt && (tgt.tagName === "INPUT" || tgt.tagName === "SELECT" ||
                tgt.tagName === "TEXTAREA")) return;
    const mod = e.ctrlKey || e.metaKey;
    if (!mod) return;
    if ((e.key === "z" || e.key === "Z") && !e.shiftKey) {
      e.preventDefault();
      void doUndo();
    } else if ((e.key === "z" || e.key === "Z") && e.shiftKey) {
      e.preventDefault();
      void doRedo();
    } else if (e.key === "y" || e.key === "Y") {
      e.preventDefault();
      void doRedo();
    }
  });

  // Refresh history UI every time a patch lands (keeps buttons in sync
  // even when another client undoes, or after our own paint).
  // We'll also call it once after boot.
  void refreshHistoryUI();

  // Wang "Fill selection" — sends current selection + active wang color
  // to the bridge. Server broadcasts the resulting paint patch via WS,
  // which this client applies like any other tile change.
  document.getElementById("wang-fill-selection")
    ?.addEventListener("click", async () => {
      const w = tools.state.wang;
      const s = tools.state.selection;
      if (!w || !s) return;
      try {
        const resp = await fetch("/wang/fill_rect", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            layer: s.layer,
            wangset_uid: w.wangset_uid,
            color: w.color,
            x0: s.x0, y0: s.y0, x1: s.x1, y1: s.y1,
          }),
        });
        if (!resp.ok) {
          const txt = await resp.text();
          console.warn("[studio] wang fill failed:", txt);
          const el = document.getElementById("ws-status");
          if (el) {
            el.textContent = `wang fill error: ${txt}`;
            el.className = "err";
          }
        }
      } catch (e) {
        console.warn("[studio] wang fill threw:", e);
      }
    });

  // WebSocket events
  ws.on((ev: WsEvent) => {
    if (ev.type === "map_loaded") {
      void applyState(ev.state, renderer, sprites, tools);
      void refreshHistoryUI();
    } else if (ev.type === "patch" && ev.op === "paint") {
      renderer.applyPaintPatch(ev.layer, ev.cells);
      void refreshHistoryUI();
    } else if (ev.type === "patch" && ev.op === "object") {
      // Object patches are rare — lazy reload for now (Phase 2 scope)
      void loadFromHttp(renderer, sprites, tools);
    } else if (ev.type === "selection") {
      // Another client (or bridge) changed the selection — mirror it.
      tools.setSelection(ev.selection);
      // setSelection already calls renderer.setSelectionRect via the
      // onSelectionChange callback wired above.
    } else if (ev.type === "error") {
      console.warn("[studio] bridge error:", ev.message);
      const el = document.getElementById("ws-status");
      if (el) {
        el.textContent = `WS: error — ${ev.message}`;
        el.className = "err";
        // Revert to "connected" after 3s so the error doesn't stick forever.
        window.setTimeout(() => {
          if (ws.isAlive()) {
            el.textContent = "WS: connected";
            el.className = "ok";
          }
        }, 3000);
      }
    }
  });
  ws.start();

  await loadFromHttp(renderer, sprites, tools);

  // Dev/test hook — not part of public API. Exposes the wired components so
  // end-to-end tests can drive paint, flip modes, inspect Konva grid, etc.
  (window as unknown as { __studio: unknown }).__studio = {
    renderer, tools, sprites, ws,
  };
}

async function loadFromHttp(
  renderer: MapRenderer,
  sprites: SpriteCache,
  tools: ToolController,
): Promise<void> {
  try {
    const res = await fetch("/state");
    if (!res.ok) return;
    const state = (await res.json()) as MapState;
    await applyState(state, renderer, sprites, tools);
  } catch (e) {
    console.warn("[studio] HTTP state fetch failed:", e);
  }
}

async function applyState(
  state: MapState,
  renderer: MapRenderer,
  sprites: SpriteCache,
  tools: ToolController,
): Promise<void> {
  if (state.empty) {
    renderMeta(state);
    renderer.render(state);
    return;
  }
  renderMeta(state);
  renderInfoPanel(state);
  const keys = sprites.collectKeys(state);
  await sprites.loadAll(keys);
  renderer.render(state);
  renderLayerPanel(state, renderer);
  populateLayerSelect(state, tools);
  populatePalette(state, tools);
  // Populate wang panel asynchronously — failure is non-fatal, panel just
  // stays empty (e.g. if DB has no wangsets for this TMX).
  void populateWangPanel(tools);
}

boot().catch((e) => {
  console.error(e);
  const el = document.getElementById("ws-status");
  if (el) {
    el.textContent = `boot error: ${String(e)}`;
    el.className = "err";
  }
});
