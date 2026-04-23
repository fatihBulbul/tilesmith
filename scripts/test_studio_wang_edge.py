"""
v0.7.1 e2e: edge-type wang resolver against the real indexed DB.

Uses the `half-sized wall` edge wangset from the ERW Grass Land 2.0
v1.9 pack (`Tileset-Terrain-new grass`) — the same tileset the
rich-80.tmx fixture already references — so the bridge can paint real
edge-typed tiles.

Scope:
  * POST /wang/paint on an edge wangset: verify affected neighborhood
    is the 5-cell plus shape (self + N/E/S/W), NOT the 8-cell corner
    ring.
  * Center cell after a point paint with color 1 should resolve to lid
    818 (the only (1,1,1,1) full-edge tile in this wangset).
  * N/S/E/W neighbors only get the single-edge variant.
  * POST /wang/fill_rect should tile a 3x3 rect with the full-edge
    tile in its inner cells.
  * Erase round-trips: edges cleared, tiles become None.
  * MCP wang_paint tool via bridge works against an edge wangset.

Run:
    python3 scripts/test_studio_wang_edge.py
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "studio" / "bridge" / "server.py"
SRC_TMX = ROOT / "output" / "rich-80.tmx"
SCRATCH_TMX = ROOT / "output" / "rich-80-wang-edge.tmx"
PORT = 3036
URL = f"http://127.0.0.1:{PORT}/"
DB = ROOT / "data" / "tiles.db"

WANGSET_UID = ("ERW - Grass Land 2.0 v1.9::"
               "Tileset-Terrain-new grass::half-sized wall")
FULL_EDGE_LID_COLOR1 = 818  # the unique (n=1,e=1,s=1,w=1) tile


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


def single_edge_lids() -> dict[str, set[int]]:
    """(n,e,s,w) partial-edge tiles in this wangset, keyed by edge
    name for easy neighbor-validation."""
    out: dict[str, set[int]] = {
        "N_only": set(), "E_only": set(), "S_only": set(), "W_only": set(),
    }
    with sqlite3.connect(str(DB)) as conn:
        cur = conn.execute(
            """
            SELECT c_n, c_e, c_s, c_w, t.local_id
              FROM wang_tiles wt
              JOIN tiles t ON t.tile_uid = wt.tile_uid
             WHERE wt.wangset_uid = ?
            """,
            (WANGSET_UID,),
        )
        for n, e, s, w, lid in cur.fetchall():
            if (n, e, s, w) == (1, 0, 0, 0):
                out["N_only"].add(lid)
            elif (n, e, s, w) == (0, 1, 0, 0):
                out["E_only"].add(lid)
            elif (n, e, s, w) == (0, 0, 1, 0):
                out["S_only"].add(lid)
            elif (n, e, s, w) == (0, 0, 0, 1):
                out["W_only"].add(lid)
    return out


def cell_lid(state: dict, layer_name: str, x: int, y: int) -> int | None:
    lay = next(L for L in state["layers"] if L["name"] == layer_name)
    k = lay["data"][y][x]
    if k is None:
        return None
    return int(k.rsplit("__", 1)[-1])


def run_scenario() -> dict:
    out: dict = {}

    state0 = http_get("state")
    # Pick a tile-layer for painting
    layer_name = next(
        L["name"] for L in state0["layers"] if L.get("type", "tile") == "tile"
    )
    out["layer"] = layer_name

    # ---- 1. Single point paint at (30, 30) with edge wangset ----
    # Expect: 5-cell plus-shape touched. Center → lid 818. N neighbor
    # (30,29) has only its S edge set, so it should resolve to a
    # (0,0,1,0) S-only tile.
    status, resp = http_post("wang/paint", {
        "layer": layer_name,
        "wangset_uid": WANGSET_UID,
        "color": 1,
        "cells": [{"x": 30, "y": 30}],
    })
    out["paint_status"] = status
    out["paint_wang_meta"] = resp.get("wang")
    # Broadcast cells list should contain exactly the 5 plus-neighborhood
    # cells (no diagonals).
    touched = {(c["x"], c["y"]) for c in resp.get("cells", [])}
    out["paint_touched"] = sorted(touched)
    expected_touched = {(30, 30), (30, 29), (31, 30), (30, 31), (29, 30)}
    out["paint_touched_is_plus"] = touched == expected_touched
    out["paint_no_diagonals"] = not any(
        (x, y) in touched
        for (x, y) in [(29, 29), (31, 29), (29, 31), (31, 31)]
    )

    time.sleep(0.15)
    state1 = http_get("state")
    out["center_lid"] = cell_lid(state1, layer_name, 30, 30)
    out["n_neighbor_lid"] = cell_lid(state1, layer_name, 30, 29)
    out["s_neighbor_lid"] = cell_lid(state1, layer_name, 30, 31)
    out["e_neighbor_lid"] = cell_lid(state1, layer_name, 31, 30)
    out["w_neighbor_lid"] = cell_lid(state1, layer_name, 29, 30)

    partials = single_edge_lids()
    out["partials_inventory"] = {k: sorted(v) for k, v in partials.items()}
    out["center_is_full_edge"] = (
        out["center_lid"] == FULL_EDGE_LID_COLOR1
    )
    out["n_neighbor_is_s_only"] = (
        out["n_neighbor_lid"] in partials["S_only"]
    )
    out["s_neighbor_is_n_only"] = (
        out["s_neighbor_lid"] in partials["N_only"]
    )
    out["e_neighbor_is_w_only"] = (
        out["e_neighbor_lid"] in partials["W_only"]
    )
    out["w_neighbor_is_e_only"] = (
        out["w_neighbor_lid"] in partials["E_only"]
    )

    # ---- 2. Rect fill a 3x3 block: interior cell should be full-edge ----
    status_r, resp_r = http_post("wang/fill_rect", {
        "layer": layer_name,
        "wangset_uid": WANGSET_UID,
        "color": 1,
        "x0": 40, "y0": 40, "x1": 42, "y1": 42,
    })
    out["rect_status"] = status_r
    out["rect_wang_touched"] = resp_r.get("wang", {}).get("cells_touched")

    time.sleep(0.15)
    state2 = http_get("state")
    # The center of the 3x3 rect (41,41) has all 4 edges painted → full.
    out["rect_center_lid"] = cell_lid(state2, layer_name, 41, 41)
    out["rect_center_is_full_edge"] = (
        out["rect_center_lid"] == FULL_EDGE_LID_COLOR1
    )

    # ---- 3. MCP wang_paint via bridge against an edge wangset ----
    sys.path.insert(0, str(ROOT / "mcp_server"))
    import server as mcp  # type: ignore
    res_mcp = mcp.tool_wang_paint(
        wangset_uid=WANGSET_UID, color=1,
        cells=[{"x": 50, "y": 50}],
        layer=layer_name, port=PORT,
    )
    out["mcp_via"] = res_mcp.get("via")
    out["mcp_error"] = res_mcp.get("error")
    out["mcp_wang_touched"] = res_mcp.get("wang", {}).get("cells_touched") \
        if not res_mcp.get("error") else None
    time.sleep(0.15)
    state3 = http_get("state")
    out["mcp_center_lid"] = cell_lid(state3, layer_name, 50, 50)
    out["mcp_center_is_full_edge"] = (
        out["mcp_center_lid"] == FULL_EDGE_LID_COLOR1
    )

    # ---- 4. Erase: (30,30) point erase clears the 5-cell neighborhood ----
    status_e, resp_e = http_post("wang/paint", {
        "layer": layer_name,
        "wangset_uid": WANGSET_UID,
        "color": 1,
        "cells": [{"x": 30, "y": 30}],
        "erase": True,
    })
    out["erase_status"] = status_e
    time.sleep(0.15)
    state4 = http_get("state")
    lid_center_after = cell_lid(state4, layer_name, 30, 30)
    lid_n_after = cell_lid(state4, layer_name, 30, 29)
    lid_s_after = cell_lid(state4, layer_name, 30, 31)
    lid_e_after = cell_lid(state4, layer_name, 31, 30)
    lid_w_after = cell_lid(state4, layer_name, 29, 30)
    out["erase_center_cleared"] = lid_center_after is None
    out["erase_neighbors_cleared"] = (
        lid_n_after is None and lid_s_after is None
        and lid_e_after is None and lid_w_after is None
    )

    return out


def main() -> int:
    shutil.copy2(SRC_TMX, SCRATCH_TMX)
    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE),
         "--tmx", str(SCRATCH_TMX), "--port", str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        health = wait_health()
        print("[bridge]", health)
        res = run_scenario()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("\n[results]")
    print(json.dumps(res, indent=2, default=str))

    checks = {
        "wang/paint HTTP 200": res["paint_status"] == 200,
        "paint wang meta present": res["paint_wang_meta"] is not None,
        "5-cell plus-shape neighborhood (self + N/E/S/W)":
            res["paint_touched_is_plus"],
        "diagonals NOT touched by edge paint":
            res["paint_no_diagonals"],
        "center cell resolves to full-edge tile (lid 818)":
            res["center_is_full_edge"],
        "N neighbor is S-only variant":
            res["n_neighbor_is_s_only"],
        "S neighbor is N-only variant":
            res["s_neighbor_is_n_only"],
        "E neighbor is W-only variant":
            res["e_neighbor_is_w_only"],
        "W neighbor is E-only variant":
            res["w_neighbor_is_e_only"],
        "fill_rect HTTP 200": res["rect_status"] == 200,
        "rect center (41,41) is full-edge tile":
            res["rect_center_is_full_edge"],
        "MCP tool_wang_paint via=bridge":
            res["mcp_via"] == "bridge" and not res["mcp_error"],
        "MCP center cell is full-edge tile":
            res["mcp_center_is_full_edge"],
        "erase HTTP 200": res["erase_status"] == 200,
        "erase cleared center cell": res["erase_center_cleared"],
        "erase cleared all 4 neighbors":
            res["erase_neighbors_cleared"],
    }
    print("\n[assertions]")
    ok = True
    for k, v in checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    if ok:
        print("\n[OK] all edge-wang assertions passed")
        return 0
    else:
        print("\n[FAIL] some assertions failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
