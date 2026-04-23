# Contributing Guide

Thanks for your interest in tilesmith!

> 🇹🇷 Türkçe sürüm: [CONTRIBUTING.tr.md](./CONTRIBUTING.tr.md)

## Development environment

```bash
git clone https://github.com/<your-username>/tilesmith.git
cd tilesmith
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The Studio frontend uses Vite + TypeScript:

```bash
cd studio/frontend
npm install
npm run build          # production bundle for studio/frontend/dist/
npm run dev            # live dev server (proxies to the MCP bridge)
```

## Project layout

- `mcp_server/server.py` — MCP stdio server, registers every tool in `TOOL_DEFS`.
- `mcp_server/scanner.py` — generic Tiled pack scanner + SQLite DDL.
- `mcp_server/consolidate.py` — atlas bin-packer + TMX rewriter.
- `mcp_server/wang.py` — corner- and edge-type wang resolvers, paint dispatcher, state classes, seeding helpers.
- `studio/bridge/server.py` — FastAPI/WebSocket bridge that serves the viewer and brokers live patches.
- `studio/frontend/` — Vite + Konva viewer.
- `skills/create_map/SKILL.md` — skill instructions used by Claude during interactive design sessions.
- `scripts/test_*.py` — regression suite (unit + e2e).

## Running the tests

Backend suite (no Studio processes spawned):

```bash
python3 scripts/test_wang_unit.py
python3 scripts/test_studio_wang.py           # corner-type e2e
python3 scripts/test_studio_wang_edge.py      # edge-type e2e
```

Scanner smoke-test on any Tiled pack:

```bash
python3 -c "
import sys; sys.path.insert(0, 'mcp_server')
from scanner import scan_folder
result = scan_folder('/path/to/tiled-pack', 'data/tiles.db')
print(result)
"
```

Consolidate smoke-test:

```bash
python3 -c "
import sys; sys.path.insert(0, 'mcp_server')
from consolidate import consolidate
result = consolidate('path/to/input.tmx', 'output/', 'test')
print(result)
"
```

## Pull request flow

1. Open a feature branch: `git checkout -b feature/my-feature`.
2. Keep commits small and focused; one logical change per commit.
3. Commit messages should explain the **why** more than the **what**.
4. If you add a new preset: update the preset mapping inside `tool_generate_map`, add it to the README, and note it in CHANGELOG.md.
5. If you add a new parser heuristic: update the relevant helpers in `scanner.py` (`is_automapping_rule`, `is_character_path`, wangset detection, etc.) and add a regression test.
6. If you touch wang resolvers: add or update cases in `scripts/test_wang_unit.py` and the corresponding e2e script.

## Code style

- Python 3.10+ type hints everywhere.
- Start every module with `from __future__ import annotations`.
- Prefer short, specific docstrings over prose.
- Replace magic numbers with named constants.
- Keep public MCP tool schemas (in `TOOL_DEFS`) stable — changes here are breaking for anyone scripting the plugin.

## Wanted contributions

- **New presets**: `desert_oasis`, `snow_forest`, `cave_lava`, `dungeon`, and similar biomes.
- **Atlas packing algorithms**: guillotine, MAXRECTS (the current implementation is a simple shelf packer).
- **Additional parser conventions**: heuristics to recognise more asset-pack layouts out of the box.
- **Broader Tiled coverage**: image layers, object templates, animation frames (TSX `<animation>` tags), staggered/hex orientations.
- **Mixed-type wangsets**: today they are indexed but rejected by the paint dispatcher — a proper resolver would unlock more asset packs.

## Reporting issues

Please include:

- Python version and OS.
- Which tileset pack you are working with (link if public).
- The full error message and stack trace.
- A minimal repro (e.g. the `scan_folder` call + the `wang_paint` arguments you ran).
- If the problem is visual, a screenshot of Tiled **and** of Tilesmith Studio side by side helps a lot.
