"""
tilesmith MCP server (stdio).

Tiled (TMX) uyumlu tileset paketleri için wang-aware harita üretim MCP server'ı.
tiles.db üzerinden sorgular + scripts/generate_map.py üzerinden TMX üretir.

Tools:
  - db_summary():              DB'deki toplam sayılar + son index zamanı
  - scan_folder(path):         Herhangi bir Tiled paketini recursive tarar
  - list_tilesets():           Indeksli tileset'ler
  - list_wang_sets():          Wang set'leri + color count
  - list_prop_categories():    Kategori bazında prop sayıları
  - list_animated_props():     Animated prop'lar (filter by category optional)
  - list_characters():         Karakter + anim state'leri
  - list_reference_layers():   Örnek haritaların layer yapısı
  - list_automapping_rules():  Ruleset + rule'lar
  - plan_map(w, h, comps):     ASCII yerleşim planı
  - generate_map(preset, ...): TMX üretir (multi-tileset)
  - consolidate_map(tmx):      Tek atlas PNG + self-contained TMX
"""

from __future__ import annotations
import json
import os
import subprocess
import sqlite3
import sys
from pathlib import Path
from typing import Any

# MCP SDK (eğer yoksa anlamlı hata ver)
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
except ImportError:
    print("ERROR: mcp paketi yüklü değil. `pip install mcp`", file=sys.stderr)
    raise

# Plugin-internal modules (relative imports)
sys.path.insert(0, str(Path(__file__).parent))
from scanner import scan_folder as scan_folder_impl  # noqa: E402
from consolidate import consolidate as consolidate_impl  # noqa: E402
from tmx_state import build_map_state  # noqa: E402

# ENV'den path'ler (plugin bunları MCP config'te set eder).
# Backward compat: eski ERW_* isimlerini de fallback olarak kabul et.
PLUGIN_ROOT = Path(__file__).parent.parent


def _env(new_key: str, legacy_key: str, default: str) -> str:
    return os.environ.get(new_key) or os.environ.get(legacy_key) or default


REPO_ROOT = Path(_env(
    "TILESMITH_REPO_ROOT", "ERW_REPO_ROOT",
    str(PLUGIN_ROOT),
))
DB_PATH = Path(_env(
    "TILESMITH_DB_PATH", "ERW_DB_PATH",
    str(PLUGIN_ROOT / "data" / "tiles.db"),
))
OUTPUT_DIR = Path(_env(
    "TILESMITH_OUTPUT_DIR", "ERW_OUTPUT_DIR",
    str(PLUGIN_ROOT / "output"),
))
# DB ve output dizinlerini hazırla
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Generator/preview script'leri — repo-içi scripts/ klasöründe self-contained.
# Override için TILESMITH_GENERATOR_SCRIPT / TILESMITH_PREVIEW_SCRIPT env var kullan.
GENERATOR_SCRIPT = Path(_env(
    "TILESMITH_GENERATOR_SCRIPT", "ERW_GENERATOR_SCRIPT",
    str(PLUGIN_ROOT / "scripts" / "generate_map.py"),
))
PREVIEW_SCRIPT = Path(_env(
    "TILESMITH_PREVIEW_SCRIPT", "ERW_PREVIEW_SCRIPT",
    str(PLUGIN_ROOT / "scripts" / "preview_map.py"),
))

server = Server("tilesmith")


# ---------------------------------------------------------------------
# DB yardımcıları
# ---------------------------------------------------------------------

def dbconn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_json(rows) -> list[dict]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------
# v0.8.2 — Pagination helper
# ---------------------------------------------------------------------

PAGINATION_MAX_LIMIT = 500


def _paginate(
    rows: list[dict],
    limit: int | None,
    offset: int | None,
) -> list[dict] | dict:
    """Opt-in pagination wrapper.

    - Back-compat: `limit=None and offset=None` (the old default) returns
      the raw list exactly as-is. Existing callers keep working.
    - Paginated: either arg given => returns
        {items, total, limit, offset, has_more, next_offset}.
      `next_offset` is `None` when there's no more data.

    `limit` is capped at `PAGINATION_MAX_LIMIT` to bound payloads; `offset`
    is floored at 0; negative values are coerced.
    """
    if limit is None and offset is None:
        return rows
    offset = max(0, int(offset or 0))
    lim = int(limit) if limit is not None else PAGINATION_MAX_LIMIT
    lim = max(1, min(PAGINATION_MAX_LIMIT, lim))
    total = len(rows)
    end = offset + lim
    items = rows[offset:end]
    has_more = end < total
    return {
        "items": items,
        "total": total,
        "limit": lim,
        "offset": offset,
        "has_more": has_more,
        "next_offset": end if has_more else None,
    }


# ---------------------------------------------------------------------
# Tool uygulamaları (saf fonksiyonlar)
# ---------------------------------------------------------------------

def tool_db_summary() -> dict:
    if not DB_PATH.exists():
        return {"error": f"DB bulunamadı: {DB_PATH}"}
    conn = dbconn()
    try:
        out: dict[str, Any] = {"db_path": str(DB_PATH)}
        # Merged views (unprefixed) — these reflect auto + user override data.
        views = [
            "tilesets", "tiles", "wang_sets", "wang_colors", "wang_tiles",
            "props", "animated_props", "characters", "character_animations",
            "reference_maps", "reference_layers",
            "automapping_rule_sets", "automapping_rules",
        ]
        for t in views:
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                out[t] = n
            except sqlite3.OperationalError:
                out[t] = None
        # Pack breakdown — indexed packs + per-pack tileset counts.
        try:
            packs = conn.execute(
                "SELECT pack_name, COUNT(*) AS tileset_count "
                "FROM tilesets GROUP BY pack_name ORDER BY pack_name"
            ).fetchall()
            out["packs"] = [dict(r) for r in packs]
        except sqlite3.OperationalError:
            out["packs"] = []
        return out
    finally:
        conn.close()


def tool_list_tilesets(
    pack_name: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[dict] | dict:
    conn = dbconn()
    try:
        if pack_name:
            rows = conn.execute(
                """SELECT pack_name, tileset_uid, name, tile_count, columns,
                          tile_width, tile_height, is_collection
                   FROM tilesets
                   WHERE pack_name = ?
                   ORDER BY name""",
                (pack_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT pack_name, tileset_uid, name, tile_count, columns,
                          tile_width, tile_height, is_collection
                   FROM tilesets
                   ORDER BY pack_name, name"""
            ).fetchall()
        return _paginate(rows_to_json(rows), limit, offset)
    finally:
        conn.close()


def tool_list_tiles(
    tileset_uid: str,
    limit: int | None = 100,
    offset: int | None = 0,
) -> dict:
    """v0.8.2: Browse tiles within a single tileset with pagination.

    Large tilesets (e.g. 3960-tile ERW terrain) were previously only
    accessible by local_id through wang/prop queries — no direct "show me
    tiles 0..99" surface. This tool fills that gap.

    Always paginated (default limit=100) because tile counts grow large.
    """
    conn = dbconn()
    try:
        # Normalise — tiles view exists with the DDL (auto + override merge).
        rows = conn.execute(
            """SELECT pack_name, tileset_uid, tileset, local_id,
                      semantic, role, walkable, atlas_row, atlas_col,
                      image_path
               FROM tiles
               WHERE tileset_uid = ?
               ORDER BY local_id""",
            (tileset_uid,),
        ).fetchall()
        items = rows_to_json(rows)
        if not items:
            return {"items": [], "total": 0, "limit": limit or 100,
                    "offset": offset or 0, "has_more": False,
                    "next_offset": None, "tileset_uid": tileset_uid,
                    "note": "tileset_uid has no tiles (unknown or empty)"}
        # Pagination always on here — even if caller passes None, force 100.
        lim = limit if limit is not None else 100
        off = offset if offset is not None else 0
        result = _paginate(items, lim, off)
        if isinstance(result, dict):
            result["tileset_uid"] = tileset_uid
        return result  # type: ignore[return-value]
    finally:
        conn.close()


def tool_list_wang_sets(
    pack_name: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[dict] | dict:
    conn = dbconn()
    try:
        if pack_name:
            rows = conn.execute(
                """SELECT pack_name, wangset_uid, tileset, name, type,
                          color_count, tile_count
                   FROM wang_sets
                   WHERE pack_name = ?
                   ORDER BY tileset, name""",
                (pack_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT pack_name, wangset_uid, tileset, name, type,
                          color_count, tile_count
                   FROM wang_sets
                   ORDER BY pack_name, tileset, name"""
            ).fetchall()
        return _paginate(rows_to_json(rows), limit, offset)
    finally:
        conn.close()


def tool_list_prop_categories(pack_name: str | None = None) -> list[dict]:
    conn = dbconn()
    try:
        if pack_name:
            rows = conn.execute(
                """SELECT category, COUNT(*) AS n
                   FROM props
                   WHERE category IS NOT NULL AND pack_name = ?
                   GROUP BY category
                   ORDER BY n DESC""",
                (pack_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT category, COUNT(*) AS n
                   FROM props
                   WHERE category IS NOT NULL
                   GROUP BY category
                   ORDER BY n DESC"""
            ).fetchall()
        return rows_to_json(rows)
    finally:
        conn.close()


def tool_list_animated_props(
    category: str | None = None,
    pack_name: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[dict] | dict:
    conn = dbconn()
    try:
        where = []
        params: list[Any] = []
        if category:
            where.append("category = ?")
            params.append(category)
        if pack_name:
            where.append("pack_name = ?")
            params.append(pack_name)
        if search:
            # v0.8.2: substring search on subject OR filename (case-insens).
            where.append("(LOWER(subject) LIKE ? OR LOWER(filename) LIKE ?)")
            like = f"%{search.lower()}%"
            params.extend([like, like])
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"""SELECT pack_name, aprop_uid, filename, category, subject, action,
                       variant, frame_count, frame_w, frame_h
                FROM animated_props
                {clause}
                ORDER BY category, subject""",
            params,
        ).fetchall()
        return _paginate(rows_to_json(rows), limit, offset)
    finally:
        conn.close()


def tool_list_characters(pack_name: str | None = None) -> dict:
    conn = dbconn()
    try:
        if pack_name:
            chars = rows_to_json(conn.execute(
                """SELECT pack_name, char_uid, name, category, folder,
                          variant_count
                   FROM characters
                   WHERE pack_name = ?
                   ORDER BY char_uid""",
                (pack_name,),
            ).fetchall())
            anims = rows_to_json(conn.execute(
                """SELECT pack_name, canim_uid, char_uid, state, variant,
                          frame_count, frame_w, frame_h
                   FROM character_animations
                   WHERE pack_name = ?
                   ORDER BY char_uid, state""",
                (pack_name,),
            ).fetchall())
        else:
            chars = rows_to_json(conn.execute(
                """SELECT pack_name, char_uid, name, category, folder,
                          variant_count
                   FROM characters
                   ORDER BY pack_name, char_uid"""
            ).fetchall())
            anims = rows_to_json(conn.execute(
                """SELECT pack_name, canim_uid, char_uid, state, variant,
                          frame_count, frame_w, frame_h
                   FROM character_animations
                   ORDER BY pack_name, char_uid, state"""
            ).fetchall())
        # Gruplandır
        by_char: dict[str, list[dict]] = {}
        for a in anims:
            by_char.setdefault(a["char_uid"], []).append(a)
        for c in chars:
            c["animations"] = by_char.get(c["char_uid"], [])
        return {"characters": chars}
    finally:
        conn.close()


def tool_list_reference_layers(map_uid: str | None = None,
                               pack_name: str | None = None) -> list[dict]:
    conn = dbconn()
    try:
        where = []
        params: list[Any] = []
        if map_uid:
            where.append("map_uid = ?")
            params.append(map_uid)
        if pack_name:
            where.append("pack_name = ?")
            params.append(pack_name)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"""SELECT pack_name, map_uid, layer_order, layer_type, layer_name,
                       semantic_role
                FROM reference_layers
                {clause}
                ORDER BY pack_name, map_uid, layer_order""",
            params,
        ).fetchall()
        return rows_to_json(rows)
    finally:
        conn.close()


def tool_list_automapping_rules(pack_name: str | None = None) -> list[dict]:
    conn = dbconn()
    try:
        if pack_name:
            sets = rows_to_json(conn.execute(
                """SELECT pack_name, ruleset_uid, category, has_transparency,
                          rule_count
                   FROM automapping_rule_sets
                   WHERE pack_name = ?
                   ORDER BY ruleset_uid""",
                (pack_name,),
            ).fetchall())
            rules = rows_to_json(conn.execute(
                """SELECT pack_name, rule_uid, ruleset_uid, phase, description
                   FROM automapping_rules
                   WHERE pack_name = ?
                   ORDER BY ruleset_uid, phase""",
                (pack_name,),
            ).fetchall())
        else:
            sets = rows_to_json(conn.execute(
                """SELECT pack_name, ruleset_uid, category, has_transparency,
                          rule_count
                   FROM automapping_rule_sets
                   ORDER BY pack_name, ruleset_uid"""
            ).fetchall())
            rules = rows_to_json(conn.execute(
                """SELECT pack_name, rule_uid, ruleset_uid, phase, description
                   FROM automapping_rules
                   ORDER BY pack_name, ruleset_uid, phase"""
            ).fetchall())
        by_set: dict[str, list[dict]] = {}
        for r in rules:
            by_set.setdefault(r["ruleset_uid"], []).append(r)
        for s in sets:
            s["rules"] = by_set.get(s["ruleset_uid"], [])
        return sets
    finally:
        conn.close()


def tool_scan_folder(path: str, db_path: str | None = None,
                     pack_name: str | None = None) -> dict:
    """Herhangi bir Tiled uyumlu paket klasörünü tara ve DB'ye indeksle."""
    target_db = Path(db_path) if db_path else DB_PATH
    return scan_folder_impl(path, target_db, pack_name)


def tool_plan_map(
    width: int = 40,
    height: int = 40,
    components: list[str] | None = None,
) -> dict:
    """Basit bir yerleşim planı ve ASCII preview döndürür.

    Bileşenler: grass, dirt, river, forest.
    Plan = zone dikdörtgenleri + oran özeti.
    """
    components = components or ["grass", "river", "forest"]
    zones = []
    ascii_grid = [["g"] * width for _ in range(height)]

    if "dirt" in components:
        # Sol-alt köşede toprak yaması
        l = max(2, width // 8)
        r = min(width - 3, l + max(4, width // 6))
        t = max(2, int(height * 0.55))
        b = min(height - 3, t + max(4, height // 6))
        zones.append({"type": "dirt", "left": l, "right": r,
                      "top": t, "bottom": b})
        for y in range(t, b + 1):
            for x in range(l, r + 1):
                ascii_grid[y][x] = "d"

    if "river" in components:
        cx = width // 2 + max(2, width // 10)
        hw = 2
        zones.append({"type": "river", "center_x": cx,
                      "half_width": hw, "wave_amp": 3, "wave_period": 18})
        import math
        for y in range(height):
            x0 = cx + round(3 * math.sin(2 * math.pi * y / 18))
            for dx in range(-hw, hw + 1):
                xi = x0 + dx
                if 0 <= xi < width:
                    ascii_grid[y][xi] = "~"

    if "forest" in components:
        l = max(width - int(width * 0.3), 2)
        r = width - 2
        t = 2
        b = height - 3
        zones.append({"type": "forest", "left": l, "right": r,
                      "top": t, "bottom": b, "density": 0.18})
        for y in range(t, b + 1, 3):
            for x in range(l, r + 1, 3):
                # skip if river
                if ascii_grid[y][x] == "~":
                    continue
                ascii_grid[y][x] = "t"

    ascii = "\n".join("".join(r) for r in ascii_grid)
    legend = "g=grass  d=dirt  ~=water  t=tree"
    return {
        "width": width, "height": height,
        "components": components,
        "zones": zones,
        "ascii_preview": ascii,
        "legend": legend,
        "summary": (
            f"{width}x{height} harita, bileşenler: {', '.join(components)}. "
            f"{len(zones)} zone planlandı."
        ),
    }


def tool_consolidate_map(tmx_path: str, out_dir: str | None = None,
                         out_stem: str = "consolidated") -> dict:
    """Rewrite a TMX into a single atlas PNG + self-contained TMX/TSX.

    DEPRECATED since v0.8.0: prefer `finalize_map` which wraps this plus a
    license summary of the packs actually used. This tool remains for
    backward compatibility and for callers that explicitly want the raw
    consolidation without extra metadata.
    """
    out = Path(out_dir) if out_dir else (OUTPUT_DIR / out_stem)
    return consolidate_impl(tmx_path, out, out_stem)


def _scan_tmx_packs(tmx_path: Path) -> list[Path]:
    """Walk the TMX's <tileset source="..."/> refs to find asset pack roots.

    A pack root is the directory two levels up from the TSX if that dir
    contains any LICENSE* / README* file; otherwise we return the TSX's
    parent directory. We dedupe by resolved path.
    """
    import xml.etree.ElementTree as ET
    roots: set[Path] = set()
    try:
        tree = ET.parse(tmx_path)
    except Exception:
        return []
    for ts in tree.getroot().findall("tileset"):
        src = ts.get("source")
        if not src:
            continue
        tsx = (tmx_path.parent / src).resolve()
        # Look for pack root: TSX dir, then up to 3 parents for LICENSE/README.
        probe = tsx.parent
        for _ in range(4):
            if any(
                (probe / cand).exists()
                for cand in ("LICENSE", "LICENSE.txt", "LICENSE.md",
                             "license.txt", "license", "README.md",
                             "README.txt", "readme.md")
            ):
                roots.add(probe)
                break
            if probe.parent == probe:
                break
            probe = probe.parent
        else:
            roots.add(tsx.parent)
    return sorted(roots)


def _license_excerpt(pack_root: Path, max_chars: int = 500) -> dict:
    """Return {file, excerpt} for a detected LICENSE/README file, else None."""
    for cand in ("LICENSE", "LICENSE.txt", "LICENSE.md",
                 "license.txt", "license",
                 "README.md", "README.txt", "readme.md"):
        p = pack_root / cand
        if p.exists():
            try:
                txt = p.read_text(encoding="utf-8", errors="replace").strip()
            except Exception as e:
                return {"file": str(p), "excerpt": None, "error": str(e)}
            if len(txt) > max_chars:
                txt = txt[:max_chars].rstrip() + "\u2026"
            return {"file": str(p), "excerpt": txt}
    return {"file": None, "excerpt": None}


def tool_finalize_map(tmx_path: str, out_dir: str | None = None,
                      out_stem: str = "final",
                      include_license_summary: bool = True) -> dict:
    """Freeze a TMX into a deliverable: single atlas PNG + self-contained
    TMX/TSX + optional license summary for every pack actually used.

    This is the v0.8+ replacement for the `generate_map → consolidate_map`
    auto-chain. Typical flow:

        plan_map(...) → generate_map(...) → open_studio(...)
        # user edits iteratively in browser
        finalize_map(tmx_path, out_stem="forest_final")

    Args:
        tmx_path: path to the working TMX (usually the output of generate_map
            after any in-Studio edits).
        out_dir: optional explicit output directory. Defaults to
            `<plugin>/output/<out_stem>/`.
        out_stem: filename stem for the deliverable (default "final").
        include_license_summary: when True (default), scans each pack root
            referenced by the TMX for LICENSE/README files and includes an
            excerpt per pack in the return payload under `license_summary`.

    Returns the underlying consolidate_map result plus:
        - `finalized_at`: ISO timestamp
        - `license_summary`: {pack_root: {file, excerpt}, ...}
        - `delivery`: flat dict pointing at the main artefacts for UI linking.
    """
    from datetime import datetime, timezone

    tmx_p = Path(tmx_path).resolve()
    if not tmx_p.exists():
        return {"error": f"TMX not found: {tmx_p}"}

    out = Path(out_dir) if out_dir else (OUTPUT_DIR / out_stem)
    res = consolidate_impl(tmx_p, out, out_stem)
    res["finalized_at"] = datetime.now(timezone.utc).isoformat()

    if include_license_summary:
        packs = _scan_tmx_packs(tmx_p)
        res["license_summary"] = {
            str(p): _license_excerpt(p) for p in packs
        }
    else:
        res["license_summary"] = None

    # Convenience: flat pointer at the main artefacts using the exact keys
    # consolidate_impl returns (tmx, tsx, atlas_png, sprites_dir).
    res["delivery"] = {
        "tmx": res.get("tmx"),
        "atlas_png": res.get("atlas_png"),
        "tsx": res.get("tsx"),
        "sprites_dir": res.get("sprites_dir"),
    }
    return res


# ---------------------------------------------------------------------
# v0.8.0 — place_props (prop-aware region fill)
# ---------------------------------------------------------------------

def _query_props(
    category: str,
    pack_name: str | None = None,
    variants: list[str] | str | None = None,
) -> list[dict]:
    """DB'den belirtilen kategori/pack/variant kombosuna uyan prop'ları
    döndür. Her satır: {prop_uid, key, tileset, local_id, size_w, size_h,
    variant, category, pack_name}.

    `variants` anlamları:
      - None or "composite": tek variant, "composite" (default).
      - "all": herhangi variant (kategori filtre yetmişse).
      - list[str]: explicit whitelist.
    """
    conn = dbconn()
    try:
        where = ["category = ?"]
        params: list[object] = [category]
        if pack_name:
            where.append("pack_name = ?")
            params.append(pack_name)
        if variants is None or variants == "composite":
            where.append("variant = ?")
            params.append("composite")
        elif variants == "all":
            pass
        elif isinstance(variants, list) and variants:
            placeholders = ",".join(["?"] * len(variants))
            where.append(f"variant IN ({placeholders})")
            params.extend(variants)
        sql = (
            "SELECT pack_name, prop_uid, tileset, local_id, "
            "       size_w, size_h, variant, category "
            "FROM props WHERE " + " AND ".join(where)
        )
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows_to_json(rows):
            # Synthesize TMX key in the format tmx_mutator expects.
            ts_stem = Path(str(r.get("tileset") or "")).stem
            safe = "".join(
                ch if (ch.isalnum() or ch in "-_.") else "_" for ch in ts_stem
            )
            r["key"] = f"{safe}__{int(r['local_id'])}"
            out.append(r)
        return out
    finally:
        conn.close()


def _resolve_region(region: dict | str,
                    port: int, host: str) -> tuple[dict | None, str | None]:
    """Normalise `region` into {x0,y0,x1,y1} with x0<=x1, y0<=y1.

    `"selection"` triggers a bridge fetch. Returns (rect, error_message).
    On success: (rect, None); on failure: (None, "human-readable error").
    """
    if isinstance(region, str):
        if region != "selection":
            return None, f"unknown region string: {region!r}"
        ok, resp = _bridge_get(port, host, "/selection")
        if not ok:
            return None, f"bridge unreachable: {resp.get('error')}"
        sel = resp.get("selection")
        if sel is None:
            return None, ("studio'da henüz bir seçim yapılmadı "
                          "(select tool ile dikdörtgen çiz)")
        region = {"x0": sel["x0"], "y0": sel["y0"],
                  "x1": sel["x1"], "y1": sel["y1"]}
    if not isinstance(region, dict):
        return None, "region must be a dict or 'selection'"
    try:
        x0, x1 = sorted((int(region["x0"]), int(region["x1"])))
        y0, y1 = sorted((int(region["y0"]), int(region["y1"])))
    except (KeyError, ValueError, TypeError) as e:
        return None, f"region needs x0,y0,x1,y1 integers: {e}"
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}, None


def _jitter_grid_sample(
    x0: int, y0: int, x1: int, y1: int,
    min_distance: int,
    density: float,
    rng: "random.Random",
) -> list[tuple[int, int]]:
    """Yield tile-space (x, y) sample positions.

    Split [x0..x1] x [y0..y1] into `min_distance`-sized cells. For each
    cell, roll `density` probability; if it hits, place one point at a
    random offset within the cell. This is a lightweight Poisson-disc
    substitute: spacing is guaranteed >= min_distance-1 tiles most of the
    time, with occasional closer pairs near cell edges (acceptable for
    natural-looking forest scatter).
    """
    md = max(1, int(min_distance))
    density = max(0.0, min(1.0, float(density)))
    positions: list[tuple[int, int]] = []
    for gy in range(y0, y1 + 1, md):
        for gx in range(x0, x1 + 1, md):
            if rng.random() > density:
                continue
            # Offset within the cell, clamped to region.
            cell_w = min(md, x1 - gx + 1)
            cell_h = min(md, y1 - gy + 1)
            ox = rng.randint(0, max(0, cell_w - 1))
            oy = rng.randint(0, max(0, cell_h - 1))
            positions.append((gx + ox, gy + oy))
    return positions


def _pick_variant(
    candidates: list[dict],
    variants: list[str] | list[tuple[str, float]] | str | None,
    rng: "random.Random",
) -> dict:
    """Choose one prop row from candidates honoring variant weighting.

    If variants is a list of (str, float) tuples, weight pools whose
    `variant` field matches. If it's a plain list[str] or "all", uniform
    over the candidates (the DB query already filtered by variant).
    """
    if isinstance(variants, list) and variants and isinstance(variants[0],
                                                               tuple):
        weights_by_v: dict[str, float] = {v: float(w) for v, w in variants}
        scored = []
        for c in candidates:
            w = weights_by_v.get(c.get("variant", ""), 0.0)
            if w > 0:
                scored.append((w, c))
        if not scored:
            return rng.choice(candidates)
        total = sum(w for w, _ in scored)
        r = rng.random() * total
        acc = 0.0
        for w, c in scored:
            acc += w
            if r <= acc:
                return c
        return scored[-1][1]
    return rng.choice(candidates)


def tool_place_props(
    tmx_path: str,
    layer: str,
    region: dict | str,
    category: str,
    variants: list[str] | list[tuple[str, float]] | str | None = None,
    density: float = 0.3,
    min_distance: int = 2,
    pack: str | None = None,
    seed: int | None = None,
    port: int = 3024,
    host: str = "127.0.0.1",
) -> dict:
    """Scatter ObjectGroup tile-objects across a region (prop-aware fill).

    Use this to turn a selection into a forest, bush patch, rock cluster,
    etc. The region's tile bounds define where candidates *can* spawn; the
    final count is driven by `density` (0.0-1.0) and `min_distance` (tiles
    between candidate cells).

    Args:
        tmx_path: path to the working TMX.
        layer: existing ObjectGroup name (case-sensitive).
        region: {"x0","y0","x1","y1"} dict (tile coords, inclusive), or
            the string "selection" to read the last bridge selection.
        category: DB category filter (e.g. "tree", "bush", "rock").
        variants: None or "composite" for single variant (default), "all"
            for uniform over all variants, list[str] to whitelist, or
            list[tuple[str,float]] to weight.
        density: probability per grid cell. 0.3 = ~30% of cells populated.
        min_distance: grid cell size in tiles (hard lower bound on
            inter-prop spacing).
        pack: optional pack_name filter.
        seed: optional reproducibility seed.
        port/host: bridge coords for region='selection' lookup.

    Returns:
        {
          "ok": True,
          "placed": int,               # number of objects actually inserted
          "attempted": int,            # sample grid hits before collision
          "skipped_no_candidates": int,# cells skipped because no variants fit
          "variant_counts": {key: n},  # histogram
          "layer": str,
          "region": {x0,y0,x1,y1},
          "via": "bridge" | "direct",
        }
        or {"error": "...", ...} on failure.
    """
    import random
    import xml.etree.ElementTree as ET
    from tmx_mutator import apply_object_add

    rect, err = _resolve_region(region, port, host)
    if err:
        return {"error": err}

    # Need map tile_w / tile_h for pixel conversion.
    tmx_p = Path(tmx_path).resolve()
    if not tmx_p.exists():
        return {"error": f"TMX not found: {tmx_p}"}
    try:
        root = ET.parse(tmx_p).getroot()
        tw = int(root.get("tilewidth"))
        th = int(root.get("tileheight"))
        map_w = int(root.get("width"))
        map_h = int(root.get("height"))
    except Exception as e:
        return {"error": f"failed to parse TMX: {e}"}

    # Clamp region to map bounds.
    x0 = max(0, min(rect["x0"], map_w - 1))
    x1 = max(0, min(rect["x1"], map_w - 1))
    y0 = max(0, min(rect["y0"], map_h - 1))
    y1 = max(0, min(rect["y1"], map_h - 1))

    # Query candidate props. DB-side variant filter only handles plain
    # list[str] / "composite" / "all"; tuple-weighted variants pass "all"
    # here and then get filtered in _pick_variant.
    db_variants: list[str] | str | None
    if isinstance(variants, list) and variants and isinstance(variants[0],
                                                               tuple):
        db_variants = "all"
    else:
        db_variants = variants
    candidates = _query_props(category, pack_name=pack, variants=db_variants)
    if not candidates:
        return {"error": f"no props match category={category!r} "
                         f"pack={pack!r} variants={variants!r}"}

    rng = random.Random(seed)
    positions = _jitter_grid_sample(x0, y0, x1, y1, min_distance, density, rng)

    to_place: list[dict] = []
    variant_counts: dict[str, int] = {}
    for (tx, ty) in positions:
        chosen = _pick_variant(candidates, variants, rng)
        key = chosen["key"]
        size_w = int(chosen.get("size_w") or tw)
        size_h = int(chosen.get("size_h") or th)
        # Tiled tile-objects anchor at BOTTOM-left; y is the baseline.
        px = tx * tw
        py = (ty + 1) * th  # bottom of the occupied tile
        to_place.append({
            "key": key, "x": px, "y": py,
            "width": size_w, "height": size_h, "rotation": 0.0,
        })
        variant_counts[key] = variant_counts.get(key, 0) + 1

    if not to_place:
        return {
            "ok": True, "placed": 0, "attempted": 0,
            "skipped_no_candidates": 0, "variant_counts": {},
            "layer": layer,
            "region": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
            "via": "direct",
            "note": "density * region produced zero candidates — try bigger "
                    "density or smaller min_distance",
        }

    # Try bridge first, fall through to direct write on unreachable.
    body = {"layer": layer, "objects": to_place}
    ok, resp = _bridge_post(port, host, "/patch/objects_add", body)
    if ok:
        resp.update({
            "placed": len(to_place),
            "attempted": len(positions),
            "skipped_no_candidates": 0,
            "variant_counts": variant_counts,
            "layer": layer,
            "region": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
            "via": "bridge",
        })
        return resp
    if "unreachable" not in resp.get("error", ""):
        return {"error": resp, "via": "bridge"}

    # Direct fallback: mutate TMX without bridge broadcast.
    try:
        res = apply_object_add(tmx_path, layer, to_place)
    except Exception as e:
        return {"error": str(e), "via": "direct"}
    res["placed"] = len(to_place)
    res["attempted"] = len(positions)
    res["skipped_no_candidates"] = 0
    res["variant_counts"] = variant_counts
    res["region"] = {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
    res["via"] = "direct"
    return res


# ---------------------------------------------------------------------
# v0.8.0 — add_object + remove_objects (single-object + region delete)
# ---------------------------------------------------------------------

def _lookup_prop_by_uid(prop_uid: str) -> dict | None:
    """Fetch a single props row by prop_uid. None if not found."""
    conn = dbconn()
    try:
        sql = (
            "SELECT pack_name, prop_uid, tileset, local_id, "
            "       size_w, size_h, variant, category "
            "FROM props WHERE prop_uid = ? LIMIT 1"
        )
        row = conn.execute(sql, (prop_uid,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        ts_stem = Path(str(d.get("tileset") or "")).stem
        safe = "".join(
            ch if (ch.isalnum() or ch in "-_.") else "_" for ch in ts_stem
        )
        d["key"] = f"{safe}__{int(d['local_id'])}"
        return d
    finally:
        conn.close()


def _tmx_tileset_map(root: "ET.Element") -> list[dict]:
    """Return [{firstgid, stem, safe_stem}] sorted by firstgid DESC.

    Used to reverse-map a gid back to (stem, local_id).
    """
    refs: list[dict] = []
    for ts_el in root.findall("tileset"):
        src = ts_el.get("source")
        if src is None:
            continue
        stem = Path(src).stem
        safe = "".join(
            ch if (ch.isalnum() or ch in "-_.") else "_" for ch in stem
        )
        refs.append({
            "firstgid": int(ts_el.get("firstgid", "0")),
            "stem": stem,
            "safe_stem": safe,
        })
    refs.sort(key=lambda r: -r["firstgid"])
    return refs


def _gid_to_stem_local(refs: list[dict],
                        gid: int) -> tuple[str, int] | None:
    """Reverse a gid into (safe_stem, local_id). None if no ref matches."""
    if gid <= 0:
        return None
    for r in refs:
        if gid >= r["firstgid"]:
            return r["safe_stem"], gid - r["firstgid"]
    return None


def tool_add_object(
    tmx_path: str,
    layer: str,
    prop_uid: str,
    x: int,
    y: int,
    rotation: float = 0.0,
    port: int = 3024,
    host: str = "127.0.0.1",
) -> dict:
    """Insert ONE tile-object into a named <objectgroup> by prop_uid.

    Tile-space coords (x, y) are converted to Tiled's pixel+bottom-anchor
    convention internally. size_w / size_h come from the DB props row.
    Prefers the bridge for live broadcast, falls back to direct write.

    Returns:
        {ok, via, object: {id, key, gid, x, y, width, height}, ...}
        or {error: ...} if prop_uid isn't in DB / TMX missing / etc.
    """
    import xml.etree.ElementTree as ET
    from tmx_mutator import apply_object_add

    tmx_p = Path(tmx_path).resolve()
    if not tmx_p.exists():
        return {"error": f"TMX not found: {tmx_p}"}

    prop = _lookup_prop_by_uid(prop_uid)
    if prop is None:
        return {"error": f"prop_uid not in DB: {prop_uid!r}"}

    try:
        root = ET.parse(tmx_p).getroot()
        tw = int(root.get("tilewidth"))
        th = int(root.get("tileheight"))
    except Exception as e:
        return {"error": f"failed to parse TMX: {e}"}

    size_w = int(prop.get("size_w") or tw)
    size_h = int(prop.get("size_h") or th)
    px = int(x) * tw
    py = (int(y) + 1) * th  # bottom-anchor
    obj_dict = {
        "key": prop["key"], "x": px, "y": py,
        "width": size_w, "height": size_h,
        "rotation": float(rotation),
    }

    body = {"layer": layer, "objects": [obj_dict]}
    ok, resp = _bridge_post(port, host, "/patch/objects_add", body)
    if ok:
        placed = (resp.get("objects") or [{}])[0]
        return {
            "ok": True, "via": "bridge",
            "layer": layer, "prop_uid": prop_uid,
            "object": placed, "tile_xy": {"x": int(x), "y": int(y)},
        }
    if "unreachable" not in resp.get("error", ""):
        return {"error": resp, "via": "bridge"}

    try:
        res = apply_object_add(tmx_path, layer, [obj_dict])
    except Exception as e:
        return {"error": str(e), "via": "direct"}
    placed = (res.get("objects") or [{}])[0]
    return {
        "ok": True, "via": "direct",
        "layer": layer, "prop_uid": prop_uid,
        "object": placed, "tile_xy": {"x": int(x), "y": int(y)},
    }


def tool_remove_objects(
    tmx_path: str,
    layer: str,
    region: dict | str,
    category: str | None = None,
    prop_uid: str | None = None,
    port: int = 3024,
    host: str = "127.0.0.1",
) -> dict:
    """Batch-remove objects whose center pixel falls inside a tile-region.

    Optional filters:
      - category: keep only objects whose gid maps to a DB prop with that
        category (non-matching ones stay put).
      - prop_uid: even stricter — only remove objects whose gid matches
        the given prop_uid's (tileset, local_id).

    Returns {removed: int, remaining_in_layer: int, removed_ids: [...],
             matched_but_skipped: int, via: "bridge"|"direct"} or {error}.
    """
    import xml.etree.ElementTree as ET
    from tmx_mutator import apply_object_remove

    tmx_p = Path(tmx_path).resolve()
    if not tmx_p.exists():
        return {"error": f"TMX not found: {tmx_p}"}

    rect, err = _resolve_region(region, port, host)
    if err:
        return {"error": err}

    try:
        root = ET.parse(tmx_p).getroot()
        tw = int(root.get("tilewidth"))
        th = int(root.get("tileheight"))
    except Exception as e:
        return {"error": f"failed to parse TMX: {e}"}

    og_el = None
    for og in root.findall("objectgroup"):
        if og.get("name") == layer:
            og_el = og
            break
    if og_el is None:
        return {"error": f"objectgroup '{layer}' yok"}

    ts_refs = _tmx_tileset_map(root)

    # Filter target: resolve category or prop_uid -> {(safe_stem, local_id)}
    # ahead of time so per-object check is O(1).
    target_filter: set[tuple[str, int]] | None = None
    if prop_uid:
        prop = _lookup_prop_by_uid(prop_uid)
        if prop is None:
            return {"error": f"prop_uid not in DB: {prop_uid!r}"}
        ts_stem = Path(str(prop.get("tileset") or "")).stem
        safe = "".join(
            ch if (ch.isalnum() or ch in "-_.") else "_" for ch in ts_stem
        )
        target_filter = {(safe, int(prop["local_id"]))}
    elif category:
        conn = dbconn()
        try:
            rows = conn.execute(
                "SELECT tileset, local_id FROM props WHERE category = ?",
                (category,),
            ).fetchall()
            target_filter = set()
            for r in rows:
                d = dict(r)
                ts_stem = Path(str(d.get("tileset") or "")).stem
                safe = "".join(
                    ch if (ch.isalnum() or ch in "-_.") else "_"
                    for ch in ts_stem
                )
                target_filter.add((safe, int(d["local_id"])))
        finally:
            conn.close()
        if not target_filter:
            return {"error": f"no props in DB match category={category!r}"}

    x0, y0, x1, y1 = rect["x0"], rect["y0"], rect["x1"], rect["y1"]
    ids_to_remove: list[int] = []
    matched_but_skipped = 0

    for o in og_el.findall("object"):
        try:
            ox = float(o.get("x", "0"))
            oy = float(o.get("y", "0"))
            ow = float(o.get("width", "0"))
            oh = float(o.get("height", "0"))
            gid = int(o.get("gid", "0"))
        except ValueError:
            continue
        # Tile-objects anchor at bottom-left: center = (x + w/2, y - h/2).
        cx = ox + ow / 2.0
        cy = oy - oh / 2.0
        tx = int(cx // tw) if tw > 0 else 0
        ty = int(cy // th) if th > 0 else 0
        if not (x0 <= tx <= x1 and y0 <= ty <= y1):
            continue
        if target_filter is not None:
            mapped = _gid_to_stem_local(ts_refs, gid)
            if mapped is None or mapped not in target_filter:
                matched_but_skipped += 1
                continue
        ids_to_remove.append(int(o.get("id", "-1")))

    if not ids_to_remove:
        return {
            "ok": True, "removed": 0, "removed_ids": [],
            "remaining_in_layer": len(og_el.findall("object")),
            "matched_but_skipped": matched_but_skipped,
            "region": rect, "via": "direct",
            "note": "no objects inside region matched the filter",
        }

    body = {"layer": layer, "ids": ids_to_remove}
    ok, resp = _bridge_post(port, host, "/patch/objects_remove", body)
    if ok:
        resp.update({
            "via": "bridge",
            "region": rect,
            "matched_but_skipped": matched_but_skipped,
        })
        return resp
    if "unreachable" not in resp.get("error", ""):
        return {"error": resp, "via": "bridge"}

    try:
        res = apply_object_remove(tmx_path, layer, ids_to_remove)
    except Exception as e:
        return {"error": str(e), "via": "direct"}
    res["via"] = "direct"
    res["region"] = rect
    res["matched_but_skipped"] = matched_but_skipped
    return res


def tool_get_map_state(tmx_path: str, summary_only: bool = True) -> dict:
    """TMX'i parse edip browser-ready JSON state döndürür.

    summary_only=True ise sadece üst-düzey istatistikler; False ise full
    layer/object data (büyük olabilir, chat için önerilmez — bridge kullan).
    """
    state, sprites = build_map_state(tmx_path)
    if summary_only:
        return {
            "tmx_path": state["tmx_path"],
            "width": state["width"], "height": state["height"],
            "tile_w": state["tile_w"], "tile_h": state["tile_h"],
            "layers": [{"name": l["name"], "visible": l["visible"],
                        "opacity": l["opacity"]} for l in state["layers"]],
            "object_groups": [{"name": og["name"], "count": len(og["objects"])}
                              for og in state["object_groups"]],
            "unique_tiles": len(state["tiles"]),
            "animated_tiles": sum(
                1 for t in state["tiles"].values() if "animation" in t
            ),
        }
    return state


# Studio bridge subprocess registry (port -> Popen)
_STUDIO_PROCS: dict[int, Any] = {}


def tool_open_studio(tmx_path: str, port: int = 3024,
                     host: str = "127.0.0.1") -> dict:
    """Studio bridge'ini subprocess olarak başlatır, URL döndürür.

    Aynı port'ta zaten bir bridge çalışıyorsa yeniden başlatmaz.
    Kullanıcı döndürülen URL'i tarayıcıda açar.
    """
    import subprocess
    import time
    import urllib.request
    import urllib.error

    tmx = Path(tmx_path).resolve()
    if not tmx.exists():
        return {"error": f"TMX bulunamadı: {tmx}"}

    bridge_script = PLUGIN_ROOT / "studio" / "bridge" / "server.py"
    if not bridge_script.exists():
        return {"error": f"Studio bridge bulunamadı: {bridge_script}"}

    # Port zaten canlı mı?
    health_url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(health_url, timeout=0.5) as r:
            existing = json.loads(r.read().decode())
            # Aynı bridge zaten çalışıyor — sadece TMX değiştir
            open_url = f"http://{host}:{port}/open"
            req = urllib.request.Request(
                open_url,
                data=json.dumps({"tmx_path": str(tmx)}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=2) as r2:
                summary = json.loads(r2.read().decode())
            return {
                "already_running": True,
                "url": f"http://{host}:{port}",
                "tmx_reloaded": str(tmx),
                "summary": summary.get("summary"),
                "prev_tmx": existing.get("tmx_path"),
            }
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        pass  # port free, start fresh

    # Başlat
    log_path = OUTPUT_DIR / f"bridge-{port}.log"
    log_fh = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, str(bridge_script),
         "--tmx", str(tmx),
         "--port", str(port),
         "--host", host],
        stdout=log_fh, stderr=subprocess.STDOUT,
        cwd=str(bridge_script.parent),
        start_new_session=True,
    )
    _STUDIO_PROCS[port] = proc

    # Hazır olana kadar poll (max 15s — 576 sprite preload'u için)
    deadline = time.time() + 15
    ready = False
    while time.time() < deadline:
        time.sleep(0.3)
        try:
            with urllib.request.urlopen(health_url, timeout=0.5) as r:
                json.loads(r.read().decode())
                ready = True
                break
        except Exception:
            pass

    if not ready:
        # Başlamadı — log'u döndür
        try:
            with open(log_path) as f:
                tail = f.read()[-2000:]
        except Exception:
            tail = "(log okunamadı)"
        return {
            "error": "bridge başlamadı (15s timeout)",
            "log_tail": tail,
            "log_path": str(log_path),
        }

    return {
        "url": f"http://{host}:{port}",
        "tmx_path": str(tmx),
        "pid": proc.pid,
        "log_path": str(log_path),
        "hint": f"Tarayıcıda aç: http://{host}:{port}",
    }


def _bridge_post(port: int, host: str, path: str, payload: dict,
                 timeout: float = 10.0) -> tuple[bool, dict]:
    """POST to the bridge if it's up. Returns (ok, response_or_error)."""
    import urllib.request
    import urllib.error
    url = f"http://{host}:{port}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()
        except Exception:
            body = str(e)
        return False, {"error": body, "status": e.code}
    except (urllib.error.URLError, OSError) as e:
        return False, {"error": f"bridge unreachable: {e}"}


def tool_paint_tiles(
    tmx_path: str,
    layer: str,
    cells: list[dict],
    port: int = 3024,
    host: str = "127.0.0.1",
) -> dict:
    """Tile layer'ına paint/erase patch'i uygula.

    Bridge port'ta çalışıyorsa onun üzerinden (canlı broadcast ile),
    değilse doğrudan TMX dosyasına yazar.

    cells: [{x, y, key|null}, ...]   key None => erase.
    """
    from tmx_mutator import apply_paint

    # Önce bridge'i dene
    body = {"tmx_path": tmx_path, "layer": layer, "cells": cells}
    ok, resp = _bridge_post(port, host, "/patch/tiles", body)
    if ok:
        resp["via"] = "bridge"
        return resp
    if "unreachable" not in resp.get("error", ""):
        # Bridge var ama patch reddetti — hatayı döndür
        return {"error": resp, "via": "bridge"}

    # Fallback: doğrudan mutator
    try:
        res = apply_paint(tmx_path, layer, cells)
        res["via"] = "direct"
        return res
    except Exception as e:
        return {"error": str(e), "via": "direct"}


def tool_patch_object(
    tmx_path: str,
    group: str,
    op: str,
    id: int,
    x: float | None = None,
    y: float | None = None,
    key: str | None = None,
    port: int = 3024,
    host: str = "127.0.0.1",
) -> dict:
    """Object group içindeki bir object'i taşı / sil / gid değiştir.

    op: "move" | "delete" | "set_key"
    """
    from tmx_mutator import apply_object_patch

    patch: dict[str, Any] = {"op": op, "id": id}
    if x is not None: patch["x"] = x
    if y is not None: patch["y"] = y
    if key is not None: patch["key"] = key

    body = {"tmx_path": tmx_path, "group": group, **patch}
    ok, resp = _bridge_post(port, host, "/patch/object", body)
    if ok:
        resp["via"] = "bridge"
        return resp
    if "unreachable" not in resp.get("error", ""):
        return {"error": resp, "via": "bridge"}

    try:
        res = apply_object_patch(tmx_path, group, patch)
        res["via"] = "direct"
        return res
    except Exception as e:
        return {"error": str(e), "via": "direct"}


def _bridge_get(port: int, host: str, path: str,
                timeout: float = 10.0) -> tuple[bool, dict]:
    import urllib.request, urllib.error
    url = f"http://{host}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return True, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()
        except Exception:
            body = str(e)
        return False, {"error": body, "status": e.code}
    except (urllib.error.URLError, OSError) as e:
        return False, {"error": f"bridge unreachable: {e}"}


def _normalize_keys(
    keys: list | None,
) -> tuple[list[tuple[str | None, float]] | None, str | None]:
    """Normalise the `keys` arg into a list of (key, weight) tuples.

    Accepted input shapes:
      - ["a", "b", "c"]             -> uniform weights
      - [("a", 0.5), ("b", 0.3)]    -> explicit weights
      - [["a", 0.5], ["b", 0.3]]    -> same (JSON arrives this way)

    Returns (pairs, error). On success, `pairs` is never empty and all
    weights > 0. `None` entries in keys are legal (erase) and preserved.
    """
    if keys is None:
        return None, None
    if not isinstance(keys, list) or not keys:
        return None, "keys must be a non-empty list"
    pairs: list[tuple[str | None, float]] = []
    for item in keys:
        if isinstance(item, (list, tuple)):
            if len(item) != 2:
                return None, f"keys entry must be [key, weight]: {item!r}"
            k, w = item
            try:
                wf = float(w)
            except (TypeError, ValueError):
                return None, f"weight must be numeric: {item!r}"
            if wf <= 0:
                return None, f"weight must be > 0: {item!r}"
            pairs.append((k if k is None else str(k), wf))
        elif item is None or isinstance(item, str):
            pairs.append((item, 1.0))
        else:
            return None, f"keys entry must be str or [str, weight]: {item!r}"
    return pairs, None


def _pick_key(pairs: list[tuple[str | None, float]],
              rng: "random.Random") -> str | None:
    """Weighted choice over _normalize_keys output. Returns one key (or None)."""
    total = sum(w for _, w in pairs)
    r = rng.random() * total
    acc = 0.0
    for k, w in pairs:
        acc += w
        if r <= acc:
            return k
    return pairs[-1][0]


def _weighted_cells(
    pairs: list[tuple[str | None, float]],
    x0: int, y0: int, x1: int, y1: int,
    seed: int | None,
) -> list[dict]:
    """Expand a rectangle into [{x,y,key}] with per-cell weighted keys."""
    import random
    rng = random.Random(seed)
    cells = []
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            cells.append({"x": x, "y": y, "key": _pick_key(pairs, rng)})
    return cells


def tool_fill_rect(
    tmx_path: str,
    layer: str,
    x0: int, y0: int, x1: int, y1: int,
    key: str | None = None,
    keys: list | None = None,
    seed: int | None = None,
    port: int = 3024,
    host: str = "127.0.0.1",
) -> dict:
    """Fill a rectangular region of a tile layer.

    Two modes (mutually exclusive; exactly one must be provided):
      - `key`:  single tile key (legacy; `None` erases the rect).
      - `keys`: list[str] (uniform) or list[[str, weight]] (weighted).
        When provided, each cell independently samples a key — giving
        natural variety ("mixed grass", "random floor tiles").

    Bridge is used when available; otherwise we fall through to direct
    TMX mutation via apply_paint.
    """
    from tmx_mutator import apply_paint

    if (key is not None) and (keys is not None):
        return {"error": "pass either `key` or `keys`, not both"}

    xmin, xmax = sorted((int(x0), int(x1)))
    ymin, ymax = sorted((int(y0), int(y1)))
    region = {"x0": xmin, "y0": ymin, "x1": xmax, "y1": ymax}

    # --- Single-key path (legacy) --------------------------------
    if keys is None:
        body = {"layer": layer, "region": region, "key": key}
        ok, resp = _bridge_post(port, host, "/fill", body)
        if ok:
            resp["via"] = "bridge"
            return resp
        if "unreachable" not in resp.get("error", ""):
            return {"error": resp, "via": "bridge"}
        cells = [{"x": x, "y": y, "key": key}
                 for y in range(ymin, ymax + 1)
                 for x in range(xmin, xmax + 1)]
        try:
            res = apply_paint(tmx_path, layer, cells)
            res["via"] = "direct"
            res["region"] = region
            return res
        except Exception as e:
            return {"error": str(e), "via": "direct"}

    # --- Multi-key / weighted path -------------------------------
    pairs, err = _normalize_keys(keys)
    if err:
        return {"error": err}
    cells = _weighted_cells(pairs, xmin, ymin, xmax, ymax, seed)
    # Per-key histogram for caller visibility
    hist: dict[str, int] = {}
    for c in cells:
        k = c["key"] or "__erase__"
        hist[k] = hist.get(k, 0) + 1

    body = {"layer": layer, "cells": cells}
    ok, resp = _bridge_post(port, host, "/patch/tiles", body)
    if ok:
        resp.update({"via": "bridge", "region": region,
                     "key_counts": hist, "seed": seed})
        return resp
    if "unreachable" not in resp.get("error", ""):
        return {"error": resp, "via": "bridge"}
    try:
        res = apply_paint(tmx_path, layer, cells)
        res.update({"via": "direct", "region": region,
                    "key_counts": hist, "seed": seed})
        return res
    except Exception as e:
        return {"error": str(e), "via": "direct"}


def tool_fill_selection(
    key: str | None = None,
    keys: list | None = None,
    seed: int | None = None,
    tmx_path: str | None = None,
    port: int = 3024,
    host: str = "127.0.0.1",
) -> dict:
    """Fill the user's last Studio rectangle selection.

    Modes:
      - `key`:  single-key fill (legacy; null erases).
      - `keys`: multi-key / weighted (list[str] or list[[str, weight]]).

    Bridge must be running — selection only lives in bridge state. When
    `keys` is provided with no bridge, we try a direct fallback only if
    `tmx_path` is given (we can't know the layer or rect otherwise).
    """
    if (key is not None) and (keys is not None):
        return {"error": "pass either `key` or `keys`, not both"}

    # 1) Read the stored selection
    ok, resp = _bridge_get(port, host, "/selection")
    if not ok:
        return {"error": resp, "via": "bridge"}
    sel = resp.get("selection")
    if sel is None:
        return {"error": "studio'da henüz bir seçim yapılmadı "
                         "(select tool ile dikdörtgen çiz)"}

    # --- Single-key path (legacy) --------------------------------
    if keys is None:
        body = {"key": key}  # bridge falls back to last_selection
        ok, filled = _bridge_post(port, host, "/fill", body)
        if not ok:
            return {"error": filled, "via": "bridge"}
        filled["via"] = "bridge"
        filled["selection_used"] = sel
        return filled

    # --- Multi-key / weighted path -------------------------------
    pairs, err = _normalize_keys(keys)
    if err:
        return {"error": err}
    xmin, xmax = sorted((int(sel["x0"]), int(sel["x1"])))
    ymin, ymax = sorted((int(sel["y0"]), int(sel["y1"])))
    layer = sel["layer"]
    cells = _weighted_cells(pairs, xmin, ymin, xmax, ymax, seed)
    hist: dict[str, int] = {}
    for c in cells:
        k = c["key"] or "__erase__"
        hist[k] = hist.get(k, 0) + 1

    body = {"layer": layer, "cells": cells}
    ok, resp = _bridge_post(port, host, "/patch/tiles", body)
    if ok:
        resp.update({"via": "bridge", "selection_used": sel,
                     "key_counts": hist, "seed": seed})
        return resp
    # Bridge responded with an error (not unreachable)
    if "unreachable" not in resp.get("error", ""):
        return {"error": resp, "via": "bridge"}
    # Bridge gone; try direct only if tmx_path provided
    if not tmx_path:
        return {"error": "bridge unreachable; pass tmx_path for direct "
                         "fallback when using multi-key fill_selection",
                "via": "direct"}
    from tmx_mutator import apply_paint
    try:
        res = apply_paint(tmx_path, layer, cells)
        res.update({"via": "direct", "selection_used": sel,
                    "key_counts": hist, "seed": seed})
        return res
    except Exception as e:
        return {"error": str(e), "via": "direct"}


def tool_list_wangsets_for_tmx(
    tmx_path: str | None = None,
    port: int = 3024,
    host: str = "127.0.0.1",
) -> dict:
    """TMX içinde referans edilen TSX'lere ait wang set'leri listeler
    (her birinin renk listesiyle birlikte). Bridge açıksa onun /wang/sets
    endpoint'ini kullanır; değilse doğrudan DB'den okur.
    """
    # Try bridge first
    ok, resp = _bridge_get(port, host, "/wang/sets")
    if ok:
        resp["via"] = "bridge"
        return resp
    if "unreachable" not in resp.get("error", ""):
        return {"error": resp, "via": "bridge"}

    # Direct fallback: need a TMX path to know the tilesets
    if not tmx_path:
        return {"error": "bridge unreachable and tmx_path not provided",
                "via": "direct"}
    import xml.etree.ElementTree as ET
    from wang import list_wangsets_for_tilesets
    try:
        root = ET.parse(tmx_path).getroot()
    except Exception as e:
        return {"error": f"TMX parse failed: {e}", "via": "direct"}
    stems: list[str] = []
    for ts in root.findall("tileset"):
        src = ts.get("source")
        if src:
            stems.append(Path(src).stem)
    sets = list_wangsets_for_tilesets(DB_PATH, stems)
    return {"sets": sets, "tileset_stems": stems, "via": "direct"}


def tool_wang_paint(
    wangset_uid: str,
    cells: list[dict],
    color: int = 1,
    layer: str | None = None,
    erase: bool = False,
    tmx_path: str | None = None,
    port: int = 3024,
    host: str = "127.0.0.1",
) -> dict:
    """Wang-aware autotile paint: verilen wangset + color ile cells'i boya.

    Komşu hücrelerin köşeleri otomatik olarak doğru transition tile'ları
    seçer (corner-type wang). Bridge açıksa /wang/paint'e post eder; yoksa
    tmx_path üzerinden doğrudan TMX'i mutasyon yapar.

    Args:
        wangset_uid: 'pack_name::tileset::set_name' formatında wang set uid
        cells: [{x,y}, ...] — kullanıcı buralara "renk C" bastı
        color: wang color_index (default 1 = ilk anlamlı renk)
        layer: hedef tile layer; None ise ilk tile layer
        erase: True ise color yerine 0 (outside) yazılır → hücreler silinir
        tmx_path: bridge düşükse bu gerekli (direct fallback)
    """
    body: dict = {
        "wangset_uid": wangset_uid,
        "color": color,
        "cells": cells,
        "erase": erase,
    }
    if layer is not None:
        body["layer"] = layer
    ok, resp = _bridge_post(port, host, "/wang/paint", body)
    if ok:
        resp["via"] = "bridge"
        return resp
    if "unreachable" not in resp.get("error", ""):
        return {"error": resp, "via": "bridge"}

    # Direct fallback: we need TMX + DB + full wang algorithm in-process
    if not tmx_path:
        return {"error": "bridge unreachable and tmx_path not provided",
                "via": "direct"}
    import xml.etree.ElementTree as ET
    from wang import (WangCornerState, apply_wang_paint,
                       seed_corners_from_layer)
    from tmx_state import build_map_state
    from tmx_mutator import apply_paint
    try:
        state, _sprites = build_map_state(tmx_path)
    except Exception as e:
        return {"error": f"TMX load failed: {e}", "via": "direct"}
    target_layer = layer
    if not target_layer:
        layers = state.get("layers") or []
        if not layers:
            return {"error": "map has no tile layers", "via": "direct"}
        target_layer = layers[0]["name"]
    layer_data: list[list[str | None]] | None = None
    for L in state.get("layers", []):
        if L["name"] == target_layer:
            layer_data = L["data"]
            break
    if layer_data is None:
        return {"error": f"layer '{target_layer}' not found",
                "via": "direct"}
    w, h = int(state["width"]), int(state["height"])
    cs = WangCornerState(width=w, height=h)
    try:
        seed_corners_from_layer(cs, layer_data, DB_PATH, wangset_uid)
    except Exception as e:
        return {"error": f"wang seed failed: {e}", "via": "direct"}
    paint_cells = apply_wang_paint(
        cs, DB_PATH, wangset_uid, int(color), cells, erase=erase,
    )
    if not paint_cells:
        return {"ok": True, "via": "direct", "cells_applied": 0,
                "wang": {"wangset_uid": wangset_uid, "color": color,
                         "cells_touched": 0}}
    try:
        res = apply_paint(tmx_path, target_layer, paint_cells)
    except Exception as e:
        return {"error": str(e), "via": "direct"}
    res["via"] = "direct"
    res["layer"] = target_layer
    res["wang"] = {
        "wangset_uid": wangset_uid, "color": color,
        "cells_touched": len(paint_cells), "erase": erase,
    }
    return res


def tool_wang_fill_rect(
    wangset_uid: str,
    x0: int, y0: int, x1: int, y1: int,
    color: int = 1,
    layer: str | None = None,
    erase: bool = False,
    tmx_path: str | None = None,
    port: int = 3024,
    host: str = "127.0.0.1",
) -> dict:
    """Wang-aware dikdörtgen doldurma: (x0,y0)-(x1,y1) hücrelerini
    verilen wangset + color ile boyar ve köşelerin otomatik komşu
    uyumuyla doğru transition tile'ları seçilir.

    Bridge açıksa /wang/fill_rect'e post eder; yoksa tmx_path ile
    direct fallback çalışır (tool_wang_paint'in aynı yolunu kullanır).
    """
    body: dict = {
        "wangset_uid": wangset_uid,
        "color": color,
        "x0": int(x0), "y0": int(y0),
        "x1": int(x1), "y1": int(y1),
        "erase": erase,
    }
    if layer is not None:
        body["layer"] = layer
    ok, resp = _bridge_post(port, host, "/wang/fill_rect", body)
    if ok:
        resp["via"] = "bridge"
        return resp
    if "unreachable" not in resp.get("error", ""):
        return {"error": resp, "via": "bridge"}

    # Direct fallback: expand rect into cells and delegate to wang_paint.
    xa, xb = sorted((int(x0), int(x1)))
    ya, yb = sorted((int(y0), int(y1)))
    cells = [{"x": x, "y": y}
             for y in range(ya, yb + 1)
             for x in range(xa, xb + 1)]
    res = tool_wang_paint(
        wangset_uid=wangset_uid, cells=cells, color=color,
        layer=layer, erase=erase, tmx_path=tmx_path,
        port=port, host=host,
    )
    if "error" not in res:
        res["rect"] = {"x0": xa, "y0": ya, "x1": xb, "y1": yb}
    return res


def tool_wang_fill_selection(
    wangset_uid: str,
    color: int = 1,
    erase: bool = False,
    port: int = 3024,
    host: str = "127.0.0.1",
) -> dict:
    """Studio'da sürükleyerek çizilen son seçimi wang-aware olarak doldurur.

    Doğal dil akışı için: kullanıcı canvas'ta alan seçer, wang set +
    renk söyler ("orayı toprak wang'i ile doldur") → Claude bu tool'u
    çağırır. Bridge çalışıyor olmalı (yoksa seçim nereden geleceği
    belirsiz).
    """
    # 1) Seçimi oku
    ok, resp = _bridge_get(port, host, "/selection")
    if not ok:
        return {"error": resp, "via": "bridge"}
    sel = resp.get("selection")
    if sel is None:
        return {"error": "studio'da henüz bir seçim yapılmadı "
                         "(select tool ile dikdörtgen çiz)",
                "via": "bridge"}

    # 2) use_selection=True ile /wang/fill_rect'e bırak
    body = {
        "wangset_uid": wangset_uid,
        "color": color,
        "erase": erase,
        "use_selection": True,
    }
    ok, filled = _bridge_post(port, host, "/wang/fill_rect", body)
    if not ok:
        return {"error": filled, "via": "bridge"}
    filled["via"] = "bridge"
    filled["selection_used"] = sel
    return filled


def tool_studio_undo(
    port: int = 3024, host: str = "127.0.0.1",
) -> dict:
    """Studio'daki en son kaydedilmiş paint/erase/wang patch'ini geri alır.
    (Wang veya düz paint ayrımı yapmaz; patch_paint chokepoint'inde
    kaydedilmiş tek operasyonu rollback eder.) Stack boşsa no-op.

    Yalnızca bridge çalışırken çalışır (history bridge'de duruyor).
    """
    ok, resp = _bridge_post(port, host, "/undo", {})
    if not ok:
        return {"error": resp, "via": "bridge"}
    resp["via"] = "bridge"
    return resp


def tool_studio_redo(
    port: int = 3024, host: str = "127.0.0.1",
) -> dict:
    """En son undo edilen paint/erase/wang patch'ini tekrar uygular.
    Undo edilen son operasyondan sonra yeni bir mutation yapıldıysa
    redo branch'i temizlenmiş olur; bu durumda no-op döner."""
    ok, resp = _bridge_post(port, host, "/redo", {})
    if not ok:
        return {"error": resp, "via": "bridge"}
    resp["via"] = "bridge"
    return resp


def tool_close_studio(port: int = 3024) -> dict:
    """Belirtilen port'taki studio bridge'ini kapatır."""
    import signal
    proc = _STUDIO_PROCS.pop(port, None)
    if proc is None:
        return {"ok": False, "reason": f"port {port} için kayıtlı proc yok"}
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=3)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "port": port}


def tool_get_selection(
    port: int = 3024, host: str = "127.0.0.1",
) -> dict:
    """Return the user's current rectangle selection from Studio bridge.

    The bridge tracks the last rect the user dragged in the browser. This
    tool wraps GET /selection and enriches the response with width/height/
    tile_count so the agent can make region-size-aware decisions (e.g. how
    many props to scatter, whether to refuse if too large).

    Returns:
        {"selection": {"layer","x0","y0","x1","y1",
                       "width","height","tile_count"} | None,
         "via": "bridge"}
        {"error": "...", "via": "bridge"}  on bridge unreachable / non-200
        {"selection": None, ...}  when bridge is up but no rect dragged yet
    """
    ok, resp = _bridge_get(port, host, "/selection")
    if not ok:
        return {"error": resp, "via": "bridge"}
    sel = resp.get("selection")
    if sel is None:
        return {"selection": None, "via": "bridge"}

    # Normalize and enrich: bridge stores raw (x0..x1) as dragged, may be
    # backward (x1 < x0). We expose a canonical rect plus derived dims.
    x0, x1 = sorted((int(sel["x0"]), int(sel["x1"])))
    y0, y1 = sorted((int(sel["y0"]), int(sel["y1"])))
    width = x1 - x0 + 1
    height = y1 - y0 + 1
    return {
        "selection": {
            "layer": sel.get("layer"),
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "width": width, "height": height,
            "tile_count": width * height,
        },
        "via": "bridge",
    }


def tool_generate_map(
    preset: str = "grass_river_forest",
    seed: int = 11,
    out_name: str = "generated.tmx",
    render_preview: bool = True,
    pack: str | None = None,
    plan: dict | None = None,
) -> dict:
    """scripts/generate_map.py'yi subprocess ile çağırır.

    v0.8.1: `plan` parametresi. `tool_plan_map(...)` çıktısıyla uyumlu bir
    dict (width, height, zones) verilirse, generator bu plana göre
    parametrik çalışır: özel boyut (ör. 20x20, 60x40), zone bounds
    (dirt/river/forest left/right/top/bottom) ve zone devre-dışı bırakma
    (plan'da yoksa skip) desteklenir. `plan=None` default davranış:
    40x40 'grass_river_forest' preset'i (backward-compat).
    """
    if not GENERATOR_SCRIPT.exists():
        return {"error": f"Generator bulunamadı: {GENERATOR_SCRIPT}"}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / out_name

    cmd = [sys.executable, str(GENERATOR_SCRIPT),
           "--seed", str(seed),
           "--out", str(out_path)]
    if pack:
        cmd.extend(["--pack", pack])

    plan_tmp: Path | None = None
    if plan is not None:
        import json as _json
        import tempfile as _tempfile
        fd, path = _tempfile.mkstemp(prefix="tilesmith-plan-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                _json.dump(plan, fh)
        except Exception as e:
            return {"error": f"plan JSON yazılamadı: {e}"}
        plan_tmp = Path(path)
        cmd.extend(["--plan", str(plan_tmp)])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                cwd=str(GENERATOR_SCRIPT.parent))
    finally:
        if plan_tmp is not None:
            try:
                plan_tmp.unlink(missing_ok=True)
            except Exception:
                pass

    output = {
        "preset": preset,
        "seed": seed,
        "pack": pack,
        "plan_applied": plan is not None,
        "tmx_path": str(out_path) if out_path.exists() else None,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "returncode": result.returncode,
    }
    if result.returncode != 0:
        return output

    if render_preview and PREVIEW_SCRIPT.exists():
        png_path = out_path.with_suffix(".png")
        prev = subprocess.run(
            [sys.executable, str(PREVIEW_SCRIPT),
             str(out_path), "--out", str(png_path)],
            capture_output=True, text=True,
            cwd=str(PREVIEW_SCRIPT.parent),
        )
        output["preview_path"] = str(png_path) if png_path.exists() else None
        output["preview_stdout"] = prev.stdout.strip()
        if prev.returncode != 0:
            output["preview_stderr"] = prev.stderr.strip()
    return output


def tool_plan_and_generate(
    width: int = 40,
    height: int = 40,
    components: list[str] | None = None,
    seed: int = 11,
    out_name: str = "generated.tmx",
    render_preview: bool = True,
    pack: str | None = None,
) -> dict:
    """v0.8.1: plan_map + generate_map tek çağrıda (plan → generate chain).

    Bu tool `plan_map(width, height, components)` çağırıp çıkan planı
    `generate_map(plan=plan)`'a besler. Chat'te "40x30 sadece çim+nehir
    üret" gibi tek-adımlı komutlar için ergonomic shortcut.

    Döner: {plan: {...}, generate: {...}} iki alt sonucu birlikte.
    Plan veya generate başarısızsa ilgili alt sonucun 'error'u yukarıya
    taşınmaz — çağıran her ikisini de okumalıdır.
    """
    plan = tool_plan_map(width=width, height=height, components=components)
    gen = tool_generate_map(
        preset="plan_and_generate",
        seed=seed,
        out_name=out_name,
        render_preview=render_preview,
        pack=pack,
        plan=plan,
    )
    return {"plan": plan, "generate": gen}


# ---------------------------------------------------------------------
# MCP Tool kayıtları
# ---------------------------------------------------------------------

TOOL_DEFS: list[tuple[str, str, dict, callable]] = [
    ("db_summary",
     "ERW DB'sindeki tüm tabloların satır sayısı ve DB yolu.",
     {"type": "object", "properties": {}},
     lambda args: tool_db_summary()),

    ("scan_folder",
     "Verilen klasörü recursive olarak tara, TSX/TMX/PNG varlıklarını "
     "DB'ye indeksle. Herhangi bir Tiled uyumlu paket için kullanılabilir. "
     "Bu pack için mevcut _auto kayıtları silinir, _overrides korunur.",
     {"type": "object",
      "properties": {
          "path": {"type": "string",
                   "description": "Taranacak klasörün mutlak yolu"},
          "db_path": {"type": "string",
                      "description": "Opsiyonel DB yolu (varsayılan: plugin DB)"},
          "pack_name": {"type": "string",
                        "description": "Opsiyonel pack adı (default: klasör adı)"},
      },
      "required": ["path"]},
     lambda args: tool_scan_folder(
         args["path"], args.get("db_path"), args.get("pack_name"))),

    ("plan_map",
     "Harita için bir yerleşim planı ve ASCII preview döndür. "
     "Onay öncesi kullanıcıya gösterilmek üzere.",
     {"type": "object",
      "properties": {
          "width": {"type": "integer", "default": 40},
          "height": {"type": "integer", "default": 40},
          "components": {"type": "array", "items": {"type": "string"},
                         "description": "grass|dirt|river|forest listesi"},
      }},
     lambda args: tool_plan_map(
         width=args.get("width", 40),
         height=args.get("height", 40),
         components=args.get("components"),
     )),

    ("consolidate_map",
     "[DEPRECATED v0.8+ — prefer finalize_map] Rewrite a TMX into a single "
     "atlas PNG + self-contained TMX/TSX using only the tiles/props "
     "actually referenced. Kept for backward compatibility.",
     {"type": "object",
      "properties": {
          "tmx_path": {"type": "string"},
          "out_dir": {"type": "string"},
          "out_stem": {"type": "string", "default": "consolidated"},
      },
      "required": ["tmx_path"]},
     lambda args: tool_consolidate_map(
         tmx_path=args["tmx_path"],
         out_dir=args.get("out_dir"),
         out_stem=args.get("out_stem", "consolidated"),
     )),

    ("finalize_map",
     "Freeze a TMX into a deliverable: single-atlas PNG + self-contained "
     "TMX/TSX + optional license summary for every pack referenced. This "
     "is the v0.8+ end-of-project tool; generate_map no longer auto-"
     "consolidates, so call finalize_map once the map is ready to ship.",
     {"type": "object",
      "properties": {
          "tmx_path": {"type": "string"},
          "out_dir": {"type": "string",
                      "description": "Optional output directory"},
          "out_stem": {"type": "string", "default": "final"},
          "include_license_summary": {"type": "boolean", "default": True},
      },
      "required": ["tmx_path"]},
     lambda args: tool_finalize_map(
         tmx_path=args["tmx_path"],
         out_dir=args.get("out_dir"),
         out_stem=args.get("out_stem", "final"),
         include_license_summary=args.get("include_license_summary", True),
     )),

    ("list_tilesets",
     "İndekslenmiş tileset'leri döndürür: pack_name, uid, isim, tile_count, "
     "boyut, is_collection. pack_name ile tek bir pakete filtrelenebilir. "
     "v0.8.2: limit/offset ile paginate edilebilir (her ikisi de opsiyonel). "
     "Paginate edildiğinde {items, total, limit, offset, has_more, "
     "next_offset} döner; aksi halde raw list (backward-compat).",
     {"type": "object",
      "properties": {
          "pack_name": {"type": "string",
                        "description": "Opsiyonel pack filtresi"},
          "limit": {"type": "integer",
                    "description": "Opsiyonel max item count (<=500)"},
          "offset": {"type": "integer", "default": 0},
      }},
     lambda args: tool_list_tilesets(
         args.get("pack_name"),
         limit=args.get("limit"),
         offset=args.get("offset"),
     )),

    ("list_tiles",
     "v0.8.2: Tek bir tileset içindeki tile'ları listele (paginated, "
     "default limit=100). Büyük tileset'lerde (ör. 3960-tile ERW terrain) "
     "local_id aralığı taramak için.",
     {"type": "object",
      "properties": {
          "tileset_uid": {"type": "string",
                          "description": "Örn. 'pack::tileset_name'"},
          "limit": {"type": "integer", "default": 100},
          "offset": {"type": "integer", "default": 0},
      },
      "required": ["tileset_uid"]},
     lambda args: tool_list_tiles(
         tileset_uid=args["tileset_uid"],
         limit=args.get("limit", 100),
         offset=args.get("offset", 0),
     )),

    ("list_wang_sets",
     "Wang set'lerini döndürür (terrain adjacency). "
     "pack_name ile filtrelenebilir. v0.8.2: limit/offset pagination.",
     {"type": "object",
      "properties": {
          "pack_name": {"type": "string"},
          "limit": {"type": "integer"},
          "offset": {"type": "integer", "default": 0},
      }},
     lambda args: tool_list_wang_sets(
         args.get("pack_name"),
         limit=args.get("limit"),
         offset=args.get("offset"),
     )),

    ("list_prop_categories",
     "Prop'ları kategoriye göre grupla (tree, bush, rock, ...). "
     "pack_name ile filtrelenebilir.",
     {"type": "object",
      "properties": {"pack_name": {"type": "string"}}},
     lambda args: tool_list_prop_categories(args.get("pack_name"))),

    ("list_animated_props",
     "Animasyon sprite'larını listele. category ve/veya pack_name ile "
     "filtrelenebilir. v0.8.2: `search` substring filtresi (subject/"
     "filename, case-insens) + limit/offset pagination.",
     {"type": "object",
      "properties": {
          "category": {"type": "string",
                       "description": "Optional: insect/fire/smoke/chest ..."},
          "pack_name": {"type": "string"},
          "search": {"type": "string",
                     "description": "Substring on subject/filename"},
          "limit": {"type": "integer"},
          "offset": {"type": "integer", "default": 0},
      }},
     lambda args: tool_list_animated_props(
         args.get("category"),
         args.get("pack_name"),
         search=args.get("search"),
         limit=args.get("limit"),
         offset=args.get("offset"),
     )),

    ("list_characters",
     "Karakterler ve onların animasyon state'lerini döndür. "
     "pack_name ile filtrelenebilir.",
     {"type": "object",
      "properties": {"pack_name": {"type": "string"}}},
     lambda args: tool_list_characters(args.get("pack_name"))),

    ("list_reference_layers",
     "Sanatçının örnek haritasındaki layer sırası + semantic_role.",
     {"type": "object",
      "properties": {
          "map_uid": {"type": "string"},
          "pack_name": {"type": "string"},
      }},
     lambda args: tool_list_reference_layers(
         args.get("map_uid"), args.get("pack_name"))),

    ("list_automapping_rules",
     "AutoMapping ruleset'leri ve rule'ları. pack_name ile filtrelenebilir.",
     {"type": "object",
      "properties": {"pack_name": {"type": "string"}}},
     lambda args: tool_list_automapping_rules(args.get("pack_name"))),

    ("generate_map",
     "Preset'e göre harita üret. TMX + PNG preview döner. "
     "v0.8.1+: `plan` dict'i verilirse (tool_plan_map çıktısıyla uyumlu) "
     "parametrik boyut + zone konumları + zone devre-dışı bırakma "
     "desteklenir. plan yoksa 40x40 'grass_river_forest' default'u.",
     {"type": "object",
      "properties": {
          "preset": {"type": "string", "default": "grass_river_forest"},
          "seed": {"type": "integer", "default": 11},
          "out_name": {"type": "string", "default": "generated.tmx"},
          "render_preview": {"type": "boolean", "default": True},
          "pack": {"type": "string",
                   "description": "DB'deki pack_name (opsiyonel, default ERW Grass Land 2.0 v1.9)"},
          "plan": {"type": "object",
                   "description": "Opsiyonel plan dict (tool_plan_map "
                                  "çıktısı). width/height + zones "
                                  "generate'e yansır."},
      }},
     lambda args: tool_generate_map(
         preset=args.get("preset", "grass_river_forest"),
         seed=args.get("seed", 11),
         out_name=args.get("out_name", "generated.tmx"),
         render_preview=args.get("render_preview", True),
         pack=args.get("pack"),
         plan=args.get("plan"),
     )),

    ("plan_and_generate",
     "v0.8.1: plan_map + generate_map'ı tek çağrıda zincirler. "
     "width/height/components ile planı oluşturur, sonra generate_map'a "
     "besler. Chat'te 'özel boyut/zone ile harita üret' tek-adım shortcut. "
     "Döner: {plan, generate} iki alt sonucu birlikte.",
     {"type": "object",
      "properties": {
          "width": {"type": "integer", "default": 40},
          "height": {"type": "integer", "default": 40},
          "components": {"type": "array", "items": {"type": "string"},
                         "description": "grass|dirt|river|forest subseti"},
          "seed": {"type": "integer", "default": 11},
          "out_name": {"type": "string", "default": "generated.tmx"},
          "render_preview": {"type": "boolean", "default": True},
          "pack": {"type": "string"},
      }},
     lambda args: tool_plan_and_generate(
         width=args.get("width", 40),
         height=args.get("height", 40),
         components=args.get("components"),
         seed=args.get("seed", 11),
         out_name=args.get("out_name", "generated.tmx"),
         render_preview=args.get("render_preview", True),
         pack=args.get("pack"),
     )),

    ("get_map_state",
     "TMX'i parse edip layer/obje/tile özetini döndürür. "
     "summary_only=true (default): istatistik. false: full JSON (büyük, "
     "chat için önerilmez — Studio bridge /state endpoint'ini kullan).",
     {"type": "object",
      "properties": {
          "tmx_path": {"type": "string"},
          "summary_only": {"type": "boolean", "default": True},
      },
      "required": ["tmx_path"]},
     lambda args: tool_get_map_state(
         tmx_path=args["tmx_path"],
         summary_only=args.get("summary_only", True),
     )),

    ("open_studio",
     "Tilesmith Studio bridge'ini başlatır ve bir TMX yükler. Kullanıcıya "
     "URL döndürür — tarayıcıda açılması gerekir. Layer/asset/animasyonlar "
     "canlı render edilir. Aynı port zaten çalışıyorsa TMX'i reload eder.",
     {"type": "object",
      "properties": {
          "tmx_path": {"type": "string"},
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      },
      "required": ["tmx_path"]},
     lambda args: tool_open_studio(
         tmx_path=args["tmx_path"],
         port=args.get("port", 3024),
         host=args.get("host", "127.0.0.1"),
     )),

    ("close_studio",
     "Studio bridge'ini kapatır.",
     {"type": "object",
      "properties": {"port": {"type": "integer", "default": 3024}}},
     lambda args: tool_close_studio(port=args.get("port", 3024))),

    ("get_selection",
     "Return the user's current rectangle selection from Studio (last rect "
     "dragged in the browser). Response is enriched with width/height/"
     "tile_count. Returns {selection: null} when bridge is up but no rect "
     "has been drawn yet; returns {error} when bridge is unreachable.",
     {"type": "object",
      "properties": {
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      }},
     lambda args: tool_get_selection(
         port=args.get("port", 3024),
         host=args.get("host", "127.0.0.1"))),

    ("place_props",
     "Scatter ObjectGroup tile-objects across a region (prop-aware fill). "
     "Use this to turn a selection into a forest, bush patch, rock cluster, "
     "etc. Region can be an explicit {x0,y0,x1,y1} rect or the string "
     "'selection' to read from the bridge. Variants can be 'composite' "
     "(default, single variant), 'all' (uniform over all variants), a list "
     "of variant names, or a list of [variant, weight] pairs. Writes via "
     "bridge if running (live broadcast) or directly to TMX otherwise.",
     {"type": "object",
      "properties": {
          "tmx_path": {"type": "string"},
          "layer": {"type": "string",
                    "description": "Existing ObjectGroup name"},
          "region": {
              "description": "Either {x0,y0,x1,y1} or the string 'selection'",
          },
          "category": {"type": "string",
                       "description": "DB category filter, e.g. tree/bush/rock"},
          "variants": {
              "description": "'composite' | 'all' | list[str] | list[[str, float]]",
          },
          "density": {"type": "number", "default": 0.3,
                      "description": "0.0-1.0 probability per grid cell"},
          "min_distance": {"type": "integer", "default": 2,
                           "description": "Min tiles between candidate cells"},
          "pack": {"type": "string",
                   "description": "Optional pack_name filter"},
          "seed": {"type": "integer",
                   "description": "Optional reproducibility seed"},
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      },
      "required": ["tmx_path", "layer", "region", "category"]},
     lambda args: tool_place_props(
         tmx_path=args["tmx_path"],
         layer=args["layer"],
         region=args["region"],
         category=args["category"],
         variants=args.get("variants"),
         density=args.get("density", 0.3),
         min_distance=args.get("min_distance", 2),
         pack=args.get("pack"),
         seed=args.get("seed"),
         port=args.get("port", 3024),
         host=args.get("host", "127.0.0.1"),
     )),

    ("add_object",
     "Insert one tile-object into an ObjectGroup by prop_uid. Tile-space "
     "x/y are converted to Tiled pixel+bottom-anchor internally. size_w "
     "and size_h come from the DB props row. Prefers bridge for live "
     "broadcast; falls back to direct TMX write.",
     {"type": "object",
      "properties": {
          "tmx_path": {"type": "string"},
          "layer": {"type": "string",
                    "description": "ObjectGroup name (case-sensitive)"},
          "prop_uid": {"type": "string",
                       "description": "prop_uid from DB props view"},
          "x": {"type": "integer",
                "description": "Tile-space x"},
          "y": {"type": "integer",
                "description": "Tile-space y"},
          "rotation": {"type": "number", "default": 0.0},
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      },
      "required": ["tmx_path", "layer", "prop_uid", "x", "y"]},
     lambda args: tool_add_object(
         tmx_path=args["tmx_path"],
         layer=args["layer"],
         prop_uid=args["prop_uid"],
         x=args["x"],
         y=args["y"],
         rotation=args.get("rotation", 0.0),
         port=args.get("port", 3024),
         host=args.get("host", "127.0.0.1"),
     )),

    ("remove_objects",
     "Batch-remove objects whose CENTER pixel falls inside the given "
     "tile-region. Region can be {x0,y0,x1,y1} or 'selection'. Optional "
     "filters: category (keep only gids whose DB prop has that category) "
     "or prop_uid (exact match). Prefers bridge; falls back to direct.",
     {"type": "object",
      "properties": {
          "tmx_path": {"type": "string"},
          "layer": {"type": "string",
                    "description": "ObjectGroup name"},
          "region": {
              "description": "{x0,y0,x1,y1} or the string 'selection'",
          },
          "category": {"type": "string",
                       "description": "Optional: only remove props of this "
                       "category (e.g. 'tree')"},
          "prop_uid": {"type": "string",
                       "description": "Optional: only remove objects whose "
                       "gid matches this prop_uid"},
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      },
      "required": ["tmx_path", "layer", "region"]},
     lambda args: tool_remove_objects(
         tmx_path=args["tmx_path"],
         layer=args["layer"],
         region=args["region"],
         category=args.get("category"),
         prop_uid=args.get("prop_uid"),
         port=args.get("port", 3024),
         host=args.get("host", "127.0.0.1"),
     )),

    ("studio_undo",
     "Studio'daki en son kaydedilmiş paint/erase/wang patch'ini geri alır. "
     "Stack max 100 derinliğinde. Bridge kapalıysa no-op.",
     {"type": "object",
      "properties": {
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      }},
     lambda args: tool_studio_undo(
         port=args.get("port", 3024),
         host=args.get("host", "127.0.0.1"))),

    ("studio_redo",
     "Studio'da en son undo edilen patch'i tekrar uygular. "
     "Undo sonrasında yeni bir mutation yapılmışsa redo branch'i "
     "temizlenmiştir — bu durumda no-op.",
     {"type": "object",
      "properties": {
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      }},
     lambda args: tool_studio_redo(
         port=args.get("port", 3024),
         host=args.get("host", "127.0.0.1"))),

    ("paint_tiles",
     "Bir tile layer'ına paint/erase patch'i uygular. cells listesindeki "
     "her eleman {x, y, key} şeklinde; key null ise o hücre silinir. "
     "Bridge çalışıyorsa HTTP üzerinden gönderilir ve tüm bağlı "
     "browser'lara canlı broadcast olur; değilse doğrudan TMX'e yazılır.",
     {"type": "object",
      "properties": {
          "tmx_path": {"type": "string"},
          "layer": {"type": "string",
                    "description": "Hedef layer'ın name attribute'u"},
          "cells": {
              "type": "array",
              "items": {
                  "type": "object",
                  "properties": {
                      "x": {"type": "integer"},
                      "y": {"type": "integer"},
                      "key": {"type": ["string", "null"],
                              "description": "Tile key (tileset_stem__lid); "
                                             "null = erase"},
                  },
                  "required": ["x", "y"],
              },
          },
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      },
      "required": ["tmx_path", "layer", "cells"]},
     lambda args: tool_paint_tiles(
         tmx_path=args["tmx_path"],
         layer=args["layer"],
         cells=args["cells"],
         port=args.get("port", 3024),
         host=args.get("host", "127.0.0.1"),
     )),

    ("patch_object",
     "Object group içinde bir object'i taşı/sil/tile-gid değiştir. "
     "op: 'move' (x/y ver), 'delete' (sadece id), 'set_key' (yeni tile key).",
     {"type": "object",
      "properties": {
          "tmx_path": {"type": "string"},
          "group": {"type": "string",
                    "description": "objectgroup name"},
          "op": {"type": "string",
                 "enum": ["move", "delete", "set_key"]},
          "id": {"type": "integer",
                 "description": "Hedef object id (TMX id attribute)"},
          "x": {"type": "number"},
          "y": {"type": "number"},
          "key": {"type": "string",
                  "description": "set_key için yeni tile key"},
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      },
      "required": ["tmx_path", "group", "op", "id"]},
     lambda args: tool_patch_object(
         tmx_path=args["tmx_path"],
         group=args["group"],
         op=args["op"],
         id=args["id"],
         x=args.get("x"),
         y=args.get("y"),
         key=args.get("key"),
         port=args.get("port", 3024),
         host=args.get("host", "127.0.0.1"),
     )),

    ("fill_rect",
     "Bir tile layer'ının dikdörtgen bölgesini doldurur (inclusive "
     "(x0,y0)..(x1,y1)). İki mod — biri zorunlu, ikisi birden verilemez:\n"
     "  - key: tek tile key (null = sil, legacy).\n"
     "  - keys: list[str] (uniform) veya list[[str, weight]] (ağırlıklı). "
     "    'Her çeşit çim' gibi variety fill'ler için.\n"
     "Bridge açıksa broadcast olur, değilse doğrudan TMX'e yazar.",
     {"type": "object",
      "properties": {
          "tmx_path": {"type": "string"},
          "layer": {"type": "string"},
          "x0": {"type": "integer"},
          "y0": {"type": "integer"},
          "x1": {"type": "integer"},
          "y1": {"type": "integer"},
          "key": {"type": ["string", "null"],
                  "description": "Tek-key fill (legacy). null ise siler."},
          "keys": {
              "description": "Multi-key fill: list[str] veya list[[str, weight]]",
          },
          "seed": {"type": "integer",
                   "description": "Reproducibility seed for multi-key"},
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      },
      "required": ["tmx_path", "layer", "x0", "y0", "x1", "y1"]},
     lambda args: tool_fill_rect(
         tmx_path=args["tmx_path"],
         layer=args["layer"],
         x0=args["x0"], y0=args["y0"],
         x1=args["x1"], y1=args["y1"],
         key=args.get("key"),
         keys=args.get("keys"),
         seed=args.get("seed"),
         port=args.get("port", 3024),
         host=args.get("host", "127.0.0.1"),
     )),

    ("fill_selection",
     "Studio browser'da kullanıcının sürükleyerek seçtiği son dikdörtgeni "
     "doldurur. Bridge çalışıyor olmalı. İki mod:\n"
     "  - key: tek tile key (null = sil, legacy).\n"
     "  - keys: list[str] (uniform) veya list[[str, weight]] (ağırlıklı). "
     "    'Her çeşit çim karıştırılmış' gibi variety fill'ler için.\n"
     "Doğal dil akışı için tasarlandı: kullanıcı alan seçer, 'buraya çim "
     "çeşitleri koy' der, Claude bu tool'u çağırır.",
     {"type": "object",
      "properties": {
          "key": {"type": ["string", "null"],
                  "description": "Tek-key fill (legacy). null ise siler."},
          "keys": {
              "description": "Multi-key fill: list[str] veya list[[str, weight]]",
          },
          "seed": {"type": "integer",
                   "description": "Reproducibility seed for multi-key"},
          "tmx_path": {"type": "string",
                       "description": "Direct fallback için (bridge yoksa)"},
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      }},
     lambda args: tool_fill_selection(
         key=args.get("key"),
         keys=args.get("keys"),
         seed=args.get("seed"),
         tmx_path=args.get("tmx_path"),
         port=args.get("port", 3024),
         host=args.get("host", "127.0.0.1"),
     )),

    ("list_wangsets_for_tmx",
     "Bir TMX'in referans ettiği tileset'lere ait wang set'leri listeler. "
     "Her set color listesi (color_index, name, color_hex) ile döner. "
     "Studio bridge açıksa ondan, değilse tmx_path'ten okur. "
     "Palette UI'ları ve 'grass1 to dirt2' gibi doğal dil sorguları için.",
     {"type": "object",
      "properties": {
          "tmx_path": {"type": "string",
                       "description": "TMX yolu (bridge kapalıysa zorunlu)"},
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      }},
     lambda args: tool_list_wangsets_for_tmx(
         tmx_path=args.get("tmx_path"),
         port=args.get("port", 3024),
         host=args.get("host", "127.0.0.1"),
     )),

    ("wang_paint",
     "Wang-aware autotile paint: verilen wangset + color ile cells'i boyar. "
     "Komşu hücrelerin köşeleri otomatik doğru transition tile'larını "
     "seçer (Tiled corner-type wang). Örn: grass1↔dirt2 wangset ile bir "
     "alana grass basınca sınır hücreleri doğru kenar/köşe tile'ı alır. "
     "Bridge açıksa canlı broadcast'lı paint patch'i üretir.",
     {"type": "object",
      "properties": {
          "wangset_uid": {"type": "string",
                          "description": "'pack::tileset::name' formatında wang set uid"},
          "cells": {"type": "array",
                    "items": {"type": "object",
                              "properties": {"x": {"type": "integer"},
                                             "y": {"type": "integer"}},
                              "required": ["x", "y"]},
                    "description": "Paint edilecek hücreler"},
          "color": {"type": "integer", "default": 1,
                    "description": "Wang color_index (1-based; 0 = outside)"},
          "layer": {"type": "string",
                    "description": "Hedef tile layer; belirtilmezse ilk layer"},
          "erase": {"type": "boolean", "default": False,
                    "description": "True ise hücrelerin wang alanından silinmesi"},
          "tmx_path": {"type": "string",
                       "description": "Bridge kapalıysa gerekli (direct fallback)"},
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      },
      "required": ["wangset_uid", "cells"]},
     lambda args: tool_wang_paint(
         wangset_uid=args["wangset_uid"],
         cells=args["cells"],
         color=args.get("color", 1),
         layer=args.get("layer"),
         erase=args.get("erase", False),
         tmx_path=args.get("tmx_path"),
         port=args.get("port", 3024),
         host=args.get("host", "127.0.0.1"),
     )),

    ("wang_fill_rect",
     "Wang-aware dikdörtgen doldurma: (x0,y0)-(x1,y1) alanını verilen "
     "wangset + color ile kaplar ve komşu köşelerin otomatik uyumuyla "
     "doğru transition tile'ları seçilir. Koordinatlar inclusive.",
     {"type": "object",
      "properties": {
          "wangset_uid": {"type": "string"},
          "x0": {"type": "integer"}, "y0": {"type": "integer"},
          "x1": {"type": "integer"}, "y1": {"type": "integer"},
          "color": {"type": "integer", "default": 1},
          "layer": {"type": "string"},
          "erase": {"type": "boolean", "default": False},
          "tmx_path": {"type": "string"},
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      },
      "required": ["wangset_uid", "x0", "y0", "x1", "y1"]},
     lambda args: tool_wang_fill_rect(
         wangset_uid=args["wangset_uid"],
         x0=args["x0"], y0=args["y0"],
         x1=args["x1"], y1=args["y1"],
         color=args.get("color", 1),
         layer=args.get("layer"),
         erase=args.get("erase", False),
         tmx_path=args.get("tmx_path"),
         port=args.get("port", 3024),
         host=args.get("host", "127.0.0.1"),
     )),

    ("wang_fill_selection",
     "Studio'da sürükleyerek çizilen son seçim dikdörtgenini wang-aware "
     "doldurur. Doğal dil akışı: 'orayı toprak wang'i ile doldur'. Bridge "
     "çalışıyor olmalı.",
     {"type": "object",
      "properties": {
          "wangset_uid": {"type": "string"},
          "color": {"type": "integer", "default": 1},
          "erase": {"type": "boolean", "default": False},
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      },
      "required": ["wangset_uid"]},
     lambda args: tool_wang_fill_selection(
         wangset_uid=args["wangset_uid"],
         color=args.get("color", 1),
         erase=args.get("erase", False),
         port=args.get("port", 3024),
         host=args.get("host", "127.0.0.1"),
     )),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name=name, description=desc, inputSchema=schema)
        for name, desc, schema, _ in TOOL_DEFS
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    impl = None
    for tn, _, _, fn in TOOL_DEFS:
        if tn == name:
            impl = fn
            break
    if impl is None:
        return [TextContent(type="text", text=json.dumps({"error": f"unknown tool {name}"}))]
    try:
        result = impl(arguments or {})
    except Exception as e:
        return [TextContent(type="text",
                            text=json.dumps({"error": str(e), "tool": name}))]
    return [TextContent(type="text",
                        text=json.dumps(result, ensure_ascii=False, indent=2))]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
