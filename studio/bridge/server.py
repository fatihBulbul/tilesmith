"""
Tilesmith Studio bridge server.

Görevi:
  - Bir TMX dosyasını load eder, MapState JSON'a çevirir
  - HTTP GET /state           → MapState JSON
  - HTTP GET /sprite/{key}.png → tile sprite (PNG)
  - HTTP POST /open           → başka TMX yükle
  - WS   /ws                  → state + patch eventleri canlı push/pull
  - Static: /                 → frontend build'ini servis eder

Port: --port 3024 (default). Birden fazla browser aynı anda bağlanabilir.
Kapanış: Ctrl+C.

Çalıştırma:
  python3 -m uvicorn server:app --port 3024
veya CLI:
  python3 server.py --tmx /path/to/map.tmx --port 3024
"""

from __future__ import annotations
import argparse
import asyncio
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

# mcp_server path
BRIDGE_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = BRIDGE_DIR.parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "mcp_server"))

from tmx_state import build_map_state  # noqa: E402
from tmx_mutator import apply_paint, apply_object_patch  # noqa: E402
from wang import (  # noqa: E402
    WangCornerState,
    WangEdgeState,
    apply_wang_paint,
    get_wangset_type,
    list_wangsets_for_tilesets,
    list_wang_tile_entries,
    seed_corners_from_layer,
    seed_edges_from_layer,
)
from PIL import Image  # noqa: E402

# DB path: mirror mcp_server/server.py env handling
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = Path(
    os.environ.get("TILESMITH_DB_PATH")
    or os.environ.get("ERW_DB_PATH")
    or str(_PLUGIN_ROOT / "data" / "tiles.db")
)

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
    from fastapi.responses import Response, FileResponse, JSONResponse, HTMLResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:
    print("ERROR: fastapi + uvicorn gerekli. `pip install fastapi uvicorn[standard]`",
          file=sys.stderr)
    raise


# ---------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------

class StudioState:
    """In-memory snapshot of the currently loaded map."""

    def __init__(self) -> None:
        self.tmx_path: Path | None = None
        self.state: dict[str, Any] = {"empty": True}
        self.sprites: dict[str, Image.Image] = {}
        self.clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        # Last-known client-side rect selection. Cleared when tile/state
        # reloaded. Shape: {"layer", "x0","y0","x1","y1"} or None.
        self.last_selection: dict[str, Any] | None = None
        # Wang state, keyed by (layer_name, wangset_uid). Value is
        # `WangCornerState` for corner-type wangsets, `WangEdgeState` for
        # edge-type. Lazily seeded from TMX on first wang_paint hitting
        # a given pair.
        self.wang_states: dict[
            tuple[str, str], WangCornerState | WangEdgeState
        ] = {}
        # Path to the DB the bridge should query for wang indices.
        self.db_path: Path = DEFAULT_DB_PATH
        # Undo/redo stacks. Entries are dicts of shape:
        #   {"kind":"paint", "layer": str,
        #    "forward": [{"x","y","key"}],   # what was applied
        #    "inverse": [{"x","y","key"}],   # what to re-apply to undo it
        #    "meta": {...}}                  # e.g. wang metadata
        # Stacks are bounded so long sessions don't grow memory unboundedly.
        # Cleared on TMX (re)load.
        self.undo_stack: list[dict] = []
        self.redo_stack: list[dict] = []
        self.undo_max = 100

    async def load(self, tmx_path: str | Path) -> dict:
        async with self._lock:
            tmx_path = Path(tmx_path).resolve()
            if not tmx_path.exists():
                raise FileNotFoundError(str(tmx_path))
            st, sprites = build_map_state(tmx_path)
            self.tmx_path = tmx_path
            self.state = st
            self.sprites = sprites
            self.last_selection = None
            # Wang corner caches are TMX-specific — drop on load.
            self.wang_states = {}
            # New map — no history from the previous one applies.
            self.undo_stack = []
            self.redo_stack = []
            return st

    def sprite_bytes(self, key: str) -> bytes | None:
        im = self.sprites.get(key)
        if im is None:
            return None
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    async def broadcast(self, event: dict, exclude: WebSocket | None = None
                        ) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.clients):
            if ws is exclude:
                continue
            try:
                await ws.send_text(json.dumps(event))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    # ------------------------------------------------------------------
    # Incremental patches
    # ------------------------------------------------------------------

    async def patch_paint(
        self, layer_name: str, cells: list[dict],
        *, record: bool = True, meta: dict | None = None,
    ) -> dict:
        """Apply a tile-layer paint/erase patch.

        Writes to TMX first (via tmx_mutator), then updates in-memory
        state.layers[*].data[y][x]. Any keys not already in the sprite
        cache will trigger a full reload (rare path).

        If `record` is True (default), captures an inverse patch in the
        undo stack so the operation can be reverted by `undo()`. Pass
        False when applying patches that are themselves undo/redo
        results (they must not re-enter the history).

        `meta` is attached to the undo entry verbatim (e.g. wang context)
        and surfaced on the response.
        """
        async with self._lock:
            if self.tmx_path is None:
                raise RuntimeError("no TMX loaded")
            # Capture inverse BEFORE mutating — inverse cells mirror
            # forward cells but carry the *previous* key at each (x,y).
            lay = None
            for L in self.state.get("layers", []):
                if L["name"] == layer_name:
                    lay = L
                    break
            if lay is None:
                raise RuntimeError(f"layer {layer_name} missing in state")
            h = len(lay["data"])
            w = len(lay["data"][0]) if h > 0 else 0
            inverse_cells: list[dict] = []
            # Dedup by (x,y) — only remember the FIRST prev value we saw
            # for each cell in the patch (the "true" starting state).
            seen: set[tuple[int, int]] = set()
            for c in cells:
                x = int(c["x"]); y = int(c["y"])
                if not (0 <= y < h and 0 <= x < w):
                    continue
                if (x, y) in seen:
                    continue
                seen.add((x, y))
                inverse_cells.append({
                    "x": x, "y": y, "key": lay["data"][y][x],
                })

            res = apply_paint(self.tmx_path, layer_name, cells)
            needs_reload = False
            applied_cells: list[dict] = []
            for c in cells:
                x = int(c["x"]); y = int(c["y"])
                key = c.get("key")
                if not (0 <= y < len(lay["data"]) and
                        0 <= x < len(lay["data"][0] if lay["data"] else [])):
                    continue
                if key is not None and key not in self.state["tiles"]:
                    # Unknown sprite — cold reload required
                    needs_reload = True
                lay["data"][y][x] = key
                applied_cells.append({"x": x, "y": y, "key": key})
            if needs_reload:
                st, sprites = build_map_state(self.tmx_path)
                self.state = st
                self.sprites = sprites
            res["reload"] = needs_reload
            res["cells"] = applied_cells
            res["layer"] = layer_name

            # Stale wang corner caches: the data just changed, so any
            # cached corner grid built from the old data is no longer
            # trustworthy. Always clear (forward paints, undos, redos);
            # next wang paint re-seeds from layer data.
            if applied_cells:
                self.wang_states = {}
            if record and applied_cells:
                entry = {
                    "kind": "paint",
                    "layer": layer_name,
                    "forward": applied_cells,
                    "inverse": inverse_cells,
                    "meta": dict(meta) if meta else {},
                }
                self.undo_stack.append(entry)
                if len(self.undo_stack) > self.undo_max:
                    self.undo_stack.pop(0)
                # Any new mutation invalidates the redo branch.
                self.redo_stack = []
            return res

    async def undo(self) -> dict | None:
        """Revert the most recent recorded patch. Returns the broadcast
        payload the caller should emit, or None if nothing to undo."""
        if not self.undo_stack:
            return None
        entry = self.undo_stack.pop()
        if entry["kind"] != "paint":
            # Only paint ops recorded for now; skip anything unknown.
            return None
        res = await self.patch_paint(
            entry["layer"], entry["inverse"], record=False,
        )
        self.redo_stack.append(entry)
        return {
            "type": "patch", "op": "paint",
            "layer": entry["layer"],
            "cells": res.get("cells", []),
            "undo": True,
            "reload": res.get("reload", False),
        }

    async def redo(self) -> dict | None:
        """Re-apply the most recently undone patch. Returns the broadcast
        payload, or None if nothing to redo."""
        if not self.redo_stack:
            return None
        entry = self.redo_stack.pop()
        if entry["kind"] != "paint":
            return None
        res = await self.patch_paint(
            entry["layer"], entry["forward"], record=False,
        )
        self.undo_stack.append(entry)
        return {
            "type": "patch", "op": "paint",
            "layer": entry["layer"],
            "cells": res.get("cells", []),
            "redo": True,
            "reload": res.get("reload", False),
        }

    async def fill_rect(
        self,
        layer_name: str,
        x0: int, y0: int, x1: int, y1: int,
        key: str | None,
    ) -> dict:
        """Fill a rectangular region of a tile layer with a single key
        (or erase if key is None). Internally expands to a cells list and
        dispatches via patch_paint (which writes TMX + broadcasts).
        """
        xmin, xmax = sorted((int(x0), int(x1)))
        ymin, ymax = sorted((int(y0), int(y1)))
        cells = [
            {"x": x, "y": y, "key": key}
            for y in range(ymin, ymax + 1)
            for x in range(xmin, xmax + 1)
        ]
        res = await self.patch_paint(layer_name, cells)
        res["region"] = {"x0": xmin, "y0": ymin, "x1": xmax, "y1": ymax}
        return res

    def _get_or_seed_wang(self, layer_name: str, wangset_uid: str
                          ) -> WangCornerState | WangEdgeState:
        """Return a WangCornerState or WangEdgeState for (layer,
        wangset), seeded from TMX the first time it is requested. State
        class is selected by the wangset's DB `type`."""
        key = (layer_name, wangset_uid)
        cached = self.wang_states.get(key)
        if cached is not None:
            return cached
        # Look up layer data + dims
        layer = None
        for L in self.state.get("layers", []):
            if L["name"] == layer_name:
                layer = L
                break
        w = int(self.state.get("width", 0))
        h = int(self.state.get("height", 0))

        wtype = get_wangset_type(self.db_path, wangset_uid)
        if wtype == "edge":
            st: WangCornerState | WangEdgeState = WangEdgeState(
                width=w, height=h,
            )
            if layer is not None:
                try:
                    seed_edges_from_layer(
                        st, layer["data"], self.db_path, wangset_uid,
                    )
                except Exception as e:
                    print(f"[bridge] wang seed (edge) failed: {e}",
                          file=sys.stderr)
        else:
            # corner (default) — also covers unknown/missing, which will
            # be caught by apply_wang_paint's type guard with a clear
            # error message.
            st = WangCornerState(width=w, height=h)
            if layer is not None:
                try:
                    seed_corners_from_layer(
                        st, layer["data"], self.db_path, wangset_uid,
                    )
                except Exception as e:
                    print(f"[bridge] wang seed (corner) failed: {e}",
                          file=sys.stderr)
        self.wang_states[key] = st
        return st

    async def wang_paint(
        self,
        layer_name: str,
        wangset_uid: str,
        color: int,
        cells: list[dict],
        *,
        erase: bool = False,
    ) -> dict:
        """Run wang autotile paint: updates corner state + dispatches a
        regular tile-layer paint through patch_paint. Returns the paint
        result augmented with `wang`: {wangset_uid, color, cells_touched}."""
        async with self._lock:
            if self.tmx_path is None:
                raise RuntimeError("no TMX loaded")
            # Resolve corner state
            cs = self._get_or_seed_wang(layer_name, wangset_uid)
            # Compute resulting paint cells
            paint_cells = apply_wang_paint(
                cs, self.db_path, wangset_uid, color, cells, erase=erase,
            )
        # Dispatch as a normal paint (reuses lock + TMX write path)
        if not paint_cells:
            return {
                "ok": True, "layer": layer_name,
                "cells_applied": 0, "cells_skipped": 0,
                "wang": {"wangset_uid": wangset_uid, "color": color,
                         "cells_touched": 0, "erase": erase},
            }
        res = await self.patch_paint(
            layer_name, paint_cells,
            meta={
                "wang": {
                    "wangset_uid": wangset_uid, "color": color,
                    "erase": erase,
                },
            },
        )
        res["wang"] = {
            "wangset_uid": wangset_uid,
            "color": color,
            "cells_touched": len(paint_cells),
            "erase": erase,
        }
        return res

    async def wang_fill_rect(
        self,
        layer_name: str,
        x0: int, y0: int, x1: int, y1: int,
        wangset_uid: str,
        color: int,
        *,
        erase: bool = False,
    ) -> dict:
        """Wang-aware rectangle fill. Expands (x0,y0)-(x1,y1) inclusive
        into a cell list, clips to layer bounds, and runs through
        `wang_paint`. Raises on empty / OOB rect."""
        if self.tmx_path is None:
            raise RuntimeError("no TMX loaded")
        # Normalize + clip
        xa, xb = (x0, x1) if x0 <= x1 else (x1, x0)
        ya, yb = (y0, y1) if y0 <= y1 else (y1, y0)
        layer = None
        for L in self.state.get("layers", []):
            if L["name"] == layer_name and L.get("type", "tile") == "tile":
                layer = L
                break
        if layer is None:
            raise RuntimeError(f"layer {layer_name} missing or not a tile layer")
        w = self.state.get("width", 0)
        h = self.state.get("height", 0)
        xa = max(0, xa); ya = max(0, ya)
        xb = min(w - 1, xb); yb = min(h - 1, yb)
        if xa > xb or ya > yb:
            raise ValueError(
                f"rect out of bounds: ({x0},{y0})-({x1},{y1}) "
                f"vs layer {w}x{h}"
            )
        cells = [{"x": x, "y": y}
                 for y in range(ya, yb + 1)
                 for x in range(xa, xb + 1)]
        res = await self.wang_paint(
            layer_name, wangset_uid, color, cells, erase=erase,
        )
        res["rect"] = {"x0": xa, "y0": ya, "x1": xb, "y1": yb}
        return res

    async def patch_objects_add(
        self, group_name: str, objects: list[dict],
    ) -> dict:
        """Insert new tile-objects into an objectgroup (v0.8+ place_props).

        After writing the TMX we rebuild the whole state + sprite cache so
        any brand-new tile-keys become resolvable for renderers. Returns
        {ok, added, objects, reload: True}.
        """
        from tmx_mutator import apply_object_add
        async with self._lock:
            if self.tmx_path is None:
                raise RuntimeError("no TMX loaded")
            res = apply_object_add(self.tmx_path, group_name, objects)
            # Force full state rebuild — new objects often reference sprites
            # not yet in our cache.
            st, sprites = build_map_state(self.tmx_path)
            self.state = st
            self.sprites = sprites
            res["reload"] = True
            return res

    async def patch_objects_remove(
        self, group_name: str, ids: list[int],
    ) -> dict:
        """Batch-remove objects (by id) from an objectgroup (v0.8+).

        After the write we patch the in-memory state in place (no full
        rebuild needed since we're only deleting — sprite cache is still
        valid). Broadcasts {type: "patch", op: "objects_remove", ...}.
        """
        from tmx_mutator import apply_object_remove
        async with self._lock:
            if self.tmx_path is None:
                raise RuntimeError("no TMX loaded")
            res = apply_object_remove(self.tmx_path, group_name, ids)
            removed_set = set(res.get("removed_ids") or [])
            for OG in self.state.get("object_groups", []):
                if OG["name"] == group_name:
                    OG["objects"] = [
                        o for o in OG["objects"]
                        if int(o.get("id", -1)) not in removed_set
                    ]
                    break
            return res

    async def patch_object(self, group_name: str, patch: dict) -> dict:
        async with self._lock:
            if self.tmx_path is None:
                raise RuntimeError("no TMX loaded")
            res = apply_object_patch(self.tmx_path, group_name, patch)
            og = None
            for OG in self.state.get("object_groups", []):
                if OG["name"] == group_name:
                    og = OG
                    break
            if og is None:
                raise RuntimeError(f"group {group_name} missing in state")
            obj_id = int(patch["id"])
            op = patch["op"]
            if op == "delete":
                og["objects"] = [o for o in og["objects"] if o["id"] != obj_id]
            elif op == "move":
                for o in og["objects"]:
                    if o["id"] == obj_id:
                        if "x" in patch:
                            o["x"] = float(patch["x"])
                        if "y" in patch:
                            o["y"] = float(patch["y"])
                        break
            elif op == "set_key":
                new_key = patch["key"]
                if new_key not in self.state["tiles"]:
                    st, sprites = build_map_state(self.tmx_path)
                    self.state = st
                    self.sprites = sprites
                    res["reload"] = True
                else:
                    for o in og["objects"]:
                        if o["id"] == obj_id:
                            o["key"] = new_key
                            break
            return res


STATE = StudioState()


# ---------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------

app = FastAPI(title="Tilesmith Studio Bridge", version="0.1.0")


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "tmx_path": str(STATE.tmx_path) if STATE.tmx_path else None,
        "clients": len(STATE.clients),
        "sprites_cached": len(STATE.sprites),
    }


@app.get("/state")
async def get_state() -> JSONResponse:
    return JSONResponse(STATE.state)


@app.post("/open")
async def open_map(body: dict) -> dict:
    tmx_path = body.get("tmx_path")
    if not tmx_path:
        raise HTTPException(400, "tmx_path required")
    try:
        st = await STATE.load(tmx_path)
    except FileNotFoundError as e:
        raise HTTPException(404, f"TMX not found: {e}")
    await STATE.broadcast({"type": "map_loaded", "state": st})
    return {"ok": True, "summary": {
        "width": st["width"], "height": st["height"],
        "layers": len(st["layers"]),
        "object_groups": len(st["object_groups"]),
        "unique_tiles": len(st["tiles"]),
    }}


@app.get("/sprite/{key}.png")
async def get_sprite(key: str) -> Response:
    data = STATE.sprite_bytes(key)
    if data is None:
        raise HTTPException(404, f"sprite {key} not in cache")
    # Long cache — sprites are immutable for the life of this session
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.post("/patch/tiles")
async def patch_tiles(body: dict) -> dict:
    """Apply a paint/erase patch to a tile layer.

    body: {layer: str, cells: [{x, y, key|null}, ...]}
    """
    layer = body.get("layer")
    cells = body.get("cells")
    if not layer or not isinstance(cells, list):
        raise HTTPException(400, "layer + cells required")
    try:
        res = await STATE.patch_paint(layer, cells)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    # Broadcast a compact delta; if reload was required, resend map_loaded
    if res.get("reload"):
        await STATE.broadcast({"type": "map_loaded", "state": STATE.state})
    else:
        await STATE.broadcast({
            "type": "patch",
            "op": "paint",
            "layer": layer,
            "cells": res["cells"],
        })
    return res


@app.get("/selection")
async def get_selection() -> dict:
    return {"selection": STATE.last_selection}


@app.post("/selection")
async def set_selection(body: dict) -> dict:
    """Stateless selection setter — the server just remembers it and
    echoes to all WS clients so every viewer can show the same rect.
    body: {selection: {layer, x0, y0, x1, y1} | null}
    """
    sel = body.get("selection")
    if sel is not None:
        for k in ("layer", "x0", "y0", "x1", "y1"):
            if k not in sel:
                raise HTTPException(400, f"selection.{k} required")
        sel = {
            "layer": str(sel["layer"]),
            "x0": int(sel["x0"]), "y0": int(sel["y0"]),
            "x1": int(sel["x1"]), "y1": int(sel["y1"]),
        }
    STATE.last_selection = sel
    await STATE.broadcast({"type": "selection", "selection": sel})
    return {"ok": True, "selection": sel}


@app.post("/fill")
async def fill_region(body: dict) -> dict:
    """Fill a tile-layer rectangle with a single key (or erase with null).

    body: {
      layer?: str,              # default: state.layers[0].name
      region?: {x0,y0,x1,y1},   # default: STATE.last_selection
      key: str | null           # required (may be null to erase)
    }
    """
    if "key" not in body:
        raise HTTPException(400, "key required (use null to erase)")
    key = body["key"]  # may be None

    layer = body.get("layer")
    region = body.get("region")
    if region is None:
        if STATE.last_selection is None:
            raise HTTPException(400, "no region and no stored selection")
        region = STATE.last_selection
        if layer is None:
            layer = region.get("layer")
    if layer is None:
        layers = STATE.state.get("layers") or []
        if not layers:
            raise HTTPException(400, "map has no tile layers")
        layer = layers[0]["name"]

    try:
        res = await STATE.fill_rect(
            layer,
            int(region["x0"]), int(region["y0"]),
            int(region["x1"]), int(region["y1"]),
            key,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))

    if res.get("reload"):
        await STATE.broadcast({"type": "map_loaded", "state": STATE.state})
    else:
        await STATE.broadcast({
            "type": "patch", "op": "paint",
            "layer": layer,
            "cells": res["cells"],
        })
    return res


@app.get("/wang/sets")
async def get_wang_sets() -> dict:
    """List wang sets whose tileset is referenced by the currently loaded
    TMX. Result includes color swatches so the palette can render them.
    """
    if STATE.tmx_path is None:
        return {"sets": []}
    # Extract tileset stems from the loaded TMX's `tileset source="..."`
    # entries. We want the RAW stems (matches DB's `tileset` field).
    import xml.etree.ElementTree as ET
    try:
        tree = ET.parse(STATE.tmx_path)
    except Exception as e:
        raise HTTPException(500, f"failed to read TMX: {e}")
    stems: list[str] = []
    for ts in tree.getroot().findall("tileset"):
        src = ts.get("source")
        if src:
            stems.append(Path(src).stem)
    sets = list_wangsets_for_tilesets(STATE.db_path, stems)
    return {"sets": sets, "tileset_stems": stems}


@app.get("/wang/tiles/{wangset_uid:path}")
async def get_wang_tiles(wangset_uid: str) -> dict:
    """Enumerate all tiles in a wangset with 8-corner colors + studio key.
    The `:path` converter lets us accept UIDs that contain '::' etc."""
    rows = list_wang_tile_entries(STATE.db_path, wangset_uid)
    return {"wangset_uid": wangset_uid, "tiles": rows}


@app.post("/wang/paint")
async def wang_paint_http(body: dict) -> dict:
    """Apply wang-aware autotile paint. body:
       {layer, wangset_uid, color: int, cells: [{x,y}], erase?: bool}
    Returns the paint result; broadcasts 'patch' op:'paint' downstream.
    """
    layer = body.get("layer")
    wsu = body.get("wangset_uid")
    color = body.get("color")
    cells = body.get("cells")
    erase = bool(body.get("erase", False))
    if not layer:
        # Default: first tile layer
        layers = STATE.state.get("layers") or []
        if not layers:
            raise HTTPException(400, "map has no tile layers")
        layer = layers[0]["name"]
    if not wsu or not isinstance(cells, list):
        raise HTTPException(400, "wangset_uid + cells required")
    if color is None:
        color = 1  # sane default: first real color
    try:
        res = await STATE.wang_paint(layer, wsu, int(color), cells,
                                     erase=erase)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))

    if res.get("reload"):
        await STATE.broadcast({"type": "map_loaded", "state": STATE.state})
    else:
        await STATE.broadcast({
            "type": "patch", "op": "paint",
            "layer": layer,
            "cells": res.get("cells", []),
            "wang": res.get("wang"),
        })
    return res


@app.post("/wang/fill_rect")
async def wang_fill_rect_http(body: dict) -> dict:
    """Wang-aware rectangle fill. body:
       {layer?, wangset_uid, color?, x0, y0, x1, y1, erase?: bool}
    If `selection` is true and x0/y0/x1/y1 missing, uses last stored
    selection. Broadcasts 'patch' op:'paint' like wang_paint.
    """
    layer = body.get("layer")
    wsu = body.get("wangset_uid")
    color = body.get("color", 1)
    erase = bool(body.get("erase", False))
    use_selection = bool(body.get("use_selection", False))
    x0 = body.get("x0"); y0 = body.get("y0")
    x1 = body.get("x1"); y1 = body.get("y1")

    if use_selection or None in (x0, y0, x1, y1):
        sel = STATE.last_selection
        if not sel:
            raise HTTPException(400, "no stored selection and no rect given")
        x0, y0, x1, y1 = sel["x0"], sel["y0"], sel["x1"], sel["y1"]
        if not layer:
            layer = sel["layer"]

    if not layer:
        layers = STATE.state.get("layers") or []
        if not layers:
            raise HTTPException(400, "map has no tile layers")
        layer = layers[0]["name"]
    if not wsu:
        raise HTTPException(400, "wangset_uid required")
    try:
        res = await STATE.wang_fill_rect(
            layer, int(x0), int(y0), int(x1), int(y1),
            wsu, int(color), erase=erase,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))

    if res.get("reload"):
        await STATE.broadcast({"type": "map_loaded", "state": STATE.state})
    else:
        await STATE.broadcast({
            "type": "patch", "op": "paint",
            "layer": layer,
            "cells": res.get("cells", []),
            "wang": res.get("wang"),
        })
    return res


@app.post("/undo")
async def undo_http(body: dict | None = None) -> dict:
    """Revert the most recent recorded paint patch. No-op if stack empty.
    Returns `{ok: true, applied: bool, depth_remaining: int}`.
    """
    try:
        event = await STATE.undo()
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    if event is None:
        return {"ok": True, "applied": False,
                "depth_remaining": len(STATE.undo_stack),
                "redo_depth": len(STATE.redo_stack)}
    # Broadcast. If the undo triggered a cold reload, resend full state;
    # otherwise just the paint patch.
    if event.get("reload"):
        await STATE.broadcast({"type": "map_loaded", "state": STATE.state})
    else:
        await STATE.broadcast(event)
    return {"ok": True, "applied": True,
            "layer": event["layer"],
            "cells_applied": len(event.get("cells", [])),
            "depth_remaining": len(STATE.undo_stack),
            "redo_depth": len(STATE.redo_stack)}


@app.post("/redo")
async def redo_http(body: dict | None = None) -> dict:
    """Re-apply the most recently undone paint patch. No-op if redo
    stack empty."""
    try:
        event = await STATE.redo()
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    if event is None:
        return {"ok": True, "applied": False,
                "depth_remaining": len(STATE.undo_stack),
                "redo_depth": len(STATE.redo_stack)}
    if event.get("reload"):
        await STATE.broadcast({"type": "map_loaded", "state": STATE.state})
    else:
        await STATE.broadcast(event)
    return {"ok": True, "applied": True,
            "layer": event["layer"],
            "cells_applied": len(event.get("cells", [])),
            "depth_remaining": len(STATE.undo_stack),
            "redo_depth": len(STATE.redo_stack)}


@app.get("/history")
async def history_http() -> dict:
    """Expose current undo/redo stack depth (for UI + debugging)."""
    return {
        "undo_depth": len(STATE.undo_stack),
        "redo_depth": len(STATE.redo_stack),
        "undo_max": STATE.undo_max,
    }


@app.post("/patch/object")
async def patch_object(body: dict) -> dict:
    """Apply an object-group patch. body: {group, id, op, ...}"""
    group = body.get("group")
    if not group:
        raise HTTPException(400, "group required")
    patch = {k: v for k, v in body.items() if k != "group"}
    try:
        res = await STATE.patch_object(group, patch)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    if res.get("reload"):
        await STATE.broadcast({"type": "map_loaded", "state": STATE.state})
    else:
        await STATE.broadcast({
            "type": "patch",
            "op": "object",
            "group": group,
            "patch": patch,
        })
    return res


@app.post("/patch/objects_add")
async def patch_objects_add(body: dict) -> dict:
    """Batch-insert new tile-objects into an objectgroup (v0.8+ place_props).

    body: {layer: str, objects: [{key, x, y, width, height, rotation?}, ...]}
    After TMX write the whole state is rebuilt and re-broadcast, because
    new object keys may reference sprites not in the client's cache.
    """
    layer = body.get("layer")
    objects = body.get("objects")
    if not layer or not isinstance(objects, list):
        raise HTTPException(400, "layer + objects required")
    try:
        res = await STATE.patch_objects_add(layer, objects)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    # patch_objects_add always sets reload=True.
    await STATE.broadcast({"type": "map_loaded", "state": STATE.state})
    return res


@app.post("/patch/objects_remove")
async def patch_objects_remove(body: dict) -> dict:
    """Batch-remove objects by id from an objectgroup (v0.8+ remove_objects).

    body: {layer: str, ids: [int, ...]}
    Broadcasts a lightweight {type: "patch", op: "objects_remove",
    group, ids} so clients can prune locally without a full reload.
    """
    layer = body.get("layer")
    ids = body.get("ids")
    if not layer or not isinstance(ids, list):
        raise HTTPException(400, "layer + ids required")
    try:
        res = await STATE.patch_objects_remove(layer, [int(i) for i in ids])
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    await STATE.broadcast({
        "type": "patch",
        "op": "objects_remove",
        "group": layer,
        "ids": res.get("removed_ids", []),
    })
    return res


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    STATE.clients.add(ws)
    # Initial snapshot
    await ws.send_text(json.dumps({"type": "map_loaded", "state": STATE.state}))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")
            if mtype == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
            elif mtype == "selection":
                sel = msg.get("selection")
                if sel is not None and isinstance(sel, dict):
                    missing = [k for k in ("layer", "x0", "y0", "x1", "y1")
                               if k not in sel]
                    if missing:
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "message": f"selection missing {missing}",
                        }))
                        continue
                    sel = {
                        "layer": str(sel["layer"]),
                        "x0": int(sel["x0"]), "y0": int(sel["y0"]),
                        "x1": int(sel["x1"]), "y1": int(sel["y1"]),
                    }
                STATE.last_selection = sel
                # Echo to everyone else (sender already knows).
                await STATE.broadcast(
                    {"type": "selection", "selection": sel}, exclude=ws,
                )
            elif mtype == "patch":
                # Client-initiated paint/erase/object patch
                op = msg.get("op")
                try:
                    if op in ("paint", "erase"):
                        res = await STATE.patch_paint(
                            msg["layer"], msg.get("cells", []),
                        )
                        if res.get("reload"):
                            await STATE.broadcast(
                                {"type": "map_loaded", "state": STATE.state})
                        else:
                            await STATE.broadcast({
                                "type": "patch", "op": "paint",
                                "layer": msg["layer"],
                                "cells": res["cells"],
                            })
                    elif op == "object":
                        res = await STATE.patch_object(
                            msg["group"], msg.get("patch", {}))
                        if res.get("reload"):
                            await STATE.broadcast(
                                {"type": "map_loaded", "state": STATE.state})
                        else:
                            await STATE.broadcast({
                                "type": "patch", "op": "object",
                                "group": msg["group"],
                                "patch": msg.get("patch"),
                            })
                    else:
                        await ws.send_text(json.dumps({
                            "type": "error", "message": f"unknown op {op}",
                        }))
                except Exception as e:
                    await ws.send_text(json.dumps({
                        "type": "error", "message": str(e),
                    }))
            elif mtype == "wang_paint":
                try:
                    layer = msg.get("layer")
                    if not layer:
                        layers = STATE.state.get("layers") or []
                        if not layers:
                            raise ValueError("map has no tile layers")
                        layer = layers[0]["name"]
                    wsu = msg.get("wangset_uid")
                    color = msg.get("color", 1)
                    cells = msg.get("cells", [])
                    erase = bool(msg.get("erase", False))
                    if not wsu or not isinstance(cells, list):
                        raise ValueError("wangset_uid + cells required")
                    res = await STATE.wang_paint(
                        layer, wsu, int(color), cells, erase=erase,
                    )
                    if res.get("reload"):
                        await STATE.broadcast(
                            {"type": "map_loaded", "state": STATE.state})
                    else:
                        await STATE.broadcast({
                            "type": "patch", "op": "paint",
                            "layer": layer,
                            "cells": res.get("cells", []),
                            "wang": res.get("wang"),
                        })
                except Exception as e:
                    await ws.send_text(json.dumps({
                        "type": "error", "message": str(e),
                    }))
    except WebSocketDisconnect:
        pass
    finally:
        STATE.clients.discard(ws)


# ---------------------------------------------------------------------
# Static frontend mount
# ---------------------------------------------------------------------

FRONTEND_DIST = PLUGIN_ROOT / "studio" / "frontend" / "dist"
FRONTEND_SRC = PLUGIN_ROOT / "studio" / "frontend"
FRONTEND_INDEX = FRONTEND_DIST / "index.html"

if FRONTEND_INDEX.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True),
              name="frontend")
else:
    # The plugin ships with a prebuilt `studio/frontend/dist/`. If we reach
    # here, something went wrong: a broken install, a stripped tarball, or
    # a dev checkout before the first `npm run build`. Serve a readable
    # HTML page (not a dev-centric JSON blob) so the end user immediately
    # understands what's happening and how to recover.
    _MISSING_BUILD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Tilesmith Studio — build missing</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
           max-width: 640px; margin: 4rem auto; padding: 0 1.5rem;
           line-height: 1.55; color: #222; background: #fafafa; }}
    @media (prefers-color-scheme: dark) {{
      body {{ color: #eee; background: #181818; }}
      code, pre {{ background: #222; color: #eee; }}
    }}
    h1 {{ font-size: 1.25rem; margin-bottom: 0.25rem; }}
    .tag {{ display: inline-block; background: #c44; color: #fff;
           padding: 2px 8px; border-radius: 4px; font-size: 0.75rem;
           letter-spacing: 0.04em; text-transform: uppercase;
           margin-bottom: 1rem; }}
    pre {{ background: #eee; padding: 0.75rem 1rem; border-radius: 6px;
          overflow-x: auto; font-size: 0.85rem; }}
    code {{ background: #eee; padding: 1px 5px; border-radius: 3px; }}
    .muted {{ color: #888; font-size: 0.85rem; }}
    a {{ color: #36c; }}
  </style>
</head>
<body>
  <span class="tag">Tilesmith Studio</span>
  <h1>Studio frontend build is missing.</h1>
  <p>
    The plugin normally ships with a prebuilt Studio UI. This install
    does not have one, which usually means a broken update or a dev
    checkout that was never built.
  </p>
  <h2>Quick fix</h2>
  <p>If you have <a href="https://nodejs.org">Node.js 18+</a> installed, run:</p>
  <pre>cd {src}
npm install
npm run build</pre>
  <p>
    Then in Claude Code, run <code>close_studio</code> and
    <code>open_studio</code> again (the bridge caches this state until
    restart).
  </p>
  <h2>Still stuck?</h2>
  <p>
    Please open an issue at
    <a href="https://github.com/fatihBulbul/tilesmith/issues">
      github.com/fatihBulbul/tilesmith
    </a>
    with your Claude Code version and the plugin path shown below.
  </p>
  <p class="muted">Plugin path: <code>{src}</code></p>
</body>
</html>"""

    @app.get("/", response_class=HTMLResponse)
    async def root_missing_build() -> HTMLResponse:
        return HTMLResponse(
            content=_MISSING_BUILD_HTML.format(src=str(FRONTEND_SRC)),
            status_code=503,
        )


# ---------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tmx", type=str, default=None,
                    help="TMX dosyası (opsiyonel, sonradan /open ile de yüklenir)")
    ap.add_argument("--port", type=int, default=3024)
    ap.add_argument("--host", type=str, default="127.0.0.1")
    args = ap.parse_args()

    import uvicorn

    if args.tmx:
        # Synchronous preload before uvicorn takes over
        asyncio.run(STATE.load(args.tmx))
        print(f"[bridge] loaded TMX: {STATE.tmx_path}")

    print(f"[bridge] http://{args.host}:{args.port}  (open in browser)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
