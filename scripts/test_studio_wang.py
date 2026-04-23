"""
Phase 4 e2e: wang-aware autotile paint.

Flow:
  1. Copy rich-80.tmx (references original TSX files with wang index).
  2. Start bridge on port 3029.
  3. GET /wang/sets — verify 'dirt' wangset discovered with 5 colors.
  4. POST /wang/paint: paint 4x4 area (10..13, 10..13) with color=1
     using the 'dirt' wangset.
  5. Re-fetch /state; verify:
       - Interior cells (11..12, 11..12) resolved to the same pure-corner
         tile (all 4 corners = 1 in DB).
       - Exactly 16 cells touched in the paint stroke.
  6. Verify TMX file mtime bumped (atomic write path).
  7. Also exercise the MCP tool (`tool_wang_paint`) via bridge.
  8. Direct-fallback test: stop bridge, call tool with tmx_path, confirm
     it still mutates the TMX.

Run:
    python3 scripts/test_studio_wang.py
"""
from __future__ import annotations
import asyncio
import json
import shutil
import subprocess
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "studio" / "bridge" / "server.py"
# rich-80.tmx refers to original pack TSX files which ARE in the DB.
SRC_TMX = ROOT / "output" / "rich-80.tmx"
SCRATCH_TMX = ROOT / "output" / "rich-80-wang.tmx"
PORT = 3029
URL = f"http://127.0.0.1:{PORT}/"
DB = ROOT / "data" / "tiles.db"

WANGSET_UID = ("ERW - Grass Land 2.0 v1.9::"
               "Tileset-Terrain-new grass::dirt")


def prep() -> None:
    shutil.copy2(SRC_TMX, SCRATCH_TMX)


def wait_health(timeout=20.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(URL + "health", timeout=2) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("bridge not healthy")


def http_get(path: str) -> dict:
    with urllib.request.urlopen(URL + path, timeout=5) as r:
        return json.loads(r.read())


def http_post(path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        URL + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()}


def pure_wang_tile_local_ids() -> set[int]:
    """Return the local_ids of tiles where (c_nw, c_ne, c_sw, c_se) = (1,1,1,1)
    for WANGSET_UID. Interior cells should all pick from this set."""
    with sqlite3.connect(str(DB)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT t.local_id FROM wang_tiles wt
              JOIN tiles t ON t.tile_uid = wt.tile_uid
             WHERE wt.wangset_uid = ?
               AND wt.c_nw = 1 AND wt.c_ne = 1
               AND wt.c_sw = 1 AND wt.c_se = 1
            """,
            (WANGSET_UID,),
        )
        return {r["local_id"] for r in cur.fetchall()}


def run_scenario() -> dict:
    out: dict = {}

    # 1. Wang sets discovery
    wset = http_get("wang/sets")
    out["wang_sets_count"] = len(wset["sets"])
    out["tileset_stems"] = wset.get("tileset_stems", [])
    dirt = next((s for s in wset["sets"]
                 if s["wangset_uid"] == WANGSET_UID), None)
    out["dirt_present"] = dirt is not None
    if dirt:
        out["dirt_color_count"] = dirt["color_count"]
        out["dirt_colors"] = dirt["colors"]

    # 2. Snapshot state + TMX mtime BEFORE paint
    before_mtime = SCRATCH_TMX.stat().st_mtime
    state_before = http_get("state")
    layer_name = state_before["layers"][0]["name"]
    out["layer"] = layer_name

    # 3. Issue the wang paint — 4x4 block at (10..13, 10..13)
    paint_cells = [{"x": x, "y": y}
                   for y in range(10, 14) for x in range(10, 14)]
    status, paint_resp = http_post("wang/paint", {
        "wangset_uid": WANGSET_UID,
        "color": 1,
        "cells": paint_cells,
        # layer omitted → bridge picks layers[0]
    })
    out["paint_status"] = status
    out["paint_resp_keys"] = sorted(paint_resp.keys())
    out["paint_wang_meta"] = paint_resp.get("wang")

    # 4. After paint — check TMX mutated, state updated
    # Give bridge a moment for the disk write
    time.sleep(0.2)
    after_mtime = SCRATCH_TMX.stat().st_mtime
    out["tmx_mutated"] = after_mtime > before_mtime

    state_after = http_get("state")
    lay = next(L for L in state_after["layers"]
               if L["name"] == layer_name)
    # Expected: 16 interior cells + 16 border cells = 36 cells affected total
    # (3x3 neighborhood union around a 4x4 stroke = 6x6 = 36 cells).
    # We only care about the "definitely interior" 2x2 center: (11,11)..(12,12).
    interior_local_ids: list[int] = []
    for y in range(11, 13):
        for x in range(11, 13):
            k = lay["data"][y][x]
            if k is None:
                interior_local_ids.append(-1)
                continue
            # "Tileset-Terrain-new_grass__1887" style
            suffix = k.rsplit("__", 1)[-1]
            interior_local_ids.append(int(suffix))
    out["interior_local_ids"] = interior_local_ids

    pure_set = pure_wang_tile_local_ids()
    out["pure_set_size"] = len(pure_set)
    out["interior_all_pure"] = all(
        lid in pure_set for lid in interior_local_ids
    )

    # 5. Cells far outside the paint region must NOT have been touched
    #    (same as before).
    bef_cell = state_before["layers"][0]["data"][40][40]
    aft_cell = state_after["layers"][0]["data"][40][40]
    out["untouched_cell_stable"] = bef_cell == aft_cell

    # 6. Verify the broadcast paint patch has the expected shape
    out["broadcast_cells"] = len(paint_resp.get("cells", []))
    # 3x3 neighborhood around 4x4 stroke = 6x6 = 36
    out["broadcast_matches_6x6"] = out["broadcast_cells"] == 36

    # 7. Also exercise the MCP tool via bridge
    sys.path.insert(0, str(ROOT / "mcp_server"))
    import server as mcp  # type: ignore
    tool_wset = mcp.tool_list_wangsets_for_tmx(port=PORT)
    out["mcp_list_via"] = tool_wset.get("via")
    out["mcp_list_count"] = len(tool_wset.get("sets", []))

    # Erase the 4x4 block via MCP tool to sanity-check erase path
    erase_cells = [{"x": x, "y": y}
                   for y in range(10, 14) for x in range(10, 14)]
    tool_erase = mcp.tool_wang_paint(
        wangset_uid=WANGSET_UID, cells=erase_cells, color=1,
        erase=True, port=PORT,
    )
    out["mcp_erase_via"] = tool_erase.get("via")
    out["mcp_erase_ok"] = tool_erase.get("ok", False) or not tool_erase.get(
        "error")

    # After erase, interior should become 0 (empty) in TMX
    time.sleep(0.2)
    state_final = http_get("state")
    lay_f = next(L for L in state_final["layers"]
                 if L["name"] == layer_name)
    out["interior_after_erase"] = [
        lay_f["data"][y][x] for y in range(11, 13) for x in range(11, 13)
    ]
    out["all_interior_erased"] = all(
        v is None for v in out["interior_after_erase"]
    )

    return out


def run_direct_fallback() -> dict:
    """Bridge is stopped for this part — MCP tool must fall back to TMX."""
    out: dict = {}
    sys.path.insert(0, str(ROOT / "mcp_server"))
    import server as mcp  # type: ignore

    mtime_before = SCRATCH_TMX.stat().st_mtime
    cells = [{"x": x, "y": y}
             for y in range(30, 32) for x in range(30, 32)]
    res = mcp.tool_wang_paint(
        wangset_uid=WANGSET_UID, cells=cells, color=1,
        tmx_path=str(SCRATCH_TMX),
        # Use a guaranteed-closed port
        port=61234,
    )
    out["direct_via"] = res.get("via")
    out["direct_ok"] = not res.get("error")
    out["direct_wang_meta"] = res.get("wang")
    out["direct_cells_applied"] = res.get("cells_applied")
    time.sleep(0.2)
    mtime_after = SCRATCH_TMX.stat().st_mtime
    out["direct_tmx_mutated"] = mtime_after > mtime_before
    return out


def main() -> int:
    prep()
    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE),
         "--tmx", str(SCRATCH_TMX), "--port", str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        health = wait_health()
        print("[bridge]", health)
        bridge_res = run_scenario()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    direct_res = run_direct_fallback()

    all_res = {"bridge": bridge_res, "direct": direct_res}
    print("\n[results]")
    print(json.dumps(all_res, indent=2, default=str))

    checks = {
        # Discovery
        "wangset catalog contains 'dirt'": bridge_res["dirt_present"],
        "dirt has 5 colors":
            bridge_res.get("dirt_color_count") == 5,
        "tileset_stems includes Tileset-Terrain-new grass":
            "Tileset-Terrain-new grass" in bridge_res.get(
                "tileset_stems", []),
        # Paint
        "paint HTTP 200":
            bridge_res["paint_status"] == 200,
        "broadcast carried 6x6 cells":
            bridge_res["broadcast_matches_6x6"],
        "wang meta present":
            bridge_res["paint_wang_meta"] is not None
            and bridge_res["paint_wang_meta"].get("wangset_uid")
                == WANGSET_UID,
        "interior cells picked from pure-wang tile set":
            bridge_res["interior_all_pure"],
        "TMX file mutated":
            bridge_res["tmx_mutated"],
        "cell outside stroke unchanged":
            bridge_res["untouched_cell_stable"],
        # MCP tool
        "MCP list_wangsets_for_tmx via=bridge":
            bridge_res["mcp_list_via"] == "bridge",
        "MCP list returned >=1 set":
            bridge_res["mcp_list_count"] >= 1,
        "MCP wang_paint erase path succeeded":
            bridge_res["mcp_erase_ok"],
        "interior cells fully erased":
            bridge_res["all_interior_erased"],
        # Direct fallback
        "direct fallback via=direct":
            direct_res["direct_via"] == "direct",
        "direct wang_paint succeeded":
            direct_res["direct_ok"],
        "direct TMX mutated":
            direct_res["direct_tmx_mutated"],
    }
    print("\n[assertions]")
    ok = True
    for k, v in checks.items():
        print(f"  {'OK ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
