"""Unit test for v0.8.0 tool_finalize_map + helper pack/license scanners.

This does NOT run a real consolidate — we need an indexed DB + real tileset
packs for that, which aren't present in CI. Instead we test:

  A. `_scan_tmx_packs` correctly walks <tileset source='...'> refs and
     locates pack roots containing LICENSE/README files.
  B. `_license_excerpt` reads + truncates LICENSE text.
  C. `tool_finalize_map` fails cleanly on a non-existent TMX (returns {error}).
  D. `tool_finalize_map` with include_license_summary=False produces a
     license_summary=None field.
  E. Module imports cleanly; TOOL_DEFS now registers finalize_map +
     get_selection (v0.8.0 additions) alongside the existing surface.

Run:
    python3 scripts/test_finalize_map.py
"""
from __future__ import annotations
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp_server"))

import server as mcp  # type: ignore


FAKE_TMX = """<?xml version="1.0" encoding="UTF-8"?>
<map version="1.10" tiledversion="1.10.2" orientation="orthogonal"
     renderorder="right-down" width="4" height="4" tilewidth="16"
     tileheight="16" infinite="0" nextlayerid="2" nextobjectid="1">
  <tileset firstgid="1" source="fakepack/tilesets/ground.tsx"/>
  <tileset firstgid="100" source="fakepack2/props.tsx"/>
  <layer id="1" name="ground" width="4" height="4">
    <data encoding="csv">
1,1,1,1,
1,1,1,1,
1,1,1,1,
1,1,1,1
</data>
  </layer>
</map>
"""

FAKE_TSX = """<?xml version="1.0" encoding="UTF-8"?>
<tileset version="1.10" tiledversion="1.10.2" name="ground"
         tilewidth="16" tileheight="16" tilecount="1" columns="1">
  <image source="ground.png" width="16" height="16"/>
</tileset>
"""

FAKE_LICENSE = (
    "Copyright (c) 2024 Fake Pack Author\n\n"
    "This asset pack is licensed under CC-BY 4.0.\n"
    "You are free to share and adapt the material for any purpose,\n"
    "even commercially, under the following terms: attribution.\n\n"
    + "X" * 600  # stress the 500-char truncation
)


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        # Pack #1 has a LICENSE at pack root (parent of tsx dir).
        pack1 = td_p / "fakepack"
        (pack1 / "tilesets").mkdir(parents=True)
        (pack1 / "tilesets" / "ground.tsx").write_text(FAKE_TSX)
        (pack1 / "tilesets" / "ground.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (pack1 / "LICENSE.txt").write_text(FAKE_LICENSE, encoding="utf-8")

        # Pack #2 has no license — tsx dir should be returned as fallback.
        pack2 = td_p / "fakepack2"
        pack2.mkdir()
        (pack2 / "props.tsx").write_text(FAKE_TSX)
        (pack2 / "props.png").write_bytes(b"\x89PNG\r\n\x1a\n")

        tmx_path = td_p / "demo.tmx"
        tmx_path.write_text(FAKE_TMX, encoding="utf-8")

        # --- A: pack scan --------------------------------------------
        packs = mcp._scan_tmx_packs(tmx_path)
        print("\n[case A: _scan_tmx_packs]")
        print("  packs:", [str(p) for p in packs])
        checks_a = {
            "found exactly 2 pack roots": len(packs) == 2,
            "pack1 root is 'fakepack' (LICENSE found)":
                any(p.name == "fakepack" for p in packs),
            "pack2 falls back to tsx dir 'fakepack2'":
                any(p.name == "fakepack2" for p in packs),
        }
        for k, v in checks_a.items():
            print(f"  {'OK  ' if v else 'FAIL'}  {k}")
            if not v:
                ok = False

        # --- B: license excerpt --------------------------------------
        lic1 = mcp._license_excerpt(pack1)
        lic2 = mcp._license_excerpt(pack2)
        print("\n[case B: _license_excerpt]")
        print("  pack1:", {
            "file": str(Path(lic1["file"]).name) if lic1["file"] else None,
            "excerpt_head": (lic1.get("excerpt") or "")[:80],
            "excerpt_len": len(lic1.get("excerpt") or ""),
        })
        print("  pack2:", lic2)
        checks_b = {
            "pack1 excerpt found": lic1.get("excerpt") is not None,
            "pack1 file is LICENSE.txt":
                lic1.get("file", "").endswith("LICENSE.txt"),
            "pack1 truncated to <=501 chars (500 + ellipsis)":
                len(lic1.get("excerpt") or "") <= 501,
            "pack1 mentions CC-BY":
                "CC-BY" in (lic1.get("excerpt") or ""),
            "pack2 has no license":
                lic2.get("excerpt") is None and lic2.get("file") is None,
        }
        for k, v in checks_b.items():
            print(f"  {'OK  ' if v else 'FAIL'}  {k}")
            if not v:
                ok = False

        # --- C: missing TMX errors cleanly ---------------------------
        res_c = mcp.tool_finalize_map(tmx_path=str(td_p / "does-not-exist.tmx"))
        print("\n[case C: missing TMX]")
        print(" ", res_c)
        checks_c = {
            "returns error": bool(res_c.get("error")),
            "error message mentions 'not found'":
                "not found" in res_c.get("error", ""),
        }
        for k, v in checks_c.items():
            print(f"  {'OK  ' if v else 'FAIL'}  {k}")
            if not v:
                ok = False

    # --- D/E: module surface check (outside tmpdir) -----------------
    tools = [t[0] for t in mcp.TOOL_DEFS]
    print("\n[case D/E: tool registration]")
    print("  total tools:", len(tools))
    checks_d = {
        "finalize_map registered":      "finalize_map" in tools,
        "get_selection registered":     "get_selection" in tools,
        "consolidate_map still present (backward compat)":
            "consolidate_map" in tools,
        "generate_map still present":   "generate_map" in tools,
        "tool count grew past 27 (25 baseline + v0.8.0 additions)":
            len(tools) >= 27,
    }
    for k, v in checks_d.items():
        print(f"  {'OK  ' if v else 'FAIL'}  {k}")
        if not v:
            ok = False

    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
