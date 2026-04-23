#!/usr/bin/env python3
"""
Generator (rich) — Çok parçalı, zengin içerikli N×N harita (default 80×80).

Üretilen içerik:
  * Çoklu dirt patch + aralarında doğal dirt patika
  * Ana dikey sinüs nehri + iki küçük gölet (durgun su)
  * 3 farklı orman kümesi: renkli yaprak, palm, naked/small
  * Çim üzerine dağılmış flower + rock prop'ları
  * Tiled animated tiles (kelebek, sinek, sivrisinek, rüzgar FX)
    — her animasyon ayrı bir TSX dosyası olarak çıktıya yazılır ve
    TMX'ten referanslanır; obje gid'leri tile 0'a (animasyon anchor'u)
    bakar, Tiled otomatik oynatır.

Kullanım:
  python3 generate_rich_map.py --seed 7 --out output/rich-80.tmx
"""

from __future__ import annotations
import argparse
import math
import os
import random
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "indexer"))

from query import (  # type: ignore
    find_pure_wang_tiles,
    find_wang_tiles_by_corners,
    pick_basic_grass_fillers,
)

# ---------------------------------------------------------------------
# Defaults / config
# ---------------------------------------------------------------------

TILE_SIZE = 32
DEFAULT_PACK = "ERW - Grass Land 2.0 v1.9"
DEFAULT_WIDTH = 80
DEFAULT_HEIGHT = 80

TERRAIN_NAME = "Tileset-Terrain-new grass"
RIVER_NAME = "platform - water to grass - river orientation"
PROPS_NAME = "Atlas-Props-sheet1-sprites"

DIRT_WANGSET_NAME = "dirt"
RIVER_WANGSET_NAME = "water to grass (river orientation)"
GRASS_COLOR = 0
RIVER_COLOR = 1
DIRT_COLOR = 2

BASIC_GRASS_IDS = [130, 131, 132, 185, 186, 187]
GRASS_WEIGHTS = [3, 3, 3, 2, 2, 2]

# Firstgid plan (hard-coded; pack-known counts)
TERRAIN_TILECOUNT = 3960
RIVER_TILECOUNT = 870
PROPS_TILECOUNT = 1318  # GL2.0 collection size

TERRAIN_FIRSTGID = 1
RIVER_FIRSTGID = TERRAIN_FIRSTGID + TERRAIN_TILECOUNT              # 3961
PROPS_FIRSTGID = RIVER_FIRSTGID + RIVER_TILECOUNT                  # 4831
ANIM_FIRSTGID_BASE = PROPS_FIRSTGID + PROPS_TILECOUNT + 100        # 6249 (buffer)

# DB
_REPO_ROOT = HERE.parent
DB_PATH = Path(
    os.environ.get("TILESMITH_DB_PATH")
    or os.environ.get("ERW_DB_PATH")
    or str(_REPO_ROOT / "data" / "tiles.db")
)


# ---------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _wangset_uid(pack: str, tileset: str, ws_name: str) -> str:
    return f"{pack}::{tileset}::{ws_name}"


def tsx_source_rel(pack: str, tileset: str, tmx_dir: Path) -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT source_path FROM tilesets "
            "WHERE pack_name = ? AND name = ?",
            (pack, tileset),
        ).fetchone()
    if row is None or not row["source_path"]:
        raise RuntimeError(
            f"TSX not found: pack={pack!r} tileset={tileset!r}")
    return os.path.relpath(Path(row["source_path"]).resolve(),
                           tmx_dir.resolve())


def get_props(category: str, pack: str,
              variant: str = "composite",
              tileset: str = PROPS_NAME,
              name_includes: list[str] | None = None,
              name_excludes: list[str] | None = None) -> list[dict]:
    """Pack içinden composite prop'ları getir, dosya adıyla filtrele."""
    sql = ("SELECT prop_uid, tileset, local_id, image_path, category, variant "
           "FROM props WHERE category = ? AND pack_name = ? "
           "AND tileset = ? AND variant = ? ORDER BY local_id")
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            sql, (category, pack, tileset, variant)).fetchall()]
    if name_includes:
        inc_low = [kw.lower() for kw in name_includes]
        rows = [r for r in rows
                if any(k in Path(r["image_path"]).name.lower()
                       for k in inc_low)]
    if name_excludes:
        exc_low = [kw.lower() for kw in name_excludes]
        rows = [r for r in rows
                if not any(k in Path(r["image_path"]).name.lower()
                           for k in exc_low)]
    return rows


def get_animated_props(pack: str,
                       subjects: list[str] | None = None) -> list[dict]:
    sql = ("SELECT aprop_uid, image_path, filename, category, subject, "
           "action, variant, frame_count, frame_w, frame_h, sheet_w, sheet_h "
           "FROM animated_props WHERE pack_name = ? ORDER BY filename")
    with db() as conn:
        rows = [dict(r) for r in conn.execute(sql, (pack,)).fetchall()]
    if subjects:
        subs_low = [s.lower() for s in subjects]
        rows = [r for r in rows
                if any(s in Path(r["image_path"]).name.lower() for s in subs_low)]
    return rows


# ---------------------------------------------------------------------
# Intent grids (corner-level paint)
# ---------------------------------------------------------------------

class World:
    """Harita niyetini corner seviyesinde tutar. (W+1) x (H+1) corner grid."""
    def __init__(self, width: int, height: int):
        self.w = width
        self.h = height
        self.dirt = [[GRASS_COLOR] * (width + 1) for _ in range(height + 1)]
        self.water = [[GRASS_COLOR] * (width + 1) for _ in range(height + 1)]

    def paint_rect_dirt(self, x0: int, y0: int, w: int, h: int,
                        round_corners: bool = True) -> None:
        for cy in range(y0, y0 + h + 1):
            for cx in range(x0, x0 + w + 1):
                if 0 <= cx <= self.w and 0 <= cy <= self.h:
                    self.dirt[cy][cx] = DIRT_COLOR
        if round_corners:
            for (cx, cy) in [(x0, y0), (x0 + w, y0),
                             (x0, y0 + h), (x0 + w, y0 + h)]:
                if 0 <= cx <= self.w and 0 <= cy <= self.h:
                    self.dirt[cy][cx] = GRASS_COLOR

    def paint_path_dirt(self, pts: list[tuple[int, int]],
                        thickness: int = 2) -> None:
        """pts arası düz doğru parçalarıyla dirt patika çiz."""
        half = thickness // 2
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            steps = max(abs(x1 - x0), abs(y1 - y0)) * 2 + 1
            for s in range(steps + 1):
                t = s / steps
                cx = round(x0 + (x1 - x0) * t)
                cy = round(y0 + (y1 - y0) * t)
                for dy in range(-half, half + 1):
                    for dx in range(-half, half + 1):
                        xx, yy = cx + dx, cy + dy
                        if 0 <= xx <= self.w and 0 <= yy <= self.h:
                            self.dirt[yy][xx] = DIRT_COLOR

    def paint_river_sinus(self, center_x: int, amp: int, period: int,
                          half_width: int) -> None:
        for cy in range(self.h + 1):
            cx_f = center_x + amp * math.sin(2 * math.pi * (cy - 0.5) / period)
            cx_int = int(round(cx_f))
            for dx in range(-half_width, half_width + 1):
                x = cx_int + dx
                if 0 <= x <= self.w:
                    self.water[cy][x] = RIVER_COLOR

    def paint_pond(self, cx: int, cy: int, radius: int) -> None:
        for y in range(cy - radius - 1, cy + radius + 2):
            for x in range(cx - radius - 1, cx + radius + 2):
                if 0 <= x <= self.w and 0 <= y <= self.h:
                    dx, dy = x - cx, y - cy
                    if dx * dx + dy * dy <= radius * radius:
                        self.water[y][x] = RIVER_COLOR


# ---------------------------------------------------------------------
# Tile pool
# ---------------------------------------------------------------------

class TilePool:
    def __init__(self, rng: random.Random, pack: str):
        self.rng = rng
        self.pack = pack
        self.dirt_ws = _wangset_uid(pack, TERRAIN_NAME, DIRT_WANGSET_NAME)
        self.river_ws = _wangset_uid(pack, RIVER_NAME, RIVER_WANGSET_NAME)
        self._wang_cache: dict[tuple[str, int, int, int, int],
                               list[dict]] = {}
        self.basic_grass = pick_basic_grass_fillers(
            tileset=TERRAIN_NAME, local_ids=BASIC_GRASS_IDS, pack_name=pack)
        self._grass_by_id = {g["local_id"]: g for g in self.basic_grass}
        self.pure_dirt = find_pure_wang_tiles(self.dirt_ws, DIRT_COLOR)
        self.pure_water = find_pure_wang_tiles(self.river_ws, RIVER_COLOR)
        for (name, lst) in [("grass", self.basic_grass),
                            ("pure dirt", self.pure_dirt),
                            ("pure water", self.pure_water)]:
            if not lst:
                raise RuntimeError(
                    f"{name} pool empty (pack={pack!r})")

    def pick_grass(self) -> dict:
        lid = self.rng.choices(BASIC_GRASS_IDS, weights=GRASS_WEIGHTS, k=1)[0]
        return self._grass_by_id[lid]

    def pick_dirt_interior(self) -> dict:
        return self.rng.choice(self.pure_dirt)

    def pick_water_interior(self) -> dict:
        return self.rng.choice(self.pure_water)

    def pick_wang(self, ws_uid: str,
                  nw: int, ne: int, sw: int, se: int) -> dict | None:
        key = (ws_uid, nw, ne, sw, se)
        if key not in self._wang_cache:
            self._wang_cache[key] = find_wang_tiles_by_corners(
                ws_uid, nw=nw, ne=ne, sw=sw, se=se)
        tiles = self._wang_cache[key]
        return self.rng.choice(tiles) if tiles else None


# ---------------------------------------------------------------------
# Layer builders
# ---------------------------------------------------------------------

def build_terrain_layer(pool: TilePool, world: World,
                        stats: dict) -> list[list[int]]:
    grid = [[0] * world.w for _ in range(world.h)]
    for y in range(world.h):
        for x in range(world.w):
            c = (world.dirt[y][x], world.dirt[y][x + 1],
                 world.dirt[y + 1][x], world.dirt[y + 1][x + 1])
            if c == (GRASS_COLOR,) * 4:
                t = pool.pick_grass(); stats["grass"] += 1
            elif c == (DIRT_COLOR,) * 4:
                t = pool.pick_dirt_interior(); stats["dirt_interior"] += 1
            else:
                t = pool.pick_wang(pool.dirt_ws, *c)
                if t is None:
                    t = pool.pick_grass(); stats["wang_missing_dirt"] += 1
                else:
                    stats["dirt_boundary"] += 1
            grid[y][x] = TERRAIN_FIRSTGID + t["local_id"]
    return grid


def build_water_layer(pool: TilePool, world: World,
                      stats: dict) -> list[list[int]]:
    grid = [[0] * world.w for _ in range(world.h)]
    for y in range(world.h):
        for x in range(world.w):
            c = (world.water[y][x], world.water[y][x + 1],
                 world.water[y + 1][x], world.water[y + 1][x + 1])
            if c == (GRASS_COLOR,) * 4:
                continue
            if c == (RIVER_COLOR,) * 4:
                t = pool.pick_water_interior(); stats["water_interior"] += 1
            else:
                t = pool.pick_wang(pool.river_ws, *c)
                if t is None:
                    stats["wang_missing_water"] += 1
                    continue
                stats["water_boundary"] += 1
            grid[y][x] = RIVER_FIRSTGID + t["local_id"]
    return grid


# ---------------------------------------------------------------------
# Prop placement (forest zones + scattered flora)
# ---------------------------------------------------------------------

def _is_zone_clear(world: World, x: int, y: int, w_t: int, h_t: int,
                   occupied: set[tuple[int, int]]) -> bool:
    for yy in range(y, y + h_t):
        for xx in range(x, x + w_t):
            if not (0 <= xx < world.w and 0 <= yy < world.h):
                return False
            if (xx, yy) in occupied:
                return False
            # avoid water cells
            for (cx, cy) in [(xx, yy), (xx + 1, yy),
                             (xx, yy + 1), (xx + 1, yy + 1)]:
                if world.water[cy][cx] == RIVER_COLOR:
                    return False
    return True


def _mark_zone(x: int, y: int, w_t: int, h_t: int,
               occupied: set[tuple[int, int]], pad: int = 1) -> None:
    for yy in range(y - pad, y + h_t + pad):
        for xx in range(x - pad, x + w_t + pad):
            occupied.add((xx, yy))


def place_forest_cluster(pool: TilePool, world: World,
                         rect: tuple[int, int, int, int],
                         tree_filter_includes: list[str] | None,
                         tree_filter_excludes: list[str] | None,
                         density: float,
                         tree_prob: float,
                         occupied: set[tuple[int, int]],
                         stats: dict) -> list[dict]:
    """Bir orman kümesi yerleştir. rect = (left, top, right, bottom)."""
    left, top, right, bottom = rect
    trees = get_props(
        "tree", pool.pack, name_includes=tree_filter_includes,
        name_excludes=tree_filter_excludes,
    )
    bushes = get_props("bush", pool.pack)
    if not trees:
        return []
    from PIL import Image as PILImage

    def sizes(rows: list[dict]) -> list[tuple[dict, int, int]]:
        out = []
        for r in rows:
            try:
                with PILImage.open(r["image_path"]) as im:
                    out.append((r, im.width, im.height))
            except Exception:
                pass
        return out
    tree_info = sizes(trees)
    bush_info = sizes(bushes)
    objs: list[dict] = []
    rng = pool.rng

    for y in range(top, bottom + 1):
        for x in range(left, right + 1):
            if rng.random() > density:
                continue
            if rng.random() < tree_prob:
                pool_list = tree_info
            else:
                pool_list = bush_info if bush_info else tree_info
            if not pool_list:
                continue
            p, pw, ph = rng.choice(pool_list)
            w_t = max(1, pw // TILE_SIZE)
            h_t = max(1, ph // TILE_SIZE)
            if not _is_zone_clear(world, x, y, w_t, h_t, occupied):
                continue
            _mark_zone(x, y, w_t, h_t, occupied, pad=1)
            gid = PROPS_FIRSTGID + p["local_id"]
            objs.append({
                "gid": gid,
                "x_px": x * TILE_SIZE,
                "y_px": (y + h_t) * TILE_SIZE,  # Tiled: y = bottom edge
                "w": pw, "h": ph,
                "name": Path(p["image_path"]).stem,
            })
            stats["trees" if "tree" in p["category"] else "bushes"] += 1
    return objs


def scatter_flora(pool: TilePool, world: World,
                  density: float, occupied: set[tuple[int, int]],
                  stats: dict) -> list[dict]:
    """Çim üzerine flower / rock / decoration scattered."""
    flowers = get_props("flower", pool.pack)
    rocks = get_props("rock", pool.pack)
    small_decos: list[dict] = []
    # Küçük decorations: mushroom/stump/log vs. (küçük boyutlu composites)
    candidates = get_props("decoration", pool.pack)
    for r in candidates:
        if r.get("size_w") and r["size_w"] <= 64 and r["size_h"] <= 64:
            small_decos.append(r)
    pool_rows = flowers + rocks + small_decos
    if not pool_rows:
        return []

    from PIL import Image as PILImage
    info = []
    for r in pool_rows:
        try:
            with PILImage.open(r["image_path"]) as im:
                if im.width <= 64 and im.height <= 64:
                    info.append((r, im.width, im.height))
        except Exception:
            pass
    if not info:
        return []

    objs: list[dict] = []
    rng = pool.rng
    for y in range(world.h):
        for x in range(world.w):
            if (x, y) in occupied:
                continue
            # Sadece tamamı çim olan hücrelere dağıt
            c = (world.dirt[y][x], world.dirt[y][x + 1],
                 world.dirt[y + 1][x], world.dirt[y + 1][x + 1],
                 world.water[y][x], world.water[y][x + 1],
                 world.water[y + 1][x], world.water[y + 1][x + 1])
            if any(v != GRASS_COLOR for v in c):
                continue
            if rng.random() > density:
                continue
            p, pw, ph = rng.choice(info)
            occupied.add((x, y))
            gid = PROPS_FIRSTGID + p["local_id"]
            objs.append({
                "gid": gid,
                "x_px": x * TILE_SIZE,
                "y_px": y * TILE_SIZE + ph,
                "w": pw, "h": ph,
                "name": Path(p["image_path"]).stem,
            })
            stats[p["category"]] = stats.get(p["category"], 0) + 1
    return objs


# ---------------------------------------------------------------------
# Animated tile TSX generation
# ---------------------------------------------------------------------

def write_anim_tsx(out_path: Path, name: str,
                   image_path: Path, frame_w: int, frame_h: int,
                   frame_count: int, duration_ms: int = 90) -> None:
    """Bir sprite sheet için Tiled animated-tile TSX yaz.

    Sheet yatay strip kabul edilir (tilecount = frame_count, columns = frame_count).
    Tile id=0 anchor'dur ve <animation> ile tüm frame'leri sırayla oynatır.
    """
    from PIL import Image as PILImage
    with PILImage.open(image_path) as im:
        sheet_w, sheet_h = im.size

    tileset = ET.Element("tileset", {
        "version": "1.9", "tiledversion": "1.9.2",
        "name": name,
        "tilewidth": str(frame_w),
        "tileheight": str(frame_h),
        "tilecount": str(frame_count),
        "columns": str(frame_count),
    })
    img_rel = os.path.relpath(image_path.resolve(), out_path.parent.resolve())
    ET.SubElement(tileset, "image", {
        "source": img_rel,
        "width": str(sheet_w),
        "height": str(sheet_h),
    })
    tile = ET.SubElement(tileset, "tile", {"id": "0"})
    anim = ET.SubElement(tile, "animation")
    for i in range(frame_count):
        ET.SubElement(anim, "frame", {
            "tileid": str(i),
            "duration": str(duration_ms),
        })
    tree = ET.ElementTree(tileset)
    ET.indent(tree, space=" ")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(out_path, encoding="UTF-8", xml_declaration=True)


def place_animated_critters(pack: str, world: World, tmx_dir: Path,
                            occupied: set[tuple[int, int]],
                            rng: random.Random,
                            stats: dict) -> tuple[list[dict], list[dict]]:
    """Anim TSX'leri yaz ve her biri için objeler üret.

    Dönüş: (tileset_refs, objects)
      tileset_refs: TMX'te <tileset firstgid=.. source=..> için meta
      objects: <object gid=..> için meta
    """
    # Her animasyon sheet'ten kaç tane haritada olsun
    plan: list[tuple[str, list[str], int]] = [
        # (nickname for tsx filename, subject name filter, count)
        ("anim-butterfly1", ["butterfly1-flying around"], 3),
        ("anim-butterfly2", ["butterfly2-flying around"], 3),
        ("anim-butterfly3", ["butterfly3-flying around"], 2),
        ("anim-mosquito1",  ["mosquito flying around-14"], 1),
        ("anim-mosquito2",  ["mosquito flying around2-14"], 1),
        ("anim-mosquito3",  ["mosquito flying around3-14"], 1),
        ("anim-flies",      ["flies96x96"], 2),
        ("anim-wind-fx",    ["wind cartoonish"], 2),
    ]
    all_anims = get_animated_props(pack)

    tsx_dir = tmx_dir / "animations"
    tileset_refs: list[dict] = []
    objects: list[dict] = []
    next_firstgid = ANIM_FIRSTGID_BASE

    for nickname, filters, count in plan:
        # Find matching animated prop
        match = None
        for a in all_anims:
            name_low = Path(a["image_path"]).name.lower()
            if any(f.lower() in name_low for f in filters):
                match = a
                break
        if match is None:
            continue
        frame_count = int(match["frame_count"])
        frame_w = int(match["frame_w"])
        frame_h = int(match["frame_h"])
        tsx_path = tsx_dir / f"{nickname}.tsx"
        write_anim_tsx(tsx_path, nickname,
                       Path(match["image_path"]),
                       frame_w, frame_h, frame_count,
                       duration_ms=70 if "fly" in nickname else 90)
        firstgid = next_firstgid
        next_firstgid += frame_count + 10  # buffer between animations

        rel = os.path.relpath(tsx_path.resolve(), tmx_dir.resolve())
        tileset_refs.append({"firstgid": firstgid, "source": rel,
                             "name": nickname})
        stats[f"anim_{nickname}_placed"] = 0

        # place `count` instances randomly on grass
        tries = 0
        placed = 0
        while placed < count and tries < count * 40:
            tries += 1
            x = rng.randint(1, world.w - 2)
            y = rng.randint(1, world.h - 2)
            # must land on grass, not on dirt/water, not overlapping others
            if (x, y) in occupied:
                continue
            cs = (world.dirt[y][x], world.dirt[y + 1][x + 1],
                  world.water[y][x], world.water[y + 1][x + 1])
            if any(v != GRASS_COLOR for v in cs):
                continue
            occupied.add((x, y))
            objects.append({
                "gid": firstgid,  # tile id 0 = animation anchor
                "x_px": x * TILE_SIZE,
                # Tiled: object y = bottom edge; anchor animations by
                # vertical center on the cell so they hover nicely.
                "y_px": y * TILE_SIZE + frame_h // 2,
                "w": frame_w, "h": frame_h,
                "name": nickname,
            })
            stats[f"anim_{nickname}_placed"] = stats[f"anim_{nickname}_placed"] + 1
            placed += 1

    return tileset_refs, objects


# ---------------------------------------------------------------------
# TMX builder
# ---------------------------------------------------------------------

def build_tmx(width: int, height: int,
              terrain: list[list[int]], water: list[list[int]],
              forest_objs: list[dict], flora_objs: list[dict],
              anim_refs: list[dict], anim_objs: list[dict],
              pack: str, tmx_dir: Path) -> ET.ElementTree:
    all_objs = forest_objs + flora_objs + anim_objs
    m = ET.Element("map", {
        "version": "1.9", "tiledversion": "1.9.2",
        "orientation": "orthogonal", "renderorder": "right-down",
        "width": str(width), "height": str(height),
        "tilewidth": str(TILE_SIZE), "tileheight": str(TILE_SIZE),
        "infinite": "0",
        "nextlayerid": "5",
        "nextobjectid": str(len(all_objs) + 1),
    })

    # Base tilesets (terrain, river, props)
    ET.SubElement(m, "tileset", {
        "firstgid": str(TERRAIN_FIRSTGID),
        "source": tsx_source_rel(pack, TERRAIN_NAME, tmx_dir),
    })
    ET.SubElement(m, "tileset", {
        "firstgid": str(RIVER_FIRSTGID),
        "source": tsx_source_rel(pack, RIVER_NAME, tmx_dir),
    })
    ET.SubElement(m, "tileset", {
        "firstgid": str(PROPS_FIRSTGID),
        "source": tsx_source_rel(pack, PROPS_NAME, tmx_dir),
    })
    # Animated tilesets
    for ref in anim_refs:
        ET.SubElement(m, "tileset", {
            "firstgid": str(ref["firstgid"]),
            "source": ref["source"],
        })

    def add_tile_layer(name: str, lid: int, grid: list[list[int]]):
        layer = ET.SubElement(m, "layer", {
            "id": str(lid), "name": name,
            "width": str(width), "height": str(height),
        })
        data = ET.SubElement(layer, "data", {"encoding": "csv"})
        rows = []
        for y, row in enumerate(grid):
            line = ",".join(str(v) for v in row)
            if y < len(grid) - 1:
                line += ","
            rows.append(line)
        data.text = "\n" + "\n".join(rows) + "\n"

    add_tile_layer("terrain", 1, terrain)
    add_tile_layer("water", 2, water)

    # objectgroups: forest (trees+bushes), flora (flowers/rocks),
    # animations (critters)
    def add_objgroup(name: str, og_id: int, objs: list[dict],
                     id_start: int) -> int:
        og = ET.SubElement(m, "objectgroup", {
            "id": str(og_id), "name": name,
        })
        oid = id_start
        for o in objs:
            ET.SubElement(og, "object", {
                "id": str(oid),
                "name": o["name"],
                "gid": str(o["gid"]),
                "x": str(o["x_px"]),
                "y": str(o["y_px"]),
                "width": str(o["w"]),
                "height": str(o["h"]),
            })
            oid += 1
        return oid

    next_id = 1
    next_id = add_objgroup("forest", 3, forest_objs, next_id)
    next_id = add_objgroup("flora", 4, flora_objs, next_id)
    next_id = add_objgroup("animations", 5, anim_objs, next_id)

    tree = ET.ElementTree(m)
    ET.indent(tree, space=" ")
    return tree


# ---------------------------------------------------------------------
# Main composition
# ---------------------------------------------------------------------

def _compose_world(w: int, h: int, rng: random.Random) -> World:
    world = World(w, h)

    # --- River (sinus, top-to-bottom, near center but offset) ---
    river_cx = int(w * 0.55)
    world.paint_river_sinus(
        center_x=river_cx,
        amp=max(2, w // 20),
        period=max(12, h // 3),
        half_width=2,
    )

    # --- Pond (small circle off to one side, NW area) ---
    world.paint_pond(cx=int(w * 0.18), cy=int(h * 0.28), radius=3)
    world.paint_pond(cx=int(w * 0.13), cy=int(h * 0.72), radius=2)

    # --- Dirt patches (3 patches + connecting path) ---
    patches = [
        (int(w * 0.06), int(h * 0.08), 14, 8),   # NW
        (int(w * 0.12), int(h * 0.46), 10, 6),   # mid-left
        (int(w * 0.32), int(h * 0.60), 8, 6),    # center-south, near river
    ]
    for (x, y, pw, ph) in patches:
        world.paint_rect_dirt(x, y, pw, ph, round_corners=True)

    # path connecting first two patches, then a spur
    world.paint_path_dirt(
        [(int(w * 0.12), int(h * 0.14)),
         (int(w * 0.10), int(h * 0.30)),
         (int(w * 0.16), int(h * 0.46))],
        thickness=2,
    )
    return world


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    ap.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    ap.add_argument("--pack", type=str, default=DEFAULT_PACK)
    ap.add_argument("--out", type=Path,
                    default=_REPO_ROOT / "output" / "rich.tmx")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    pack = args.pack
    tmx_dir = args.out.parent
    tmx_dir.mkdir(parents=True, exist_ok=True)

    world = _compose_world(args.width, args.height, rng)
    pool = TilePool(rng, pack)

    stats: dict[str, int] = {
        "grass": 0, "dirt_interior": 0, "dirt_boundary": 0,
        "wang_missing_dirt": 0,
        "water_interior": 0, "water_boundary": 0, "wang_missing_water": 0,
        "trees": 0, "bushes": 0,
    }
    terrain = build_terrain_layer(pool, world, stats)
    water = build_water_layer(pool, world, stats)

    occupied: set[tuple[int, int]] = set()

    # --- Forest clusters (3 farklı tip) ---
    forest_objs: list[dict] = []
    forest_objs += place_forest_cluster(
        pool, world, rect=(int(args.width * 0.72), int(args.height * 0.06),
                           args.width - 2, int(args.height * 0.28)),
        tree_filter_includes=["tree - color scheme"],
        tree_filter_excludes=None,
        density=0.28, tree_prob=0.55,
        occupied=occupied, stats=stats,
    )
    forest_objs += place_forest_cluster(
        pool, world, rect=(int(args.width * 0.70), int(args.height * 0.62),
                           args.width - 2, args.height - 3),
        tree_filter_includes=["palm tree"],
        tree_filter_excludes=None,
        density=0.20, tree_prob=0.65,
        occupied=occupied, stats=stats,
    )
    forest_objs += place_forest_cluster(
        pool, world, rect=(3, int(args.height * 0.72),
                           int(args.width * 0.30), args.height - 3),
        tree_filter_includes=["tree - naked"],
        tree_filter_excludes=None,
        density=0.25, tree_prob=0.5,
        occupied=occupied, stats=stats,
    )

    # --- Scattered flora (flowers + rocks on grass) ---
    flora_objs = scatter_flora(
        pool, world, density=0.015, occupied=occupied, stats=stats,
    )

    # --- Animated critters ---
    anim_refs, anim_objs = place_animated_critters(
        pack, world, tmx_dir, occupied, rng, stats,
    )

    tmx = build_tmx(
        args.width, args.height,
        terrain, water,
        forest_objs, flora_objs,
        anim_refs, anim_objs,
        pack, tmx_dir,
    )
    tmx.write(args.out, encoding="UTF-8", xml_declaration=True)
    print(f"Yazıldı: {args.out}")
    print(f"Boyut: {args.width}x{args.height}  seed={args.seed}")
    print("İstatistikler:")
    for k, v in sorted(stats.items()):
        print(f"  {k:25s} : {v}")
    print(f"Anim TSX: {len(anim_refs)}  placed objects: {len(anim_objs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
