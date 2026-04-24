"""Unit-ish smoke test for v0.8.0 tool_get_selection.

Does NOT require playwright — drives the bridge purely via HTTP.

Flow:
  1. Start bridge on port 3029 with a scratch TMX
  2. POST /selection with a known rect (backward x0>x1 on purpose to test normalization)
  3. Call tool_get_selection() → verify enriched fields (width, height, tile_count)
  4. POST /selection null → clear
  5. Call tool_get_selection() → verify selection=None, no error
  6. Close bridge, call tool_get_selection() → verify {error, via:bridge}

Run:
    python3 scripts/test_get_selection.py
"""
from __future__ import annotations
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "studio" / "bridge" / "server.py"
PORT = 3029
URL = f"http://127.0.0.1:{PORT}"


def wait_health(timeout: float = 20.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(URL + "/health", timeout=2) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("bridge not healthy")


def post_selection(sel: dict | None) -> dict:
    req = urllib.request.Request(
        URL + "/selection",
        data=json.dumps({"selection": sel}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())


def main() -> int:
    # Bridge starts without a TMX — --tmx is optional and /selection works
    # on the stateless STATE object regardless of whether a map is loaded.
    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE), "--port", str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    ok = True
    try:
        health = wait_health()
        print("[bridge]", health)

        sys.path.insert(0, str(ROOT / "mcp_server"))
        import server as mcp  # type: ignore

        # --- Case 1: backward rect (x1 < x0) should be normalized ---------
        post_selection({"layer": "ground", "x0": 14, "y0": 8,
                        "x1": 10, "y1": 12})
        res_a = mcp.tool_get_selection(port=PORT)
        print("\n[case A: backward rect, normalized]")
        print(json.dumps(res_a, indent=2))

        sel = res_a.get("selection") or {}
        checks_a = {
            "no error": not res_a.get("error"),
            "via is bridge": res_a.get("via") == "bridge",
            "x0 normalized (=10)": sel.get("x0") == 10,
            "x1 normalized (=14)": sel.get("x1") == 14,
            "y0 normalized (=8)":  sel.get("y0") == 8,
            "y1 normalized (=12)": sel.get("y1") == 12,
            "width = 5":           sel.get("width") == 5,
            "height = 5":          sel.get("height") == 5,
            "tile_count = 25":     sel.get("tile_count") == 25,
            "layer preserved":     sel.get("layer") == "ground",
        }
        for k, v in checks_a.items():
            print(f"  {'OK  ' if v else 'FAIL'}  {k}")
            if not v:
                ok = False

        # --- Case 2: null selection -----------------------------------
        post_selection(None)
        res_b = mcp.tool_get_selection(port=PORT)
        print("\n[case B: cleared selection]")
        print(json.dumps(res_b, indent=2))
        checks_b = {
            "no error":       not res_b.get("error"),
            "selection None": res_b.get("selection") is None,
            "via is bridge":  res_b.get("via") == "bridge",
        }
        for k, v in checks_b.items():
            print(f"  {'OK  ' if v else 'FAIL'}  {k}")
            if not v:
                ok = False

        # --- Case 3: bridge down → error, not crash -------------------
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        proc = None  # don't double-kill in finally

        res_c = mcp.tool_get_selection(port=PORT)
        print("\n[case C: bridge unreachable]")
        print(json.dumps(res_c, indent=2))
        checks_c = {
            "reports error":  bool(res_c.get("error")),
            "via is bridge":  res_c.get("via") == "bridge",
        }
        for k, v in checks_c.items():
            print(f"  {'OK  ' if v else 'FAIL'}  {k}")
            if not v:
                ok = False

        return 0 if ok else 2
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
