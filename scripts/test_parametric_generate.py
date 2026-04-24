"""Unit test for v0.8.1 parametric generate_map + plan→generate chain.

Covers:

  A. `_apply_plan` mutates module globals correctly for each zone type;
     zone toggles (DIRT/RIVER/FOREST _ENABLED) reflect plan contents.
  B. `_apply_plan` returns the effective config snapshot.
  C. Missing zones in plan => toggles off (skip layer).
  D. `tool_generate_map(plan=...)` serializes plan to JSON tempfile and
     passes `--plan <path>` to the subprocess; backward-compat (plan=None)
     omits the flag.
  E. `tool_plan_and_generate` composes plan_map + generate_map:
     - returns {plan, generate}
     - plan has width/height/zones
     - generate subprocess receives --plan
  F. TOOL_DEFS registers `plan_and_generate`; generate_map schema lists
     `plan` parameter; tool count grew to >= 31.

Run:
    python3 scripts/test_parametric_generate.py

This test does NOT require the real DB or any indexed pack — we stub the
generator script via TILESMITH_GENERATOR_SCRIPT env var so the
subprocess just records its argv and exits 0.
"""
from __future__ import annotations
import importlib
import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp_server"))
sys.path.insert(0, str(ROOT / "scripts"))

# Sandbox DB path so server.py imports cleanly.
_TMP = Path(tempfile.mkdtemp(prefix="tilesmith-paramgen-"))
os.environ["TILESMITH_DB_PATH"] = str(_TMP / "tiles.db")
os.environ["TILESMITH_OUTPUT_DIR"] = str(_TMP / "out")

# Stub generator: records argv + JSON plan to /dump, exits 0. We also
# stub the preview script so tool_generate_map's optional render path
# succeeds without needing PIL / a real TMX.
STUB_GENERATOR = _TMP / "stub_generator.py"
STUB_GENERATOR.write_text(textwrap.dedent("""
    import json, os, sys
    from pathlib import Path
    dump = Path(os.environ["TILESMITH_TEST_DUMP"])
    info = {"argv": sys.argv[1:]}
    plan_path = None
    for i, a in enumerate(sys.argv):
        if a == "--plan" and i + 1 < len(sys.argv):
            plan_path = sys.argv[i + 1]
    info["plan_path"] = plan_path
    if plan_path and Path(plan_path).exists():
        info["plan_content"] = json.loads(Path(plan_path).read_text())
    else:
        info["plan_content"] = None
    # Emit an 'out' file so tool_generate_map sees tmx_path populated.
    for i, a in enumerate(sys.argv):
        if a == "--out" and i + 1 < len(sys.argv):
            Path(sys.argv[i + 1]).parent.mkdir(parents=True, exist_ok=True)
            Path(sys.argv[i + 1]).write_text("<map/>")
    dump.write_text(json.dumps(info))
    print("stub generator OK")
"""), encoding="utf-8")

STUB_PREVIEW = _TMP / "stub_preview.py"
STUB_PREVIEW.write_text(textwrap.dedent("""
    import sys
    from pathlib import Path
    for i, a in enumerate(sys.argv):
        if a == "--out" and i + 1 < len(sys.argv):
            Path(sys.argv[i + 1]).parent.mkdir(parents=True, exist_ok=True)
            Path(sys.argv[i + 1]).write_bytes(b"\\x89PNG\\r\\n\\x1a\\n")
    print("stub preview OK")
"""), encoding="utf-8")

os.environ["TILESMITH_GENERATOR_SCRIPT"] = str(STUB_GENERATOR)
os.environ["TILESMITH_PREVIEW_SCRIPT"] = str(STUB_PREVIEW)
DUMP_PATH = _TMP / "dump.json"
os.environ["TILESMITH_TEST_DUMP"] = str(DUMP_PATH)

import server as mcp           # noqa: E402
import generate_map as gen     # noqa: E402


def _reset_globals() -> None:
    """Bring generate_map globals back to defaults between cases."""
    importlib.reload(gen)


def _read_dump() -> dict:
    return json.loads(DUMP_PATH.read_text(encoding="utf-8"))


def main() -> int:
    ok = True

    # --- A: _apply_plan mutates globals -------------------------------
    print("[case A: _apply_plan mutates globals]")
    _reset_globals()
    plan_full = {
        "width": 60, "height": 30,
        "zones": [
            {"type": "dirt", "left": 2, "right": 8, "top": 5, "bottom": 10},
            {"type": "river", "center_x": 30, "half_width": 3,
             "wave_amp": 5, "wave_period": 22},
            {"type": "forest", "left": 40, "right": 55,
             "top": 2, "bottom": 25, "density": 0.25},
        ],
    }
    summary = gen._apply_plan(plan_full)
    a_checks = {
        "MAP_WIDTH overridden":     gen.MAP_WIDTH == 60,
        "MAP_HEIGHT overridden":    gen.MAP_HEIGHT == 30,
        "DIRT_PATCH_LEFT":          gen.DIRT_PATCH_LEFT == 2,
        "DIRT_PATCH_RIGHT":         gen.DIRT_PATCH_RIGHT == 8,
        "DIRT_PATCH_TOP":           gen.DIRT_PATCH_TOP == 5,
        "DIRT_PATCH_BOTTOM":        gen.DIRT_PATCH_BOTTOM == 10,
        "RIVER_CENTER_X":           gen.RIVER_CENTER_X == 30,
        "RIVER_HALF_WIDTH":         gen.RIVER_HALF_WIDTH == 3,
        "RIVER_WAVE_AMP":           gen.RIVER_WAVE_AMP == 5,
        "RIVER_WAVE_PERIOD":        gen.RIVER_WAVE_PERIOD == 22,
        "FOREST_LEFT":              gen.FOREST_LEFT == 40,
        "FOREST_RIGHT":             gen.FOREST_RIGHT == 55,
        "FOREST_TOP":               gen.FOREST_TOP == 2,
        "FOREST_BOTTOM":            gen.FOREST_BOTTOM == 25,
        "FOREST_DENSITY":           abs(gen.FOREST_DENSITY - 0.25) < 1e-9,
        "dirt enabled":             gen.DIRT_ENABLED is True,
        "river enabled":            gen.RIVER_ENABLED is True,
        "forest enabled":           gen.FOREST_ENABLED is True,
    }
    for k, v in a_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- B: summary dict ---------------------------------------------
    print("\n[case B: _apply_plan summary]")
    b_checks = {
        "summary width": summary["width"] == 60,
        "summary height": summary["height"] == 30,
        "summary dirt_enabled": summary["dirt_enabled"] is True,
        "summary river_enabled": summary["river_enabled"] is True,
        "summary forest_enabled": summary["forest_enabled"] is True,
    }
    for k, v in b_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- C: missing zones => toggle off ------------------------------
    print("\n[case C: missing zones disable toggles]")
    _reset_globals()
    plan_min = {
        "width": 20, "height": 20,
        "zones": [
            {"type": "dirt", "left": 2, "right": 6, "top": 2, "bottom": 6},
        ],
    }
    summary2 = gen._apply_plan(plan_min)
    c_checks = {
        "dirt enabled (in plan)":  gen.DIRT_ENABLED is True,
        "river disabled (absent)": gen.RIVER_ENABLED is False,
        "forest disabled (absent)": gen.FOREST_ENABLED is False,
        "width 20":                 gen.MAP_WIDTH == 20,
        "height 20":                gen.MAP_HEIGHT == 20,
    }
    for k, v in c_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- D: tool_generate_map serializes plan to --plan --------------
    print("\n[case D: tool_generate_map passes --plan tempfile]")
    res_with = mcp.tool_generate_map(
        preset="plan_and_generate",
        seed=7,
        out_name="with-plan.tmx",
        render_preview=False,
        pack=None,
        plan={"width": 16, "height": 16,
              "zones": [{"type": "dirt", "left": 1, "right": 5,
                         "top": 1, "bottom": 5}]},
    )
    dump1 = _read_dump()
    d_checks = {
        "plan_applied flag":         res_with.get("plan_applied") is True,
        "returncode 0":              res_with.get("returncode") == 0,
        "tmx_path populated":        res_with.get("tmx_path") is not None,
        "--plan in argv":            "--plan" in dump1["argv"],
        "plan_content echoed":       dump1["plan_content"] is not None,
        "plan width 16":             dump1["plan_content"]["width"] == 16,
        "plan zone count 1":         len(dump1["plan_content"]["zones"]) == 1,
    }
    for k, v in d_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # Back-compat: plan=None => no --plan flag
    res_no = mcp.tool_generate_map(
        preset="grass_river_forest", seed=1,
        out_name="noplan.tmx", render_preview=False,
        pack=None, plan=None,
    )
    dump2 = _read_dump()
    d2_checks = {
        "plan_applied False (no plan)": res_no.get("plan_applied") is False,
        "--plan NOT in argv":            "--plan" not in dump2["argv"],
        "plan_content None":             dump2["plan_content"] is None,
    }
    for k, v in d2_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- E: tool_plan_and_generate composes --------------------------
    print("\n[case E: tool_plan_and_generate chain]")
    res_chain = mcp.tool_plan_and_generate(
        width=25, height=25,
        components=["grass", "river"],
        seed=3, out_name="chained.tmx", render_preview=False, pack=None,
    )
    dump3 = _read_dump()
    plan_out = res_chain.get("plan") or {}
    gen_out = res_chain.get("generate") or {}
    e_checks = {
        "returns plan":            isinstance(plan_out, dict),
        "plan width 25":           plan_out.get("width") == 25,
        "plan has zones":          isinstance(plan_out.get("zones"), list),
        "generate returncode 0":   gen_out.get("returncode") == 0,
        "generate plan_applied":   gen_out.get("plan_applied") is True,
        "stub received --plan":    "--plan" in dump3["argv"],
        "plan forwarded width":
            dump3["plan_content"] and
            dump3["plan_content"].get("width") == 25,
        "components respected (no forest zone)":
            not any(z.get("type") == "forest"
                    for z in plan_out.get("zones", [])),
    }
    for k, v in e_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    # --- F: TOOL_DEFS registration -----------------------------------
    print("\n[case F: TOOL_DEFS surface]")
    tools = [t[0] for t in mcp.TOOL_DEFS]
    gen_def = next(t for t in mcp.TOOL_DEFS if t[0] == "generate_map")
    props = gen_def[2]["properties"]
    f_checks = {
        "plan_and_generate registered": "plan_and_generate" in tools,
        "generate_map still present":   "generate_map" in tools,
        "generate_map schema has `plan` property": "plan" in props,
        "tool count >= 31":             len(tools) >= 31,
    }
    print(f"  total tools: {len(tools)}")
    for k, v in f_checks.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
