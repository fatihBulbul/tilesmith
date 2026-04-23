"""Direct-call smoke test for all tilesmith MCP tools.

Imports each tool_* function from server.py, calls it, and verifies:
  - no exception
  - result is JSON-serializable
  - reports shape (keys/length) for quick eyeballing

Run:
  python3 scripts/test_tools.py
"""

from __future__ import annotations
import json
import sys
import time
import traceback
from pathlib import Path

# server.py is at mcp_server/server.py
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "mcp_server"))

import server as srv  # noqa: E402


def _shape(v):
    if isinstance(v, dict):
        return f"dict[{len(v)}] keys={list(v.keys())[:6]}{'...' if len(v) > 6 else ''}"
    if isinstance(v, list):
        if v and isinstance(v[0], dict):
            return f"list[{len(v)}] of dict; sample_keys={list(v[0].keys())[:6]}"
        return f"list[{len(v)}]"
    if isinstance(v, str):
        return f"str[len={len(v)}]"
    return type(v).__name__


GREEN = "\033[92m"
RED = "\033[91m"
YEL = "\033[93m"
DIM = "\033[2m"
END = "\033[0m"


def run(name, fn):
    t0 = time.perf_counter()
    try:
        r = fn()
    except Exception as e:
        dt = (time.perf_counter() - t0) * 1000
        print(f"{RED}FAIL{END} {name:<28}  {dt:6.1f} ms  exception: {e}")
        traceback.print_exc(limit=1)
        return False, None
    dt = (time.perf_counter() - t0) * 1000
    try:
        json.dumps(r, ensure_ascii=False)
    except Exception as e:
        print(f"{YEL}JSON{END} {name:<28}  {dt:6.1f} ms  not-serializable: {e}")
        return False, r
    print(f"{GREEN} OK {END} {name:<28}  {dt:6.1f} ms  {_shape(r)}")
    return True, r


def main() -> int:
    print(f"{DIM}DB_PATH={srv.DB_PATH}{END}")
    print(f"{DIM}OUTPUT_DIR={srv.OUTPUT_DIR}{END}")
    print()

    results: dict[str, bool] = {}

    # 1. db_summary (no args)
    ok, summary = run("db_summary", srv.tool_db_summary)
    results["db_summary"] = ok

    # Pick the first pack to use for filtered calls
    pack_name = None
    if ok and summary and summary.get("packs"):
        pack_name = summary["packs"][0]["pack_name"]
        print(f"{DIM}  → using pack_name={pack_name!r} for filtered tests{END}\n")

    # 2. list_tilesets (unfiltered)
    ok, _ = run("list_tilesets", lambda: srv.tool_list_tilesets())
    results["list_tilesets"] = ok
    # 2b. list_tilesets (filtered)
    if pack_name:
        ok, _ = run("list_tilesets (pack)",
                    lambda: srv.tool_list_tilesets(pack_name))
        results["list_tilesets/pack"] = ok

    # 3. list_wang_sets
    ok, _ = run("list_wang_sets", lambda: srv.tool_list_wang_sets())
    results["list_wang_sets"] = ok

    # 4. list_prop_categories
    ok, cats = run("list_prop_categories", lambda: srv.tool_list_prop_categories())
    results["list_prop_categories"] = ok
    if ok and cats:
        sample_cat = cats[0]["category"]
        print(f"{DIM}  → sample category={sample_cat!r}{END}")

    # 5. list_animated_props (unfiltered)
    ok, _ = run("list_animated_props", lambda: srv.tool_list_animated_props())
    results["list_animated_props"] = ok
    # 5b. with category filter
    ok, _ = run("list_animated_props (cat)",
                lambda: srv.tool_list_animated_props(category="insect"))
    results["list_animated_props/cat"] = ok

    # 6. list_characters
    ok, _ = run("list_characters", lambda: srv.tool_list_characters())
    results["list_characters"] = ok

    # 7. list_reference_layers
    ok, _ = run("list_reference_layers",
                lambda: srv.tool_list_reference_layers())
    results["list_reference_layers"] = ok

    # 8. list_automapping_rules
    ok, _ = run("list_automapping_rules",
                lambda: srv.tool_list_automapping_rules())
    results["list_automapping_rules"] = ok

    # 9. plan_map
    ok, _ = run("plan_map default",
                lambda: srv.tool_plan_map())
    results["plan_map/default"] = ok
    ok, _ = run("plan_map full",
                lambda: srv.tool_plan_map(
                    width=30, height=20,
                    components=["grass", "dirt", "river", "forest"]))
    results["plan_map/full"] = ok

    # 10. scan_folder — optional (heavy). Only test error path on bogus path.
    ok, _ = run("scan_folder bogus",
                lambda: srv.tool_scan_folder(
                    str(PLUGIN_ROOT / "nonexistent_folder_xyz")))
    # Note: may or may not raise; we just want to make sure it returns JSON
    results["scan_folder/bogus"] = ok

    # 11. generate_map — tiny seed run (preview off)
    ok, gen = run("generate_map tiny",
                  lambda: srv.tool_generate_map(
                      seed=42, out_name="test-tool-gen.tmx",
                      render_preview=False))
    results["generate_map"] = ok

    # 12. consolidate_map (uses the rich-80 we already have)
    rich_tmx = srv.OUTPUT_DIR / "rich-80.tmx"
    if rich_tmx.exists():
        ok, cons = run("consolidate_map rich-80",
                       lambda: srv.tool_consolidate_map(
                           tmx_path=str(rich_tmx),
                           out_stem="test-tool-cons"))
        results["consolidate_map"] = ok
    else:
        print(f"{YEL}SKIP{END} consolidate_map                rich-80.tmx not found")

    # 13. MCP dispatch layer — use list_tools()
    print()
    print(f"{DIM}--- MCP dispatch layer ---{END}")
    try:
        import asyncio
        tools = asyncio.run(srv.list_tools())
        print(f"{GREEN} OK {END} mcp list_tools                  {len(tools)} tools registered")
        for t in tools:
            print(f"       · {t.name}")
        results["mcp/list_tools"] = True
    except Exception as e:
        print(f"{RED}FAIL{END} mcp list_tools                  exception: {e}")
        results["mcp/list_tools"] = False

    # Round-trip call one tool through the MCP call_tool dispatcher
    try:
        import asyncio
        result_list = asyncio.run(srv.call_tool("db_summary", {}))
        txt = result_list[0].text
        parsed = json.loads(txt)
        print(f"{GREEN} OK {END} mcp call_tool(db_summary)       "
              f"response_len={len(txt)}, keys={list(parsed.keys())[:5]}")
        results["mcp/call_tool"] = True
    except Exception as e:
        print(f"{RED}FAIL{END} mcp call_tool(db_summary)       exception: {e}")
        results["mcp/call_tool"] = False

    # Summary
    print()
    print(f"{DIM}{'=' * 60}{END}")
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    color = GREEN if passed == total else RED
    print(f"{color}{passed}/{total} tests passed{END}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
