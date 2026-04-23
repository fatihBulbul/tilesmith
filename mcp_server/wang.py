"""
Wang-aware autotile paint helpers.

Tiled iki wangset türünü destekler:

  * corner (default) — her hücrenin 4 köşesi (NW/NE/SW/SE) bir renk
    taşır; komşu 8 hücreyle köşeler paylaşılır. Kullanıcı bir hücreye
    "renk C" basarsa o hücrenin 4 köşesi C olur; 3x3 etkilenen blokta
    her hücre 4-köşe kombinasyonundan DB'de `wang_tiles.(c_nw..c_se)`
    üzerinden aranır.

  * edge — her hücrenin 4 kenarı (N/E/S/W) bir renk taşır; sadece
    orthogonal (N/E/S/W) komşularla kenarlar paylaşılır — diagonallar
    paylaşmaz. Kullanıcı bir hücreye "renk C" basarsa o hücrenin 4
    kenarı C olur; 5 etkilenen hücre (self + 4 orthogonal) için
    `wang_tiles.(c_n, c_e, c_s, c_w)` üzerinden aranır.

  * mixed — corner + edge birleşimi. Şu an desteklenmez
    (`SUPPORTED_WANG_TYPES` dışında).

Data:
    WangCornerState / WangEdgeState — state sınıfları wangset type'ına
    göre seçilir. Her ikisi de `width`/`height` (layer cell boyutları)
    ve bir ya da iki grid tutar. `paint_cell(x,y,color)` tekil paint
    uygulaması; `get_signature(x,y)` resolve için (nw,ne,sw,se) ya da
    (n,e,s,w) döner.

DB köprüsü:
    resolve_wang_tile_corner(wangset_uid, nw,ne,sw,se) → tile_uid | None
    resolve_wang_tile_edge(wangset_uid, n,e,s,w)       → tile_uid | None
    resolve_wang_tile(...) geriye dönük uyumluluk için alias (corner).

Studio key dönüşümü:
    tile_uid_to_studio_key("pack::Stem::7") → "Stem__7"

apply_wang_paint(...) ana entry-point — wangset type'a göre corner ya
da edge yol'unu seçer. Çıkış: [{x, y, key|None}, ...].
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
import sqlite3


# ---------------------------------------------------------------------
# Safe stem (mirror of tmx_state._safe_key / tmx_mutator._safe_stem)
# ---------------------------------------------------------------------

def _safe_stem(stem: str) -> str:
    out = []
    for ch in stem:
        if ch.isalnum() or ch in "-_.":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def tile_uid_to_studio_key(tile_uid: str) -> str | None:
    """'pack::Stem::7' -> 'safe_stem__7'. Ters dönüşüm ile bağlanır."""
    parts = tile_uid.rsplit("::", 2)
    if len(parts) < 2:
        return None
    try:
        lid = int(parts[-1])
    except ValueError:
        return None
    stem = parts[-2]
    return f"{_safe_stem(stem)}__{lid}"


# ---------------------------------------------------------------------
# WangCornerState: per-layer (H+1) x (W+1) corner colors
# ---------------------------------------------------------------------

@dataclass
class WangCornerState:
    width: int          # layer cell width
    height: int         # layer cell height
    # corners[cy][cx], cy in [0..H], cx in [0..W]
    # 0 = "outside" / wildcard (no wang coverage yet)
    corners: list[list[int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.corners:
            self.corners = [
                [0] * (self.width + 1) for _ in range(self.height + 1)
            ]

    def _in_bounds_cell(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def paint_cell(self, x: int, y: int, color: int) -> None:
        """Set all 4 corners of cell (x,y) to `color`."""
        if not self._in_bounds_cell(x, y):
            return
        self.corners[y][x] = color         # NW
        self.corners[y][x + 1] = color     # NE
        self.corners[y + 1][x] = color     # SW
        self.corners[y + 1][x + 1] = color # SE

    def erase_cell(self, x: int, y: int) -> None:
        """Reset all 4 corners of cell (x,y) to 0 (outside)."""
        self.paint_cell(x, y, 0)

    def get_corners(self, x: int, y: int) -> tuple[int, int, int, int]:
        """(nw, ne, sw, se) for cell (x,y)."""
        if not self._in_bounds_cell(x, y):
            return (0, 0, 0, 0)
        return (
            self.corners[y][x],
            self.corners[y][x + 1],
            self.corners[y + 1][x],
            self.corners[y + 1][x + 1],
        )

    def as_dict(self) -> dict:
        return {
            "width": self.width, "height": self.height,
            "corners": self.corners,
        }


# ---------------------------------------------------------------------
# WangEdgeState: per-layer 2 grids (horizontal edges + vertical edges)
# ---------------------------------------------------------------------

@dataclass
class WangEdgeState:
    """Edge-type wangset state.

    Each cell (x,y) has 4 edges: N, E, S, W. Edges are shared with the
    orthogonal neighbors:
      - my N edge == neighbor_above.S edge
      - my S edge == neighbor_below.N edge
      - my W edge == neighbor_left.E edge
      - my E edge == neighbor_right.W edge

    We store two grids:
      h_edges[ey][ex]  — horizontal edges, 0 <= ey <= H, 0 <= ex < W
                         (ey = 0 is the top-most row's N edge; ey = H is
                         the bottom-most row's S edge.)
      v_edges[ey][ex]  — vertical edges, 0 <= ey < H, 0 <= ex <= W
                         (ex = 0 is the left column's W edge; ex = W is
                         the right column's E edge.)

    0 = "outside" / wildcard (no wang coverage).
    """
    width: int
    height: int
    h_edges: list[list[int]] = field(default_factory=list)
    v_edges: list[list[int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.h_edges:
            self.h_edges = [
                [0] * self.width for _ in range(self.height + 1)
            ]
        if not self.v_edges:
            self.v_edges = [
                [0] * (self.width + 1) for _ in range(self.height)
            ]

    def _in_bounds_cell(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def paint_cell(self, x: int, y: int, color: int) -> None:
        """Set all 4 edges of cell (x,y) to `color`."""
        if not self._in_bounds_cell(x, y):
            return
        # N + S horizontal edges
        self.h_edges[y][x] = color          # N edge
        self.h_edges[y + 1][x] = color      # S edge
        # W + E vertical edges
        self.v_edges[y][x] = color          # W edge
        self.v_edges[y][x + 1] = color      # E edge

    def erase_cell(self, x: int, y: int) -> None:
        self.paint_cell(x, y, 0)

    def get_edges(self, x: int, y: int) -> tuple[int, int, int, int]:
        """(n, e, s, w) for cell (x,y)."""
        if not self._in_bounds_cell(x, y):
            return (0, 0, 0, 0)
        return (
            self.h_edges[y][x],          # N
            self.v_edges[y][x + 1],      # E
            self.h_edges[y + 1][x],      # S
            self.v_edges[y][x],          # W
        )

    def as_dict(self) -> dict:
        return {
            "width": self.width, "height": self.height,
            "h_edges": self.h_edges, "v_edges": self.v_edges,
        }


# ---------------------------------------------------------------------
# DB-backed resolvers
# ---------------------------------------------------------------------

def resolve_wang_tile_corner(
    db_path: str | Path,
    wangset_uid: str,
    nw: int, ne: int, sw: int, se: int,
) -> str | None:
    """Corner-type resolver: match by 4 corner colors. Lowest local_id wins."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT wt.tile_uid, t.local_id
              FROM wang_tiles wt
              JOIN tiles t ON t.tile_uid = wt.tile_uid
             WHERE wt.wangset_uid = ?
               AND wt.c_nw = ? AND wt.c_ne = ?
               AND wt.c_sw = ? AND wt.c_se = ?
             ORDER BY t.local_id ASC
             LIMIT 1
            """,
            (wangset_uid, nw, ne, sw, se),
        )
        row = cur.fetchone()
        return row["tile_uid"] if row else None


def resolve_wang_tile_edge(
    db_path: str | Path,
    wangset_uid: str,
    n: int, e: int, s: int, w: int,
) -> str | None:
    """Edge-type resolver: match by 4 side colors. Lowest local_id wins."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT wt.tile_uid, t.local_id
              FROM wang_tiles wt
              JOIN tiles t ON t.tile_uid = wt.tile_uid
             WHERE wt.wangset_uid = ?
               AND wt.c_n = ? AND wt.c_e = ?
               AND wt.c_s = ? AND wt.c_w = ?
             ORDER BY t.local_id ASC
             LIMIT 1
            """,
            (wangset_uid, n, e, s, w),
        )
        row = cur.fetchone()
        return row["tile_uid"] if row else None


# Back-compat alias — older call-sites referred to corner resolver as just
# `resolve_wang_tile`. Keep the name working.
resolve_wang_tile = resolve_wang_tile_corner


SUPPORTED_WANG_TYPES = {"corner", "edge"}


def list_wangsets_for_tilesets(
    db_path: str | Path,
    raw_stems: Iterable[str],
) -> list[dict]:
    """Wang sets whose tileset (raw stem) is in the given set. Includes
    per-set color list so the palette UI can render swatches.

    Each set carries a `supported` bool: True iff the autotile resolver
    can handle it. Currently only 'corner' type. Edge/mixed types are
    reported but marked unsupported — callers should filter or disable
    them in UI.
    """
    stems = list(raw_stems)
    if not stems:
        return []
    placeholders = ",".join("?" * len(stems))
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            f"""
            SELECT pack_name, wangset_uid, tileset, name, type,
                   color_count, tile_count
              FROM wang_sets
             WHERE tileset IN ({placeholders})
             ORDER BY pack_name, tileset, name
            """,
            stems,
        )
        sets = [dict(r) for r in cur.fetchall()]
        for s in sets:
            s["supported"] = s["type"] in SUPPORTED_WANG_TYPES
            cur = conn.execute(
                "SELECT color_index, name, color_hex "
                "FROM wang_colors WHERE wangset_uid = ? "
                "ORDER BY color_index",
                (s["wangset_uid"],),
            )
            s["colors"] = [dict(r) for r in cur.fetchall()]
    return sets


def get_wangset_type(db_path: str | Path, wangset_uid: str) -> str | None:
    """Fetch the `type` column for a single wangset, or None."""
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            "SELECT type FROM wang_sets WHERE wangset_uid = ?",
            (wangset_uid,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def list_wang_tile_entries(
    db_path: str | Path,
    wangset_uid: str,
) -> list[dict]:
    """All tiles in a wangset with 8-corner colors + studio key.

    Useful for building a small in-browser lookup table if we ever want
    pure client-side tile resolution. Not wired in v1 (bridge does it).
    """
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT wt.tile_uid, wt.c_n, wt.c_ne, wt.c_e, wt.c_se,
                   wt.c_s, wt.c_sw, wt.c_w, wt.c_nw,
                   t.tileset, t.local_id
              FROM wang_tiles wt
              JOIN tiles t ON t.tile_uid = wt.tile_uid
             WHERE wt.wangset_uid = ?
             ORDER BY t.local_id
            """,
            (wangset_uid,),
        )
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            d["studio_key"] = tile_uid_to_studio_key(d["tile_uid"])
            rows.append(d)
        return rows


# ---------------------------------------------------------------------
# Paint algorithms
# ---------------------------------------------------------------------

def apply_wang_paint_corner(
    state: WangCornerState,
    db_path: str | Path,
    wangset_uid: str,
    color: int,
    paint_cells: list[dict],
    *,
    erase: bool = False,
) -> list[dict]:
    """Corner-type wang paint.

    Algorithm:
      1. For each painted (x,y): set 4 corners to color (or 0 if erase).
      2. Collect the 3x3 neighborhood of every painted cell (union, deduped).
      3. For each affected cell, read its 4 corners and call
         resolve_wang_tile_corner(...). If the corners are (0,0,0,0), we
         erase that cell (None key). If resolve returns no match, we
         ALSO erase (None) so TMX stays well-formed.
      4. Return [{x, y, key|None}, ...].

    State is mutated in-place.
    """
    # 1. Update corner state
    paint_color = 0 if erase else color
    for c in paint_cells:
        x = int(c["x"]); y = int(c["y"])
        state.paint_cell(x, y, paint_color)

    # 2. Collect affected 3x3 neighborhoods (corners are shared with 8 neighbors)
    affected: set[tuple[int, int]] = set()
    for c in paint_cells:
        x = int(c["x"]); y = int(c["y"])
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                nx, ny = x + dx, y + dy
                if 0 <= nx < state.width and 0 <= ny < state.height:
                    affected.add((nx, ny))

    # 3. Resolve each affected cell
    out: list[dict] = []
    cache: dict[tuple[int, int, int, int], str | None] = {}
    for (x, y) in sorted(affected):
        nw, ne, sw, se = state.get_corners(x, y)
        if (nw, ne, sw, se) == (0, 0, 0, 0):
            out.append({"x": x, "y": y, "key": None})
            continue
        ck = (nw, ne, sw, se)
        if ck not in cache:
            tile_uid = resolve_wang_tile_corner(db_path, wangset_uid, *ck)
            cache[ck] = tile_uid_to_studio_key(tile_uid) if tile_uid else None
        out.append({"x": x, "y": y, "key": cache[ck]})

    return out


def apply_wang_paint_edge(
    state: WangEdgeState,
    db_path: str | Path,
    wangset_uid: str,
    color: int,
    paint_cells: list[dict],
    *,
    erase: bool = False,
) -> list[dict]:
    """Edge-type wang paint.

    Algorithm:
      1. For each painted (x,y): set 4 edges to color (or 0 if erase).
      2. Collect the 5-cell neighborhood (self + N/E/S/W) of every
         painted cell. Diagonals are NOT affected — edges are only
         shared with 4-orthogonal neighbors.
      3. For each affected cell, read its 4 edges and call
         resolve_wang_tile_edge(...). If the edges are (0,0,0,0), erase
         that cell (None key). If resolve returns no match, also erase
         so TMX stays well-formed.
      4. Return [{x, y, key|None}, ...].

    State is mutated in-place.
    """
    # 1. Update edge state
    paint_color = 0 if erase else color
    for c in paint_cells:
        x = int(c["x"]); y = int(c["y"])
        state.paint_cell(x, y, paint_color)

    # 2. Collect affected 5-cell (plus-shaped) neighborhoods
    affected: set[tuple[int, int]] = set()
    for c in paint_cells:
        x = int(c["x"]); y = int(c["y"])
        for dx, dy in ((0, 0), (0, -1), (0, 1), (-1, 0), (1, 0)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < state.width and 0 <= ny < state.height:
                affected.add((nx, ny))

    # 3. Resolve each affected cell
    out: list[dict] = []
    cache: dict[tuple[int, int, int, int], str | None] = {}
    for (x, y) in sorted(affected):
        n, e, s, w = state.get_edges(x, y)
        if (n, e, s, w) == (0, 0, 0, 0):
            out.append({"x": x, "y": y, "key": None})
            continue
        ck = (n, e, s, w)
        if ck not in cache:
            tile_uid = resolve_wang_tile_edge(db_path, wangset_uid, *ck)
            cache[ck] = tile_uid_to_studio_key(tile_uid) if tile_uid else None
        out.append({"x": x, "y": y, "key": cache[ck]})

    return out


def apply_wang_paint(
    state: "WangCornerState | WangEdgeState",
    db_path: str | Path,
    wangset_uid: str,
    color: int,
    paint_cells: list[dict],
    *,
    erase: bool = False,
) -> list[dict]:
    """Dispatcher: branches on wangset type ('corner' vs 'edge').

    Raises ValueError if the wangset type is not supported (mixed or
    unknown), or if the state class doesn't match the wangset type
    (caller must pass WangCornerState for corner sets and WangEdgeState
    for edge sets).
    """
    wtype = get_wangset_type(db_path, wangset_uid)
    if wtype is None:
        raise ValueError(f"unknown wangset: {wangset_uid}")
    if wtype not in SUPPORTED_WANG_TYPES:
        raise ValueError(
            f"wangset type '{wtype}' not supported (only "
            f"{sorted(SUPPORTED_WANG_TYPES)} supported; got {wangset_uid})"
        )

    if wtype == "corner":
        if not isinstance(state, WangCornerState):
            raise ValueError(
                f"wangset {wangset_uid} is corner-type but got "
                f"{type(state).__name__}"
            )
        return apply_wang_paint_corner(
            state, db_path, wangset_uid, color, paint_cells, erase=erase,
        )
    # edge
    if not isinstance(state, WangEdgeState):
        raise ValueError(
            f"wangset {wangset_uid} is edge-type but got "
            f"{type(state).__name__}"
        )
    return apply_wang_paint_edge(
        state, db_path, wangset_uid, color, paint_cells, erase=erase,
    )


# ---------------------------------------------------------------------
# Seed from existing TMX layer
# ---------------------------------------------------------------------

def seed_corners_from_layer(
    state: WangCornerState,
    layer_data: list[list[str | None]],
    db_path: str | Path,
    wangset_uid: str,
) -> int:
    """Initialize the corner grid by looking up every existing tile's
    4 corners in the DB. Returns number of cells that were wang-known.

    Tile_uid matching is done via the studio key path suffix (local_id)
    combined with the wangset's tileset raw stem, which we fetch once.
    """
    # Fetch wangset's tileset stem + pack_name (for exact tile_uid)
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT pack_name, tileset FROM wang_sets "
            "WHERE wangset_uid = ?",
            (wangset_uid,),
        ).fetchone()
        if row is None:
            return 0
        pack_name, raw_stem = row["pack_name"], row["tileset"]
        safe = _safe_stem(raw_stem)
        # All tiles in this wangset, keyed by local_id
        cur = conn.execute(
            """
            SELECT t.local_id, wt.c_nw, wt.c_ne, wt.c_sw, wt.c_se
              FROM wang_tiles wt
              JOIN tiles t ON t.tile_uid = wt.tile_uid
             WHERE wt.wangset_uid = ?
            """,
            (wangset_uid,),
        )
        by_lid: dict[int, tuple[int, int, int, int]] = {}
        for r in cur.fetchall():
            by_lid[r["local_id"]] = (
                r["c_nw"], r["c_ne"], r["c_sw"], r["c_se"],
            )

    known = 0
    for y, row_ in enumerate(layer_data):
        if y >= state.height:
            break
        for x, key in enumerate(row_):
            if x >= state.width or key is None:
                continue
            # Expect "{safe_stem}__{lid}"
            if "__" not in key:
                continue
            stem_part, lid_str = key.rsplit("__", 1)
            if stem_part != safe:
                continue
            try:
                lid = int(lid_str)
            except ValueError:
                continue
            corners = by_lid.get(lid)
            if corners is None:
                continue
            nw, ne, sw, se = corners
            state.corners[y][x] = nw
            state.corners[y][x + 1] = ne
            state.corners[y + 1][x] = sw
            state.corners[y + 1][x + 1] = se
            known += 1
    return known


def seed_edges_from_layer(
    state: WangEdgeState,
    layer_data: list[list[str | None]],
    db_path: str | Path,
    wangset_uid: str,
) -> int:
    """Initialize the edge grids from an existing layer. Returns number
    of cells that were wang-known.

    Only tiles whose studio key stem matches the wangset's tileset are
    considered — others are ignored.
    """
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT pack_name, tileset FROM wang_sets "
            "WHERE wangset_uid = ?",
            (wangset_uid,),
        ).fetchone()
        if row is None:
            return 0
        _, raw_stem = row["pack_name"], row["tileset"]
        safe = _safe_stem(raw_stem)
        cur = conn.execute(
            """
            SELECT t.local_id, wt.c_n, wt.c_e, wt.c_s, wt.c_w
              FROM wang_tiles wt
              JOIN tiles t ON t.tile_uid = wt.tile_uid
             WHERE wt.wangset_uid = ?
            """,
            (wangset_uid,),
        )
        by_lid: dict[int, tuple[int, int, int, int]] = {}
        for r in cur.fetchall():
            by_lid[r["local_id"]] = (
                r["c_n"], r["c_e"], r["c_s"], r["c_w"],
            )

    known = 0
    for y, row_ in enumerate(layer_data):
        if y >= state.height:
            break
        for x, key in enumerate(row_):
            if x >= state.width or key is None:
                continue
            if "__" not in key:
                continue
            stem_part, lid_str = key.rsplit("__", 1)
            if stem_part != safe:
                continue
            try:
                lid = int(lid_str)
            except ValueError:
                continue
            edges = by_lid.get(lid)
            if edges is None:
                continue
            n, e, s, w = edges
            state.h_edges[y][x] = n
            state.v_edges[y][x + 1] = e
            state.h_edges[y + 1][x] = s
            state.v_edges[y][x] = w
            known += 1
    return known
