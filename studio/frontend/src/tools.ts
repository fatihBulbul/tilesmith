// Tool state + toolbar + palette UI.

import type {
  MapState, Selection, ToolMode, WangSelection, WangSet,
} from "./types";

export interface ToolState {
  mode: ToolMode;
  activeLayer: string;
  selectedKey: string | null;
  selection: Selection | null;
  wang: WangSelection | null;
}

type Cb<T> = (v: T) => void;

export class ToolController {
  state: ToolState = {
    mode: "view", activeLayer: "", selectedKey: null,
    selection: null, wang: null,
  };
  private onModeChange: Cb<ToolMode> = () => {};
  private onLayerChange: Cb<string> = () => {};
  private onKeyChange: Cb<string | null> = () => {};
  private onSelectionChange: Cb<Selection | null> = () => {};
  private onWangChange: Cb<WangSelection | null> = () => {};

  bind(
    onMode: Cb<ToolMode>,
    onLayer: Cb<string>,
    onKey: Cb<string | null>,
    onSelection?: Cb<Selection | null>,
    onWang?: Cb<WangSelection | null>,
  ): void {
    this.onModeChange = onMode;
    this.onLayerChange = onLayer;
    this.onKeyChange = onKey;
    if (onSelection) this.onSelectionChange = onSelection;
    if (onWang) this.onWangChange = onWang;

    (["view", "paint", "erase", "select", "wang"] as ToolMode[]).forEach((m) => {
      const btn = document.getElementById(`tool-${m}`);
      btn?.addEventListener("click", () => this.setMode(m));
    });

    window.addEventListener("keydown", (e) => {
      // Don't hijack typing inside inputs
      const tgt = e.target as HTMLElement | null;
      if (tgt && (tgt.tagName === "INPUT" || tgt.tagName === "SELECT" ||
                  tgt.tagName === "TEXTAREA")) return;
      if (e.key === "v" || e.key === "V") this.setMode("view");
      else if (e.key === "b" || e.key === "B") this.setMode("paint");
      else if (e.key === "e" || e.key === "E") this.setMode("erase");
      else if (e.key === "r" || e.key === "R") this.setMode("select");
      else if (e.key === "w" || e.key === "W") this.setMode("wang");
      else if (e.key === "Escape") this.setSelection(null);
    });

    const sel = document.getElementById("active-layer") as HTMLSelectElement;
    sel?.addEventListener("change", () => {
      this.setActiveLayer(sel.value);
    });
  }

  setMode(m: ToolMode): void {
    if (this.state.mode === m) return;
    this.state.mode = m;
    for (const n of ["view", "paint", "erase", "select", "wang"]) {
      document.getElementById(`tool-${n}`)?.classList.toggle("active", n === m);
    }
    const statusEl = document.getElementById("mode");
    if (statusEl) statusEl.textContent = `mode: ${m}`;
    this.onModeChange(m);
  }

  setWang(sel: WangSelection | null): void {
    this.state.wang = sel;
    const el = document.getElementById("wang-active");
    if (el) {
      el.innerHTML = sel
        ? `wang: <b>${sel.wangset_uid.split("::").slice(-1)[0]}</b> ` +
          `color=${sel.color}` +
          (sel.color_hex ? ` <span style="color:${sel.color_hex}">●</span>` : "")
        : "";
    }
    // Highlight active swatch
    document.querySelectorAll(".wang-color.active").forEach(
      (e) => e.classList.remove("active"));
    if (sel) {
      const cell = document.querySelector(
        `.wang-color[data-uid="${CSS.escape(sel.wangset_uid)}"]` +
        `[data-color="${sel.color}"]`);
      cell?.classList.add("active");
    }
    this._refreshWangFillButton();
    this.onWangChange(sel);
  }

  /** Enable the wang "Fill selection" button iff both a wang swatch
   *  and a selection are currently active. */
  private _refreshWangFillButton(): void {
    const btn = document.getElementById(
      "wang-fill-selection") as HTMLButtonElement | null;
    if (!btn) return;
    btn.disabled = !(this.state.wang && this.state.selection);
  }

  setSelection(sel: Selection | null): void {
    this.state.selection = sel;
    const el = document.getElementById("selection-info");
    if (el) {
      el.textContent = sel
        ? `selection: ${sel.layer} [${sel.x0},${sel.y0}]-[${sel.x1},${sel.y1}] ` +
          `(${sel.x1 - sel.x0 + 1}×${sel.y1 - sel.y0 + 1})`
        : "";
    }
    this._refreshWangFillButton();
    this.onSelectionChange(sel);
  }

  setActiveLayer(name: string): void {
    this.state.activeLayer = name;
    const sel = document.getElementById("active-layer") as HTMLSelectElement;
    if (sel && sel.value !== name) sel.value = name;
    this.onLayerChange(name);
  }

  setSelectedKey(key: string | null): void {
    this.state.selectedKey = key;
    const el = document.getElementById("current-key");
    if (el) el.textContent = key ?? "—";
    // Update palette selection visual
    document.querySelectorAll(".pal-cell.active").forEach(
      (e) => e.classList.remove("active"));
    if (key) {
      const cell = document.querySelector(
        `.pal-cell[data-key="${CSS.escape(key)}"]`);
      cell?.classList.add("active");
    }
    this.onKeyChange(key);
  }
}

export function populateLayerSelect(state: MapState, ctl: ToolController): void {
  const sel = document.getElementById("active-layer") as HTMLSelectElement;
  if (!sel) return;
  sel.innerHTML = "";
  for (const l of state.layers) {
    const opt = document.createElement("option");
    opt.value = l.name;
    opt.textContent = l.name;
    sel.appendChild(opt);
  }
  if (state.layers.length > 0) {
    ctl.setActiveLayer(state.layers[0].name);
  }
}

export function populatePalette(state: MapState, ctl: ToolController): void {
  const pal = document.getElementById("palette");
  if (!pal) return;
  pal.innerHTML = "";
  const keys = Object.keys(state.tiles).sort((a, b) => {
    // Put animated first, then by key
    const aa = state.tiles[a].animation ? 0 : 1;
    const bb = state.tiles[b].animation ? 0 : 1;
    if (aa !== bb) return aa - bb;
    return a.localeCompare(b);
  });
  for (const key of keys) {
    const cell = document.createElement("div");
    cell.className = "pal-cell";
    cell.dataset.key = key;
    cell.title = key;
    cell.style.backgroundImage = `url(/sprite/${encodeURIComponent(key)}.png)`;
    cell.addEventListener("click", () => ctl.setSelectedKey(key));
    pal.appendChild(cell);
  }
  // Default selection
  if (keys.length > 0 && ctl.state.selectedKey === null) {
    ctl.setSelectedKey(keys[0]);
  }
}

/** Populate the Wang panel with the loaded TMX's wang sets.
 *
 * Dropdown selects a wang set; swatches appear below with one box per color.
 * Clicking a swatch sets ctl.state.wang and auto-enables wang mode.
 */
export async function populateWangPanel(
  ctl: ToolController,
  fetchImpl: typeof fetch = fetch,
): Promise<void> {
  const selectEl = document.getElementById(
    "wang-set-select") as HTMLSelectElement | null;
  const colorsEl = document.getElementById("wang-colors");
  if (!selectEl || !colorsEl) return;

  selectEl.innerHTML = "";
  colorsEl.innerHTML = "";

  let allSets: WangSet[] = [];
  try {
    const resp = await fetchImpl("/wang/sets");
    const data = await resp.json();
    allSets = (data.sets || []) as WangSet[];
  } catch (e) {
    // Offline / error — leave panel empty
    return;
  }

  // Only wangsets we can actually paint (corner-type). Edge/mixed are
  // skipped with an informational row so users aren't confused by
  // greyed options that silently no-op. Default `supported` to
  // type==='corner' for back-compat with bridges that don't send it.
  const sets = allSets.filter(
    (s) => s.supported ?? (s.type === "corner"));
  const unsupportedCount = allSets.length - sets.length;

  if (sets.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = unsupportedCount > 0
      ? `(${unsupportedCount} wang set(s), none supported)`
      : "(no wang sets)";
    selectEl.appendChild(opt);
    selectEl.disabled = true;
    return;
  }
  selectEl.disabled = false;

  for (const s of sets) {
    const opt = document.createElement("option");
    opt.value = s.wangset_uid;
    opt.textContent = `${s.tileset} — ${s.name} (${s.type}, ` +
      `${s.color_count}c)`;
    selectEl.appendChild(opt);
  }
  if (unsupportedCount > 0) {
    const note = document.createElement("option");
    note.disabled = true;
    note.value = "";
    note.textContent =
      `— ${unsupportedCount} edge/mixed wangset(s) hidden (not supported)`;
    selectEl.appendChild(note);
  }

  const render = (uid: string) => {
    colorsEl.innerHTML = "";
    const set = sets.find((s) => s.wangset_uid === uid);
    if (!set) return;
    for (const c of set.colors) {
      const sw = document.createElement("div");
      sw.className = "wang-color";
      sw.dataset.uid = uid;
      sw.dataset.color = String(c.color_index);
      sw.style.backgroundColor = c.color_hex || "#666";
      sw.title =
        `${c.name || "(unnamed)"}  color=${c.color_index}  ${c.color_hex || ""}`;
      sw.addEventListener("click", () => {
        ctl.setWang({
          wangset_uid: uid,
          color: c.color_index,
          color_hex: c.color_hex,
          color_name: c.name,
        });
        ctl.setMode("wang");
      });
      colorsEl.appendChild(sw);
    }
  };

  selectEl.addEventListener("change", () => render(selectEl.value));
  render(sets[0].wangset_uid);
}
