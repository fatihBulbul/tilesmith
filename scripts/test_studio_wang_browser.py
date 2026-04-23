"""
Phase 4 browser e2e: drives the wang UI end-to-end in a headless Chromium
(from inside the sandbox), proving:
  - `/wang/sets` populates the panel
  - Clicking a swatch sets ctl.state.wang + mode='wang'
  - A ws.send({type:'wang_paint', ...}) produces correct autotiles on the
    canvas (konva grid + /state mutation)
  - TMX file updates on disk

Run:
    python3 scripts/test_studio_wang_browser.py
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
SRC_TMX = ROOT / "output" / "rich-80.tmx"
SCRATCH_TMX = ROOT / "output" / "rich-80-wang-ui.tmx"
PORT = 3030
URL = f"http://127.0.0.1:{PORT}/"
DB = ROOT / "data" / "tiles.db"
SCREEN = ROOT / "output" / "studio_wang_browser_screenshot.png"

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


def pure_wang_tile_local_ids() -> set[int]:
    with sqlite3.connect(str(DB)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT t.local_id FROM wang_tiles wt "
            "  JOIN tiles t ON t.tile_uid = wt.tile_uid "
            " WHERE wt.wangset_uid = ? "
            "   AND wt.c_nw=1 AND wt.c_ne=1 "
            "   AND wt.c_sw=1 AND wt.c_se=1",
            (WANGSET_UID,),
        )
        return {r["local_id"] for r in cur.fetchall()}


async def run() -> dict:
    from playwright.async_api import async_playwright  # type: ignore
    out: dict = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1400,
                                                  "height": 900})
        page = await ctx.new_page()
        page.on("pageerror", lambda e: print("pageerror:", e))
        await page.goto(URL, wait_until="networkidle")
        await page.wait_for_selector("#canvas-host canvas", timeout=10_000)

        # Wait for WS
        for _ in range(40):
            if await page.evaluate(
                "() => window.__studio?.ws?.isAlive?.() ?? false"
            ):
                break
            await asyncio.sleep(0.1)

        # Wait for wang panel to populate (needs GET /wang/sets response)
        await page.wait_for_function(
            "document.getElementById('wang-set-select')?.options?.length > 0",
            timeout=5000,
        )
        # Read the full set list
        wang_list = await page.evaluate(
            """() => {
              const sel = document.getElementById('wang-set-select');
              return Array.from(sel.options).map(o => ({
                uid: o.value, text: o.textContent,
              }));
            }"""
        )
        out["wang_sets_in_dropdown"] = len(wang_list)
        out["has_dirt_option"] = any(
            w["uid"] == WANGSET_UID for w in wang_list)

        # Select "dirt" and click color 1
        await page.evaluate(
            f"""() => {{
              const sel = document.getElementById('wang-set-select');
              sel.value = {json.dumps(WANGSET_UID)};
              sel.dispatchEvent(new Event('change'));
            }}"""
        )
        await asyncio.sleep(0.15)
        # How many swatches?
        swatch_count = await page.evaluate(
            "() => document.querySelectorAll('.wang-color').length"
        )
        out["dirt_swatches"] = swatch_count

        # Click color_index=1 swatch
        clicked = await page.evaluate(
            """(uid) => {
              const el = document.querySelector(
                `.wang-color[data-uid="${CSS.escape(uid)}"][data-color="1"]`);
              if (!el) return false;
              el.click();
              return true;
            }""",
            WANGSET_UID,
        )
        out["swatch_clicked"] = clicked

        # After click: mode=wang, wang selection set
        post_click = await page.evaluate(
            """() => {
              const s = window.__studio;
              return {
                mode: s.tools.state.mode,
                wang: s.tools.state.wang,
                modeLabel: document.getElementById('mode')?.textContent,
              };
            }"""
        )
        out["post_click_state"] = post_click

        # Send a wang_paint stroke via WS (simulate user drawing 3x3 patch
        # at 20..22, 20..22)
        cells = [{"x": x, "y": y} for y in range(20, 23)
                 for x in range(20, 23)]
        mtime_before = SCRATCH_TMX.stat().st_mtime
        await page.evaluate(
            f"""() => {{
              const s = window.__studio;
              s.ws.send({{
                type: 'wang_paint',
                wangset_uid: {json.dumps(WANGSET_UID)},
                color: 1,
                cells: {json.dumps(cells)}
              }});
            }}"""
        )
        # Wait for broadcast to round-trip + TMX write
        await asyncio.sleep(0.8)

        # Verify server state + konva reflect the interior
        probe = await page.evaluate(
            """async () => {
              const st = await (await fetch('/state')).json();
              const L = st.layers[0];
              const interior = [];
              for (let y=20; y<=22; y++) {
                for (let x=20; x<=22; x++) {
                  const key = L.data[y][x];
                  const inKonva = window.__studio.renderer.inspectCell(
                    L.name, x, y);
                  interior.push({x, y, key, inKonva});
                }
              }
              return interior;
            }"""
        )
        out["interior_probe"] = probe

        mtime_after = SCRATCH_TMX.stat().st_mtime
        out["tmx_mutated"] = mtime_after > mtime_before

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

        pure_set = pure_wang_tile_local_ids()
        # Interior 3x3 — the CENTER cell (21,21) is guaranteed to be pure-1
        # since all 4 corners are painted with color 1 from its own stroke
        # plus the neighbor strokes. The outer ring (20,22) may have varied
        # corner colors depending on pre-existing tiles, so we only strictly
        # assert on the center cell's purity.
        center = next(c for c in res["interior_probe"]
                      if c["x"] == 21 and c["y"] == 21)
        center_lid = (int(center["key"].rsplit("__", 1)[-1])
                      if center["key"] else None)

        checks = {
            "wang dropdown populated":
                res["wang_sets_in_dropdown"] >= 1,
            "dirt wangset present":
                res["has_dirt_option"],
            "dirt swatches rendered (5)":
                res["dirt_swatches"] == 5,
            "color swatch click succeeded":
                res["swatch_clicked"],
            "mode switched to 'wang'":
                res["post_click_state"]["mode"] == "wang",
            "wang selection stored":
                res["post_click_state"]["wang"]
                and res["post_click_state"]["wang"]["wangset_uid"]
                    == WANGSET_UID
                and res["post_click_state"]["wang"]["color"] == 1,
            "status bar updated":
                "wang" in (res["post_click_state"]["modeLabel"] or ""),
            "all 9 interior cells painted":
                all(c["key"] is not None for c in res["interior_probe"]),
            "center cell uses pure-wang tile":
                center_lid in pure_set,
            "konva grid has all 9 cells":
                all(c["inKonva"] for c in res["interior_probe"]),
            "TMX file mutated":
                res["tmx_mutated"],
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
