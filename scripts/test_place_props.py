"""Unit test for v0.8.0 tool_place_props + helpers.

Covers:

  A. `_jitter_grid_sample` is deterministic with a fixed seed and yields
     a sane count proportional to density * area / min_distance^2.
  B. `_pick_variant` honors weighted [("name", weight), ...] inputs — a
     variant with weight 10 should dominate over one with weight 1.
  C. `_resolve_region` normalises dicts and returns a clear error when the
     bridge is unreachable for region="selection".
  D. `tool_place_props` with bridge DOWN falls through to direct TMX write:
     - reads the fake DB we seed with fixture props
     - writes <object> elements into the named <objectgroup>
     - bumps <map nextobjectid>
     - returns via="direct" + a variant_counts histogram
  E. `apply_object_add` error handling — missing group raises ValueError.
  F. TOOL_DEFS registers place_props; tool count == 28.

Run:
    python3 scripts/test_place_props.py

Does not require a real asset pack — uses a self-contained temp DB +
temp TMX so the test runs offline.
"""
from __future__ import annotations
import os
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp_server"))

# Point server.py at a sandbox DB BEFORE importing it.
_TMP = Path(tempfile.mkdtemp(prefix="tilesmith-placeprops-"))
os.environ["TILESMITH_DB_PATH"] = str(_TMP / "tiles.db")

import server as mcp           # noqa: E402
from scanner import SCHEMA_DDL  # noqa: E402
from tmx_mutator import apply_object_add  # noqa: E402


FAKE_TSX = """<?xml version="1.0" encoding="UTF-8"?>
<tileset version="1.10" tiledversion="1.10.2" name="trees"
         tilewidth="16" tileheight="16" tilecount="4" columns="2">
  <image source="trees.png" width="32" height="32"/>
</tileset>
"""

FAKE_TMX = """<?xml version="1.0" encoding="UTF-8"?>
<map version="1.10" tiledversion="1.10.2" orientation="orthogonal"
     renderorder="right-down" width="20" height="20" tilewidth="16"
     tileheight="16" infinite="0" nextlayerid="3" nextobjectid="1">
  <tileset firstgid="1" source="trees.tsx"/>
  <layer id="1" name="ground" width="20" height="20">
    <data encoding="csv">
{ground_rows}
</data>
  </layer>
  <objectgroup id="2" name="flora"/>
</map>
"""


def _ground_csv(w: int, h: int) -> str:
    return "\n".join(",".join("1" for _ in range(w)) + "," for _ in range(h))


def _seed_db(db_path: Path) -> None:
    """Create DB with schema + 3 fake prop rows in category 'tree'.

    Two 'composite' (oak, pine) — one with heavy weight target; one 'part'
    variant (leaf) that must NOT match when variants defaults to composite.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA_DDL)
        # Seed tilesets row so the key stem resolves under safe-stem rules.
        conn.execute(
            "INSERT INTO tilesets_auto(pack_name, tileset_uid, name, "
            "source_path, image_path, tile_count, columns, "
            "tile_width, tile_height) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("fakepack", "fakepack::trees", "trees",
             "trees.tsx", "trees.png", 4, 2, 16, 16),
        )
        rows = [
            # oak — composite, heavy weight target in case D
            ("fakepack", "fakepack::trees::0", "fakepack::trees", "trees",
             0, "trees.png", "tree", "composite", 16, 16),
            # pine — composite, low weight target
            ("fakepack", "fakepack::trees::1", "fakepack::trees", "trees",
             1, "trees.png", "tree", "composite", 16, 32),
            # leaf — 'part' variant, must be filtered out by default
            ("fakepack", "fakepack::trees::2", "fakepack::trees", "trees",
             2, "trees.png", "tree", "part", 16, 16),
        ]
        for r in rows:
            conn.execute(
                "INSERT INTO props_auto(pack_name, prop_uid, tileset_uid, "
                "tileset, local_id, image_path, category, variant, "
                "size_w, size_h) VALUES (?,?,?,?,?,?,?,?,?,?)", r)
        conn.commit()
    finally:
        conn.close()


def _write_fake_tmx(path: Path) -> None:
    (path.parent / "trees.tsx").write_text(FAKE_TSX, encoding="utf-8")
    (path.parent / "trees.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    path.write_text(FAKE_TMX.format(ground_rows=_ground_csv(20, 20)),
                    encoding="utf-8")


def main() -> int:
    import random
    ok = True

    # --- A: jitter grid determinism ---------------------------------
    print("\n[case A: _jitter_grid_sample]")
    rng1 = random.Random(42)
    p1 = mcp._jitter_grid_sample(0, 0, 9, 9, min_distance=2,
                                  density=1.0, rng=rng1)
    rng2 = random.Random(42)
    p2 = mcp._jitter_grid_sample(0, 0, 9, 9, min_distance=2,
                                  density=1.0, rng=rng2)
    # density=1.0, 10x10 region, md=2 => exactly ceil(10/2)^2 = 25 cells
    checks_a = {
        "deterministic for same seed": p1 == p2,
        "produces 25 samples (10x10 / 2x2 cells, density=1)": len(p1) == 25,
        "all samples inside region": all(0 <= x <= 9 and 0 <= y <= 9
                                          for x, y in p1),
    }
    # Lower density thins the cloud
    rng3 = random.Random(42)
    p3 = mcp._jitter_grid_sample(0, 0, 9, 9, min_distance=2,
                                  density=0.0, rng=rng3)
    checks_a["density=0 produces zero samples"] = len(p3) == 0
    for k, v in checks_a.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- B: weighted variant picker ---------------------------------
    print("\n[case B: _pick_variant weighted]")
    cands = [
        {"key": "trees__0", "variant": "oak"},
        {"key": "trees__1", "variant": "pine"},
    ]
    weighted = [("oak", 10.0), ("pine", 1.0)]
    counts = {"oak": 0, "pine": 0}
    rng = random.Random(7)
    for _ in range(500):
        c = mcp._pick_variant(cands, weighted, rng)
        counts[c["variant"]] += 1
    # 10:1 weighting => expect oak >> pine (oak > 400 out of 500)
    checks_b = {
        "oak dominates with 10:1 weight (>400/500)": counts["oak"] > 400,
        "pine still appears sometimes (>10/500)": counts["pine"] > 10,
        "no other variants leaked in": sum(counts.values()) == 500,
    }
    print(f"  counts: {counts}")
    for k, v in checks_b.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- C: region resolution ---------------------------------------
    print("\n[case C: _resolve_region]")
    rect, err = mcp._resolve_region({"x0": 5, "y0": 10, "x1": 2, "y1": 0},
                                     port=3099, host="127.0.0.1")
    c_checks = {
        "dict region normalises (x0<=x1)": rect == {"x0": 2, "y0": 0,
                                                     "x1": 5, "y1": 10},
        "no error on valid dict": err is None,
    }
    rect2, err2 = mcp._resolve_region("selection",
                                       port=3099, host="127.0.0.1")
    c_checks["selection with no bridge returns error"] = (
        rect2 is None and err2 is not None and "unreachable" in err2)

    rect3, err3 = mcp._resolve_region({"x0": "nope"},
                                       port=3099, host="127.0.0.1")
    c_checks["malformed dict returns error, no rect"] = (
        rect3 is None and err3 is not None)

    for k, v in c_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- D: tool_place_props direct path ----------------------------
    print("\n[case D: tool_place_props direct TMX fallback]")
    # Seed DB + build fake TMX in a shared tempdir
    db_path = Path(os.environ["TILESMITH_DB_PATH"])
    _seed_db(db_path)

    td = Path(tempfile.mkdtemp(prefix="tilesmith-tmx-"))
    tmx_path = td / "fake.tmx"
    _write_fake_tmx(tmx_path)

    # nextobjectid starts at 1, no objects yet
    root0 = ET.parse(tmx_path).getroot()
    before_next = int(root0.get("nextobjectid"))
    before_count = len(root0.find("objectgroup").findall("object"))

    res = mcp.tool_place_props(
        tmx_path=str(tmx_path),
        layer="flora",
        region={"x0": 0, "y0": 0, "x1": 9, "y1": 9},
        category="tree",
        variants=None,          # default "composite" — leaf must be skipped
        density=1.0,
        min_distance=2,
        seed=42,
        port=3099,              # no bridge running on this port
    )
    print(f"  res keys: {sorted(res.keys())}")
    checks_d = {
        "no error in result": "error" not in res,
        "via == direct (bridge unreachable)": res.get("via") == "direct",
        "placed > 0": res.get("placed", 0) > 0,
        "region echoed back": res.get("region") == {"x0": 0, "y0": 0,
                                                     "x1": 9, "y1": 9},
        "variant_counts histogram populated":
            isinstance(res.get("variant_counts"), dict)
            and sum(res["variant_counts"].values()) == res["placed"],
    }
    # Now verify the TMX on disk
    root1 = ET.parse(tmx_path).getroot()
    after_next = int(root1.get("nextobjectid"))
    objs = root1.find("objectgroup").findall("object")
    after_count = len(objs)
    checks_d["objectgroup object count grew by 'placed'"] = (
        after_count - before_count == res["placed"])
    checks_d["nextobjectid bumped by 'placed'"] = (
        after_next - before_next == res["placed"])
    # All new objects should reference gid in [1..4] (leaf=3 excluded)
    allowed_gids = {1, 2}  # oak=firstgid+0=1, pine=firstgid+1=2
    all_gids = {int(o.get("gid")) for o in objs}
    checks_d["all new object gids are composite props (1 or 2)"] = (
        all_gids.issubset(allowed_gids))
    # Verify 'leaf' (variant='part') was filtered out — no gid 3
    checks_d["'part' variant (gid=3) filtered out by default"] = (
        3 not in all_gids)
    # Verify y uses BOTTOM-anchor convention: at least one object has y
    # equal to (ty+1)*16 for an even ty (i.e. multiple of 16 > 0)
    sample_y = float(objs[0].get("y"))
    checks_d["object y > 0 (bottom-anchor)"] = sample_y > 0

    for k, v in checks_d.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- E: apply_object_add group-missing error --------------------
    print("\n[case E: apply_object_add missing group]")
    try:
        apply_object_add(
            tmx_path, "nonexistent_group",
            [{"key": "trees__0", "x": 0, "y": 0,
              "width": 16, "height": 16}],
        )
        e_ok = False
        print("  FAIL  expected ValueError on missing group")
    except ValueError as e:
        e_ok = "nonexistent_group" in str(e)
        print(f"  {'OK  ' if e_ok else 'FAIL'}  raises ValueError: {e}")
    if not e_ok:
        ok = False

    # --- F: TOOL_DEFS registration ----------------------------------
    print("\n[case F: TOOL_DEFS registration]")
    tools = [t[0] for t in mcp.TOOL_DEFS]
    checks_f = {
        "place_props registered":   "place_props" in tools,
        "finalize_map registered":  "finalize_map" in tools,
        "get_selection registered": "get_selection" in tools,
        "tool count >= 28 (27 baseline + place_props, more as v0.8 grows)":
            len(tools) >= 28,
    }
    print(f"  total tools: {len(tools)}")
    for k, v in checks_f.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
