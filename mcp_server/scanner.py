"""
Generic folder scanner for Tiled-compatible tileset packs.

Walks a folder recursively, identifies TSX/TMX/PNG assets, and indexes
them into the shared tiles.db schema. Works on any folder layout —
autodetects tileset (TSX), automapping rules (TMX in rule-like paths),
reference maps (other TMX), animated props (PNG with "X frames WxH"
filename), and character sprites (PNG in Characters-like folders).

Pure-Python, no external deps beyond PIL.

Schema overview (v0.4.0)
------------------------
Every asset kind has three layers:
  * ``<kind>_auto``        — scanner-controlled, rebuilt per pack on every scan.
  * ``<kind>_overrides``   — user-controlled, NEVER touched by the scanner.
  * ``<kind>`` (VIEW)      — merged view used by readers/queries; override
                             columns win over auto columns via COALESCE.

Every row is scoped to a ``pack_name`` (the top-level folder name the scanner
was invoked on) so packs with colliding tileset names (e.g. both shipping a
``Tileset-Terrain.tsx``) don't overwrite each other.
"""

from __future__ import annotations
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path
from PIL import Image

# ------------------------------------------------------------------
# Table lists (used to generate DDL + views + cleanup)
# ------------------------------------------------------------------

# Asset kinds that have _auto + _overrides + merged view.
# Each entry: (kind_name, columns_ddl, primary_key, pack_filter_column)
ASSET_TABLES: list[tuple[str, str, str, str]] = [
    (
        "tilesets",
        """pack_name TEXT NOT NULL, tileset_uid TEXT NOT NULL,
           name TEXT, source_path TEXT, image_path TEXT,
           tile_count INTEGER, columns INTEGER,
           tile_width INTEGER, tile_height INTEGER,
           is_collection INTEGER DEFAULT 0,
           indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP""",
        "tileset_uid",
        "pack_name",
    ),
    (
        "tiles",
        """pack_name TEXT NOT NULL, tile_uid TEXT NOT NULL,
           tileset_uid TEXT, tileset TEXT, local_id INTEGER,
           semantic TEXT, biome TEXT, role TEXT, walkable INTEGER,
           variant_group TEXT, probability REAL,
           atlas_row INTEGER, atlas_col INTEGER,
           image_path TEXT,
           label_source TEXT, confidence REAL DEFAULT 1.0""",
        "tile_uid",
        "pack_name",
    ),
    (
        "wang_sets",
        """pack_name TEXT NOT NULL, wangset_uid TEXT NOT NULL,
           tileset_uid TEXT, tileset TEXT, name TEXT,
           type TEXT, color_count INTEGER, tile_count INTEGER""",
        "wangset_uid",
        "pack_name",
    ),
    (
        "wang_colors",
        """pack_name TEXT NOT NULL, wangset_uid TEXT NOT NULL,
           color_index INTEGER NOT NULL,
           name TEXT, color_hex TEXT""",
        "wangset_uid, color_index",
        "pack_name",
    ),
    (
        "wang_tiles",
        """pack_name TEXT NOT NULL, tile_uid TEXT NOT NULL,
           wangset_uid TEXT NOT NULL,
           c_n INTEGER DEFAULT 0, c_ne INTEGER DEFAULT 0,
           c_e INTEGER DEFAULT 0, c_se INTEGER DEFAULT 0,
           c_s INTEGER DEFAULT 0, c_sw INTEGER DEFAULT 0,
           c_w INTEGER DEFAULT 0, c_nw INTEGER DEFAULT 0""",
        "tile_uid, wangset_uid",
        "pack_name",
    ),
    (
        "props",
        """pack_name TEXT NOT NULL, prop_uid TEXT NOT NULL,
           tileset_uid TEXT, tileset TEXT, local_id INTEGER,
           image_path TEXT, category TEXT, variant TEXT,
           biome_tags TEXT,
           size_w INTEGER, size_h INTEGER, label_source TEXT""",
        "prop_uid",
        "pack_name",
    ),
    (
        "animated_props",
        """pack_name TEXT NOT NULL, aprop_uid TEXT NOT NULL,
           image_path TEXT, filename TEXT,
           category TEXT, subject TEXT, action TEXT, variant TEXT,
           frame_count INTEGER, frame_w INTEGER, frame_h INTEGER,
           sheet_w INTEGER, sheet_h INTEGER,
           label_source TEXT DEFAULT 'filename'""",
        "aprop_uid",
        "pack_name",
    ),
    (
        "characters",
        """pack_name TEXT NOT NULL, char_uid TEXT NOT NULL,
           name TEXT, category TEXT,
           folder TEXT, variant_count INTEGER,
           label_source TEXT DEFAULT 'folder'""",
        "char_uid",
        "pack_name",
    ),
    (
        "character_animations",
        """pack_name TEXT NOT NULL, canim_uid TEXT NOT NULL,
           char_uid TEXT NOT NULL,
           state TEXT, variant TEXT, image_path TEXT,
           frame_count INTEGER, frame_w INTEGER, frame_h INTEGER,
           sheet_w INTEGER, sheet_h INTEGER""",
        "canim_uid",
        "pack_name",
    ),
    (
        "reference_maps",
        """pack_name TEXT NOT NULL, map_uid TEXT NOT NULL,
           source_path TEXT,
           width INTEGER, height INTEGER,
           tile_width INTEGER, tile_height INTEGER,
           tileset_count INTEGER, layer_count INTEGER""",
        "map_uid",
        "pack_name",
    ),
    (
        "reference_layers",
        """pack_name TEXT NOT NULL, map_uid TEXT NOT NULL,
           layer_order INTEGER NOT NULL,
           layer_id INTEGER, layer_type TEXT, layer_name TEXT,
           semantic_role TEXT""",
        "map_uid, layer_order",
        "pack_name",
    ),
    (
        "automapping_rule_sets",
        """pack_name TEXT NOT NULL, ruleset_uid TEXT NOT NULL,
           category TEXT, has_transparency INTEGER DEFAULT 0,
           rule_count INTEGER""",
        "ruleset_uid",
        "pack_name",
    ),
    (
        "automapping_rules",
        """pack_name TEXT NOT NULL, rule_uid TEXT NOT NULL,
           ruleset_uid TEXT NOT NULL, phase INTEGER NOT NULL,
           source_path TEXT, description TEXT""",
        "rule_uid",
        "pack_name",
    ),
]

CORNER_SLOTS = ["c_n", "c_ne", "c_e", "c_se", "c_s", "c_sw", "c_w", "c_nw"]

FRAMES_RE = re.compile(r"(\d+)\s*frames?", re.IGNORECASE)
SIZE_RE = re.compile(r"(\d+)\s*[xX]\s*(\d+)")
STATE_RE = re.compile(r"-(idle|walk|atk1|atk2|hurt|death|run|jump)(?:-|\.|$)",
                      re.IGNORECASE)

# Filename → prop category heuristic (ERW atlas props)
PROP_CATEGORY_KEYWORDS = {
    "tree": ["tree", "oak", "pine", "birch", "willow"],
    "bush": ["bush", "shrub"],
    "rock": ["rock", "boulder", "stone"],
    "flower": ["flower", "daisy"],
    "grass": ["grass patch", "tall grass"],
    "building": ["house", "cabin", "building", "barn"],
    "decoration": ["fence", "barrel", "crate", "log", "stump", "sign"],
}

# Filename → variant heuristic. Packs often ship multi-part assets where the
# artist provides both a full composite (e.g. "palm tree 1 - on grass.png")
# and individual parts ("- only trunk", "- only foliage", "- base only") for
# advanced composing. Only composites are safe to drop onto a map as-is;
# parts must not be selected randomly. Consumers should default to
# `variant='composite'` when placing props.
#
# Matches are probed against the lowercased filename in order; first hit
# wins. Anything unmatched falls through to 'composite'.
PROP_VARIANT_RULES: list[tuple[str, str]] = [
    (" - only trunk",              "trunk"),
    (" - only foliage",            "foliage"),   # incl. "- with coconuts"
    (" - base only",               "base"),
    (" - naked - shadow",          "shadow"),
    (" - shadow",                  "shadow"),
    # Generic catch-all for other part-like suffixes we haven't enumerated.
    (" - only ",                   "part"),
]


# ------------------------------------------------------------------
# DDL generation
# ------------------------------------------------------------------

def _ddl_for(kind: str, columns: str, pk: str) -> str:
    """Return CREATE TABLE IF NOT EXISTS DDL for _auto and _overrides + VIEW."""
    auto = (f"CREATE TABLE IF NOT EXISTS {kind}_auto (\n    {columns},\n"
            f"    PRIMARY KEY ({pk}));")
    # Overrides: same columns (minus server-populated defaults) with PK.
    # For simplicity we mirror the full schema, just without NOT NULL on
    # non-key cols so users can patch sparsely.
    override_cols = columns.replace("NOT NULL", "").replace(
        "DEFAULT CURRENT_TIMESTAMP", "").replace("DEFAULT 'filename'", "")\
        .replace("DEFAULT 'folder'", "").replace("DEFAULT 0", "")\
        .replace("DEFAULT 1.0", "")
    # Keep pk cols NOT NULL so the PK makes sense.
    # Simpler: just declare override with same DDL as auto.
    override = (f"CREATE TABLE IF NOT EXISTS {kind}_overrides (\n"
                f"    {columns},\n    PRIMARY KEY ({pk}));")
    # View: every auto column, with override taking precedence via COALESCE
    # (skipped — just UNION approach is simpler and correct for reads)
    # Use LEFT JOIN so every _auto row surfaces, plus override attributes.
    col_names = [c.strip().split()[0] for c in columns.split(",")
                 if c.strip() and not c.strip().startswith("PRIMARY")]
    # Dedup preserving order
    seen = set()
    col_names = [c for c in col_names if not (c in seen or seen.add(c))]
    pk_cols = [c.strip() for c in pk.split(",")]
    # For each column, if it's in PK use auto's value (they match by PK),
    # otherwise COALESCE(override.col, auto.col)
    select_parts = []
    for c in col_names:
        if c in pk_cols:
            select_parts.append(f"a.{c}")
        else:
            select_parts.append(f"COALESCE(o.{c}, a.{c}) AS {c}")
    join_cond = " AND ".join([f"a.{c} = o.{c}" for c in pk_cols])
    view = (f"DROP VIEW IF EXISTS {kind};\n"
            f"CREATE VIEW {kind} AS\n"
            f"  SELECT {', '.join(select_parts)}\n"
            f"  FROM {kind}_auto a\n"
            f"  LEFT JOIN {kind}_overrides o ON {join_cond};")
    return "\n".join([auto, override, view])


def _build_schema_ddl() -> str:
    parts = []
    for kind, cols, pk, _ in ASSET_TABLES:
        parts.append(_ddl_for(kind, cols, pk))
    # Helpful indexes
    parts.append(
        "CREATE INDEX IF NOT EXISTS idx_tiles_auto_tileset_uid "
        "ON tiles_auto(tileset_uid);")
    parts.append(
        "CREATE INDEX IF NOT EXISTS idx_wang_tiles_auto_wangset_uid "
        "ON wang_tiles_auto(wangset_uid);")
    parts.append(
        "CREATE INDEX IF NOT EXISTS idx_props_auto_tileset_uid "
        "ON props_auto(tileset_uid);")
    return "\n".join(parts)


SCHEMA_DDL = _build_schema_ddl()


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_DDL)


# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------

def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def guess_prop_category(filename: str) -> str:
    low = filename.lower()
    for cat, kws in PROP_CATEGORY_KEYWORDS.items():
        for k in kws:
            if k in low:
                return cat
    return "decoration"


def guess_prop_variant(filename: str) -> str:
    """Classify a prop filename into a placement variant.

    Returns one of: 'composite' (default; safe to place as-is),
    'trunk', 'foliage', 'base', 'shadow', 'part'.
    """
    low = filename.lower()
    for needle, variant in PROP_VARIANT_RULES:
        if needle in low:
            return variant
    return "composite"


def _resolve_asset_path(relative: str | None, anchor: Path) -> str | None:
    """Resolve a relative asset path (from TSX/TMX `source=`) to an absolute path.

    We don't require the file to exist — some packs ship tileset references
    that point outside the pack root (sibling packs). Return a string, or None.
    """
    if not relative:
        return None
    try:
        resolved = (anchor / relative).resolve()
    except Exception:
        return None
    return str(resolved)


def _wipe_pack(conn: sqlite3.Connection, pack_name: str) -> None:
    """Remove every _auto row owned by this pack so a rescan is idempotent.

    _overrides tables are NEVER touched — those are user data.
    """
    for kind, _cols, _pk, filter_col in ASSET_TABLES:
        conn.execute(f"DELETE FROM {kind}_auto WHERE {filter_col} = ?",
                     (pack_name,))


# ------------------------------------------------------------------
# TSX (tileset) parsing
# ------------------------------------------------------------------

def parse_tsx(tsx_path: Path, root: Path, pack_name: str,
              conn: sqlite3.Connection) -> dict:
    """Parse a TSX file and index into tilesets_auto / tiles_auto /
    wang_sets_auto / wang_tiles_auto / props_auto."""
    stats = {"tiles": 0, "wang_sets": 0, "wang_tiles": 0, "props": 0}
    tree = ET.parse(tsx_path)
    tsroot = tree.getroot()
    name = tsx_path.stem
    tileset_uid = f"{pack_name}::{name}"
    source_abs = str(tsx_path.resolve())
    tile_w = int(tsroot.get("tilewidth", 32))
    tile_h = int(tsroot.get("tileheight", 32))
    tile_count = int(tsroot.get("tilecount", 0))
    columns = int(tsroot.get("columns", 0))

    # Detect atlas vs collection
    img = tsroot.find("image")
    is_collection = 0 if img is not None else 1
    image_abs = _resolve_asset_path(img.get("source") if img is not None
                                    else None, tsx_path.parent)

    conn.execute(
        """INSERT OR REPLACE INTO tilesets_auto
           (pack_name, tileset_uid, name, source_path, image_path,
            tile_count, columns, tile_width, tile_height, is_collection)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (pack_name, tileset_uid, name, source_abs, image_abs,
         tile_count, columns, tile_w, tile_h, is_collection),
    )

    # Tile entries (both atlas + collection use <tile id="N">)
    for tile_el in tsroot.findall("tile"):
        tid = int(tile_el.get("id"))
        tile_uid = f"{tileset_uid}::{tid}"
        timg = tile_el.find("image")
        timg_rel = timg.get("source") if timg is not None else None
        timg_abs = _resolve_asset_path(timg_rel, tsx_path.parent)

        if is_collection and timg_rel:
            conn.execute(
                """INSERT OR REPLACE INTO tiles_auto
                   (pack_name, tile_uid, tileset_uid, tileset, local_id,
                    image_path, label_source, confidence)
                   VALUES (?,?,?,?,?,?, 'scanner', 1.0)""",
                (pack_name, tile_uid, tileset_uid, name, tid, timg_abs),
            )
            stats["tiles"] += 1
            # Collection tilesets are effectively prop catalogs.
            filename = Path(timg_rel).name
            category = guess_prop_category(filename)
            variant = guess_prop_variant(filename)
            try:
                w = int(timg.get("width")) if timg.get("width") else None
                h = int(timg.get("height")) if timg.get("height") else None
            except Exception:
                w = h = None
            conn.execute(
                """INSERT OR REPLACE INTO props_auto
                   (pack_name, prop_uid, tileset_uid, tileset, local_id,
                    image_path, category, variant,
                    size_w, size_h, label_source)
                   VALUES (?,?,?,?,?,?,?,?,?,?, 'filename')""",
                (pack_name, tile_uid, tileset_uid, name, tid,
                 timg_abs, category, variant, w, h),
            )
            stats["props"] += 1

    # For atlas tilesets, expand the tile range into tiles_auto.
    if not is_collection:
        for tid in range(tile_count):
            tile_uid = f"{tileset_uid}::{tid}"
            conn.execute(
                """INSERT OR REPLACE INTO tiles_auto
                   (pack_name, tile_uid, tileset_uid, tileset, local_id,
                    atlas_row, atlas_col, label_source, confidence)
                   VALUES (?,?,?,?,?,?,?, 'scanner', 1.0)""",
                (pack_name, tile_uid, tileset_uid, name, tid,
                 tid // max(columns, 1), tid % max(columns, 1)),
            )
            stats["tiles"] += 1

    # Wang sets
    wsets = tsroot.find("wangsets")
    if wsets is not None:
        for ws in wsets.findall("wangset"):
            ws_name = ws.get("name", "unnamed")
            ws_type = ws.get("type", "mixed")
            ws_uid = f"{tileset_uid}::{ws_name}"
            wcolors = ws.findall("wangcolor")
            wtiles = ws.findall("wangtile")
            conn.execute(
                """INSERT OR REPLACE INTO wang_sets_auto
                   (pack_name, wangset_uid, tileset_uid, tileset,
                    name, type, color_count, tile_count)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (pack_name, ws_uid, tileset_uid, name,
                 ws_name, ws_type, len(wcolors), len(wtiles)),
            )
            stats["wang_sets"] += 1
            for ci, wc in enumerate(wcolors, start=1):
                conn.execute(
                    """INSERT OR REPLACE INTO wang_colors_auto
                       (pack_name, wangset_uid, color_index, name, color_hex)
                       VALUES (?,?,?,?,?)""",
                    (pack_name, ws_uid, ci, wc.get("name"), wc.get("color")),
                )
            for wt in wtiles:
                tid = int(wt.get("tileid"))
                wid = wt.get("wangid", "0,0,0,0,0,0,0,0")
                slots = [int(x) for x in wid.split(",")]
                slots += [0] * (8 - len(slots))
                tile_uid = f"{tileset_uid}::{tid}"
                conn.execute(
                    f"""INSERT OR REPLACE INTO wang_tiles_auto
                        (pack_name, tile_uid, wangset_uid,
                         {", ".join(CORNER_SLOTS)})
                        VALUES (?,?,?, ?,?,?,?,?,?,?,?)""",
                    (pack_name, tile_uid, ws_uid, *slots),
                )
                stats["wang_tiles"] += 1
    return stats


# ------------------------------------------------------------------
# TMX parsing (reference maps + automapping rules)
# ------------------------------------------------------------------

def is_automapping_rule(tmx_path: Path) -> bool:
    """Heuristic: filename or any parent folder contains 'rule'."""
    if "rule" in tmx_path.name.lower():
        return True
    for p in tmx_path.parents:
        if "rule" in p.name.lower():
            return True
    return False


def parse_tmx_reference_map(tmx_path: Path, root: Path, pack_name: str,
                            conn: sqlite3.Connection) -> dict:
    stats = {"map": 1, "layers": 0}
    tree = ET.parse(tmx_path)
    mroot = tree.getroot()
    map_uid = f"{pack_name}::{slug(tmx_path.stem)}"
    w = int(mroot.get("width", 0))
    h = int(mroot.get("height", 0))
    tw = int(mroot.get("tilewidth", 32))
    th = int(mroot.get("tileheight", 32))
    tilesets_el = mroot.findall("tileset")
    layer_els = [el for el in mroot
                 if el.tag in ("layer", "objectgroup", "imagelayer", "group")]

    conn.execute(
        """INSERT OR REPLACE INTO reference_maps_auto
           (pack_name, map_uid, source_path, width, height,
            tile_width, tile_height, tileset_count, layer_count)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (pack_name, map_uid, str(tmx_path.resolve()), w, h, tw, th,
         len(tilesets_el), len(layer_els)),
    )

    conn.execute("DELETE FROM reference_layers_auto WHERE map_uid = ?",
                 (map_uid,))
    for i, el in enumerate(layer_els):
        layer_type = ("tile" if el.tag == "layer"
                      else "object" if el.tag == "objectgroup"
                      else el.tag)
        name = el.get("name", "")
        low = name.lower()
        if "grass" in low or "terrain" in low:
            role = "terrain_base"
        elif "water" in low or "river" in low:
            role = "water"
        elif "tree" in low or "object" in low or "prop" in low:
            role = "objects"
        elif "wall" in low:
            role = "wall"
        elif "hole" in low:
            role = "holes"
        else:
            role = None
        conn.execute(
            """INSERT INTO reference_layers_auto
               (pack_name, map_uid, layer_order, layer_id, layer_type,
                layer_name, semantic_role)
               VALUES (?,?,?,?,?,?,?)""",
            (pack_name, map_uid, i,
             int(el.get("id", i)) if el.get("id") else i,
             layer_type, name, role),
        )
        stats["layers"] += 1
    return stats


def parse_tmx_automapping(tmx_path: Path, root: Path, pack_name: str,
                          conn: sqlite3.Connection) -> dict:
    """Group rule TMX files by their ruleset (folder or basename prefix)."""
    stats = {"rules": 1}
    parent = tmx_path.parent.name
    name = tmx_path.stem
    m = re.match(r"(.+?)-rule(\d+)(?:-(.*))?$", name)
    if m:
        ruleset_local = slug(m.group(1))
        phase = int(m.group(2))
        desc = m.group(3) or ""
    else:
        ruleset_local = slug(parent or name)
        phase = 0
        desc = name
    ruleset_uid = f"{pack_name}::{ruleset_local}"
    low = ruleset_local.lower()
    category = ("hole" if "hole" in low
                else "wall" if "wall" in low else "misc")
    has_trans = 1 if "transp" in low else 0
    cur = conn.execute(
        "SELECT rule_count FROM automapping_rule_sets_auto "
        "WHERE ruleset_uid = ?",
        (ruleset_uid,))
    row = cur.fetchone()
    rc = (row[0] if row else 0) + 1
    conn.execute(
        """INSERT OR REPLACE INTO automapping_rule_sets_auto
           (pack_name, ruleset_uid, category, has_transparency, rule_count)
           VALUES (?,?,?,?,?)""",
        (pack_name, ruleset_uid, category, has_trans, rc),
    )
    rule_uid = f"{ruleset_uid}::rule{phase}:{slug(desc) or name}"
    conn.execute(
        """INSERT OR REPLACE INTO automapping_rules_auto
           (pack_name, rule_uid, ruleset_uid, phase, source_path, description)
           VALUES (?,?,?,?,?,?)""",
        (pack_name, rule_uid, ruleset_uid, phase,
         str(tmx_path.resolve()), desc),
    )
    return stats


# ------------------------------------------------------------------
# PNG classification (animated props, characters)
# ------------------------------------------------------------------

def is_animated_prop_filename(fn: str) -> bool:
    return bool(FRAMES_RE.search(fn))


def is_character_path(p: Path) -> bool:
    for parent in p.parents:
        if parent.name.lower() in {"characters", "character"}:
            return True
    return bool(STATE_RE.search(p.name.lower()))


def parse_animated_prop(png: Path, root: Path, pack_name: str,
                        conn: sqlite3.Connection) -> dict:
    stats = {"animated_props": 1}
    stem = png.stem
    m_frames = FRAMES_RE.search(stem)
    m_size = SIZE_RE.search(stem)
    frame_count = int(m_frames.group(1)) if m_frames else None
    try:
        with Image.open(png) as im:
            sw, sh = im.size
    except Exception:
        return {"animated_props": 0}
    if m_size:
        fw, fh = int(m_size.group(1)), int(m_size.group(2))
    elif frame_count and sw % frame_count == 0:
        fw, fh = sw // frame_count, sh
    else:
        fw, fh = sw, sh

    parent_low = png.parent.name.lower()
    cat_map = {
        "insects": "insect", "chests": "chest",
        "chimney": "smoke", "campfire": "fire",
        "cabin": "structure", "shrine": "shrine",
    }
    category = cat_map.get(parent_low, parent_low or "misc")
    parts = stem.split("-")
    subject = slug(parts[0]) if parts else stem
    action = None
    variant = None
    for tok in parts[1:]:
        tok_low = tok.strip().lower()
        if tok_low in {"flying", "opening", "closing", "going up",
                       "going down", "idle", "hit1", "hit2", "hit3"}:
            action = tok_low
        elif "no_grass" in tok_low.replace(" ", "_"):
            variant = "no_grass"

    aprop_uid = f"{pack_name}::{slug(str(png.relative_to(root)))}"
    conn.execute(
        """INSERT OR REPLACE INTO animated_props_auto
           (pack_name, aprop_uid, image_path, filename, category,
            subject, action, variant, frame_count, frame_w, frame_h,
            sheet_w, sheet_h, label_source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'filename')""",
        (pack_name, aprop_uid, str(png.resolve()), png.name,
         category, subject, action, variant,
         frame_count, fw, fh, sw, sh),
    )
    return stats


def parse_character(png: Path, root: Path, pack_name: str,
                    conn: sqlite3.Connection) -> dict:
    stats = {"chars": 0, "char_anims": 1}
    try:
        with Image.open(png) as im:
            sw, sh = im.size
    except Exception:
        return {"chars": 0, "char_anims": 0}
    char_folder = None
    for parent in png.parents:
        if parent == root:
            break
        if parent.parent.name.lower() in {"characters", "character"}:
            char_folder = parent
            break
    if char_folder:
        char_local = slug(char_folder.name)
        char_name = char_folder.name
        folder_rel = str(char_folder.relative_to(root))
    else:
        stem_parts = png.stem.split("-")
        char_local = slug(stem_parts[0])
        char_name = stem_parts[0].strip().title()
        folder_rel = str(png.parent.relative_to(root))
    char_uid = f"{pack_name}::{char_local}"

    cur = conn.execute("SELECT 1 FROM characters_auto WHERE char_uid = ?",
                       (char_uid,))
    if cur.fetchone() is None:
        conn.execute(
            """INSERT INTO characters_auto
               (pack_name, char_uid, name, category, folder,
                variant_count, label_source)
               VALUES (?,?,?,?,?,?, 'scanner')""",
            (pack_name, char_uid, char_name,
             "enemy" if "orc" in char_local or "enemy" in char_local
             else "npc" if "vendor" in char_local or "npc" in char_local
             else "animal", folder_rel, 1),
        )
        stats["chars"] = 1

    m = STATE_RE.search(png.name.lower())
    state = m.group(1) if m else "unknown"
    fh = sh
    fw = sh  # square assumption
    fc = sw // fw if fw > 0 and sw >= fw else 1

    canim_uid = f"{pack_name}::{slug(str(png.relative_to(root)))}"
    conn.execute(
        """INSERT OR REPLACE INTO character_animations_auto
           (pack_name, canim_uid, char_uid, state, variant, image_path,
            frame_count, frame_w, frame_h, sheet_w, sheet_h)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (pack_name, canim_uid, char_uid, state, None,
         str(png.resolve()), fc, fw, fh, sw, sh),
    )
    return stats


# ------------------------------------------------------------------
# Main entry
# ------------------------------------------------------------------

def scan_folder(folder: str | Path, db_path: str | Path,
                pack_name: str | None = None) -> dict:
    """Scan ``folder`` recursively and populate ``db_path``.

    ``pack_name`` defaults to the folder's basename. All _auto rows belonging
    to that pack are wiped before indexing, making rescans idempotent. The
    corresponding _overrides rows are never touched.
    """
    root = Path(folder).resolve()
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not root.exists():
        return {"error": f"Folder not found: {root}"}
    if pack_name is None:
        pack_name = root.name

    conn = sqlite3.connect(db_path)
    try:
        apply_schema(conn)
        _wipe_pack(conn, pack_name)
        totals = {"pack_name": pack_name,
                  "tilesets": 0, "tiles": 0,
                  "wang_sets": 0, "wang_tiles": 0, "props": 0,
                  "reference_maps": 0, "reference_layers": 0,
                  "automapping_rules": 0, "animated_props": 0,
                  "characters": 0, "character_animations": 0,
                  "errors": []}

        # 1. TSX
        for tsx in sorted(root.rglob("*.tsx")):
            try:
                s = parse_tsx(tsx, root, pack_name, conn)
                totals["tilesets"] += 1
                totals["tiles"] += s["tiles"]
                totals["wang_sets"] += s["wang_sets"]
                totals["wang_tiles"] += s["wang_tiles"]
                totals["props"] += s["props"]
            except Exception as e:
                totals["errors"].append(f"TSX {tsx.name}: {e}")

        # 2. TMX
        for tmx in sorted(root.rglob("*.tmx")):
            try:
                if is_automapping_rule(tmx):
                    parse_tmx_automapping(tmx, root, pack_name, conn)
                    totals["automapping_rules"] += 1
                else:
                    s = parse_tmx_reference_map(tmx, root, pack_name, conn)
                    totals["reference_maps"] += 1
                    totals["reference_layers"] += s["layers"]
            except Exception as e:
                totals["errors"].append(f"TMX {tmx.name}: {e}")

        # 3. PNG (animated props + characters)
        for png in sorted(root.rglob("*.png")):
            try:
                if is_animated_prop_filename(png.name):
                    parse_animated_prop(png, root, pack_name, conn)
                    totals["animated_props"] += 1
                elif is_character_path(png):
                    s = parse_character(png, root, pack_name, conn)
                    totals["characters"] += s["chars"]
                    totals["character_animations"] += s["char_anims"]
            except Exception as e:
                totals["errors"].append(f"PNG {png.name}: {e}")

        conn.commit()
        totals["db_path"] = str(db_path)
        totals["root"] = str(root)
        return totals
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: scanner.py <folder> <db_path> [pack_name]")
        sys.exit(1)
    pn = sys.argv[3] if len(sys.argv) > 3 else None
    result = scan_folder(sys.argv[1], sys.argv[2], pn)
    import json
    print(json.dumps(result, indent=2, ensure_ascii=False))
