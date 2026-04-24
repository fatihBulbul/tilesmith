"""Unit test for v0.8.0 fill_rect + fill_selection multi-key / weighted.

Covers:

  A. `_normalize_keys` shape handling:
     - uniform list[str] -> weight 1.0 per entry
     - weighted list[[str, w]] -> floats, errors on non-positive weights
     - None entries (erase) preserved in the pair list
     - empty / malformed -> returns error
  B. `_pick_key` weighting bias holds over N trials.
  C. `_weighted_cells` expands a rect and is deterministic with a seed.
  D. `tool_fill_rect` (direct fallback) with `keys`:
     - writes the rect to TMX with per-cell variety
     - returns key_counts histogram summing to cell count
  E. `tool_fill_rect` backward-compat: single `key` path still works.
  F. `tool_fill_rect` validation: passing both key and keys is an error.
  G. `tool_fill_selection` multi-key without bridge returns error
     (selection lives in bridge only; can't recover without it).

Run:
    python3 scripts/test_fill_multikey.py
"""
from __future__ import annotations
import os
import random
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp_server"))

_TMP = Path(tempfile.mkdtemp(prefix="tilesmith-fillmk-"))
os.environ["TILESMITH_DB_PATH"] = str(_TMP / "tiles.db")

import server as mcp  # noqa: E402


FAKE_TSX = """<?xml version="1.0" encoding="UTF-8"?>
<tileset version="1.10" tiledversion="1.10.2" name="grass"
         tilewidth="16" tileheight="16" tilecount="4" columns="2">
  <image source="grass.png" width="32" height="32"/>
</tileset>
"""

FAKE_TMX = """<?xml version="1.0" encoding="UTF-8"?>
<map version="1.10" tiledversion="1.10.2" orientation="orthogonal"
     renderorder="right-down" width="10" height="10" tilewidth="16"
     tileheight="16" infinite="0" nextlayerid="2" nextobjectid="1">
  <tileset firstgid="1" source="grass.tsx"/>
  <layer id="1" name="ground" width="10" height="10">
    <data encoding="csv">
{rows}
</data>
  </layer>
</map>
"""


def _rows(w: int, h: int) -> str:
    return "\n".join(",".join("0" for _ in range(w)) + "," for _ in range(h))


def _write_fake_tmx(path: Path) -> None:
    (path.parent / "grass.tsx").write_text(FAKE_TSX, encoding="utf-8")
    (path.parent / "grass.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    path.write_text(FAKE_TMX.format(rows=_rows(10, 10)), encoding="utf-8")


def _read_grid(tmx: Path) -> list[list[int]]:
    root = ET.parse(tmx).getroot()
    lay = root.find("layer")
    data = (lay.find("data").text or "").strip().split("\n")
    return [[int(t) for t in row.strip().rstrip(",").split(",")]
            for row in data]


def main() -> int:
    ok = True

    # --- A: _normalize_keys ----------------------------------------
    print("[case A: _normalize_keys]")
    pairs, err = mcp._normalize_keys(["a", "b", "c"])
    a_checks = {
        "uniform -> all weight 1.0":
            pairs == [("a", 1.0), ("b", 1.0), ("c", 1.0)] and err is None,
    }
    pairs, err = mcp._normalize_keys([["a", 2.0], ["b", 1.0]])
    a_checks["weighted pairs"] = (
        pairs == [("a", 2.0), ("b", 1.0)] and err is None)
    pairs, err = mcp._normalize_keys(["a", None, "b"])
    a_checks["None entries preserved"] = (
        pairs == [("a", 1.0), (None, 1.0), ("b", 1.0)] and err is None)
    _, err = mcp._normalize_keys([])
    a_checks["empty list -> error"] = err is not None
    _, err = mcp._normalize_keys([["a", 0]])
    a_checks["zero weight -> error"] = err is not None
    _, err = mcp._normalize_keys([["a", -1.0]])
    a_checks["negative weight -> error"] = err is not None
    _, err = mcp._normalize_keys([["a", "not-a-number"]])
    a_checks["non-numeric weight -> error"] = err is not None
    _, err = mcp._normalize_keys([["a"]])
    a_checks["[key] missing weight -> error"] = err is not None
    _, err = mcp._normalize_keys([42])
    a_checks["non-str/list entry -> error"] = err is not None
    for k, v in a_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- B: _pick_key weighting bias -------------------------------
    print("\n[case B: _pick_key bias]")
    pairs = [("hot", 9.0), ("cold", 1.0)]
    rng = random.Random(0)
    counts = {"hot": 0, "cold": 0}
    for _ in range(1000):
        counts[mcp._pick_key(pairs, rng)] += 1
    b_checks = {
        "hot > 800/1000 (9:1 bias)": counts["hot"] > 800,
        "cold > 50/1000 (not zero)": counts["cold"] > 50,
    }
    print(f"  counts: {counts}")
    for k, v in b_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- C: _weighted_cells deterministic --------------------------
    print("\n[case C: _weighted_cells]")
    pairs = [("a", 1.0), ("b", 1.0)]
    cells_1 = mcp._weighted_cells(pairs, 0, 0, 4, 4, seed=42)
    cells_2 = mcp._weighted_cells(pairs, 0, 0, 4, 4, seed=42)
    cells_3 = mcp._weighted_cells(pairs, 0, 0, 4, 4, seed=43)
    c_checks = {
        "count == 25 (5x5)":           len(cells_1) == 25,
        "deterministic same seed":     cells_1 == cells_2,
        "different seed differs":      cells_1 != cells_3,
        "all cells have x,y,key":      all(
            set(c) == {"x", "y", "key"} for c in cells_1),
    }
    for k, v in c_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- D: tool_fill_rect multi-key direct ------------------------
    print("\n[case D: tool_fill_rect multi-key direct]")
    td = Path(tempfile.mkdtemp(prefix="tilesmith-fillmk-tmx-"))
    tmx = td / "fake.tmx"
    _write_fake_tmx(tmx)

    # grass gids 1..4 (firstgid=1, tilecount=4)
    res = mcp.tool_fill_rect(
        tmx_path=str(tmx), layer="ground",
        x0=0, y0=0, x1=4, y1=4,
        keys=["grass__0", "grass__1", "grass__2"],
        seed=42,
        port=3099,  # no bridge
    )
    print(f"  result key_counts: {res.get('key_counts')}")
    d_checks = {
        "no error":                "error" not in res,
        "via direct":              res.get("via") == "direct",
        "key_counts populated":
            isinstance(res.get("key_counts"), dict)
            and sum(res["key_counts"].values()) == 25,
        "region echoed back":
            res.get("region") == {"x0": 0, "y0": 0, "x1": 4, "y1": 4},
    }
    grid = _read_grid(tmx)
    painted_gids = {grid[y][x] for y in range(5) for x in range(5)}
    # gids must be in {1, 2, 3} (grass__0..2 -> firstgid+0..2)
    d_checks["painted gids subset of {1,2,3}"] = painted_gids.issubset({1, 2, 3})
    d_checks["at least 2 distinct gids"] = len(painted_gids) >= 2
    for k, v in d_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- E: backward compat single key -----------------------------
    print("\n[case E: fill_rect single-key legacy]")
    shutil.rmtree(td, ignore_errors=True)
    td = Path(tempfile.mkdtemp(prefix="tilesmith-fillmk-legacy-"))
    tmx = td / "fake.tmx"
    _write_fake_tmx(tmx)

    res = mcp.tool_fill_rect(
        tmx_path=str(tmx), layer="ground",
        x0=0, y0=0, x1=2, y1=2,
        key="grass__0",
        port=3099,
    )
    grid = _read_grid(tmx)
    e_checks = {
        "no error":  "error" not in res,
        "via direct": res.get("via") == "direct",
        "all 9 cells == gid 1":
            all(grid[y][x] == 1 for y in range(3) for x in range(3)),
    }
    for k, v in e_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- F: key + keys both set -> error ---------------------------
    print("\n[case F: validation: key+keys both]")
    res = mcp.tool_fill_rect(
        tmx_path=str(tmx), layer="ground",
        x0=0, y0=0, x1=1, y1=1,
        key="grass__0", keys=["grass__1"],
        port=3099,
    )
    f_ok = (res.get("error") is not None
            and "both" in res["error"].lower())
    print(f"  {'OK  ' if f_ok else 'FAIL'}  error returned: {res.get('error')}")
    if not f_ok:
        ok = False

    # --- G: fill_selection multi-key without bridge ----------------
    print("\n[case G: fill_selection w/o bridge]")
    res = mcp.tool_fill_selection(
        keys=["grass__0", "grass__1"],
        seed=1,
        port=3099,  # no bridge
    )
    g_ok = (res.get("error") is not None
            and "bridge unreachable" in str(res["error"]).lower())
    print(f"  {'OK  ' if g_ok else 'FAIL'}  error: {res.get('error')}")
    if not g_ok:
        ok = False

    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
