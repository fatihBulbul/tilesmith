"""Unit test for v0.8.2 pagination + list_tiles + search polish.

Covers:

  A. `_paginate` backward-compat: both limit and offset None => raw list.
  B. `_paginate` shape: {items, total, limit, offset, has_more, next_offset}
     - first page has_more=True, next_offset=limit
     - last page has_more=False, next_offset=None
     - limit capped at PAGINATION_MAX_LIMIT
     - offset coerced to >=0
  C. `tool_list_tilesets` pagination round-trip over seeded DB.
  D. `tool_list_tiles` pagination + unknown tileset_uid returns empty.
  E. `tool_list_animated_props` `search` substring filter (case-insens,
     matches subject OR filename).
  F. TOOL_DEFS exposes `list_tiles`; list_tilesets schema lists limit/
     offset; tool count grew to >=32.

Run:
    python3 scripts/test_pagination.py
"""
from __future__ import annotations
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp_server"))

_TMP = Path(tempfile.mkdtemp(prefix="tilesmith-pagination-"))
os.environ["TILESMITH_DB_PATH"] = str(_TMP / "tiles.db")

import server as mcp           # noqa: E402
from scanner import SCHEMA_DDL  # noqa: E402


def _seed_db() -> None:
    db_path = Path(os.environ["TILESMITH_DB_PATH"])
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA_DDL)
        # Seed 12 tilesets across 2 packs
        for i in range(12):
            pack = "packA" if i < 6 else "packB"
            conn.execute(
                "INSERT INTO tilesets_auto(pack_name, tileset_uid, name, "
                "source_path, image_path, tile_count, columns, "
                "tile_width, tile_height) VALUES (?,?,?,?,?,?,?,?,?)",
                (pack, f"{pack}::ts{i}", f"ts{i}",
                 f"{pack}/ts{i}.tsx", f"{pack}/ts{i}.png",
                 50, 10, 16, 16),
            )
        # Seed 150 tiles in one tileset (packA::ts0)
        for lid in range(150):
            conn.execute(
                "INSERT INTO tiles_auto(pack_name, tile_uid, tileset_uid, "
                "tileset, local_id, semantic, atlas_row, atlas_col) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("packA", f"packA::ts0::{lid}", "packA::ts0", "ts0",
                 lid, "grass" if lid % 3 == 0 else None,
                 lid // 10, lid % 10),
            )
        # Seed animated props — mix subjects for search test
        specs = [
            ("packA", "fireflies", "fireflies.png", "insect",
             "fireflies", "idle"),
            ("packA", "torch_flame", "torch_flame.png", "fire",
             "torch", "burn"),
            ("packA", "campfire", "campfire_big.png", "fire",
             "campfire", "burn"),
            ("packA", "chest_gold", "chest_gold.png", "chest",
             "gold_chest", "open"),
            ("packB", "butterfly", "butterfly.png", "insect",
             "butterfly", "idle"),
        ]
        for pack, uid, fname, cat, subj, action in specs:
            conn.execute(
                "INSERT INTO animated_props_auto(pack_name, aprop_uid, "
                "filename, category, subject, action, frame_count, "
                "frame_w, frame_h) VALUES (?,?,?,?,?,?,?,?,?)",
                (pack, uid, fname, cat, subj, action, 4, 32, 32),
            )
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    ok = True
    _seed_db()

    # --- A: _paginate backward-compat ---------------------------------
    print("[case A: _paginate backward-compat]")
    raw = [{"n": i} for i in range(5)]
    out = mcp._paginate(raw, None, None)
    a_checks = {
        "raw list returned unchanged": out == raw,
        "type is list": isinstance(out, list),
    }
    for k, v in a_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- B: _paginate shape ------------------------------------------
    print("\n[case B: _paginate shape]")
    items = [{"n": i} for i in range(25)]
    first = mcp._paginate(items, 10, 0)
    last = mcp._paginate(items, 10, 20)
    capped = mcp._paginate(items, 10_000, 0)
    neg = mcp._paginate(items, 10, -5)
    b_checks = {
        "first page items len 10":   first["items"] and len(first["items"]) == 10,
        "first total == 25":         first["total"] == 25,
        "first has_more True":       first["has_more"] is True,
        "first next_offset 10":      first["next_offset"] == 10,
        "last page len 5":           len(last["items"]) == 5,
        "last has_more False":       last["has_more"] is False,
        "last next_offset None":     last["next_offset"] is None,
        "limit capped to 500":       capped["limit"] == 500,
        "negative offset coerced 0": neg["offset"] == 0,
    }
    for k, v in b_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- C: tool_list_tilesets pagination ----------------------------
    print("\n[case C: tool_list_tilesets pagination]")
    raw_all = mcp.tool_list_tilesets()
    paged = mcp.tool_list_tilesets(limit=5, offset=0)
    page2 = mcp.tool_list_tilesets(limit=5, offset=5)
    c_checks = {
        "raw returns list (no pagination)": isinstance(raw_all, list),
        "raw total 12":                      len(raw_all) == 12,
        "paged is dict":                     isinstance(paged, dict),
        "paged items 5":                     len(paged["items"]) == 5,
        "paged total 12":                    paged["total"] == 12,
        "paged has_more":                    paged["has_more"] is True,
        "paged next_offset 5":               paged["next_offset"] == 5,
        "page2 first item follows":
            page2["items"][0]["tileset_uid"] == raw_all[5]["tileset_uid"],
    }
    for k, v in c_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- D: tool_list_tiles ------------------------------------------
    print("\n[case D: tool_list_tiles]")
    t0 = mcp.tool_list_tiles(tileset_uid="packA::ts0", limit=50, offset=0)
    t_last = mcp.tool_list_tiles(tileset_uid="packA::ts0", limit=50,
                                  offset=100)
    t_none = mcp.tool_list_tiles(tileset_uid="doesnotexist::x")
    d_checks = {
        "t0 items len 50":          len(t0["items"]) == 50,
        "t0 total 150":             t0["total"] == 150,
        "t0 has_more":              t0["has_more"] is True,
        "t0 echoes tileset_uid":    t0["tileset_uid"] == "packA::ts0",
        "t0 first local_id 0":      t0["items"][0]["local_id"] == 0,
        "t_last items len 50":      len(t_last["items"]) == 50,
        "t_last has_more False":    t_last["has_more"] is False,
        "t_last next_offset None":  t_last["next_offset"] is None,
        "unknown tileset empty":    t_none["items"] == [],
        "unknown tileset total 0":  t_none["total"] == 0,
        "unknown tileset has note": "note" in t_none,
    }
    for k, v in d_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- E: animated_props search ------------------------------------
    print("\n[case E: list_animated_props search]")
    fire_results = mcp.tool_list_animated_props(search="fire")
    # Should match subject="fireflies"+category="insect", subject="campfire"+
    # cat="fire", and filename starting "torch_flame" has no 'fire' — BUT
    # filename 'campfire_big.png' + subject 'fireflies' + subject 'campfire'
    # => fireflies (subject match), campfire (subject + filename match)
    subjects = sorted(r["subject"] for r in fire_results)
    case_check = mcp.tool_list_animated_props(search="FIRE")
    # Category filter still works alongside search
    fire_cat = mcp.tool_list_animated_props(category="fire", search="fire")
    paged_search = mcp.tool_list_animated_props(search="fire",
                                                  limit=1, offset=0)
    e_checks = {
        "fire search finds fireflies + campfire":
            "fireflies" in subjects and "campfire" in subjects,
        "case-insensitive match":
            sorted(r["subject"] for r in case_check) == subjects,
        "category+search composable":
            all(r["category"] == "fire" for r in fire_cat),
        "search+pagination":
            isinstance(paged_search, dict)
            and paged_search["total"] == len(fire_results)
            and len(paged_search["items"]) == 1,
    }
    for k, v in e_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- F: TOOL_DEFS surface ----------------------------------------
    print("\n[case F: TOOL_DEFS surface]")
    tools = [t[0] for t in mcp.TOOL_DEFS]
    ts_def = next(t for t in mcp.TOOL_DEFS if t[0] == "list_tilesets")
    ap_def = next(t for t in mcp.TOOL_DEFS if t[0] == "list_animated_props")
    ts_props = ts_def[2]["properties"]
    ap_props = ap_def[2]["properties"]
    f_checks = {
        "list_tiles registered":         "list_tiles" in tools,
        "list_tilesets schema has limit": "limit" in ts_props,
        "list_tilesets schema has offset": "offset" in ts_props,
        "list_animated_props has search":  "search" in ap_props,
        "list_animated_props has limit":   "limit" in ap_props,
        "tool count >= 32":              len(tools) >= 32,
    }
    print(f"  total tools: {len(tools)}")
    for k, v in f_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
