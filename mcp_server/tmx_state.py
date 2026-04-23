"""
TMX -> browser-friendly JSON state + sprite extraction.

Tek bir TMX dosyasını parse eder, şunları üretir:
  - MapState dict: layers/objectgroups/tiles (string key'ler ile)
  - unique_tiles: dict[key -> PIL.Image] (bridge bunu cache'ler ve
    GET /sprite/{key}.png olarak servis eder)

Tile key formatı: "{tileset_stem}__{local_id}"  (unique identifier)

Animations:
  - Bir tile'ın TSX'i <animation><frame tileid=X duration=Y/></animation>
    içeriyorsa, MapState.tiles[key].animation listesi çıkarılır.
  - Frame key'leri de aynı tileset'ten extract edilir (unique_tiles'a eklenir).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from PIL import Image


def _safe_key(tileset_stem: str, local_id: int) -> str:
    """URL- and filesystem-safe, unique tile key.

    Replaces whitespace and path separators with underscores so the key
    can be used directly in HTTP paths without URL-encoding.
    """
    safe = []
    for ch in tileset_stem:
        if ch.isalnum() or ch in "-_.":
            safe.append(ch)
        else:
            safe.append("_")
    return f"{''.join(safe)}__{local_id}"


@dataclass
class TilesetInfo:
    tsx_path: Path
    tile_w: int
    tile_h: int
    columns: int
    type: str                    # "atlas" | "collection"
    image_path: Path | None = None  # atlas only
    collection: dict[int, Path] = field(default_factory=dict)  # collection only
    # local_id -> list of (frame_local_id, duration_ms)
    animations: dict[int, list[tuple[int, int]]] = field(default_factory=dict)


def _load_tileset(tsx_abs: Path) -> TilesetInfo:
    root = ET.parse(tsx_abs).getroot()
    tw = int(root.get("tilewidth", 32))
    th = int(root.get("tileheight", 32))
    cols = int(root.get("columns", 0))
    img_el = root.find("image")
    info = TilesetInfo(
        tsx_path=tsx_abs, tile_w=tw, tile_h=th, columns=cols,
        type="atlas" if img_el is not None else "collection",
    )
    if img_el is not None:
        info.image_path = (tsx_abs.parent / img_el.get("source")).resolve()
    else:
        for tile_el in root.findall("tile"):
            tid = int(tile_el.get("id"))
            timg = tile_el.find("image")
            if timg is not None:
                info.collection[tid] = (
                    tsx_abs.parent / timg.get("source")
                ).resolve()
    # Animation metadata
    for tile_el in root.findall("tile"):
        tid = int(tile_el.get("id"))
        anim = tile_el.find("animation")
        if anim is None:
            continue
        frames: list[tuple[int, int]] = []
        for fr in anim.findall("frame"):
            frames.append((
                int(fr.get("tileid", "0")),
                int(fr.get("duration", "100")),
            ))
        if frames:
            info.animations[tid] = frames
    return info


def _get_tile_image(ts: TilesetInfo, local_id: int) -> Image.Image | None:
    """Extract a single tile image from its tileset."""
    if ts.type == "atlas":
        if ts.columns <= 0 or ts.image_path is None:
            return None
        col = local_id % ts.columns
        row = local_id // ts.columns
        with Image.open(ts.image_path) as im:
            atlas = im.convert("RGBA")
        box = (col * ts.tile_w, row * ts.tile_h,
               (col + 1) * ts.tile_w, (row + 1) * ts.tile_h)
        if box[2] > atlas.width or box[3] > atlas.height:
            return None
        return atlas.crop(box)
    # collection
    p = ts.collection.get(local_id)
    if p is None or not p.exists():
        return None
    with Image.open(p) as im:
        return im.convert("RGBA").copy()


def _tile_size(ts: TilesetInfo, local_id: int) -> tuple[int, int]:
    """Return (w, h) without loading the full image when possible."""
    if ts.type == "atlas":
        return (ts.tile_w, ts.tile_h)
    p = ts.collection.get(local_id)
    if p is None or not p.exists():
        return (ts.tile_w, ts.tile_h)
    with Image.open(p) as im:
        return im.size


def build_map_state(tmx_path: str | Path) -> tuple[dict, dict[str, Image.Image]]:
    """Build browser-ready MapState dict + unique_tiles cache.

    Returns:
        (state, unique_tiles)
        state: JSON-serializable dict for frontend
        unique_tiles: dict[key -> PIL.Image], bridge bunu HTTP'den servis eder
    """
    tmx_path = Path(tmx_path).resolve()
    root = ET.parse(tmx_path).getroot()

    map_w = int(root.get("width", 0))
    map_h = int(root.get("height", 0))
    tile_w = int(root.get("tilewidth", 32))
    tile_h = int(root.get("tileheight", 32))

    # 1. Load all external tilesets
    tsx_refs: list[tuple[int, TilesetInfo]] = []
    for ts_el in root.findall("tileset"):
        fgid = int(ts_el.get("firstgid", 0))
        src = ts_el.get("source")
        if src is None:
            continue
        tsx_abs = (tmx_path.parent / src).resolve()
        try:
            tsx_refs.append((fgid, _load_tileset(tsx_abs)))
        except Exception as e:
            print(f"[tmx_state] failed to load {tsx_abs}: {e}")
    tsx_refs.sort(key=lambda x: -x[0])  # larger firstgid first

    def resolve_gid(gid: int) -> tuple[TilesetInfo, int] | None:
        g = gid & 0x1FFFFFFF
        if g == 0:
            return None
        for fgid, ts in tsx_refs:
            if g >= fgid:
                return (ts, g - fgid)
        return None

    # 2. Walk layers & collect used tiles
    used: dict[str, tuple[TilesetInfo, int]] = {}  # key -> (ts, lid)

    def key_for(ts: TilesetInfo, lid: int) -> str:
        return _safe_key(ts.tsx_path.stem, lid)

    def add_used(ts: TilesetInfo, lid: int) -> str:
        k = key_for(ts, lid)
        if k not in used:
            used[k] = (ts, lid)
            # Expand animation frames too (they're needed as sprites)
            for fid, _dur in ts.animations.get(lid, []):
                k2 = key_for(ts, fid)
                if k2 not in used:
                    used[k2] = (ts, fid)
        return k

    # Tile layers
    layers: list[dict] = []
    for layer_el in root.findall("layer"):
        name = layer_el.get("name", "")
        opacity = float(layer_el.get("opacity", 1.0))
        visible = layer_el.get("visible", "1") != "0"
        data_el = layer_el.find("data")
        rows: list[list[str | None]] = []
        if data_el is not None and (data_el.get("encoding") or "csv") == "csv":
            txt = (data_el.text or "").strip()
            for row_text in txt.split("\n"):
                row_text = row_text.strip().rstrip(",")
                if not row_text:
                    continue
                cells: list[str | None] = []
                for tok in row_text.split(","):
                    tok = tok.strip()
                    if not tok or tok == "0":
                        cells.append(None)
                        continue
                    r = resolve_gid(int(tok))
                    if r is None:
                        cells.append(None)
                        continue
                    ts, lid = r
                    cells.append(add_used(ts, lid))
                rows.append(cells)
        layers.append({
            "name": name,
            "type": "tile",
            "visible": visible,
            "opacity": opacity,
            "data": rows,
        })

    # Object groups
    object_groups: list[dict] = []
    for og_el in root.findall("objectgroup"):
        name = og_el.get("name", "")
        visible = og_el.get("visible", "1") != "0"
        opacity = float(og_el.get("opacity", 1.0))
        objs: list[dict] = []
        for o in og_el.findall("object"):
            gid_s = o.get("gid")
            if gid_s is None:
                continue
            r = resolve_gid(int(gid_s))
            if r is None:
                continue
            ts, lid = r
            k = add_used(ts, lid)
            objs.append({
                "id": int(o.get("id", 0)),
                "key": k,
                "x": float(o.get("x", 0)),
                "y": float(o.get("y", 0)),   # Tiled: bottom-left y
                "w": float(o.get("width", 0)),
                "h": float(o.get("height", 0)),
            })
        object_groups.append({
            "name": name,
            "visible": visible,
            "opacity": opacity,
            "objects": objs,
        })

    # 3. Build TileAsset records + sprite cache
    tiles: dict[str, dict] = {}
    unique_tiles: dict[str, Image.Image] = {}
    for key, (ts, lid) in used.items():
        im = _get_tile_image(ts, lid)
        if im is None:
            # Unable to extract — skip silently
            continue
        unique_tiles[key] = im
        entry: dict[str, Any] = {
            "w": im.width,
            "h": im.height,
            "sprite_url": f"/sprite/{key}.png",
        }
        # Animation? Anchor-tile metadata applies only to the anchor lid.
        anim = ts.animations.get(lid)
        if anim:
            entry["animation"] = [
                {"key": key_for(ts, fid), "duration": dur}
                for (fid, dur) in anim
            ]
        tiles[key] = entry

    state = {
        "tmx_path": str(tmx_path),
        "width": map_w,
        "height": map_h,
        "tile_w": tile_w,
        "tile_h": tile_h,
        "layers": layers,
        "object_groups": object_groups,
        "tiles": tiles,
    }
    return state, unique_tiles


if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) < 2:
        print("Usage: tmx_state.py <path.tmx>")
        sys.exit(1)
    st, sprites = build_map_state(sys.argv[1])
    print(json.dumps({
        "summary": {
            "size": f"{st['width']}x{st['height']}",
            "tile_size": f"{st['tile_w']}x{st['tile_h']}",
            "layers": [l["name"] for l in st["layers"]],
            "object_groups": [
                f"{og['name']}({len(og['objects'])})"
                for og in st["object_groups"]
            ],
            "unique_tiles": len(st["tiles"]),
            "animated_tiles": sum(
                1 for t in st["tiles"].values() if "animation" in t
            ),
            "sprite_cache": len(sprites),
        }
    }, indent=2))
