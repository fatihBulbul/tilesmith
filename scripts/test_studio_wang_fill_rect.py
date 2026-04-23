"""
Phase 4.1 e2e: wang-aware rectangle / selection fill.

Exercises the two new paths introduced alongside v0.6.2:
  - HTTP POST /wang/fill_rect with explicit coords.
  - HTTP POST /wang/fill_rect with use_selection=true after storing a
    selection via POST /selection.
  - MCP tool_wang_fill_rect (via bridge).
  - MCP tool_wang_fill_selection (via bridge, uses stored selection).

The bridge's wang_fill_rect delegates to wang_paint, so the core
resolver is already covered by test_studio_wang.py. Here we focus on
the rect-expansion and selection-plumbing paths.

Run:
    python3 scripts/test_studio_wang_fill_rect.py
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
SCRATCH_TMX = ROOT / "output" / "rich-80-wang-fill.tmx"
PORT = 3032
URL = f"http://127.0.0.1:{PORT}/"
DB = ROOT / "data" / "tiles.db"

WANGSET_UID = ("ERW - Grass Land 2.0 v1.9::"
               "Tileset-Terrain-new grass::dirt")


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
    """local_ids of tiles with (c_nw,c_ne,c_sw,c_se)=(1,1,1,1)."""
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


def interior_local_ids(state: dict, layer_name: str,
                       xa: int, ya: int, xb: int, yb: int) -> list[int]:
    lay = next(L for L in state["layers"] if L["name"] == layer_name)
    out: list[int] = []
    for y in range(ya, yb + 1):
        for x in range(xa, xb + 1):
            k = lay["data"][y][x]
            if k is None:
                out.append(-1)
                continue
            suffix = k.rsplit("__", 1)[-1]
            out.append(int(suffix))
    return out


def run_scenario() -> dict:
    out: dict = {}

    state0 = http_get("state")
    layer_name = state0["layers"][0]["name"]
    out["layer"] = layer_name

    # ---- 1. Explicit-rect fill: 5x5 block at (20..24, 20..24) ----
    mtime0 = SCRATCH_TMX.stat().st_mtime
    status, resp = http_post("wang/fill_rect", {
        "wangset_uid": WANGSET_UID,
        "color": 1,
        "x0": 20, "y0": 20, "x1": 24, "y1": 24,
    })
    out["rect_status"] = status
    out["rect_wang_meta"] = resp.get("wang")
    out["rect_meta"] = resp.get("rect")
    # 3x3 neighborhood around 5x5 stroke = 7x7 = 49 broadcast cells
    out["rect_broadcast_cells"] = len(resp.get("cells", []))

    time.sleep(0.2)
    mtime1 = SCRATCH_TMX.stat().st_mtime
    out["rect_tmx_mutated"] = mtime1 > mtime0

    state1 = http_get("state")
    pure = pure_wang_tile_local_ids()
    out["pure_set_size"] = len(pure)
    # Interior of a 5x5 stroke: 3x3 at (21..23, 21..23) must be pure-wang.
    rect_interior = interior_local_ids(state1, layer_name, 21, 21, 23, 23)
    out["rect_interior_local_ids"] = rect_interior
    out["rect_interior_all_pure"] = all(lid in pure for lid in rect_interior)

    # ---- 2. Rect with reversed coords (x0>x1, y0>y1) must be normalized ----
    status_rev, _ = http_post("wang/fill_rect", {
        "wangset_uid": WANGSET_UID,
        "color": 1,
        "x0": 34, "y0": 34, "x1": 30, "y1": 30,
    })
    out["reversed_rect_status"] = status_rev
    time.sleep(0.2)
    state2 = http_get("state")
    # Interior (31..33, 31..33) must again be pure-wang
    rev_interior = interior_local_ids(state2, layer_name, 31, 31, 33, 33)
    out["reversed_interior_all_pure"] = all(lid in pure for lid in rev_interior)

    # ---- 3. Missing wangset_uid → 400 ----
    bad_status, _ = http_post("wang/fill_rect", {
        "x0": 0, "y0": 0, "x1": 1, "y1": 1,
    })
    out["missing_wangset_400"] = bad_status == 400

    # ---- 4. use_selection=true when no selection stored → 400 ----
    no_sel_status, _ = http_post("wang/fill_rect", {
        "wangset_uid": WANGSET_UID,
        "use_selection": True,
    })
    out["no_selection_400"] = no_sel_status == 400

    # ---- 5. Store a selection, then fill with use_selection=true ----
    sel_rect = {
        "layer": layer_name,
        "x0": 40, "y0": 40, "x1": 44, "y1": 44,
    }
    sel_status, _ = http_post("selection", {"selection": sel_rect})
    out["selection_post_status"] = sel_status
    # Sanity: /selection round-trips
    out["selection_roundtrip"] = http_get("selection").get(
        "selection") == sel_rect

    mtime2 = SCRATCH_TMX.stat().st_mtime
    status_sel, resp_sel = http_post("wang/fill_rect", {
        "wangset_uid": WANGSET_UID,
        "color": 1,
        "use_selection": True,
    })
    out["selection_fill_status"] = status_sel
    out["selection_fill_rect"] = resp_sel.get("rect")
    out["selection_fill_wang_layer"] = resp_sel.get("wang", {}).get(
        "cells_touched")
    time.sleep(0.2)
    mtime3 = SCRATCH_TMX.stat().st_mtime
    out["selection_fill_tmx_mutated"] = mtime3 > mtime2

    state3 = http_get("state")
    sel_interior = interior_local_ids(state3, layer_name, 41, 41, 43, 43)
    out["selection_interior_local_ids"] = sel_interior
    out["selection_interior_all_pure"] = all(
        lid in pure for lid in sel_interior)

    # ---- 6. MCP tool_wang_fill_rect (via bridge) ----
    sys.path.insert(0, str(ROOT / "mcp_server"))
    import server as mcp  # type: ignore
    res_mcp_rect = mcp.tool_wang_fill_rect(
        wangset_uid=WANGSET_UID, color=1,
        x0=50, y0=50, x1=54, y1=54,
        port=PORT,
    )
    out["mcp_rect_via"] = res_mcp_rect.get("via")
    out["mcp_rect_error"] = res_mcp_rect.get("error")
    out["mcp_rect_wang_touched"] = res_mcp_rect.get("wang", {}).get(
        "cells_touched") if not res_mcp_rect.get("error") else None
    time.sleep(0.2)
    state4 = http_get("state")
    mcp_rect_interior = interior_local_ids(state4, layer_name, 51, 51, 53, 53)
    out["mcp_rect_interior_all_pure"] = all(
        lid in pure for lid in mcp_rect_interior)

    # ---- 7. MCP tool_wang_fill_selection ----
    # Store a new selection, then invoke.
    sel2 = {
        "layer": layer_name,
        "x0": 60, "y0": 60, "x1": 64, "y1": 64,
    }
    http_post("selection", {"selection": sel2})
    res_mcp_sel = mcp.tool_wang_fill_selection(
        wangset_uid=WANGSET_UID, color=1, port=PORT,
    )
    out["mcp_sel_via"] = res_mcp_sel.get("via")
    out["mcp_sel_error"] = res_mcp_sel.get("error")
    out["mcp_sel_rect"] = res_mcp_sel.get("rect")
    time.sleep(0.2)
    state5 = http_get("state")
    mcp_sel_interior = interior_local_ids(state5, layer_name, 61, 61, 63, 63)
    out["mcp_sel_interior_all_pure"] = all(
        lid in pure for lid in mcp_sel_interior)

    # ---- 8. Erase path: fill_rect with erase=true empties the rect ----
    status_erase, _ = http_post("wang/fill_rect", {
        "wangset_uid": WANGSET_UID,
        "color": 1,
        "x0": 20, "y0": 20, "x1": 24, "y1": 24,
        "erase": True,
    })
    out["erase_status"] = status_erase
    time.sleep(0.2)
    state6 = http_get("state")
    lay6 = next(L for L in state6["layers"] if L["name"] == layer_name)
    # After full-rect erase, interior cells must be None.
    erased_interior = [lay6["data"][y][x]
                       for y in range(21, 24) for x in range(21, 24)]
    out["erase_interior_all_none"] = all(v is None for v in erased_interior)

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
        # Explicit rect
        "fill_rect HTTP 200": res["rect_status"] == 200,
        "fill_rect wang meta present": res["rect_wang_meta"] is not None,
        "fill_rect rect meta present": res["rect_meta"] == {
            "x0": 20, "y0": 20, "x1": 24, "y1": 24},
        "fill_rect broadcast 7x7 cells":
            res["rect_broadcast_cells"] == 49,
        "fill_rect TMX mutated": res["rect_tmx_mutated"],
        "fill_rect interior all pure-wang":
            res["rect_interior_all_pure"],
        # Reversed coords normalize
        "reversed rect HTTP 200": res["reversed_rect_status"] == 200,
        "reversed rect interior all pure-wang":
            res["reversed_interior_all_pure"],
        # Error paths
        "missing wangset_uid → 400": res["missing_wangset_400"],
        "use_selection without stored selection → 400":
            res["no_selection_400"],
        # Selection plumbing
        "selection POST 200": res["selection_post_status"] == 200,
        "selection round-trips via GET": res["selection_roundtrip"],
        "selection fill HTTP 200":
            res["selection_fill_status"] == 200,
        "selection fill rect metadata matches stored sel":
            res["selection_fill_rect"] == {
                "x0": 40, "y0": 40, "x1": 44, "y1": 44},
        "selection fill TMX mutated":
            res["selection_fill_tmx_mutated"],
        "selection fill interior all pure-wang":
            res["selection_interior_all_pure"],
        # MCP tools
        "MCP tool_wang_fill_rect via=bridge":
            res["mcp_rect_via"] == "bridge" and not res["mcp_rect_error"],
        "MCP fill_rect interior all pure-wang":
            res["mcp_rect_interior_all_pure"],
        "MCP tool_wang_fill_selection via=bridge":
            res["mcp_sel_via"] == "bridge" and not res["mcp_sel_error"],
        "MCP fill_selection rect == stored":
            res["mcp_sel_rect"] == {
                "x0": 60, "y0": 60, "x1": 64, "y1": 64},
        "MCP fill_selection interior all pure-wang":
            res["mcp_sel_interior_all_pure"],
        # Erase
        "erase fill_rect HTTP 200": res["erase_status"] == 200,
        "erased interior all None": res["erase_interior_all_none"],
    }
    print("\n[assertions]")
    ok = True
    for k, v in checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    if ok:
        print("\n[OK] all assertions passed")
        return 0
    else:
        print("\n[FAIL] some assertions failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
