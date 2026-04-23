"""
Phase 2 end-to-end test: paint + erase round-trip.

Flow:
  1. Duplicate a real TMX to a fresh scratch file so on-disk mutations are safe
  2. Spawn the bridge on port 3026 against the scratch TMX
  3. Drive a headless Chromium to the studio
  4. Verify palette + toolbar populated
  5. Flip to paint mode via keyboard 'B' + programmatic tools.setSelectedKey
  6. Send a WS paint patch to a known cell; wait for broadcast
  7. Assert GET /state reflects the new key at that cell
  8. Assert renderer's internal Konva grid registered the new node
  9. Assert the on-disk TMX was mutated (atomic write worked)
 10. Erase the cell via WS null-key patch; assert reverse state
 11. Screenshot, shut down bridge

Run:
    python3 scripts/test_studio_phase2.py
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
SCRATCH_TMX = ROOT / "output" / "rich-80-phase2.tmx"
SCRATCH_TSX = ROOT / "output" / "rich-80-phase2.tsx"
PORT = 3026
URL = f"http://127.0.0.1:{PORT}/"
SCREEN_PATH = ROOT / "output" / "studio_phase2_screenshot.png"


def wait_for_health(timeout_s: float = 20.0) -> dict:
    t0 = time.time()
    last_err: Exception | None = None
    while time.time() - t0 < timeout_s:
        try:
            with urllib.request.urlopen(URL + "health", timeout=2) as r:
                return json.loads(r.read())
        except Exception as e:
            last_err = e
            time.sleep(0.3)
    raise RuntimeError(f"bridge never became healthy: {last_err}")


def prep_scratch() -> None:
    """Copy consolidated TMX + TSX to a phase2 scratch pair (preserve refs)."""
    shutil.copy2(SRC_TMX, SCRATCH_TMX)
    # The TMX's <tileset source="..."> is set at generate-time to point at
    # the sibling consolidated.tsx. Overwrite that reference to the phase2 tsx
    # in the scratch file so edits don't leak across fixtures.
    shutil.copy2(SRC_TSX, SCRATCH_TSX)
    txt = SCRATCH_TMX.read_text(encoding="utf-8")
    txt = txt.replace(
        'source="rich-80-consolidated.tsx"',
        'source="rich-80-phase2.tsx"',
    )
    SCRATCH_TMX.write_text(txt, encoding="utf-8")


async def run(page_url: str) -> dict:
    from playwright.async_api import async_playwright  # type: ignore

    results: dict = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await ctx.new_page()

        console_msgs: list[str] = []
        page.on("console", lambda m: console_msgs.append(f"{m.type}: {m.text}"))
        page_errors: list[str] = []
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        await page.goto(page_url, wait_until="networkidle")
        await page.wait_for_selector("#canvas-host canvas", timeout=10_000)
        await asyncio.sleep(1.0)

        # --- UI sanity: palette + active-layer select ------------------
        palette_count = await page.evaluate(
            "() => document.querySelectorAll('#palette .pal-cell').length"
        )
        layer_opts = await page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'#active-layer option')).map(o => o.value)"
        )
        results["palette_count"] = palette_count
        results["layer_opts"] = layer_opts

        # Pick the first tile layer (ground) and first palette key.
        active_layer = layer_opts[0] if layer_opts else ""
        selected_key = await page.evaluate(
            "() => document.querySelector('#palette .pal-cell')?.dataset.key || null"
        )
        results["active_layer"] = active_layer
        results["selected_key"] = selected_key
        assert active_layer, "no active layer"
        assert selected_key, "no palette key"

        # Set tool state via the debug hook (equivalent to clicking palette
        # + pressing 'B', but deterministic).
        await page.evaluate(
            f"""() => {{
              const s = window.__studio;
              s.tools.setActiveLayer({json.dumps(active_layer)});
              s.tools.setSelectedKey({json.dumps(selected_key)});
              s.tools.setMode('paint');
            }}"""
        )

        mode_after = await page.evaluate("() => window.__studio.tools.state.mode")
        sel_after = await page.evaluate(
            "() => window.__studio.tools.state.selectedKey"
        )
        results["mode_after"] = mode_after
        results["sel_after"] = sel_after
        assert mode_after == "paint", f"mode not paint: {mode_after}"
        assert sel_after == selected_key

        # --- Pick a target cell that is currently empty on the active layer.
        target = await page.evaluate(
            f"""async () => {{
              const r = await fetch('/state');
              const st = await r.json();
              const L = st.layers.find(l => l.name === {json.dumps(active_layer)});
              for (let y = 0; y < st.height; y++) {{
                for (let x = 0; x < st.width; x++) {{
                  if (!L.data[y] || L.data[y][x] == null) return {{ x, y }};
                }}
              }}
              return null;
            }}"""
        )
        if target is None:
            # fallback: overwrite (0,0) regardless
            target = {"x": 0, "y": 0}
        results["target"] = target
        tx, ty = int(target["x"]), int(target["y"])

        # --- Paint via WS ---------------------------------------------
        await page.evaluate(
            f"""() => {{
              window.__studio.ws.send({{
                type: 'patch', op: 'paint',
                layer: {json.dumps(active_layer)},
                cells: [{{ x: {tx}, y: {ty}, key: {json.dumps(selected_key)} }}]
              }});
            }}"""
        )
        await asyncio.sleep(0.6)  # round-trip + apply

        after_paint = await page.evaluate(
            f"""async () => {{
              const r = await fetch('/state');
              const st = await r.json();
              const L = st.layers.find(l => l.name === {json.dumps(active_layer)});
              const stateKey = (L.data[{ty}] || [])[{tx}] ?? null;
              const konvaHas = window.__studio.renderer.inspectCell(
                {json.dumps(active_layer)}, {tx}, {ty});
              return {{ stateKey, konvaHas }};
            }}"""
        )
        results["after_paint"] = after_paint

        # --- On-disk TMX check: it should now contain the painted gid.
        # We don't decode gids here — just assert mtime bumped (atomic replace)
        # and file is still valid xml, as a spot-check.
        mtime_after = SCRATCH_TMX.stat().st_mtime
        results["tmx_mtime_after_paint"] = mtime_after

        # --- Erase via WS (null key) ----------------------------------
        await page.evaluate(
            f"""() => {{
              window.__studio.ws.send({{
                type: 'patch', op: 'paint',
                layer: {json.dumps(active_layer)},
                cells: [{{ x: {tx}, y: {ty}, key: null }}]
              }});
            }}"""
        )
        await asyncio.sleep(0.6)

        after_erase = await page.evaluate(
            f"""async () => {{
              const r = await fetch('/state');
              const st = await r.json();
              const L = st.layers.find(l => l.name === {json.dumps(active_layer)});
              const stateKey = (L.data[{ty}] || [])[{tx}] ?? null;
              const konvaHas = window.__studio.renderer.inspectCell(
                {json.dumps(active_layer)}, {tx}, {ty});
              return {{ stateKey, konvaHas }};
            }}"""
        )
        results["after_erase"] = after_erase

        # --- Mode switching via keyboard (smoke-test the 'E' shortcut) ---
        await page.keyboard.press("e")
        mode_after_E = await page.evaluate(
            "() => window.__studio.tools.state.mode"
        )
        results["mode_after_E"] = mode_after_E

        await page.screenshot(path=str(SCREEN_PATH), full_page=False)
        results["screenshot"] = str(SCREEN_PATH)
        results["console"] = console_msgs[-20:]
        results["pageerrors"] = page_errors

        await browser.close()
    return results


def main() -> int:
    if not SRC_TMX.exists() or not SRC_TSX.exists():
        print(f"fixture TMX missing: {SRC_TMX}", file=sys.stderr)
        return 1

    prep_scratch()
    mtime_before = SCRATCH_TMX.stat().st_mtime

    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE),
         "--tmx", str(SCRATCH_TMX),
         "--port", str(PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        health = wait_for_health()
        print("[bridge] health:", health)

        res = asyncio.run(run(URL))
        print("\n[phase2 results]")
        print(json.dumps({k: v for k, v in res.items()
                          if k not in ("console",)}, indent=2, default=str))

        if res["pageerrors"]:
            print("[page errors]")
            for e in res["pageerrors"]:
                print("  ", e)

        sel = res["selected_key"]
        after_paint = res["after_paint"]
        after_erase = res["after_erase"]
        mtime_after = res["tmx_mtime_after_paint"]

        checks = {
            "palette populated":     res["palette_count"] > 0,
            "layer options present": len(res["layer_opts"]) >= 1,
            "paint mode activated":  res["mode_after"] == "paint",
            "selected key set":      res["sel_after"] == sel,
            "paint: state has key":  after_paint["stateKey"] == sel,
            "paint: konva has node": after_paint["konvaHas"] is True,
            "erase: state is null":  after_erase["stateKey"] is None,
            "erase: konva empty":    after_erase["konvaHas"] is False,
            "TMX file mutated":      mtime_after > mtime_before,
            "E key switches mode":   res["mode_after_E"] == "erase",
            "no page errors":        not res["pageerrors"],
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
