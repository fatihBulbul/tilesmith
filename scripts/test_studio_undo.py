"""
Phase 5 e2e: undo / redo stack.

Exercises the undo/redo chokepoint in StudioState.patch_paint:
  - Regular paint → undo → redo → undo again.
  - Wang paint → undo restores the previous tile key AND the 3x3
    neighborhood update is fully reverted.
  - fill_rect is a single undo step (not N per cell).
  - New mutation after undo clears the redo stack.
  - MCP tool_studio_undo + tool_studio_redo via bridge.
  - GET /history returns stack depths that track real state.

Run:
    python3 scripts/test_studio_undo.py
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "studio" / "bridge" / "server.py"
SRC_TMX = ROOT / "output" / "rich-80.tmx"
SCRATCH_TMX = ROOT / "output" / "rich-80-undo.tmx"
PORT = 3033
URL = f"http://127.0.0.1:{PORT}/"

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


def cell_at(state: dict, layer_name: str, x: int, y: int) -> str | None:
    lay = next(L for L in state["layers"] if L["name"] == layer_name)
    return lay["data"][y][x]


def run_scenario() -> dict:
    out: dict = {}

    state0 = http_get("state")
    layer = state0["layers"][0]["name"]
    out["layer"] = layer

    hist0 = http_get("history")
    out["hist0"] = hist0
    # Starting state: no history
    out["history_starts_empty"] = (
        hist0["undo_depth"] == 0 and hist0["redo_depth"] == 0)

    # Snapshot some cells we'll mutate
    cx, cy = 5, 5
    original_c = cell_at(state0, layer, cx, cy)
    out["original_cell"] = original_c

    # ---- 1. Single-cell paint → undo restores → redo re-applies ----
    # Pick an arbitrary existing tile key from the palette.
    some_key = next(iter(state0["tiles"].keys()))
    out["paint_key"] = some_key
    status, _ = http_post("patch/tiles", {
        "layer": layer,
        "cells": [{"x": cx, "y": cy, "key": some_key}],
    })
    out["paint_status"] = status
    time.sleep(0.1)
    state1 = http_get("state")
    out["after_paint_cell"] = cell_at(state1, layer, cx, cy)

    hist1 = http_get("history")
    out["after_paint_undo_depth"] = hist1["undo_depth"]

    # Undo the paint
    status_u, resp_u = http_post("undo", {})
    out["undo_status"] = status_u
    out["undo_applied"] = resp_u.get("applied")
    out["undo_cells_applied"] = resp_u.get("cells_applied")
    time.sleep(0.1)
    state2 = http_get("state")
    out["after_undo_cell"] = cell_at(state2, layer, cx, cy)
    out["undo_restores_cell"] = (
        cell_at(state2, layer, cx, cy) == original_c)

    hist2 = http_get("history")
    out["after_undo_depths"] = (hist2["undo_depth"], hist2["redo_depth"])

    # Redo the paint
    status_r, resp_r = http_post("redo", {})
    out["redo_status"] = status_r
    out["redo_applied"] = resp_r.get("applied")
    time.sleep(0.1)
    state3 = http_get("state")
    out["after_redo_cell"] = cell_at(state3, layer, cx, cy)
    out["redo_reapplies"] = (
        cell_at(state3, layer, cx, cy) == some_key)

    hist3 = http_get("history")
    out["after_redo_depths"] = (hist3["undo_depth"], hist3["redo_depth"])

    # ---- 2. New mutation after redo → redo stack preserved; after undo
    #         + new mutation, redo stack clears ----
    # Undo once, then paint a new cell → redo stack should become empty.
    http_post("undo", {})  # returns to state before paint
    time.sleep(0.05)
    http_post("patch/tiles", {
        "layer": layer,
        "cells": [{"x": cx + 1, "y": cy, "key": some_key}],
    })
    time.sleep(0.1)
    hist4 = http_get("history")
    out["after_new_mutation_redo_depth"] = hist4["redo_depth"]
    out["redo_cleared_by_new_mutation"] = hist4["redo_depth"] == 0

    # Undo the new cell so our scratch stays clean.
    http_post("undo", {})
    time.sleep(0.05)

    # ---- 3. fill_rect = 1 undo step (not N) ----
    hist_before_rect = http_get("history")
    status_fr, resp_fr = http_post("fill", {
        "layer": layer,
        "region": {"x0": 10, "y0": 10, "x1": 13, "y1": 13},  # 4x4=16 cells
        "key": some_key,
    })
    out["fill_rect_status"] = status_fr
    time.sleep(0.1)
    hist_after_rect = http_get("history")
    # Depth bumped by exactly 1, regardless of 16 cells touched.
    out["fill_rect_one_undo_step"] = (
        hist_after_rect["undo_depth"] == hist_before_rect["undo_depth"] + 1)

    # Verify: snapshot an interior cell, undo, check it reverted.
    state_after_fr = http_get("state")
    pre_cell = cell_at(state_after_fr, layer, 11, 11)
    out["fill_rect_cell"] = pre_cell
    http_post("undo", {})
    time.sleep(0.1)
    state_after_undo_fr = http_get("state")
    post_cell = cell_at(state_after_undo_fr, layer, 11, 11)
    out["fill_rect_undo_reverts"] = post_cell != pre_cell

    # ---- 4. Wang paint → undo reverts the full 3x3-updated neighborhood ----
    # Snapshot the 6x6 neighborhood around a 4x4 stroke at (20..23, 20..23).
    pre_wang = {}
    for y in range(19, 25):
        for x in range(19, 25):
            pre_wang[(x, y)] = cell_at(state_after_undo_fr, layer, x, y)

    wang_cells = [{"x": x, "y": y}
                  for y in range(20, 24) for x in range(20, 24)]
    status_wp, resp_wp = http_post("wang/paint", {
        "wangset_uid": WANGSET_UID,
        "color": 1,
        "cells": wang_cells,
    })
    out["wang_paint_status"] = status_wp
    out["wang_paint_cells_touched"] = resp_wp.get("wang", {}).get(
        "cells_touched")
    time.sleep(0.1)

    # Depth should be +1
    hist_after_wp = http_get("history")
    out["wang_paint_undo_depth_delta"] = (
        hist_after_wp["undo_depth"] - hist_after_rect["undo_depth"] + 1)
    # Actually: we undid fill_rect above, so depth dropped, then wang_paint
    # added 1. Just check undo_depth > 0 and redo == 0.
    out["wang_paint_depth_positive"] = hist_after_wp["undo_depth"] > 0
    out["wang_paint_redo_cleared"] = hist_after_wp["redo_depth"] == 0

    # Verify neighborhood changed
    state_after_wp = http_get("state")
    diffs = 0
    for (x, y), prev in pre_wang.items():
        if cell_at(state_after_wp, layer, x, y) != prev:
            diffs += 1
    out["wang_paint_cells_changed"] = diffs
    out["wang_paint_changed_some"] = diffs > 0

    # Undo wang paint → entire neighborhood must revert
    status_wu, resp_wu = http_post("undo", {})
    out["wang_undo_status"] = status_wu
    out["wang_undo_cells"] = resp_wu.get("cells_applied")
    time.sleep(0.1)
    state_after_wu = http_get("state")
    mismatches = 0
    for (x, y), prev in pre_wang.items():
        if cell_at(state_after_wu, layer, x, y) != prev:
            mismatches += 1
    out["wang_undo_mismatches"] = mismatches
    out["wang_undo_full_revert"] = mismatches == 0

    # ---- 5. MCP tool_studio_undo + tool_studio_redo ----
    sys.path.insert(0, str(ROOT / "mcp_server"))
    import server as mcp  # type: ignore

    # Push a fresh paint so there's something to undo via MCP.
    http_post("patch/tiles", {
        "layer": layer,
        "cells": [{"x": 7, "y": 7, "key": some_key}],
    })
    time.sleep(0.05)
    state_before_mcp = http_get("state")
    mcp_pre = cell_at(state_before_mcp, layer, 7, 7)

    res_undo = mcp.tool_studio_undo(port=PORT)
    out["mcp_undo_via"] = res_undo.get("via")
    out["mcp_undo_applied"] = res_undo.get("applied")
    time.sleep(0.1)
    state_after_mcp_u = http_get("state")
    mcp_post_u = cell_at(state_after_mcp_u, layer, 7, 7)
    out["mcp_undo_reverted"] = mcp_post_u != mcp_pre

    res_redo = mcp.tool_studio_redo(port=PORT)
    out["mcp_redo_via"] = res_redo.get("via")
    out["mcp_redo_applied"] = res_redo.get("applied")
    time.sleep(0.1)
    state_after_mcp_r = http_get("state")
    mcp_post_r = cell_at(state_after_mcp_r, layer, 7, 7)
    out["mcp_redo_reapplied"] = mcp_post_r == mcp_pre

    # ---- 6. Empty-stack undo is a no-op, not an error ----
    # Drain undo stack fully.
    for _ in range(200):
        _, resp = http_post("undo", {})
        if not resp.get("applied"):
            break
    status_noop, resp_noop = http_post("undo", {})
    out["empty_undo_status"] = status_noop
    out["empty_undo_applied"] = resp_noop.get("applied")
    out["empty_undo_is_noop"] = (
        status_noop == 200 and resp_noop.get("applied") is False)

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
        "history starts empty": res["history_starts_empty"],
        "paint HTTP 200": res["paint_status"] == 200,
        "undo HTTP 200": res["undo_status"] == 200,
        "undo applied=true": res["undo_applied"] is True,
        "undo restores original cell": res["undo_restores_cell"],
        "undo moves entry to redo stack":
            res["after_undo_depths"][1] == 1,
        "redo HTTP 200": res["redo_status"] == 200,
        "redo applied=true": res["redo_applied"] is True,
        "redo re-applies paint": res["redo_reapplies"],
        "redo cleared by new mutation":
            res["redo_cleared_by_new_mutation"],
        "fill_rect = one undo step":
            res["fill_rect_one_undo_step"],
        "fill_rect undo reverts interior":
            res["fill_rect_undo_reverts"],
        "wang paint applied": res["wang_paint_changed_some"],
        "wang paint redo cleared":
            res["wang_paint_redo_cleared"],
        "wang undo reverts full neighborhood":
            res["wang_undo_full_revert"],
        "MCP tool_studio_undo via=bridge":
            res["mcp_undo_via"] == "bridge"
            and res["mcp_undo_applied"] is True,
        "MCP undo reverted cell": res["mcp_undo_reverted"],
        "MCP tool_studio_redo via=bridge":
            res["mcp_redo_via"] == "bridge"
            and res["mcp_redo_applied"] is True,
        "MCP redo re-applied cell": res["mcp_redo_reapplied"],
        "empty-stack undo is no-op":
            res["empty_undo_is_noop"],
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
