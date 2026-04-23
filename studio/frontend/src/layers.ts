// Left-panel UI: layer and object-group toggles + opacity sliders.

import type { MapState } from "./types";
import type { MapRenderer } from "./canvas";

export function renderLayerPanel(
  state: MapState,
  renderer: MapRenderer
): void {
  const layerList = document.getElementById("layer-list");
  const ogList = document.getElementById("objgroup-list");
  if (!layerList || !ogList) return;

  layerList.innerHTML = "";
  for (const layer of state.layers) {
    const row = buildRow({
      name: layer.name,
      visible: layer.visible,
      opacity: layer.opacity,
      count: countCells(layer.data),
      onVisible: (v) => renderer.setLayerVisible(layer.name, v),
      onOpacity: (o) => renderer.setLayerOpacity(layer.name, o),
    });
    layerList.appendChild(row);
  }

  ogList.innerHTML = "";
  for (const og of state.object_groups) {
    const row = buildRow({
      name: og.name,
      visible: og.visible,
      opacity: og.opacity,
      count: og.objects.length,
      onVisible: (v) => renderer.setObjectGroupVisible(og.name, v),
      onOpacity: (o) => renderer.setObjectGroupOpacity(og.name, o),
    });
    ogList.appendChild(row);
  }
}

function countCells(data: (string | null)[][]): number {
  let n = 0;
  for (const row of data) for (const c of row) if (c) n++;
  return n;
}

interface RowSpec {
  name: string;
  visible: boolean;
  opacity: number;
  count: number;
  onVisible: (v: boolean) => void;
  onOpacity: (o: number) => void;
}

function buildRow(spec: RowSpec): HTMLElement {
  const row = document.createElement("div");
  row.className = "layer-row";

  const chk = document.createElement("input");
  chk.type = "checkbox";
  chk.checked = spec.visible;
  chk.addEventListener("change", () => spec.onVisible(chk.checked));

  const name = document.createElement("span");
  name.className = "name";
  name.textContent = spec.name || "(unnamed)";

  const count = document.createElement("span");
  count.className = "count";
  count.textContent = String(spec.count);

  const opac = document.createElement("input");
  opac.type = "range";
  opac.min = "0";
  opac.max = "1";
  opac.step = "0.05";
  opac.value = String(spec.opacity);
  opac.title = "opacity";
  opac.addEventListener("input", () => spec.onOpacity(Number(opac.value)));

  row.append(chk, name, count, opac);
  return row;
}

export function renderInfoPanel(state: MapState): void {
  const info = document.getElementById("info-panel");
  if (!info) return;
  const animated = Object.values(state.tiles).filter(
    (t) => t.animation && t.animation.length > 0
  ).length;
  const rows: [string, string][] = [
    ["size", `${state.width} × ${state.height}`],
    ["tile", `${state.tile_w} × ${state.tile_h}`],
    ["layers", String(state.layers.length)],
    ["obj groups", String(state.object_groups.length)],
    ["unique tiles", String(Object.keys(state.tiles).length)],
    ["animated", String(animated)],
  ];
  info.innerHTML = rows
    .map(
      ([k, v]) =>
        `<div class="info-row"><span>${k}</span><b>${v}</b></div>`
    )
    .join("");
}

export function renderMeta(state: MapState): void {
  const meta = document.getElementById("meta");
  if (!meta) return;
  const short = state.tmx_path
    ? state.tmx_path.split("/").slice(-2).join("/")
    : "(no map loaded)";
  meta.textContent = short;
}
