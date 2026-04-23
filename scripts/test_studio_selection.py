"""
Phase 3 e2e: rect selection + fill_selection round trip.

Flow:
  1. Copy TMX to a scratch file
  2. Start bridge on port 3028
  3. Launch a headless browser, wait for studio to boot
  4. Programmatically enter 'select' mode + drive a selection via
     tools.setSelection(...) + ws.send({type:"selection",...})
  5. Call GET /selection to verify bridge stored it
  6. Call MCP-layer tool_fill_selection(key=<first-palette-key>)
  7. Spot-check 4 corners of the rect in GET /state for the new key
  8. Verify renderer.inspectCell on all 4 corners returns true
  9. Verify on-disk TMX mtime bumped
 10. Clear selection via Esc-equivalent WS send, assert bridge last_selection=None
 11. Screenshot, shut down

Run:
    python3 scripts/test_studio_selection.py
"""
from __future__ import annotations
import asyncio
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "studio" / "bridge" / "server.py"
SRC_TMX = ROOT / "output" / "rich-80-consolidated.tmx"
SRC_TSX = ROOT / "output" / "rich-80-consolidated.tsx"
SCRATCH_TMX = ROOT / "output" / "rich-80-selection.tmx"
SCRATCH_TSX = ROOT / "output" / "rich-80-selection.tsx"
PORT = 3028
URL = f"http://127.0.0.1:{PORT}/"
SCREEN = ROOT / "output" / "studio_selection_screenshot.png"


def prep() -> None:
    shutil.copy2(SRC_TMX, SCRATCH_TMX)
    shutil.copy2(SRC_TSX, SCRATCH_TSX)
    txt = SCRATCH_TMX.read_text(encoding="utf-8")
    SCRATCH_TMX.write_text(
        txt.replace(
            'source="rich-80-consolidated.tsx"',
            'source="rich-80-selection.tsx"',
        ),
        encoding="utf-8",
    )


def wait_health(timeout=20.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(URL + "health", timeout=2) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("bridge not healthy")


async def run() -> dict:
    from playwright.async_api import async_playwright  # type: ignore
    out: dict = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await ctx.new_page()
        page.on("pageerror", lambda e: print("pageerror:", e))
        await page.goto(URL, wait_until="networkidle")
        await page.wait_for_selector("#canvas-host canvas", timeout=10_000)
        # wait for WS alive
        for _ in range(40):
            if await page.evaluate(
                "() => window.__studio?.ws?.isAlive?.() ?? false"
            ):
                break
            await asyncio.sleep(0.1)

        # Use first tile layer + first tile key
        meta = await page.evaluate(
            """async () => {
              const st = await (await fetch('/state')).json();
              return { layer: st.layers[0].name, key: Object.keys(st.tiles)[0],
                       width: st.width, height: st.height };
            }"""
        )
        out["meta"] = meta
        layer = meta["layer"]
        key = meta["key"]

        # --- Enter select mode + set a known rect selection -----------
        x0, y0, x1, y1 = 10, 10, 14, 12   # 5 x 3 = 15 cells
        out["rect"] = {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
        await page.evaluate(
            f"""() => {{
              const s = window.__studio;
              s.tools.setActiveLayer({json.dumps(layer)});
              s.tools.setMode('select');
              s.tools.setSelection({{
                layer:{json.dumps(layer)},
                x0:{x0}, y0:{y0}, x1:{x1}, y1:{y1}
              }});
              s.ws.send({{type:'selection', selection:{{
                layer:{json.dumps(layer)},
                x0:{x0}, y0:{y0}, x1:{x1}, y1:{y1}
              }}}});
            }}"""
        )
        await asyncio.sleep(0.3)

        # Bridge should now know the selection
        with urllib.request.urlopen(URL + "selection", timeout=2) as r:
            sel_resp = json.loads(r.read())
        out["bridge_selection"] = sel_resp

        # Also: selection-info DOM element reflects it
        sel_info = await page.evaluate(
            "() => document.getElementById('selection-info').textContent"
        )
        out["selection_info"] = sel_info

        # --- Call the MCP tool_fill_selection (simulates Claude's intent)
        # We import it directly; it hits the bridge via HTTP.
        mtime_before = SCRATCH_TMX.stat().st_mtime
        sys.path.insert(0, str(ROOT / "mcp_server"))
        import server as mcp  # type: ignore

        fill_res = mcp.tool_fill_selection(key=key, port=PORT)
        out["fill_result"] = fill_res

        # Wait for broadcast to apply in browser
        await asyncio.sleep(0.6)

        # --- Spot check: all 4 corners + center should be painted
        probes = [(x0, y0), (x1, y0), (x0, y1), (x1, y1),
                  ((x0 + x1) // 2, (y0 + y1) // 2)]
        state_probe = await page.evaluate(
            f"""async (probes) => {{
              const st = await (await fetch('/state')).json();
              const L = st.layers.find(l => l.name === {json.dumps(layer)});
              return probes.map(([x, y]) => ({{
                x, y,
                stateKey: (L.data[y] || [])[x] ?? null,
                konvaHas: window.__studio.renderer.inspectCell(
                  {json.dumps(layer)}, x, y),
              }}));
            }}""",
            probes,
        )
        out["state_probe"] = state_probe

        # --- Cell OUTSIDE rect must remain untouched (sanity) ---------
        outside = await page.evaluate(
            f"""async () => {{
              const st = await (await fetch('/state')).json();
              const L = st.layers.find(l => l.name === {json.dumps(layer)});
              // A cell well outside the rect
              return (L.data[50] || [])[50] ?? null;
            }}"""
        )
        out["outside_cell"] = outside

        mtime_after = SCRATCH_TMX.stat().st_mtime
        out["tmx_mutated"] = mtime_after > mtime_before

        # --- Clear selection via Esc-equivalent ------------------------
        await page.evaluate(
            "() => window.__studio.tools.setSelection(null)"
        )
        await asyncio.sleep(0.3)
        with urllib.request.urlopen(URL + "selection", timeout=2) as r:
            cleared = json.loads(r.read())
        out["bridge_selection_after_clear"] = cleared

        await page.screenshot(path=str(SCREEN))
        out["screenshot"] = str(SCREEN)
        await browser.close()
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
        res = asyncio.run(run())
        print("\n[results]")
        print(json.dumps(res, indent=2, default=str))

        sel = res["bridge_selection"]["selection"]
        probes = res["state_probe"]
        sel_info = res["selection_info"]
        cleared = res["bridge_selection_after_clear"]["selection"]

        checks = {
            "bridge stored selection":
                sel and sel["x0"] == res["rect"]["x0"]
                    and sel["y1"] == res["rect"]["y1"],
            "selection-info DOM shows bounds":
                "[10,10]" in sel_info and "[14,12]" in sel_info,
            "fill_selection via MCP succeeded":
                not res["fill_result"].get("error"),
            "cells_applied == 15":
                res["fill_result"].get("cells_applied") == 15,
            "all probed cells have key":
                all(p["stateKey"] == res["meta"]["key"] for p in probes),
            "all probed cells have konva node":
                all(p["konvaHas"] for p in probes),
            "TMX file mutated (atomic write)":
                res["tmx_mutated"],
            "cell outside rect untouched (is not filled key)":
                res["outside_cell"] != res["meta"]["key"],
            "selection cleared on bridge":
                cleared is None,
        }
        print("\n[assertions]")
        ok = True
        for k, v in checks.items():
            print(f"  {'OK ' if v else 'FAIL'}  {k}")
            if not v:
                ok = False
        return 0 if ok else 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
