"""Unit test for v0.8.0 tool_add_object + tool_remove_objects.

Covers:

  A. `_lookup_prop_by_uid` returns the key ("{safe_stem}__{local_id}")
     and size fields, or None for an unknown uid.
  B. `_gid_to_stem_local` reverses gid -> (safe_stem, local_id) using
     the TMX's <tileset source="..." firstgid="..."> table.
  C. `tool_add_object` with bridge DOWN:
     - writes one <object> with correct pixel coords (x*tw, (y+1)*th)
     - picks up size_w/size_h from the DB row
     - returns via="direct"
  D. `tool_remove_objects` (no filter): removes every object whose
     CENTER pixel lies inside the region; leaves others alone.
  E. `tool_remove_objects` with category filter: only objects whose gid
     maps to a prop in that category get deleted. matched_but_skipped
     reports how many region-hits were filtered away.
  F. `apply_object_remove` error paths:
     - missing group -> ValueError
     - unknown ids -> reported in missing_ids, removed=0
  G. TOOL_DEFS registers add_object + remove_objects; tool count == 30.

Run:
    python3 scripts/test_add_remove_objects.py
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

_TMP = Path(tempfile.mkdtemp(prefix="tilesmith-addrm-"))
os.environ["TILESMITH_DB_PATH"] = str(_TMP / "tiles.db")

import server as mcp              # noqa: E402
from scanner import SCHEMA_DDL    # noqa: E402
from tmx_mutator import apply_object_remove  # noqa: E402


FAKE_TSX_TREES = """<?xml version="1.0" encoding="UTF-8"?>
<tileset version="1.10" tiledversion="1.10.2" name="trees"
         tilewidth="16" tileheight="16" tilecount="4" columns="2">
  <image source="trees.png" width="32" height="32"/>
</tileset>
"""

FAKE_TSX_ROCKS = """<?xml version="1.0" encoding="UTF-8"?>
<tileset version="1.10" tiledversion="1.10.2" name="rocks"
         tilewidth="16" tileheight="16" tilecount="4" columns="2">
  <image source="rocks.png" width="32" height="32"/>
</tileset>
"""

FAKE_TMX = """<?xml version="1.0" encoding="UTF-8"?>
<map version="1.10" tiledversion="1.10.2" orientation="orthogonal"
     renderorder="right-down" width="20" height="20" tilewidth="16"
     tileheight="16" infinite="0" nextlayerid="3" nextobjectid="1">
  <tileset firstgid="1" source="trees.tsx"/>
  <tileset firstgid="100" source="rocks.tsx"/>
  <layer id="1" name="ground" width="20" height="20">
    <data encoding="csv">
{rows}
</data>
  </layer>
  <objectgroup id="2" name="flora"/>
</map>
"""


def _ground_csv(w: int, h: int) -> str:
    return "\n".join(",".join("1" for _ in range(w)) + "," for _ in range(h))


def _seed_db(db_path: Path) -> None:
    """Two packs, each with one prop. Trees -> category='tree'; Rocks ->
    category='rock'. Both use variant='composite'."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_DDL)
        conn.execute(
            "INSERT INTO tilesets_auto(pack_name, tileset_uid, name, "
            "source_path, image_path, tile_count, columns, "
            "tile_width, tile_height) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("fakepack", "fakepack::trees", "trees",
             "trees.tsx", "trees.png", 4, 2, 16, 16),
        )
        conn.execute(
            "INSERT INTO tilesets_auto(pack_name, tileset_uid, name, "
            "source_path, image_path, tile_count, columns, "
            "tile_width, tile_height) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("fakepack", "fakepack::rocks", "rocks",
             "rocks.tsx", "rocks.png", 4, 2, 16, 16),
        )
        # oak
        conn.execute(
            "INSERT INTO props_auto(pack_name, prop_uid, tileset_uid, "
            "tileset, local_id, image_path, category, variant, "
            "size_w, size_h) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("fakepack", "fakepack::trees::oak", "fakepack::trees",
             "trees", 0, "trees.png", "tree", "composite", 16, 32),
        )
        # boulder
        conn.execute(
            "INSERT INTO props_auto(pack_name, prop_uid, tileset_uid, "
            "tileset, local_id, image_path, category, variant, "
            "size_w, size_h) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("fakepack", "fakepack::rocks::boulder", "fakepack::rocks",
             "rocks", 0, "rocks.png", "rock", "composite", 16, 16),
        )
        conn.commit()
    finally:
        conn.close()


def _write_fake_tmx(path: Path) -> None:
    (path.parent / "trees.tsx").write_text(FAKE_TSX_TREES, encoding="utf-8")
    (path.parent / "rocks.tsx").write_text(FAKE_TSX_ROCKS, encoding="utf-8")
    (path.parent / "trees.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (path.parent / "rocks.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    path.write_text(FAKE_TMX.format(rows=_ground_csv(20, 20)),
                    encoding="utf-8")


def main() -> int:  # noqa: C901 — plenty of small cases
    ok = True
    db_path = Path(os.environ["TILESMITH_DB_PATH"])
    _seed_db(db_path)

    td = Path(tempfile.mkdtemp(prefix="tilesmith-addrm-tmx-"))
    tmx = td / "fake.tmx"
    _write_fake_tmx(tmx)

    # --- A: _lookup_prop_by_uid ------------------------------------
    print("[case A: _lookup_prop_by_uid]")
    oak = mcp._lookup_prop_by_uid("fakepack::trees::oak")
    none = mcp._lookup_prop_by_uid("fakepack::trees::ghost")
    checks_a = {
        "oak found":          oak is not None,
        "oak.key == trees__0": (oak or {}).get("key") == "trees__0",
        "oak.size_w == 16":   int((oak or {}).get("size_w", 0)) == 16,
        "oak.size_h == 32":   int((oak or {}).get("size_h", 0)) == 32,
        "oak.category tree":  (oak or {}).get("category") == "tree",
        "unknown uid -> None": none is None,
    }
    for k, v in checks_a.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- B: _gid_to_stem_local -------------------------------------
    print("\n[case B: _gid_to_stem_local]")
    root = ET.parse(tmx).getroot()
    refs = mcp._tmx_tileset_map(root)
    b_checks = {
        "gid 1 -> ('trees', 0)":  mcp._gid_to_stem_local(refs, 1)   == ("trees", 0),
        "gid 2 -> ('trees', 1)":  mcp._gid_to_stem_local(refs, 2)   == ("trees", 1),
        "gid 100 -> ('rocks', 0)": mcp._gid_to_stem_local(refs, 100) == ("rocks", 0),
        "gid 101 -> ('rocks', 1)": mcp._gid_to_stem_local(refs, 101) == ("rocks", 1),
        "gid 0 -> None":          mcp._gid_to_stem_local(refs, 0)   is None,
    }
    for k, v in b_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- C: tool_add_object direct path ----------------------------
    print("\n[case C: tool_add_object direct]")
    res = mcp.tool_add_object(
        tmx_path=str(tmx), layer="flora",
        prop_uid="fakepack::trees::oak",
        x=3, y=5, port=3099,
    )
    print(f"  result: {res.get('object')}")
    checks_c = {
        "no error":                  "error" not in res,
        "via direct":                res.get("via") == "direct",
        "prop_uid echoed":           res.get("prop_uid") == "fakepack::trees::oak",
        "tile_xy == {3, 5}":         res.get("tile_xy") == {"x": 3, "y": 5},
        "object.x == 48 (3*16)":     float(res["object"]["x"]) == 48.0,
        "object.y == 96 ((5+1)*16)": float(res["object"]["y"]) == 96.0,
        "object.width == 16 (DB)":   float(res["object"]["width"]) == 16.0,
        "object.height == 32 (DB)":  float(res["object"]["height"]) == 32.0,
        "object.gid == 1 (oak)":     int(res["object"]["gid"]) == 1,
    }
    for k, v in checks_c.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # Add a rock too so we have mixed-category objects for case E.
    rock_res = mcp.tool_add_object(
        tmx_path=str(tmx), layer="flora",
        prop_uid="fakepack::rocks::boulder",
        x=4, y=5, port=3099,
    )
    assert "error" not in rock_res, rock_res
    # Extra oak outside the remove region — should survive case D.
    far_oak = mcp.tool_add_object(
        tmx_path=str(tmx), layer="flora",
        prop_uid="fakepack::trees::oak",
        x=15, y=15, port=3099,
    )
    assert "error" not in far_oak, far_oak
    # Count before remove
    root1 = ET.parse(tmx).getroot()
    og = root1.find("objectgroup")
    before_count = len(og.findall("object"))
    print(f"  pre-remove object count: {before_count}")
    assert before_count == 3, before_count

    # --- D: tool_remove_objects no filter --------------------------
    print("\n[case D: tool_remove_objects no filter]")
    rres = mcp.tool_remove_objects(
        tmx_path=str(tmx), layer="flora",
        region={"x0": 0, "y0": 0, "x1": 10, "y1": 10},
        port=3099,
    )
    print(f"  removed={rres.get('removed')} "
          f"remaining={rres.get('remaining_in_layer')}")
    checks_d = {
        "no error":                  "error" not in rres,
        "via direct":                rres.get("via") == "direct",
        "removed == 2 (oak+rock)":   rres.get("removed") == 2,
        "remaining == 1 (far oak)":  rres.get("remaining_in_layer") == 1,
        "matched_but_skipped == 0":  rres.get("matched_but_skipped") == 0,
    }
    # Verify the survivor is the far oak at (15,15)
    root2 = ET.parse(tmx).getroot()
    survivors = root2.find("objectgroup").findall("object")
    checks_d["exactly 1 survivor in TMX"] = len(survivors) == 1
    if survivors:
        # object anchored bottom-left at (15*16, 16*16) = (240, 256)
        sx = float(survivors[0].get("x"))
        sy = float(survivors[0].get("y"))
        checks_d["survivor pos == (240, 256)"] = (sx, sy) == (240.0, 256.0)
    for k, v in checks_d.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- E: tool_remove_objects with category filter ---------------
    print("\n[case E: remove_objects category filter]")
    # Reset: populate region again with mixed objects.
    mcp.tool_add_object(str(tmx), "flora", "fakepack::trees::oak",
                        2, 2, port=3099)
    mcp.tool_add_object(str(tmx), "flora", "fakepack::rocks::boulder",
                        3, 3, port=3099)
    mcp.tool_add_object(str(tmx), "flora", "fakepack::trees::oak",
                        4, 4, port=3099)
    # Now region {0..5, 0..5} has: 2 oaks + 1 rock (+ no far oak).
    eres = mcp.tool_remove_objects(
        tmx_path=str(tmx), layer="flora",
        region={"x0": 0, "y0": 0, "x1": 5, "y1": 5},
        category="tree",
        port=3099,
    )
    print(f"  removed={eres.get('removed')} "
          f"skipped={eres.get('matched_but_skipped')}")
    checks_e = {
        "no error":                 "error" not in eres,
        "removed == 2 (only trees)": eres.get("removed") == 2,
        "1 rock matched region but skipped":
            eres.get("matched_but_skipped") == 1,
    }
    # Verify rock still in objectgroup
    root3 = ET.parse(tmx).getroot()
    remaining_gids = {int(o.get("gid"))
                      for o in root3.find("objectgroup").findall("object")}
    checks_e["rock gid=100 still present"] = 100 in remaining_gids
    for k, v in checks_e.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- F: apply_object_remove error paths ------------------------
    print("\n[case F: apply_object_remove error paths]")
    try:
        apply_object_remove(tmx, "nonexistent", [1])
        raised = False
    except ValueError as e:
        raised = "nonexistent" in str(e)
    if raised:
        print("  OK    missing group -> ValueError")
    else:
        print("  FAIL  missing group should raise ValueError")
        ok = False

    rres2 = apply_object_remove(tmx, "flora", [99999, 99998])
    checks_f = {
        "unknown ids -> removed == 0": rres2.get("removed") == 0,
        "missing_ids populated":
            sorted(rres2.get("missing_ids") or []) == [99998, 99999],
        "remaining_in_layer sane":
            isinstance(rres2.get("remaining_in_layer"), int),
    }
    for k, v in checks_f.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- G: TOOL_DEFS registration ---------------------------------
    print("\n[case G: TOOL_DEFS registration]")
    tools = [t[0] for t in mcp.TOOL_DEFS]
    g_checks = {
        "add_object registered":     "add_object" in tools,
        "remove_objects registered": "remove_objects" in tools,
        "place_props registered":    "place_props" in tools,
        "tool count >= 30 (28 + add_object + remove_objects; grows in v0.8.1+)":
            len(tools) >= 30,
    }
    print(f"  total: {len(tools)}")
    for k, v in g_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
