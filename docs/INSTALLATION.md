# Installation Guide

> 🇹🇷 Türkçe sürüm: [INSTALLATION.tr.md](./INSTALLATION.tr.md)

## Prerequisites

- Python 3.10 or newer
- pip
- Claude Code CLI or Cowork
- Node.js 18+ (only if you want to rebuild the Studio frontend from source)

## 1. Install dependencies

```bash
pip install -r requirements.txt
```

The Studio frontend ships pre-built as `studio/frontend/dist/`. You only need Node if you plan to modify the viewer — in that case:

```bash
cd studio/frontend
npm install
npm run build
```

## 2. Install the plugin into Claude Code

### Method A — install from GitHub via the marketplace

Inside Claude Code:

```
/plugin marketplace add <your-username>/tilesmith
/plugin install tilesmith@tilesmith-marketplace
```

The short form expands to the GitHub URL. Full URLs work too:

```
/plugin marketplace add https://github.com/<your-username>/tilesmith
```

### Method B — install from a local folder (great for testing before you push)

```
/plugin marketplace add /path/to/tilesmith
/plugin install tilesmith@tilesmith-marketplace
```

### Method C — install from a `.plugin` bundle

Download the `.plugin` file from the GitHub Releases page, then use the matching install command inside Claude Code.

## 3. Use it via Cowork / Claude Code

Once the plugin is loaded, talk to Claude in natural language:

```
"Scan this folder: /path/to/my-tiled-pack"
```

Claude will call the `scan_folder` tool and populate the SQLite index.

Then:

```
"Make a 40x40 map with grass, a river, and a forest."
```

The `create_map` skill kicks in and starts the interactive design flow (size → biome mix → component plan → approval → TMX).

To edit live:

```
"Open the map in Studio."
"Fill that selection with dirt."
"Undo."
```

## 4. Manual verification

To confirm the plugin is wired up correctly:

```bash
# Run the MCP server directly — any import or config error surfaces on stderr.
python3 tilesmith/mcp_server/server.py --help 2>&1 | head
```

Inside Claude Code:

```
/plugin        # list installed plugins
/mcp           # list active MCP servers — tilesmith should show "connected"
```

To change where the SQLite DB lives, update `TILESMITH_DB_PATH` inside `.mcp.json`.

## Troubleshooting

**"ERROR: `mcp` package is not installed"** → Run `pip install mcp`.

**"Atlas PNG is empty / broken"** → Make sure `scan_folder` was run first. If the DB is empty, `consolidate_map` cannot find the tiles it needs.

**"Tiled shows missing images when opening the TMX"** → The sprite folder (`<stem>_sprites/`) must sit next to the TMX. If you move the TMX, move the sprites folder with it.

**"`scan_folder` scans nothing"** → Pass an absolute path. `~` and relative paths may not resolve the way you expect.

**"Nothing shows up in Studio"** → The Vite frontend must be built at least once. Run `cd studio/frontend && npm install && npm run build`. The bundle is served from `studio/frontend/dist/` and is gitignored on purpose.

**"Using legacy `ERW_*` environment variables"** → They still work for backward compatibility. New installs should prefer the `TILESMITH_*` prefix.
