"""
Pure unit test for mcp_server/wang.py core algorithm.

No fixtures — builds a synthetic in-memory SQLite schema on the fly,
populates a 2-color corner wangset with the 16 possible 4-corner tiles,
plus a 2-color edge wangset with all 16 possible 4-edge tiles, and
exercises WangCornerState + WangEdgeState + apply_wang_paint (both
corner and edge dispatch) + resolvers + list_wangsets_for_tilesets.

Runs in <1s, no network, no TMX, no bridge — ideal for CI.

Run:
    python3 scripts/test_wang_unit.py
"""
from __future__ import annotations
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp_server"))

from wang import (  # noqa: E402
    WangCornerState, WangEdgeState, _safe_stem, tile_uid_to_studio_key,
    resolve_wang_tile, resolve_wang_tile_corner, resolve_wang_tile_edge,
    list_wangsets_for_tilesets, list_wang_tile_entries,
    apply_wang_paint, apply_wang_paint_corner, apply_wang_paint_edge,
    seed_corners_from_layer, seed_edges_from_layer,
    get_wangset_type, SUPPORTED_WANG_TYPES,
)


PACK = "TestPack"
STEM = "My Tileset"  # has a space — exercises _safe_stem
WSET_NAME = "dirt"
WSET_UID = f"{PACK}::{STEM}::{WSET_NAME}"


def build_fixture_db(path: Path) -> None:
    """Minimal schema matching what wang.py reads:
       - tiles(tile_uid, tileset, local_id)
       - wang_sets(wangset_uid, pack_name, tileset, name, type,
                   color_count, tile_count)
       - wang_colors(wangset_uid, color_index, name, color_hex)
       - wang_tiles(wangset_uid, tile_uid, c_n,c_ne,c_e,c_se,c_s,c_sw,
                    c_w,c_nw)
    Populate one wangset with 2 colors and all 16 corner combinations
    (color 0 = wildcard + 1 = 'grass'; cells paint color=1).
    """
    with sqlite3.connect(str(path)) as conn:
        conn.executescript("""
            CREATE TABLE tiles(
                tile_uid TEXT PRIMARY KEY,
                tileset  TEXT,
                local_id INTEGER
            );
            CREATE TABLE wang_sets(
                wangset_uid TEXT PRIMARY KEY,
                pack_name   TEXT, tileset TEXT, name TEXT, type TEXT,
                color_count INTEGER, tile_count INTEGER
            );
            CREATE TABLE wang_colors(
                wangset_uid TEXT, color_index INTEGER,
                name TEXT, color_hex TEXT,
                PRIMARY KEY(wangset_uid, color_index)
            );
            CREATE TABLE wang_tiles(
                wangset_uid TEXT, tile_uid TEXT,
                c_n INTEGER, c_ne INTEGER, c_e  INTEGER, c_se INTEGER,
                c_s INTEGER, c_sw INTEGER, c_w  INTEGER, c_nw INTEGER
            );
        """)
        conn.execute(
            "INSERT INTO wang_sets VALUES(?,?,?,?,?,?,?)",
            (WSET_UID, PACK, STEM, WSET_NAME, "corner", 2, 16),
        )
        conn.executemany(
            "INSERT INTO wang_colors VALUES(?,?,?,?)",
            [
                (WSET_UID, 1, "grass", "#00ff00"),
                (WSET_UID, 2, "sand",  "#ffaa00"),
            ],
        )
        lid = 1
        for nw in (1, 2):
            for ne in (1, 2):
                for sw in (1, 2):
                    for se in (1, 2):
                        tuid = f"{PACK}::{STEM}::{lid}"
                        conn.execute(
                            "INSERT INTO tiles VALUES(?,?,?)",
                            (tuid, STEM, lid),
                        )
                        conn.execute(
                            "INSERT INTO wang_tiles VALUES "
                            "(?,?,?,?,?,?,?,?,?,?)",
                            (WSET_UID, tuid,
                             0, ne, 0, se, 0, sw, 0, nw),
                        )
                        lid += 1
        # Add a duplicate (1,1,1,1) match with a HIGHER local_id, so
        # resolver must pick the lower one deterministically.
        conn.execute(
            "INSERT INTO tiles VALUES(?,?,?)",
            (f"{PACK}::{STEM}::99", STEM, 99),
        )
        conn.execute(
            "INSERT INTO wang_tiles VALUES(?,?,?,?,?,?,?,?,?,?)",
            (WSET_UID, f"{PACK}::{STEM}::99",
             0, 1, 0, 1, 0, 1, 0, 1),
        )
        # Add an edge-type wangset with all 16 n/e/s/w combinations,
        # exercised via apply_wang_paint dispatcher.
        edge_uid = f"{PACK}::{STEM}::fence"
        conn.execute(
            "INSERT INTO wang_sets VALUES(?,?,?,?,?,?,?)",
            (edge_uid, PACK, STEM, "fence", "edge", 1, 16),
        )
        conn.execute(
            "INSERT INTO wang_colors VALUES(?,?,?,?)",
            (edge_uid, 1, "fence", "#808080"),
        )
        # local_id range 200..215 so edge tiles don't collide with the
        # corner set's lid 1..16/99.
        lid = 200
        for n in (0, 1):
            for e in (0, 1):
                for s in (0, 1):
                    for w in (0, 1):
                        tuid = f"{PACK}::{STEM}::{lid}"
                        conn.execute(
                            "INSERT INTO tiles VALUES(?,?,?)",
                            (tuid, STEM, lid),
                        )
                        conn.execute(
                            "INSERT INTO wang_tiles VALUES "
                            "(?,?,?,?,?,?,?,?,?,?)",
                            (edge_uid, tuid,
                             n, 0, e, 0, s, 0, w, 0),
                        )
                        lid += 1
        # Add a mixed-type wangset to verify the dispatcher still
        # rejects unsupported types.
        mixed_uid = f"{PACK}::{STEM}::mixed"
        conn.execute(
            "INSERT INTO wang_sets VALUES(?,?,?,?,?,?,?)",
            (mixed_uid, PACK, STEM, "mixed", "mixed", 1, 0),
        )


def main() -> int:
    fails: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  {'OK ' if cond else 'FAIL'}  {name}")
        if not cond:
            fails.append(name)

    print("[_safe_stem]")
    check("space/punct normalized",
          _safe_stem("My Tileset!v2") == "My_Tileset_v2")
    check("already safe passthrough",
          _safe_stem("Tileset-Terrain_new") == "Tileset-Terrain_new")

    print("\n[tile_uid_to_studio_key]")
    check("pack::stem::lid → safe_stem__lid",
          tile_uid_to_studio_key("TestPack::My Tileset::7") == "My_Tileset__7")
    check("malformed → None",
          tile_uid_to_studio_key("bad") is None)
    check("non-int local_id → None",
          tile_uid_to_studio_key("a::b::c") is None)

    print("\n[WangCornerState]")
    s = WangCornerState(width=4, height=3)
    check("corner grid dims (H+1)x(W+1)",
          len(s.corners) == 4 and len(s.corners[0]) == 5)
    s.paint_cell(1, 1, 5)
    check("paint sets 4 corners",
          (s.corners[1][1], s.corners[1][2],
           s.corners[2][1], s.corners[2][2]) == (5, 5, 5, 5))
    check("neighbors share corners",
          s.get_corners(0, 0)[3] == 5)  # (0,0).se == (1,1).nw
    s.erase_cell(1, 1)
    check("erase resets corners to 0",
          s.get_corners(1, 1) == (0, 0, 0, 0))
    s.paint_cell(99, 99, 1)  # no-op, out of bounds
    check("out-of-bounds paint is no-op",
          all(v == 0 for row in s.corners for v in row))

    print("\n[DB-backed helpers]")
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "wang_unit.db"
        build_fixture_db(db)

        # resolve_wang_tile: determinism
        uid = resolve_wang_tile(db, WSET_UID, 1, 1, 1, 1)
        check("resolve (1,1,1,1) returns lowest local_id",
              uid == f"{PACK}::{STEM}::1")
        uid2 = resolve_wang_tile(db, WSET_UID, 1, 2, 2, 1)
        # lid encoding: nw=1,ne=2,sw=2,se=1 → index 1*8+2*4+2*2+1*1-... easier
        # to just assert the tile exists:
        check("resolve non-trivial combo returns a match", uid2 is not None)
        no_uid = resolve_wang_tile(db, WSET_UID, 9, 9, 9, 9)
        check("resolve unknown combo → None", no_uid is None)

        # list_wangsets_for_tilesets
        sets = list_wangsets_for_tilesets(db, [STEM])
        check("list returns 3 sets (corner + edge + mixed)", len(sets) == 3)
        corner = next(s for s in sets if s["type"] == "corner")
        edge = next(s for s in sets if s["type"] == "edge")
        mixed = next(s for s in sets if s["type"] == "mixed")
        check("corner set has nested colors[2]",
              len(corner["colors"]) == 2 and
              corner["colors"][0]["name"] == "grass")
        check("corner set marked supported=true",
              corner["supported"] is True)
        check("edge set marked supported=true (v0.7.1)",
              edge["supported"] is True)
        check("mixed set marked supported=false (not yet)",
              mixed["supported"] is False)
        check("list empty for unknown stem",
              list_wangsets_for_tilesets(db, ["NoSuchStem"]) == [])
        check("list empty for empty input",
              list_wangsets_for_tilesets(db, []) == [])

        # get_wangset_type
        check("get_wangset_type returns 'corner'",
              get_wangset_type(db, WSET_UID) == "corner")
        check("get_wangset_type returns 'edge'",
              get_wangset_type(db, edge["wangset_uid"]) == "edge")
        check("get_wangset_type returns None for missing",
              get_wangset_type(db, "nope::nope::nope") is None)
        check("SUPPORTED_WANG_TYPES covers corner + edge",
              SUPPORTED_WANG_TYPES == {"corner", "edge"})

        # list_wang_tile_entries
        tiles = list_wang_tile_entries(db, WSET_UID)
        check("wang_tile_entries count == 17", len(tiles) == 17)
        check("entries carry studio_key",
              all(t["studio_key"] for t in tiles))

        # apply_wang_paint — 2x2 stroke of color 1 at (1,1)..(2,2)
        # in a 4x4 layer.
        state = WangCornerState(width=4, height=4)
        cells = [{"x": x, "y": y} for y in (1, 2) for x in (1, 2)]
        result = apply_wang_paint(state, db, WSET_UID, 1, cells)
        # 3x3 neighborhoods clipped to 4x4 → affects cells (0..3, 0..3)
        # actually union of 3x3 around each → all 16 cells potentially.
        # Let's verify: each paint cell's 3x3 is bounded → union is
        # (0..3, 0..3) = full grid (since corners reach edges).
        by_xy = {(c["x"], c["y"]): c["key"] for c in result}
        check("center cell (1,1) painted with pure-wang tile",
              by_xy[(1, 1)] == "My_Tileset__1")  # all-1-corner, lid=1
        check("center cell (2,2) painted",
              by_xy[(2, 2)] == "My_Tileset__1")
        # (0,0) has corners (0, 0, 0, 1) — not all zero, should attempt resolve;
        # no match for (0,0,0,1) in our DB → erased (None)
        check("boundary cell (0,0) with partial corner → None",
              by_xy.get((0, 0)) is None)

        # Unsupported-type rejection: mixed
        try:
            apply_wang_paint(
                WangCornerState(width=3, height=3),
                db, mixed["wangset_uid"], 1, [{"x": 0, "y": 0}])
            check("mixed-type wangset rejected with ValueError", False)
        except ValueError as e:
            check("mixed-type wangset rejected with ValueError",
                  "not supported" in str(e))
        try:
            apply_wang_paint(
                WangCornerState(width=3, height=3),
                db, "unknown::set::foo", 1, [{"x": 0, "y": 0}])
            check("unknown wangset raises ValueError", False)
        except ValueError as e:
            check("unknown wangset raises ValueError",
                  "unknown wangset" in str(e))

        # State-type mismatch rejection: edge wangset + corner state
        try:
            apply_wang_paint(
                WangCornerState(width=3, height=3),
                db, edge["wangset_uid"], 1, [{"x": 0, "y": 0}])
            check("type mismatch (edge wset / corner state) rejected",
                  False)
        except ValueError as e:
            check("type mismatch (edge wset / corner state) rejected",
                  "edge-type" in str(e) or "got WangCornerState" in str(e))
        # State-type mismatch rejection: corner wangset + edge state
        try:
            apply_wang_paint(
                WangEdgeState(width=3, height=3),
                db, WSET_UID, 1, [{"x": 0, "y": 0}])
            check("type mismatch (corner wset / edge state) rejected",
                  False)
        except ValueError as e:
            check("type mismatch (corner wset / edge state) rejected",
                  "corner-type" in str(e) or "got WangEdgeState" in str(e))

        # Erase path
        erase_result = apply_wang_paint(
            state, db, WSET_UID, 1, cells, erase=True)
        by_xy_e = {(c["x"], c["y"]): c["key"] for c in erase_result}
        check("erase stroke empties center cell",
              by_xy_e[(1, 1)] is None)
        check("all corners reset to 0 after erase",
              all(v == 0 for row in state.corners for v in row))

        # Cache behavior — no crash with many calls
        state2 = WangCornerState(width=3, height=3)
        big_cells = [{"x": x, "y": y} for y in range(3) for x in range(3)]
        out = apply_wang_paint(state2, db, WSET_UID, 1, big_cells)
        check("9-cell stroke returns 9 results",
              len({(c["x"], c["y"]) for c in out}) == 9)
        check("fully-saturated interior uses pure-wang tile",
              next(c for c in out if c["x"] == 1 and c["y"] == 1)["key"]
              == "My_Tileset__1")

        # -------- Edge-type wangset coverage (v0.7.1) --------
        print("\n[WangEdgeState]")
        es = WangEdgeState(width=4, height=3)
        check("h_edges dims (H+1)xW",
              len(es.h_edges) == 4 and len(es.h_edges[0]) == 4)
        check("v_edges dims Hx(W+1)",
              len(es.v_edges) == 3 and len(es.v_edges[0]) == 5)
        es.paint_cell(1, 1, 7)
        # Cell (1,1)'s four edges should all be 7:
        n, e, s, w = es.get_edges(1, 1)
        check("paint_cell sets all 4 edges", (n, e, s, w) == (7, 7, 7, 7))
        # Neighbor (1,0)'s S edge == cell (1,1)'s N edge → 7.
        check("N-neighbor S-edge shared",
              es.get_edges(1, 0)[2] == 7)
        check("E-neighbor W-edge shared",
              es.get_edges(2, 1)[3] == 7)
        check("S-neighbor N-edge shared",
              es.get_edges(1, 2)[0] == 7)
        check("W-neighbor E-edge shared",
              es.get_edges(0, 1)[1] == 7)
        # Diagonal is NOT shared — edge-type only shares orthogonals.
        check("NE diagonal does NOT share edge",
              es.get_edges(2, 0) == (0, 0, 0, 0))
        es.erase_cell(1, 1)
        check("erase_cell resets 4 edges",
              es.get_edges(1, 1) == (0, 0, 0, 0))
        # Erase did not leak into unrelated cells' edges
        check("erase does not zero neighbor edges",
              es.get_edges(1, 0) == (0, 0, 0, 0))

        print("\n[resolve_wang_tile_edge]")
        e_uid = edge["wangset_uid"]
        # (n=1,e=0,s=0,w=0) — local_id pattern is 200..215 in (n,e,s,w)
        # order with n as MSB. (1,0,0,0) → index 8 → lid 208.
        got = resolve_wang_tile_edge(db, e_uid, 1, 0, 0, 0)
        check("resolve_wang_tile_edge(1,0,0,0) matches",
              got == f"{PACK}::{STEM}::208")
        got2 = resolve_wang_tile_edge(db, e_uid, 0, 0, 0, 0)
        check("resolve_wang_tile_edge(0,0,0,0) matches",
              got2 == f"{PACK}::{STEM}::200")
        got_none = resolve_wang_tile_edge(db, e_uid, 9, 9, 9, 9)
        check("resolve_wang_tile_edge unknown → None",
              got_none is None)
        # Corner resolver on edge wangset should find nothing (edge
        # tiles have c_nw/ne/sw/se = 0 but corner resolver only
        # queries on those).
        corner_on_edge = resolve_wang_tile_corner(db, e_uid, 1, 1, 1, 1)
        check("corner resolver on edge wset → None (c_nw/etc are 0)",
              corner_on_edge is None)

        print("\n[apply_wang_paint_edge + dispatcher]")
        # 4x4 grid, paint a single cell at (1,1) with color 1.
        est = WangEdgeState(width=4, height=4)
        r = apply_wang_paint(
            est, db, e_uid, 1, [{"x": 1, "y": 1}])
        r_by_xy = {(c["x"], c["y"]): c["key"] for c in r}
        # 5-cell neighborhood (self + 4 orthogonal): (1,1), (1,0),
        # (2,1), (1,2), (0,1).
        check("edge paint touches 5 cells (self + 4 orthog)",
              set(r_by_xy.keys()) == {(1, 1), (1, 0), (2, 1), (1, 2), (0, 1)})
        # Center (1,1): all 4 edges = 1 → (n=1,e=1,s=1,w=1) → lid 215.
        check("center cell (1,1) = all-1 edges tile",
              r_by_xy[(1, 1)] == f"My_Tileset__215")
        # (1,0) has only its S edge = 1 (shared with (1,1) N). All
        # others 0 → (n=0, e=0, s=1, w=0) → lid 202.
        check("N neighbor gets S-edge-only tile",
              r_by_xy[(1, 0)] == f"My_Tileset__202")
        # (2,1): only W edge = 1 → (0,0,0,1) → lid 201.
        check("E neighbor gets W-edge-only tile",
              r_by_xy[(2, 1)] == f"My_Tileset__201")
        # Diagonals NOT touched.
        check("diagonals not touched by edge paint",
              (2, 0) not in r_by_xy and (0, 0) not in r_by_xy)

        # Erase round-trip
        r_e = apply_wang_paint(
            est, db, e_uid, 1, [{"x": 1, "y": 1}], erase=True)
        r_e_by_xy = {(c["x"], c["y"]): c["key"] for c in r_e}
        check("edge erase clears center",
              r_e_by_xy[(1, 1)] is None)
        check("edge erase returns all 4 edges to 0",
              est.get_edges(1, 1) == (0, 0, 0, 0))
        # Neighbors (fully zero edges) -> None
        check("edge erase: N neighbor None",
              r_e_by_xy[(1, 0)] is None)

        print("\n[seed_edges_from_layer]")
        # Build a 3x3 layer where (0,0) holds an edge tile with n=1
        # (lid 208) and verify seeding populates the expected edges.
        layer_data = [
            [f"My_Tileset__208", None, None],
            [None, None, None],
            [None, None, None],
        ]
        ss = WangEdgeState(width=3, height=3)
        n_known = seed_edges_from_layer(ss, layer_data, db, e_uid)
        check("seed_edges_from_layer count", n_known == 1)
        n_seed, e_seed, s_seed, w_seed = ss.get_edges(0, 0)
        check("seed edges correctly set (n=1, rest=0)",
              (n_seed, e_seed, s_seed, w_seed) == (1, 0, 0, 0))

    print()
    if fails:
        print(f"[FAIL] {len(fails)} assertion(s) failed:")
        for f in fails:
            print(f"   - {f}")
        return 2
    print("[OK] all unit assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
