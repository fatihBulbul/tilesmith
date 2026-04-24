"""End-to-end integration test for the v0.8.0 release.

Validates the UX-gap crown-jewel scenario from the user's test report:

  1. User draws a rectangle in the Studio browser.
  2. Agent calls `get_selection` -> rect bounds come back.
  3. Agent calls `place_props(region="selection", category="tree")`
     -> tree objects are inserted live via bridge.
  4. Agent calls `fill_selection(keys=[...])` (multi-key variety fill).
  5. Agent calls `remove_objects(region="selection", category="tree")`
     -> those trees disappear.
  6. Agent calls `add_object(prop_uid, x, y)` -> one new object.

All of this runs against a REAL bridge subprocess on an isolated port
(3028, away from the default 3024) so the test is independent of the
user's Studio session. No real asset pack is required — we ship a fake
DB + fake TMX in a tempdir.

Atlas creation (`finalize_map`) is covered by test_finalize_map.py as a
unit test; we skip it here because it needs real PNG images.

Run:
    python3 scripts/test_v080_e2e.py
"""
from __future__ import annotations
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp_server"))

TEST_PORT = 3028
BRIDGE_URL = f"http://127.0.0.1:{TEST_PORT}"
BRIDGE_SCRIPT = ROOT / "studio" / "bridge" / "server.py"

# Sandbox DB before importing server
_TMPDIR = Path(tempfile.mkdtemp(prefix="tilesmith-e2e-"))
os.environ["TILESMITH_DB_PATH"] = str(_TMPDIR / "tiles.db")

import server as mcp          # noqa: E402
from scanner import SCHEMA_DDL  # noqa: E402


FAKE_TSX_TREES = """<?xml version="1.0" encoding="UTF-8"?>
<tileset version="1.10" tiledversion="1.10.2" name="trees"
         tilewidth="16" tileheight="16" tilecount="4" columns="2">
  <image source="trees.png" width="32" height="32"/>
</tileset>
"""

FAKE_TSX_GRASS = """<?xml version="1.0" encoding="UTF-8"?>
<tileset version="1.10" tiledversion="1.10.2" name="grass"
         tilewidth="16" tileheight="16" tilecount="4" columns="2">
  <image source="grass.png" width="32" height="32"/>
</tileset>
"""

FAKE_TMX = """<?xml version="1.0" encoding="UTF-8"?>
<map version="1.10" tiledversion="1.10.2" orientation="orthogonal"
     renderorder="right-down" width="20" height="20" tilewidth="16"
     tileheight="16" infinite="0" nextlayerid="3" nextobjectid="1">
  <tileset firstgid="1" source="grass.tsx"/>
  <tileset firstgid="100" source="trees.tsx"/>
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


def _prepare_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_DDL)
        # grass tileset (for multi-key fill)
        conn.execute(
            "INSERT INTO tilesets_auto(pack_name, tileset_uid, name, "
            "source_path, image_path, tile_count, columns, tile_width, "
            "tile_height) VALUES (?,?,?,?,?,?,?,?,?)",
            ("fakepack", "fakepack::grass", "grass",
             "grass.tsx", "grass.png", 4, 2, 16, 16),
        )
        # trees tileset (for place_props + add_object + remove_objects)
        conn.execute(
            "INSERT INTO tilesets_auto(pack_name, tileset_uid, name, "
            "source_path, image_path, tile_count, columns, tile_width, "
            "tile_height) VALUES (?,?,?,?,?,?,?,?,?)",
            ("fakepack", "fakepack::trees", "trees",
             "trees.tsx", "trees.png", 4, 2, 16, 16),
        )
        # oak + pine composites
        for local, uid in ((0, "oak"), (1, "pine")):
            conn.execute(
                "INSERT INTO props_auto(pack_name, prop_uid, tileset_uid, "
                "tileset, local_id, image_path, category, variant, size_w, "
                "size_h) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("fakepack", f"fakepack::trees::{uid}", "fakepack::trees",
                 "trees", local, "trees.png", "tree", "composite", 16, 32),
            )
        conn.commit()
    finally:
        conn.close()


def _write_real_png(path: Path, color: tuple[int, int, int]) -> None:
    """Create a real 32x32 RGBA PNG that PIL can open. 2x2 grid of 16x16
    sub-tiles so firstgid+0..3 are all valid lookups."""
    from PIL import Image
    im = Image.new("RGBA", (32, 32), color + (255,))
    im.save(str(path), "PNG")


def _write_tmx(tmx_path: Path) -> None:
    (tmx_path.parent / "grass.tsx").write_text(FAKE_TSX_GRASS)
    _write_real_png(tmx_path.parent / "grass.png", (100, 200, 100))
    (tmx_path.parent / "trees.tsx").write_text(FAKE_TSX_TREES)
    _write_real_png(tmx_path.parent / "trees.png", (40, 120, 40))
    tmx_path.write_text(FAKE_TMX.format(rows=_ground_csv(20, 20)))


def _http_post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BRIDGE_URL + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _wait_health(timeout: float = 20.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(
                BRIDGE_URL + "/health", timeout=1
            ) as r:
                json.loads(r.read())
                return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("bridge did not come up")


def _count_objects(tmx: Path, group: str) -> int:
    for og in ET.parse(tmx).getroot().findall("objectgroup"):
        if og.get("name") == group:
            return len(og.findall("object"))
    return -1


def _ok(label: str, cond: bool, extra: str = "") -> bool:
    marker = "OK  " if cond else "FAIL"
    print(f"  {marker}  {label}" + (f"  — {extra}" if extra else ""))
    return cond


def main() -> int:  # noqa: C901
    _prepare_db(Path(os.environ["TILESMITH_DB_PATH"]))
    td = Path(tempfile.mkdtemp(prefix="tilesmith-e2e-tmx-"))
    tmx = td / "map.tmx"
    _write_tmx(tmx)

    print(f"[e2e] bridge on :{TEST_PORT}, tmx={tmx}")
    env = dict(os.environ)
    env["TILESMITH_DB_PATH"] = os.environ["TILESMITH_DB_PATH"]
    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE_SCRIPT),
         "--tmx", str(tmx), "--port", str(TEST_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env,
    )
    all_ok = True
    try:
        _wait_health()

        # --- 1. Selection set via HTTP ---------------------------
        print("\n[step 1] set selection")
        _http_post("/selection", {
            "selection": {"layer": "ground",
                          "x0": 4, "y0": 4, "x1": 9, "y1": 9},
        })

        # --- 2. tool_get_selection --------------------------------
        print("\n[step 2] get_selection")
        res = mcp.tool_get_selection(port=TEST_PORT)
        all_ok &= _ok("no error",            "error" not in res)
        all_ok &= _ok("via == bridge",       res.get("via") == "bridge")
        sel = res.get("selection") or {}
        all_ok &= _ok("selection width == 6", sel.get("width") == 6)
        all_ok &= _ok("selection height == 6", sel.get("height") == 6)
        all_ok &= _ok("tile_count == 36",    sel.get("tile_count") == 36)
        all_ok &= _ok("layer preserved",     sel.get("layer") == "ground")

        # --- 3. tool_place_props (region="selection") ------------
        print("\n[step 3] place_props (category=tree)")
        before_objs = _count_objects(tmx, "flora")
        res = mcp.tool_place_props(
            tmx_path=str(tmx), layer="flora",
            region="selection", category="tree",
            variants="all", density=1.0, min_distance=2,
            seed=42, port=TEST_PORT,
        )
        all_ok &= _ok("no error", "error" not in res, str(res)[:120])
        all_ok &= _ok("via == bridge",  res.get("via") == "bridge")
        all_ok &= _ok("placed > 0",     res.get("placed", 0) > 0)
        after_objs = _count_objects(tmx, "flora")
        all_ok &= _ok(
            f"flora grew by 'placed' ({before_objs}->{after_objs})",
            after_objs - before_objs == res["placed"],
        )
        placed_count = res["placed"]

        # --- 4. tool_fill_selection (multi-key weighted) ---------
        print("\n[step 4] fill_selection multi-key")
        # Move selection onto the ground layer (was already there)
        _http_post("/selection", {
            "selection": {"layer": "ground",
                          "x0": 0, "y0": 0, "x1": 4, "y1": 4},
        })
        res = mcp.tool_fill_selection(
            keys=[["grass__0", 3.0], ["grass__1", 1.0]],
            seed=7, port=TEST_PORT,
        )
        all_ok &= _ok("no error",        "error" not in res)
        all_ok &= _ok("via == bridge",   res.get("via") == "bridge")
        khist = res.get("key_counts") or {}
        all_ok &= _ok("key_counts sums to 25",
                      sum(khist.values()) == 25, str(khist))
        all_ok &= _ok("grass__0 dominates 3:1 weighting",
                      khist.get("grass__0", 0)
                      > khist.get("grass__1", 0))
        # Verify actual TMX: gid 1 should appear in region (0,0)..(4,4)
        grid_root = ET.parse(tmx).getroot()
        layer_data = (grid_root.find("layer").find("data").text or "").strip()
        rows = [r.strip().rstrip(",").split(",") for r in
                layer_data.split("\n")]
        painted_gids = {int(rows[y][x]) for y in range(5) for x in range(5)}
        all_ok &= _ok("painted area uses gids 1 or 2",
                      painted_gids.issubset({1, 2}))

        # --- 5. remove_objects with category filter --------------
        print("\n[step 5] remove_objects (category=tree, region=selection)")
        # Selection back to the region we placed trees in
        _http_post("/selection", {
            "selection": {"layer": "flora",
                          "x0": 4, "y0": 4, "x1": 9, "y1": 9},
        })
        res = mcp.tool_remove_objects(
            tmx_path=str(tmx), layer="flora",
            region="selection", category="tree", port=TEST_PORT,
        )
        all_ok &= _ok("no error",  "error" not in res)
        all_ok &= _ok("via == bridge", res.get("via") == "bridge")
        all_ok &= _ok(f"removed == {placed_count}",
                      res.get("removed") == placed_count)
        final_objs = _count_objects(tmx, "flora")
        all_ok &= _ok("flora back to pre-place count",
                      final_objs == before_objs)

        # --- 6. add_object single insert -------------------------
        print("\n[step 6] add_object")
        res = mcp.tool_add_object(
            tmx_path=str(tmx), layer="flora",
            prop_uid="fakepack::trees::oak",
            x=12, y=15, port=TEST_PORT,
        )
        all_ok &= _ok("no error", "error" not in res, str(res)[:160])
        all_ok &= _ok("via == bridge", res.get("via") == "bridge")
        obj = res.get("object") or {}
        all_ok &= _ok("object.gid == 100 (oak in trees)",
                      int(obj.get("gid", 0)) == 100)
        all_ok &= _ok("object.x == 192 (12*16)",
                      float(obj.get("x", 0)) == 192.0)
        all_ok &= _ok("object.y == 256 ((15+1)*16)",
                      float(obj.get("y", 0)) == 256.0)
        all_ok &= _ok("flora count == pre + 1",
                      _count_objects(tmx, "flora") == before_objs + 1)

    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    if all_ok:
        print("\n[e2e] ALL v0.8.0 E2E ASSERTIONS PASSED")
        return 0
    print("\n[e2e] some assertions FAILED — see above")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
