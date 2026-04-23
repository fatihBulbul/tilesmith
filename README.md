# tilesmith

> **Conversation-driven 2D top-down RPG map authoring for Tiled (TMX) — powered by Claude Code / Cowork.**

Tilesmith turns a folder of Tiled tileset packs into an indexed, wang-aware map-authoring backend. It scans `.tsx` / `.tmx` / `.png` files recursively, builds a SQLite index of every tileset, wang set, prop, animated prop, character and automapping rule, then lets you design maps through natural conversation or a browser-based live editor called **Tilesmith Studio**.

Türkçe sürüm: [README.tr.md](./README.tr.md)

---

## Table of Contents

1. [What it does](#what-it-does)
2. [The artist contract — why wangset quality matters](#the-artist-contract)
3. [Installation](#installation)
4. [Quickstart](#quickstart)
5. [Tilesmith Studio (browser editor)](#tilesmith-studio)
6. [MCP tool reference](#mcp-tool-reference)
7. [Architecture](#architecture)
8. [Troubleshooting](#troubleshooting)
9. [Contributing](#contributing)
10. [License](#license)

---

## What it does

At its core, tilesmith is a bridge between three things: a Tiled asset pack on disk, a SQLite catalog of everything that pack contains, and a set of MCP tools that Claude can call to design, edit, render and export maps.

**Indexing.** Run `scan_folder("path/to/pack")` once per pack. The scanner walks the directory, parses every `.tsx` tileset (both atlas-style and collection-style), every inline `<tileset>` in TMX files, and extracts all wang sets, terrain colors, prop variants, animations, and automap rules. Each row is namespaced by a `pack_name` prefix so packs with overlapping tileset names do not collide.

**Design.** The `create_map` skill runs a short Q&A flow — size, biome mix, seed, style preferences — then proposes an ASCII layout plan. Once you approve, it produces a TMX file populated with wang-correct transitions, props, animations and spawn objects.

**Edit.** `open_studio(tmx_path=...)` boots a FastAPI + WebSocket bridge with a Konva-based browser canvas. You paint, erase, select rectangles, wang-fill selections, undo/redo — all of it live-broadcast to every connected browser tab. Claude can edit the same map from chat with MCP tools (`paint_tiles`, `wang_fill_selection`, `studio_undo`, …) and you see the result instantly.

**Export.** `consolidate_map` rewrites a TMX into a self-contained single-atlas form. It scans all used GIDs, shelf bin-packs the referenced tiles into one PNG, rewrites the TMX to reference only that atlas, and drops the output next to your map. The result no longer depends on the original pack's folder structure — ship it anywhere.

---

## The artist contract

Tilesmith is a **faithful renderer of whatever the tile artist declared in Tiled**. It does not guess, invent, or paper over gaps. This is a deliberate choice, and it has a sharp consequence:

> **The quality of your maps is bounded by the quality of your wangsets.**

If your tileset ships with incomplete or inconsistent wang sets, tilesmith will produce incomplete or inconsistent maps. No amount of prompting will fix it, because the missing information simply is not in the index. Before you can author beautiful maps with this plugin, the tile artist needs to have done the wangset work in Tiled correctly.

### What tilesmith expects from a wangset

Tiled supports three wangset types: **corner**, **edge**, and **mixed**. Each cell in your map has either 4 corners, 4 edges, or both, each carrying one of the wangset's declared colors (index `0` meaning "outside / wildcard").

For a wangset with N declared colors, the complete tile catalog is:

| Wangset type | Combinations the artist must provide |
|---|---|
| corner — e.g. grass-to-dirt transition | `(N+1)⁴` tiles if you include the wildcard; in practice `2⁴ = 16` combinations per color pair |
| edge — e.g. fences, half-walls, cliffs | same math: `2⁴ = 16` orientations per color pair |
| mixed — corner + edge simultaneously | both sets combined (currently not supported by tilesmith's resolver) |

Tilesmith's resolver asks the index one question for every affected cell: *"Given these 4 corner/edge colors, which tile should I place here?"* It picks the lowest `local_id` that matches. If the combination is missing from the wangset, **the resolver returns `None` and the cell becomes empty.** That empty cell will punch a visible hole in the map.

### The failure modes you will hit if the wangset is sloppy

1. **Missing combinations → visible holes.** If the artist only painted 12 of the 16 corner combinations for a grass-dirt wang, every cell on the map that needed one of the 4 missing combinations will be blank. Gaps in the source show up as gaps in the output.
2. **Inconsistent color indices across tilesets.** If pack A uses color index `1` for "grass" and pack B uses index `2` for "grass", tilesmith has no way to know they are the same thing. Transitions between tiles from the two tilesets will be treated as totally unrelated terrains.
3. **Mislabeled tiles.** If the artist accidentally marks the NW corner of a tile as "grass" when the sprite visually shows dirt, the map will show a jarring visual discontinuity that looks like a bug in tilesmith — but the bug is in the `.tsx`.
4. **Wangset type set to "mixed" when it could be pure corner or edge.** Tilesmith's v0.7.1 resolver supports corner and edge. Mixed wangsets are listed but marked `supported: false` and cannot be painted automatically. This is usually avoidable: most wangsets are conceptually either corner-sharing (terrain gradients) or edge-sharing (fences, walls) and can be authored as one or the other.
5. **Colors never used by any tile.** Declared in the palette but not present in any tile's 4-corner or 4-edge signature. The UI shows the swatch but painting with it will always return `None`.

### What a tile artist should do before shipping a pack

1. Decide the color palette up front. Each distinct terrain / material type gets one color index. Use consistent names across related wangsets in the same tileset.
2. For every color pair in a wangset, paint **all 16** corner or edge combinations. It is tedious, but there is no shortcut. Tiled's wang-editor makes this somewhat faster by letting you paint with the 2D wang brush and auto-assigning colors from the palette.
3. After authoring, open a blank map in Tiled and try painting with the wangset. If any combination produces a visual glitch or Tiled leaves a gap, that combination is either missing or mislabeled — fix it in the `.tsx` before shipping.
4. Keep wangsets focused. A single wangset with 7 colors is much harder to complete than seven 2-color wangsets. Break complex terrain systems into small, pairwise wangsets when you can.
5. Choose type deliberately: gradients that need to blend visually (grass to dirt, water to sand) are **corner**. Hard boundaries that snap to the grid (fences, walls, cliff tops, paths) are **edge**. Pick one and commit.

### How tilesmith helps you catch wangset problems

`list_wangsets_for_tmx` returns every wangset referenced by a TMX, along with color count, tile count, and a `supported` flag. A wangset with tile count well below `2⁴ × (color_count choose 2)` is almost certainly incomplete. You can also query the index directly:

```sql
-- Count wang tiles per wangset; compare to expected 2^4 = 16 per color pair.
SELECT ws.wangset_uid, ws.type, ws.color_count, COUNT(*) AS tile_count
  FROM wang_sets ws
  JOIN wang_tiles wt ON wt.wangset_uid = ws.wangset_uid
 GROUP BY ws.wangset_uid
 ORDER BY tile_count;
```

If you see wang sets with only 6 or 7 tiles in a pack claiming 4 colors, that pack will not produce clean maps. Either refuse to use those wangsets, or fix the source `.tsx` yourself.

---

## Installation

### Requirements

- Python 3.10 or newer
- Node.js 20+ (for the Studio frontend build — one-time)
- A Tiled asset pack you own or have a license for (tilesmith does **not** ship any tileset art)

### As a Claude Code / Cowork plugin

The repository is structured as a Claude Code plugin marketplace. Install it with:

```text
/plugin marketplace add <your-github-username>/tilesmith
/plugin install tilesmith@tilesmith-marketplace
```

That's it — Claude Code auto-discovers `.mcp.json`, `skills/`, and the MCP server. The first run will prompt you to scan a tileset folder.

### Manual install (standalone)

```bash
git clone https://github.com/<your-github-username>/tilesmith.git
cd tilesmith
pip install -r requirements.txt
cd studio/frontend && npm install && npm run build && cd ../..
```

Point `TILESMITH_DB_PATH` at a writable directory (defaults to `./data/tiles.db`) and you can run the MCP server directly:

```bash
python3 mcp_server/server.py
```

---

## Quickstart

### 1. Scan an asset pack

In chat:

> Scan the tileset pack at `/path/to/ERW-GrassLand-v1.9` with tilesmith.

Claude calls `scan_folder` and populates the SQLite index. The first scan of a mid-size pack (~20 tilesets, ~500 wang tiles) takes a few seconds.

### 2. Generate a map

> Make me a 60×60 grass-land map with a river running north-south and a dirt path along the east edge.

The `create_map` skill asks a few clarifying questions, shows an ASCII layout plan, waits for approval, then emits a TMX in `output/`.

### 3. Open it in Studio

> Open the generated map in tilesmith studio.

The bridge boots on `http://127.0.0.1:3024/`. Open it in your browser. You now have a live Konva canvas of the map with all layers, objects and animations rendering in real time.

### 4. Edit from chat

> Fill the selected rectangle with the dirt wang.

Select a rectangle on the canvas (press **R**, drag), then send that message. Claude calls `wang_fill_selection` — you see the autotile transitions appear live.

### 5. Export a self-contained version

> Consolidate this map into a single atlas.

`consolidate_map` rewrites the TMX so it depends only on one generated PNG. Ship the folder, it works standalone.

---

## Tilesmith Studio

Studio is a FastAPI + WebSocket bridge that runs a Konva-based single-page app in your browser. It is how you and Claude edit the same TMX at the same time.

### Keyboard & mouse

| Key / action | Tool |
|---|---|
| `V` + drag, scroll-wheel | Pan / zoom |
| `Fit` button | Fit map to viewport |
| `B` | Paint — pick a tile from the palette, click or drag on the canvas |
| `E` | Erase — clear a cell |
| `R` + drag | Rectangle select; **Esc** clears the selection |
| `W` | Wang mode — pick a wangset and color, click or drag to autotile |
| `Ctrl/Cmd + Z` | Undo (history depth 100) |
| `Ctrl/Cmd + Shift + Z` / `Ctrl + Y` | Redo |

### What corner vs. edge wang mode looks like in practice

When you click a single cell in wang mode, the affected neighborhood is different depending on the wangset type:

**Corner wangset** (grass-to-dirt, water-to-sand): your click paints the four corners of the target cell with the chosen color. The tool then re-resolves a 3×3 neighborhood (8 neighbors + self), because those corners are shared with all eight neighbors. Smooth gradient transitions appear around the click.

**Edge wangset** (fences, half-walls, cliffs): your click paints the four edges of the target cell. The tool re-resolves a 5-cell plus-shaped neighborhood (self + N / E / S / W) because edges are only shared with the four orthogonal neighbors. Diagonal neighbors are untouched — which is exactly what you want for a fence that should not connect to a fence one tile diagonally away.

### Multi-client live editing

Open `http://127.0.0.1:3024/` in two browser tabs. Paint in one — the other updates in milliseconds via WebSocket. Claude's MCP calls flow through the same broadcast, so chat-driven edits appear in both tabs too.

---

## MCP tool reference

Tilesmith exposes 25 MCP tools. The full set is declared in `mcp_server/server.py`; here are the highlights.

**Indexing & query**

| Tool | Purpose |
|---|---|
| `scan_folder(path)` | Index a Tiled asset pack into SQLite |
| `list_packs()` | Every pack currently in the index |
| `list_tilesets(pack_name?)`, `list_wangsets(tileset?)`, `list_props`, `list_characters`, `list_animations` | Catalog queries |

**Map generation & export**

| Tool | Purpose |
|---|---|
| `create_map_preset(name, ...)` | Invoke one of the built-in presets (grassland, rich-80, …) |
| `consolidate_map(tmx_path, out_dir)` | Rewrite a TMX as a self-contained single-atlas file |

**Studio bridge**

| Tool | Purpose |
|---|---|
| `open_studio(tmx_path, port?, host?)` | Launch the bridge + browser URL |
| `close_studio(port?)` | Stop the bridge |
| `paint_tiles(tmx_path, layer, cells, port?)` | Patch a tile layer with `cells: [{x, y, key|null}]` |
| `patch_object(...)` | Move / delete / set-key on objects |
| `fill_rect(tmx_path, layer, x0, y0, x1, y1, key, port?)` | Rectangle fill with one tile key |
| `fill_selection(key, port?)` | Fill the Studio's last rectangle selection |
| `list_wangsets_for_tmx(tmx_path?, port?)` | All wangsets referenced by a TMX, with colors + `supported` flag |
| `wang_paint(wangset_uid, cells, color=1, layer?, erase?)` | Wang-aware autotile paint (corner + edge) |
| `wang_fill_rect(wangset_uid, x0, y0, x1, y1, color=1, layer?, erase?)` | Wang-aware rectangle fill |
| `wang_fill_selection(wangset_uid, color=1, erase?)` | Wang-aware selection fill |
| `studio_undo(port?)`, `studio_redo(port?)` | History navigation (depth 100) |

All studio tools try to reach the running bridge first over HTTP, broadcast to every connected browser tab, and fall back to direct atomic file writes if no bridge is running. Either way your TMX on disk stays consistent.

---

## Architecture

```
  Tiled asset pack (.tsx / .tmx / .png)
              │
              ▼
  ┌────────────────────────┐
  │  scanner.py            │  parse + normalize + SQLite DDL
  │  (auto + overrides +   │
  │   VIEW COALESCE)       │
  └───────────┬────────────┘
              ▼
     data/tiles.db  ◄─────────── queries (server.py, wang.py, generator)
              │
              ▼
  ┌─────────────────────────────────────────────────┐
  │  MCP server (stdio)         Studio bridge (HTTP+WS) │
  │  25 tools                   FastAPI + Konva frontend│
  │  create_map skill           single-page app         │
  └──────┬──────────────────────────┬─────────────────┘
         │                          │
         ▼                          ▼
   TMX on disk  ◄─── atomic write ────┘
```

The three-layer catalog — `<kind>_auto` (rewritten by every scan), `<kind>_overrides` (hand-edited, persistent), and a `<kind>` VIEW that `COALESCE`s them — means you can tweak any index row (e.g. correcting a mislabelled wang color) without your change being wiped on the next rescan.

All state changes flow through one chokepoint in the Studio bridge (`patch_paint`). Wang paint, rect fill, selection fill and undo all go through that one function, which keeps inverse-patch history honest and broadcast semantics uniform.

---

## Troubleshooting

**"I painted with a wangset and half the cells went blank."**
The wangset is missing corner/edge combinations. Run the SQL query from [the artist contract section](#the-artist-contract) to see per-wangset tile counts. You will likely see the culprit wangset has far fewer tiles than its color count would require.

**"Transitions between two of my tilesets look broken."**
The two tilesets probably use different color index numbering for the same conceptual terrain. Tilesmith treats color indices as opaque integers — index 1 in tileset A is not automatically the same as index 1 in tileset B. You need to edit the `.tsx` files (or use a wangset override row in `wang_colors_overrides`) to make them consistent.

**"Studio shows holes after I undo a wang paint."**
This should not happen in v0.7.0+ — the undo path invalidates the corner/edge cache before re-applying the inverse patch. If you see it, open an issue with the exact repro (wangset type, layer, paint sequence).

**"Mixed-type wangsets are greyed out."**
Correct — they are listed but `supported: false` in v0.7.1. Only corner and edge are implemented. Most mixed wangsets can be re-authored as pure corner or pure edge in Tiled.

**"`scan_folder` runs forever on a big pack."**
The scanner is single-threaded. A mid-sized pack (~20 tilesets, hundreds of tiles) takes seconds; a huge one (100+ tilesets) can take a minute. If it truly hangs, it is usually because one `.tsx` has a circular template reference or a very large embedded base64 image.

---

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md). Issues and PRs are welcome, especially:

- additional wangset types (mixed, 2-edge, custom),
- object drag-and-drop in the Studio canvas,
- automap rule engine (the `automap_rules` table is indexed but no tool consumes it yet),
- more `create_map` presets.

A full test suite lives in `scripts/`. `test_wang_unit.py` is a fixture-free pure unit test that runs in under a second — start there.

---

## License

MIT — see [LICENSE](./LICENSE).

Tilesmith indexes and renders Tiled tileset packs; it does **not** bundle or redistribute any tile artwork. You are responsible for ensuring that you own or are licensed to use any pack you scan. Popular commercial packs (e.g. the ERW family) are sold separately by their respective authors.
