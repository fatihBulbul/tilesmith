"""
End-to-end smoke test for the Tilesmith Studio:
  1. Spawn the bridge against a real TMX
  2. Drive a headless Chromium to http://127.0.0.1:3024/
  3. Assert Konva stage + layers mounted, sprites loaded, animations ticking
  4. Capture a screenshot
  5. Cleanly shut down the bridge

Run:
    python3 scripts/test_studio_e2e.py
"""
from __future__ import annotations
import asyncio
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "studio" / "bridge" / "server.py"
TMX = ROOT / "output" / "rich-80-consolidated.tmx"
URL = "http://127.0.0.1:3024/"
SCREEN_PATH = ROOT / "output" / "studio_e2e_screenshot.png"


def wait_for_health(timeout_s: float = 20.0) -> dict:
    t0 = time.time()
    last_err = None
    while time.time() - t0 < timeout_s:
        try:
            with urllib.request.urlopen(URL + "health", timeout=2) as r:
                return json.loads(r.read())
        except Exception as e:  # pragma: no cover
            last_err = e
            time.sleep(0.3)
    raise RuntimeError(f"bridge never became healthy: {last_err}")


async def run_browser_test() -> dict:
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

        await page.goto(URL, wait_until="networkidle")

        # Wait for sprites preload + render (our main.ts awaits loadAll before render)
        # canvas element appears when Konva mounts
        await page.wait_for_selector("#canvas-host canvas", timeout=10_000)

        # Give the RAF loop a couple of ticks to update fps + anim swaps
        await asyncio.sleep(1.5)

        probe = await page.evaluate(
            """() => {
              const host = document.getElementById('canvas-host');
              const cvs = host ? host.querySelectorAll('canvas') : [];
              return {
                canvases: cvs.length,
                canvasSize: cvs.length
                  ? [cvs[0].width, cvs[0].height]
                  : null,
                meta: document.getElementById('meta')?.textContent || '',
                wsStatus: document.getElementById('ws-status')?.textContent || '',
                fps: document.getElementById('fps')?.textContent || '',
                animCount: document.getElementById('anim-count')?.textContent || '',
                layerRows: document.querySelectorAll('#layer-list .layer-row').length,
                ogRows: document.querySelectorAll('#objgroup-list .layer-row').length,
                infoRows: document.querySelectorAll('#info-panel .info-row').length,
              };
            }"""
        )

        results["probe"] = probe
        results["console"] = console_msgs[-20:]
        results["pageerrors"] = page_errors

        await page.screenshot(path=str(SCREEN_PATH), full_page=False)
        results["screenshot"] = str(SCREEN_PATH)

        await browser.close()
    return results


def main() -> int:
    if not TMX.exists():
        print(f"TMX yok: {TMX}", file=sys.stderr)
        return 1

    # Spawn bridge
    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE), "--tmx", str(TMX), "--port", "3024"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        health = wait_for_health()
        print("[bridge] health:", health)
        assert health["sprites_cached"] > 0, "no sprites cached"

        res = asyncio.run(run_browser_test())
        print("[probe]", json.dumps(res["probe"], indent=2))
        if res["pageerrors"]:
            print("[page errors]", *res["pageerrors"], sep="\n  ")
        print(f"[screenshot] {res['screenshot']}")

        probe = res["probe"]
        assertions = {
            "canvas present":      probe["canvases"] >= 1,
            "meta shows TMX":      "rich-80" in probe["meta"],
            "WS connected":        "connected" in probe["wsStatus"],
            "fps shown":           "fps" in probe["fps"],
            "anim count nonzero":  any(c.isdigit() for c in probe["animCount"])
                                   and probe["animCount"] != "0 anim",
            "2 layer rows":        probe["layerRows"] == 2,
            "3 objgroup rows":     probe["ogRows"] == 3,
            "info panel filled":   probe["infoRows"] >= 4,
            "no page errors":      not res["pageerrors"],
        }
        print("\n[assertions]")
        all_ok = True
        for k, ok in assertions.items():
            print(f"  {'OK ' if ok else 'FAIL'}  {k}")
            if not ok:
                all_ok = False

        # Show any worrying console noise (warn/error only)
        bad = [m for m in res["console"]
               if m.startswith(("error:", "warning:"))]
        if bad:
            print("\n[console warn/error]")
            for m in bad:
                print(" ", m)

        return 0 if all_ok else 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
