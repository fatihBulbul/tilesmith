"""
Generator v3 - Çoklu katman, çoklu tileset, DB-driven harita.

Senaryo: 40x40 harita
  - Arka plan (terrain): çim + sol yarıda toprak yaması
  - Su (water): ortadan kıvrılarak geçen dikey nehir (wang)
  - Nesneler (objects): sağ yarıda orman (ağaç + çalı)

Üç tileset kullanır:
  1. Tileset-Terrain-new grass          (gid 1..)
  2. platform - water to grass - river  (gid ..)
  3. Atlas-Props-sheet1-sprites         (gid ..) - collection tileset

Nehir yolu basit bir sinüs dalgalı dikey hat. Corner grid üzerinden
hangi köşelerin 'su' (c1) olduğunu işaretliyoruz; dışı 0 (grass).
"""

from __future__ import annotations
import argparse
import json
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

# --- Harita parametreleri ---------------------------------------------

MAP_WIDTH = 40
MAP_HEIGHT = 40
TILE_SIZE = 32

# Toprak yaması (sol yarıda)
DIRT_PATCH_LEFT = 5
DIRT_PATCH_RIGHT = 11
DIRT_PATCH_TOP = 22
DIRT_PATCH_BOTTOM = 28

# Nehir (dikey, haritayı boylamasına geçer, sinüs dalgası)
RIVER_CENTER_X = 22      # başlangıç merkez hücresi
RIVER_WAVE_AMP = 3       # kaç hücre sallansın
RIVER_WAVE_PERIOD = 18   # ne sıklıkla salına
RIVER_HALF_WIDTH = 2     # 2*2+1 = 5 hücre genişliğinde nehir

# Orman alanı (nehrin sağ tarafında)
FOREST_LEFT = 28
FOREST_RIGHT = 38
FOREST_TOP = 2
FOREST_BOTTOM = 37
FOREST_DENSITY = 0.18    # her hücreye bir obje denemesi olasılığı
MIN_FOREST_GAP_TILES = 2

# --- Zone toggle'ları (v0.8.1 plan-driven) ---------------------------
# Plan'da bir zone yoksa ilgili katman komple atlanır (boş kalır).
DIRT_ENABLED = True
RIVER_ENABLED = True
FOREST_ENABLED = True

# --- Pack referansı --------------------------------------------------
# Varsayılan preset 'grass_river_forest' için ERW Grass Land 2.0 kullanılır.
# Farklı bir pack ile çalıştırmak için env var veya --pack CLI arg kullan.

DEFAULT_PACK = "ERW - Grass Land 2.0 v1.9"

# --- Tileset referansları --------------------------------------------
# TSX path'leri TMX çıktısının bulunduğu klasöre göre relative yazılır.
# Generate sırasında --tmx-dir-to-pack override edilebilir.

TERRAIN_NAME = "Tileset-Terrain-new grass"
RIVER_NAME = "platform - water to grass - river orientation"
PROPS_NAME = "Atlas-Props-sheet1-sprites"

TERRAIN_FIRSTGID = 1               # 3960 tile -> 1..3960
RIVER_FIRSTGID = 1 + 3960          # 3961
PROPS_FIRSTGID = 1 + 3960 + 870    # 4831

# --- Wang sabitleri (pack-prefixed UID'ler) --------------------------
# UID formatı: "<pack>::<tileset>::<wangset_name>"

def _wangset_uid(pack: str, tileset: str, ws_name: str) -> str:
    return f"{pack}::{tileset}::{ws_name}"

DIRT_WANGSET_NAME = "dirt"
DIRT_COLOR = 2           # dirt1 to main grass
RIVER_WANGSET_NAME = "water to grass (river orientation)"
RIVER_COLOR = 1
GRASS_COLOR = 0          # "boş" corner

BASIC_GRASS_IDS = [130, 131, 132, 185, 186, 187]
GRASS_WEIGHTS = [3, 3, 3, 2, 2, 2]

# DB yolu: önce TILESMITH_DB_PATH / ERW_DB_PATH env, yoksa repo-içi default.
_REPO_ROOT = HERE.parent  # tilesmith/
DB_PATH = Path(
    os.environ.get("TILESMITH_DB_PATH")
    or os.environ.get("ERW_DB_PATH")
    or str(_REPO_ROOT / "data" / "tiles.db")
)


# --- Prop DB queries (yerel, query.py'de yok) ------------------------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def get_props_by_category(category: str, pack_name: str,
                          tileset: str = PROPS_NAME,
                          variant: str | None = "composite") -> list[dict]:
    """Verilen pack içinden, tileset bazlı prop'ları getir (merged view).

    Default olarak yalnızca `variant='composite'` satırları döner. Böylece
    haritaya "sadece gövde" / "sadece taç" / "sadece taban" gibi parça
    varyantları rastgele atılmaz. `variant=None` verilirse filtre
    uygulanmaz.
    """
    clauses = ["category = ?", "tileset = ?", "pack_name = ?"]
    params: list = [category, tileset, pack_name]
    if variant is not None:
        clauses.append("variant = ?")
        params.append(variant)
    sql = (
        "SELECT prop_uid, pack_name, tileset, local_id, "
        "       image_path, category, variant "
        "FROM props WHERE " + " AND ".join(clauses) +
        " ORDER BY local_id"
    )
    with db() as conn:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# --- Dünya niyeti (corner grids) -------------------------------------

def build_dirt_corner_grid() -> list[list[int]]:
    corners = [[GRASS_COLOR] * (MAP_WIDTH + 1) for _ in range(MAP_HEIGHT + 1)]
    for y in range(DIRT_PATCH_TOP, DIRT_PATCH_BOTTOM + 2):
        for x in range(DIRT_PATCH_LEFT, DIRT_PATCH_RIGHT + 2):
            corners[y][x] = DIRT_COLOR
    # köşeleri yuvarla
    for cx, cy in [
        (DIRT_PATCH_LEFT, DIRT_PATCH_TOP),
        (DIRT_PATCH_RIGHT + 1, DIRT_PATCH_TOP),
        (DIRT_PATCH_LEFT, DIRT_PATCH_BOTTOM + 1),
        (DIRT_PATCH_RIGHT + 1, DIRT_PATCH_BOTTOM + 1),
    ]:
        corners[cy][cx] = GRASS_COLOR
    return corners


def river_center_x_at(y: int) -> float:
    """y hücresindeki nehrin merkez X'i (sinüs dalgası)."""
    return RIVER_CENTER_X + RIVER_WAVE_AMP * math.sin(
        2 * math.pi * y / RIVER_WAVE_PERIOD
    )


def build_river_corner_grid() -> list[list[int]]:
    """Nehrin bulunduğu corner'ları c1 (water), dışı 0 (grass) yap."""
    corners = [[GRASS_COLOR] * (MAP_WIDTH + 1) for _ in range(MAP_HEIGHT + 1)]
    for cy in range(MAP_HEIGHT + 1):
        cx_f = river_center_x_at(cy - 0.5)  # corner'lar hücre aralarında
        cx_int = int(round(cx_f))
        for dx in range(-RIVER_HALF_WIDTH, RIVER_HALF_WIDTH + 1):
            x = cx_int + dx
            if 0 <= x <= MAP_WIDTH:
                corners[cy][x] = RIVER_COLOR
    return corners


# --- Tile pool (DB cached) -------------------------------------------

class TilePool:
    def __init__(self, rng: random.Random, pack_name: str):
        self.rng = rng
        self.pack_name = pack_name
        self.dirt_wangset_uid = _wangset_uid(
            pack_name, TERRAIN_NAME, DIRT_WANGSET_NAME)
        self.river_wangset_uid = _wangset_uid(
            pack_name, RIVER_NAME, RIVER_WANGSET_NAME)
        self._wang_cache: dict[tuple[str, int, int, int, int], list[dict]] = {}

        # Basic grass (pack-scoped)
        self.basic_grass = pick_basic_grass_fillers(
            tileset=TERRAIN_NAME, local_ids=BASIC_GRASS_IDS,
            pack_name=pack_name,
        )
        self._grass_by_id = {g["local_id"]: g for g in self.basic_grass}

        # Pure dirt interior (c2)
        self.pure_dirt = find_pure_wang_tiles(self.dirt_wangset_uid, DIRT_COLOR)
        # Pure water interior (river wang c1)
        self.pure_water = find_pure_wang_tiles(
            self.river_wangset_uid, RIVER_COLOR)

        if not self.basic_grass:
            raise RuntimeError(
                f"grass fillers yok (pack={pack_name!r}, "
                f"tileset={TERRAIN_NAME!r})")
        if not self.pure_dirt:
            raise RuntimeError(
                f"pure dirt yok (wangset_uid={self.dirt_wangset_uid!r})")
        if not self.pure_water:
            raise RuntimeError(
                f"pure water yok (wangset_uid={self.river_wangset_uid!r})")

    def pick_grass(self) -> dict:
        lid = self.rng.choices(BASIC_GRASS_IDS, weights=GRASS_WEIGHTS, k=1)[0]
        return self._grass_by_id[lid]

    def pick_dirt_interior(self) -> dict:
        return self.rng.choice(self.pure_dirt)

    def pick_water_interior(self) -> dict:
        return self.rng.choice(self.pure_water)

    def pick_wang_tile(self, wangset_uid: str, nw: int, ne: int,
                       sw: int, se: int) -> dict | None:
        key = (wangset_uid, nw, ne, sw, se)
        if key not in self._wang_cache:
            self._wang_cache[key] = find_wang_tiles_by_corners(
                wangset_uid, nw=nw, ne=ne, sw=sw, se=se
            )
        tiles = self._wang_cache[key]
        if not tiles:
            return None
        return self.rng.choice(tiles)


# --- Katman üretimi --------------------------------------------------

def build_terrain_layer(pool: TilePool,
                        dirt_corners: list[list[int]],
                        stats: dict) -> list[list[int]]:
    """Terrain (grass + dirt yaması) katmanı. Local tile ID'ler + TERRAIN_FIRSTGID."""
    grid = [[0] * MAP_WIDTH for _ in range(MAP_HEIGHT)]
    for y in range(MAP_HEIGHT):
        for x in range(MAP_WIDTH):
            nw = dirt_corners[y][x]
            ne = dirt_corners[y][x + 1]
            sw = dirt_corners[y + 1][x]
            se = dirt_corners[y + 1][x + 1]
            c = (nw, ne, sw, se)
            if c == (GRASS_COLOR,) * 4:
                tile = pool.pick_grass()
                stats["grass"] += 1
            elif c == (DIRT_COLOR,) * 4:
                tile = pool.pick_dirt_interior()
                stats["dirt_interior"] += 1
            else:
                tile = pool.pick_wang_tile(
                    pool.dirt_wangset_uid, nw, ne, sw, se)
                if tile is None:
                    tile = pool.pick_grass()
                    stats["wang_missing_dirt"] += 1
                else:
                    stats["dirt_boundary"] += 1
            grid[y][x] = TERRAIN_FIRSTGID + tile["local_id"]
    return grid


def build_river_layer(pool: TilePool,
                      river_corners: list[list[int]],
                      stats: dict) -> list[list[int]]:
    """Su katmanı. Su OLMAYAN hücreler 0 (şeffaf). Local ID'ler + RIVER_FIRSTGID."""
    grid = [[0] * MAP_WIDTH for _ in range(MAP_HEIGHT)]
    for y in range(MAP_HEIGHT):
        for x in range(MAP_WIDTH):
            nw = river_corners[y][x]
            ne = river_corners[y][x + 1]
            sw = river_corners[y + 1][x]
            se = river_corners[y + 1][x + 1]
            if (nw, ne, sw, se) == (GRASS_COLOR,) * 4:
                continue  # su yok
            if (nw, ne, sw, se) == (RIVER_COLOR,) * 4:
                tile = pool.pick_water_interior()
                stats["water_interior"] += 1
            else:
                tile = pool.pick_wang_tile(
                    pool.river_wangset_uid, nw, ne, sw, se)
                if tile is None:
                    stats["wang_missing_water"] += 1
                    continue
                stats["water_boundary"] += 1
            grid[y][x] = RIVER_FIRSTGID + tile["local_id"]
    return grid


# --- Orman (object layer) -------------------------------------------

def place_forest_objects(pool: TilePool,
                         river_corners: list[list[int]],
                         stats: dict,
                         tmx_dir: Path) -> list[dict]:
    """Ağaç ve çalı objelerini üret. Her obje:
       { 'gid': int, 'x_px': int, 'y_px': int, 'w': int, 'h': int, 'name': str }

    Ağaçlar 192x192, çalılar 64x64. Tiled'da object y koordinatı objenin
    ALT kenarıdır; bu yüzden y_px = (row+1)*TILE_SIZE.
    """
    trees = get_props_by_category("tree", pool.pack_name)
    bushes = get_props_by_category("bush", pool.pack_name)
    if not trees:
        raise RuntimeError(
            f"ağaç yok (pack={pool.pack_name!r}, tileset={PROPS_NAME!r})")
    if not bushes:
        raise RuntimeError(
            f"çalı yok (pack={pool.pack_name!r}, tileset={PROPS_NAME!r})")

    # image_path artık DB'de absolute. PIL ile boyutları oku.
    from PIL import Image as PILImage

    def resolve_prop(p: dict) -> tuple[Path, int, int]:
        img_path = Path(p["image_path"])
        with PILImage.open(img_path) as im:
            w, h = im.size
        return img_path, w, h

    # Boyut cache'i
    tree_info = [(t, *resolve_prop(t)[1:]) for t in trees]
    bush_info = [(b, *resolve_prop(b)[1:]) for b in bushes]

    # Orman alanında grid tarayarak yerleştir
    rng = pool.rng
    occupied: set[tuple[int, int]] = set()

    def area_free(x0: int, y0: int, w_t: int, h_t: int) -> bool:
        for yy in range(y0, y0 + h_t):
            for xx in range(x0, x0 + w_t):
                if not (0 <= xx < MAP_WIDTH and 0 <= yy < MAP_HEIGHT):
                    return False
                if (xx, yy) in occupied:
                    return False
                # Nehir içinde olmasın
                for (cx, cy) in [(xx, yy), (xx + 1, yy), (xx, yy + 1), (xx + 1, yy + 1)]:
                    if river_corners[cy][cx] == RIVER_COLOR:
                        return False
        return True

    def mark(x0: int, y0: int, w_t: int, h_t: int):
        for yy in range(y0, y0 + h_t):
            for xx in range(x0, x0 + w_t):
                occupied.add((xx, yy))
        # Ekstra gap
        for yy in range(y0 - MIN_FOREST_GAP_TILES, y0 + h_t + MIN_FOREST_GAP_TILES):
            for xx in range(x0 - MIN_FOREST_GAP_TILES, x0 + w_t + MIN_FOREST_GAP_TILES):
                occupied.add((xx, yy))

    objs: list[dict] = []

    # Önce ağaçları dene (büyükler önce)
    for y in range(FOREST_TOP, FOREST_BOTTOM + 1):
        for x in range(FOREST_LEFT, FOREST_RIGHT + 1):
            if rng.random() > FOREST_DENSITY:
                continue
            # %40 ağaç, %60 çalı şansı
            if rng.random() < 0.4:
                p, pw, ph = rng.choice(tree_info)
            else:
                p, pw, ph = rng.choice(bush_info)
            w_t = pw // TILE_SIZE
            h_t = ph // TILE_SIZE
            if not area_free(x, y, w_t, h_t):
                continue
            mark(x, y, w_t, h_t)
            gid = PROPS_FIRSTGID + p["local_id"]
            # Tiled object y = bottom pixel. Objenin üst-sol (px)?= (x*TS, y*TS)
            # Tiled'da tile object için y = objenin ALT kenarı.
            x_px = x * TILE_SIZE
            y_px = (y + h_t) * TILE_SIZE
            objs.append({
                "gid": gid,
                "x_px": x_px,
                "y_px": y_px,
                "w": pw,
                "h": ph,
                "name": Path(p["image_path"]).stem,
            })
            if "tree" in p["category"]:
                stats["trees"] += 1
            else:
                stats["bushes"] += 1

    return objs


# --- TMX -------------------------------------------------------------

def _tsx_source_for(pack_name: str, tileset: str, tmx_dir: Path) -> str:
    """DB'den tileset'in absolute source_path'ini al ve TMX output dir'ine
    göre relative path olarak döndür."""
    with db() as conn:
        row = conn.execute(
            "SELECT source_path FROM tilesets "
            "WHERE pack_name = ? AND name = ?",
            (pack_name, tileset),
        ).fetchone()
    if row is None or not row["source_path"]:
        raise RuntimeError(
            f"TSX bulunamadı: pack={pack_name!r} tileset={tileset!r}")
    tsx_abs = Path(row["source_path"]).resolve()
    return os.path.relpath(tsx_abs, tmx_dir.resolve())


def build_tmx(terrain: list[list[int]],
              river: list[list[int]],
              objs: list[dict],
              pack_name: str,
              tmx_dir: Path) -> ET.ElementTree:
    m = ET.Element("map", {
        "version": "1.9", "tiledversion": "1.9.2",
        "orientation": "orthogonal", "renderorder": "right-down",
        "width": str(MAP_WIDTH), "height": str(MAP_HEIGHT),
        "tilewidth": str(TILE_SIZE), "tileheight": str(TILE_SIZE),
        "infinite": "0",
        "nextlayerid": "4", "nextobjectid": str(len(objs) + 1),
    })

    # Tilesets — DB'den sorgulanan absolute path'ler TMX output dir'ine
    # göre relative'e çevrilir.
    terrain_src = _tsx_source_for(pack_name, TERRAIN_NAME, tmx_dir)
    river_src = _tsx_source_for(pack_name, RIVER_NAME, tmx_dir)
    props_src = _tsx_source_for(pack_name, PROPS_NAME, tmx_dir)

    ET.SubElement(m, "tileset", {
        "firstgid": str(TERRAIN_FIRSTGID), "source": terrain_src
    })
    ET.SubElement(m, "tileset", {
        "firstgid": str(RIVER_FIRSTGID), "source": river_src
    })
    ET.SubElement(m, "tileset", {
        "firstgid": str(PROPS_FIRSTGID), "source": props_src
    })

    def add_tile_layer(name: str, layer_id: int, grid: list[list[int]]):
        layer = ET.SubElement(m, "layer", {
            "id": str(layer_id), "name": name,
            "width": str(MAP_WIDTH), "height": str(MAP_HEIGHT),
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
    add_tile_layer("river", 2, river)

    # Object layer
    og = ET.SubElement(m, "objectgroup", {"id": "3", "name": "forest"})
    for i, o in enumerate(objs, start=1):
        ET.SubElement(og, "object", {
            "id": str(i),
            "name": o["name"],
            "gid": str(o["gid"]),
            "x": str(o["x_px"]),
            "y": str(o["y_px"]),
            "width": str(o["w"]),
            "height": str(o["h"]),
        })

    tree = ET.ElementTree(m)
    ET.indent(tree, space=" ")
    return tree


# --- Plan uygulama (v0.8.1) -----------------------------------------

def _apply_plan(plan: dict) -> dict:
    """Plan dict'ini module global'lerine yansıt.

    plan = {
      "width": int, "height": int,
      "zones": [
        {"type": "dirt",   "left": int, "right": int,
                           "top": int, "bottom": int},
        {"type": "river",  "center_x": int, "half_width": int,
                           "wave_amp": int, "wave_period": int},
        {"type": "forest", "left": int, "right": int,
                           "top": int, "bottom": int,
                           "density": float},
      ]
    }

    Plan'da olmayan zone tipleri devre dışı bırakılır (DIRT/RIVER/FOREST
    _ENABLED bayrakları). width/height verilmişse MAP_WIDTH/HEIGHT
    overwrite edilir. Döndürülen dict, hangi zone'ların aktif olduğunu +
    efektif width/height'ı içerir (debug için).
    """
    global MAP_WIDTH, MAP_HEIGHT
    global DIRT_PATCH_LEFT, DIRT_PATCH_RIGHT, DIRT_PATCH_TOP, DIRT_PATCH_BOTTOM
    global RIVER_CENTER_X, RIVER_WAVE_AMP, RIVER_WAVE_PERIOD, RIVER_HALF_WIDTH
    global FOREST_LEFT, FOREST_RIGHT, FOREST_TOP, FOREST_BOTTOM, FOREST_DENSITY
    global DIRT_ENABLED, RIVER_ENABLED, FOREST_ENABLED

    if "width" in plan:
        MAP_WIDTH = int(plan["width"])
    if "height" in plan:
        MAP_HEIGHT = int(plan["height"])

    zones = plan.get("zones") or []
    zone_types = {z.get("type") for z in zones}

    DIRT_ENABLED = "dirt" in zone_types
    RIVER_ENABLED = "river" in zone_types
    FOREST_ENABLED = "forest" in zone_types

    for z in zones:
        t = z.get("type")
        if t == "dirt":
            DIRT_PATCH_LEFT = int(z["left"])
            DIRT_PATCH_RIGHT = int(z["right"])
            DIRT_PATCH_TOP = int(z["top"])
            DIRT_PATCH_BOTTOM = int(z["bottom"])
        elif t == "river":
            RIVER_CENTER_X = int(z["center_x"])
            RIVER_HALF_WIDTH = int(z.get("half_width", RIVER_HALF_WIDTH))
            RIVER_WAVE_AMP = int(z.get("wave_amp", RIVER_WAVE_AMP))
            RIVER_WAVE_PERIOD = int(z.get("wave_period", RIVER_WAVE_PERIOD))
        elif t == "forest":
            FOREST_LEFT = int(z["left"])
            FOREST_RIGHT = int(z["right"])
            FOREST_TOP = int(z["top"])
            FOREST_BOTTOM = int(z["bottom"])
            if "density" in z:
                FOREST_DENSITY = float(z["density"])

    return {
        "width": MAP_WIDTH, "height": MAP_HEIGHT,
        "dirt_enabled": DIRT_ENABLED,
        "river_enabled": RIVER_ENABLED,
        "forest_enabled": FOREST_ENABLED,
    }


def _empty_grid(w: int, h: int) -> list[list[int]]:
    return [[0] * w for _ in range(h)]


# --- CLI ------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--out", type=Path,
                    default=_REPO_ROOT / "output" / "demo-river-forest.tmx")
    ap.add_argument("--pack", type=str, default=DEFAULT_PACK,
                    help="Kullanılacak pack adı (DB'deki pack_name)")
    ap.add_argument("--plan", type=Path, default=None,
                    help="Opsiyonel plan JSON dosyası (tool_plan_map "
                         "çıktısıyla uyumlu). Verilirse width/height + "
                         "zone bounds bu plandan alınır.")
    args = ap.parse_args()

    # v0.8.1: plan varsa module globals'ı override et.
    plan_summary: dict | None = None
    if args.plan is not None:
        try:
            plan_data = json.loads(args.plan.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"ERROR: plan okunamadı ({args.plan}): {e}",
                  file=sys.stderr)
            return 2
        plan_summary = _apply_plan(plan_data)
        print(f"Plan uygulandı: {plan_summary}")

    rng = random.Random(args.seed)
    pool = TilePool(rng, pack_name=args.pack)

    stats = {
        "grass": 0, "dirt_interior": 0, "dirt_boundary": 0,
        "wang_missing_dirt": 0,
        "water_interior": 0, "water_boundary": 0, "wang_missing_water": 0,
        "trees": 0, "bushes": 0,
    }

    # Zone toggle'larına göre her katman ya normal ya boş üretilir.
    if DIRT_ENABLED:
        dirt_corners = build_dirt_corner_grid()
        terrain = build_terrain_layer(pool, dirt_corners, stats)
    else:
        # Saf çimenlik — her hücre random grass tile.
        terrain = _empty_grid(MAP_WIDTH, MAP_HEIGHT)
        for y in range(MAP_HEIGHT):
            for x in range(MAP_WIDTH):
                t = pool.pick_grass()
                terrain[y][x] = TERRAIN_FIRSTGID + t["local_id"]
                stats["grass"] += 1

    if RIVER_ENABLED:
        river_corners = build_river_corner_grid()
        river = build_river_layer(pool, river_corners, stats)
    else:
        # River katmanı tamamen şeffaf. Orman collision için boş corner grid.
        river_corners = [[GRASS_COLOR] * (MAP_WIDTH + 1)
                         for _ in range(MAP_HEIGHT + 1)]
        river = _empty_grid(MAP_WIDTH, MAP_HEIGHT)

    args.out.parent.mkdir(parents=True, exist_ok=True)

    if FOREST_ENABLED:
        objs = place_forest_objects(pool, river_corners, stats,
                                    tmx_dir=args.out.parent)
    else:
        objs = []

    tree = build_tmx(terrain, river, objs,
                     pack_name=args.pack, tmx_dir=args.out.parent)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tree.write(args.out, encoding="UTF-8", xml_declaration=True)

    print(f"Yazıldı: {args.out}")
    print(f"Boyut: {MAP_WIDTH}x{MAP_HEIGHT} seed={args.seed}")
    print("İstatistikler:")
    for k, v in stats.items():
        print(f"  {k:22}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
