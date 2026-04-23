"""
End-to-end test for the write path:
  1. Direct TMX mutation via tmx_mutator
  2. Bridge HTTP POST /patch/tiles + /patch/object
  3. Bridge WS client-initiated patch with broadcast
  4. MCP tool_paint_tiles + tool_patch_object

Always runs on a temporary copy of rich-80-consolidated.tmx.
"""
from __future__ import annotations
import asyncio
import json
import shutil
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp_server"))

from tmx_mutator import apply_paint, apply_object_patch  # noqa: E402


TMX_SRC = ROOT / "output" / "rich-80-consolidated.tmx"
TMX_TMP = ROOT / "output" / "rich-80-writetest.tmx"
BRIDGE = ROOT / "studio" / "bridge" / "server.py"
PORT = 3025  # ayrı port — Phase 1 e2e ile çakışmasın
URL = f"http://127.0.0.1:{PORT}"


def ok(label: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}" +
          (f"  — {extra}" if extra else ""))
    if not cond:
        sys.exit(2)


def read_cell(tmx: Path, layer_name: str, x: int, y: int) -> int:
    root = ET.parse(tmx).getroot()
    for L in root.findall("layer"):
        if L.get("name") == layer_name:
            lines = (L.find("data").text or "").strip().split("\n")
            row = lines[y].strip().rstrip(",").split(",")
            return int(row[x])
    raise ValueError(f"layer {layer_name} not found")


def count_objects(tmx: Path, group: str) -> int:
    root = ET.parse(tmx).getroot()
    for og in root.findall("objectgroup"):
        if og.get("name") == group:
            return len(og.findall("object"))
    return -1


def object_xy(tmx: Path, group: str, obj_id: int) -> tuple[float, float]:
    root = ET.parse(tmx).getroot()
    for og in root.findall("objectgroup"):
        if og.get("name") == group:
            for o in og.findall("object"):
                if int(o.get("id")) == obj_id:
                    return float(o.get("x")), float(o.get("y"))
    raise ValueError("obj not found")


# -------------------------------------------------------------
# Phase 1 — direct mutator
# -------------------------------------------------------------

def phase_direct() -> None:
    print("[phase] direct mutator")
    shutil.copy(TMX_SRC, TMX_TMP)

    r = apply_paint(TMX_TMP, "terrain", [
        {"x": 0, "y": 0, "key": "rich-80-consolidated__275"},  # gid 276
        {"x": 1, "y": 0, "key": None},                         # erase -> 0
    ])
    ok("paint return cells_applied==2", r["cells_applied"] == 2, str(r))
    ok("cell (0,0) == 276", read_cell(TMX_TMP, "terrain", 0, 0) == 276)
    ok("cell (1,0) == 0",  read_cell(TMX_TMP, "terrain", 1, 0) == 0)

    root = ET.parse(TMX_TMP).getroot()
    og = [og for og in root.findall("objectgroup")
          if og.get("name") == "forest"][0]
    first_id = int(og.findall("object")[0].get("id"))
    before = count_objects(TMX_TMP, "forest")

    r = apply_object_patch(TMX_TMP, "forest",
                           {"op": "move", "id": first_id, "x": 10, "y": 20})
    ok("move ok", r["ok"])
    x, y = object_xy(TMX_TMP, "forest", first_id)
    ok("moved x==10", x == 10.0)
    ok("moved y==20", y == 20.0)

    apply_object_patch(TMX_TMP, "forest", {"op": "delete", "id": first_id})
    after = count_objects(TMX_TMP, "forest")
    ok(f"forest count {before}->{after}", after == before - 1)


# -------------------------------------------------------------
# Phase 2 — via bridge HTTP + WS
# -------------------------------------------------------------

def wait_health(timeout: float = 20.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(URL + "/health", timeout=1) as r:
                json.loads(r.read())
                return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("bridge timeout")


def http_post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        URL + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


async def ws_round_trip() -> dict:
    import websockets
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws") as ws:
        # drain initial snapshot
        await asyncio.wait_for(ws.recv(), timeout=3)
        # send paint
        await ws.send(json.dumps({
            "type": "patch", "op": "paint", "layer": "terrain",
            "cells": [{"x": 4, "y": 4, "key": "rich-80-consolidated__274"}],
        }))
        # expect broadcast
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        return msg


def phase_bridge() -> None:
    print("[phase] bridge HTTP + WS")
    shutil.copy(TMX_SRC, TMX_TMP)

    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE),
         "--tmx", str(TMX_TMP), "--port", str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    try:
        wait_health()

        # HTTP paint
        r = http_post("/patch/tiles", {
            "layer": "terrain",
            "cells": [
                {"x": 2, "y": 2, "key": "rich-80-consolidated__275"},  # gid 276
                {"x": 3, "y": 2, "key": None},
            ],
        })
        ok("HTTP paint ok", r["ok"] and r["cells_applied"] == 2, str(r))
        ok("cell (2,2) == 276", read_cell(TMX_TMP, "terrain", 2, 2) == 276)
        ok("cell (3,2) == 0",   read_cell(TMX_TMP, "terrain", 3, 2) == 0)

        # HTTP object patch
        root = ET.parse(TMX_TMP).getroot()
        og = [og for og in root.findall("objectgroup")
              if og.get("name") == "flora"][0]
        target_id = int(og.findall("object")[0].get("id"))

        r = http_post("/patch/object", {
            "group": "flora", "op": "move", "id": target_id,
            "x": 55, "y": 66,
        })
        ok("HTTP move ok", r["ok"], str(r))
        x, y = object_xy(TMX_TMP, "flora", target_id)
        ok("moved x==55", x == 55.0)

        # WS client-initiated patch + broadcast
        msg = asyncio.run(ws_round_trip())
        ok("WS broadcast received", msg.get("type") == "patch"
           and msg.get("op") == "paint", str(msg)[:200])
        ok("cell (4,4) == 275 via WS",
           read_cell(TMX_TMP, "terrain", 4, 4) == 275)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# -------------------------------------------------------------
# Phase 3 — MCP tool wrappers
# -------------------------------------------------------------

def phase_mcp_tools() -> None:
    print("[phase] MCP tool wrappers (direct fallback)")
    shutil.copy(TMX_SRC, TMX_TMP)

    # Bridge NOT running — should fall back to direct
    from server import tool_paint_tiles, tool_patch_object

    r = tool_paint_tiles(
        tmx_path=str(TMX_TMP),
        layer="terrain",
        cells=[{"x": 10, "y": 10, "key": "rich-80-consolidated__275"}],
        port=3029,  # port yok
    )
    ok("MCP paint via direct", r.get("via") == "direct" and r.get("ok"),
       str(r))
    ok("MCP painted cell==276", read_cell(TMX_TMP, "terrain", 10, 10) == 276)

    root = ET.parse(TMX_TMP).getroot()
    og = [og for og in root.findall("objectgroup")
          if og.get("name") == "animations"][0]
    tid = int(og.findall("object")[0].get("id"))

    r = tool_patch_object(
        tmx_path=str(TMX_TMP), group="animations", op="move", id=tid,
        x=99, y=99, port=3029,
    )
    ok("MCP object move via direct",
       r.get("via") == "direct" and r.get("ok"), str(r))


def phase_mcp_via_bridge() -> None:
    print("[phase] MCP tool wrappers (via bridge)")
    shutil.copy(TMX_SRC, TMX_TMP)

    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE),
         "--tmx", str(TMX_TMP), "--port", str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    try:
        wait_health()
        from server import tool_paint_tiles

        r = tool_paint_tiles(
            tmx_path=str(TMX_TMP),
            layer="terrain",
            cells=[{"x": 20, "y": 20, "key": "rich-80-consolidated__275"}],
            port=PORT,
        )
        ok("MCP paint via bridge", r.get("via") == "bridge" and r.get("ok"),
           str(r))
        ok("MCP bridge painted cell==276",
           read_cell(TMX_TMP, "terrain", 20, 20) == 276)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    phase_direct()
    phase_bridge()
    phase_mcp_tools()
    phase_mcp_via_bridge()
    print("\nALL WRITE-PATH TESTS PASSED")
