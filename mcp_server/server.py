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


def tool_list_tilesets(pack_name: str | None = None) -> list[dict]:
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
        return rows_to_json(rows)
    finally:
        conn.close()


def tool_list_wang_sets(pack_name: str | None = None) -> list[dict]:
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
        return rows_to_json(rows)
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


def tool_list_animated_props(category: str | None = None,
                             pack_name: str | None = None) -> list[dict]:
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
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"""SELECT pack_name, aprop_uid, filename, category, subject, action,
                       variant, frame_count, frame_w, frame_h
                FROM animated_props
                {clause}
                ORDER BY category, subject""",
            params,
        ).fetchall()
        return rows_to_json(rows)
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
    """TMX'i tek atlas PNG + kullanılan asset'lerle yeniden yaz."""
    out = Path(out_dir) if out_dir else (OUTPUT_DIR / out_stem)
    return consolidate_impl(tmx_path, out, out_stem)


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


def tool_fill_rect(
    tmx_path: str,
    layer: str,
    x0: int, y0: int, x1: int, y1: int,
    key: str | None,
    port: int = 3024,
    host: str = "127.0.0.1",
) -> dict:
    """Tile layer'ının dikdörtgen bir bölgesini tek bir tile key ile doldurur
    (key=None ise bölgeyi siler).

    Bridge açıksa onun üzerinden broadcast olur; değilse doğrudan TMX'e
    yazılır.
    """
    from tmx_mutator import apply_paint

    body = {
        "layer": layer,
        "region": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
        "key": key,
    }
    ok, resp = _bridge_post(port, host, "/fill", body)
    if ok:
        resp["via"] = "bridge"
        return resp
    if "unreachable" not in resp.get("error", ""):
        return {"error": resp, "via": "bridge"}

    # Direct fallback: expand rect into cells, call apply_paint
    xmin, xmax = sorted((int(x0), int(x1)))
    ymin, ymax = sorted((int(y0), int(y1)))
    cells = [{"x": x, "y": y, "key": key}
             for y in range(ymin, ymax + 1)
             for x in range(xmin, xmax + 1)]
    try:
        res = apply_paint(tmx_path, layer, cells)
        res["via"] = "direct"
        res["region"] = {"x0": xmin, "y0": ymin, "x1": xmax, "y1": ymax}
        return res
    except Exception as e:
        return {"error": str(e), "via": "direct"}


def tool_fill_selection(
    key: str | None,
    port: int = 3024,
    host: str = "127.0.0.1",
) -> dict:
    """Kullanıcının studio'da sürükleyerek seçtiği son dikdörtgeni belirtilen
    tile key ile doldurur (null ise siler). Bridge çalışıyor olmalı.

    Bridge GET /selection ile mevcut seçimi okur, sonra /fill'e post eder.
    """
    # 1) Seçimi oku
    ok, resp = _bridge_get(port, host, "/selection")
    if not ok:
        return {"error": resp, "via": "bridge"}
    sel = resp.get("selection")
    if sel is None:
        return {"error": "studio'da henüz bir seçim yapılmadı "
                         "(select tool ile dikdörtgen çiz)"}

    # 2) Doldur
    body = {"key": key}  # bridge uses last_selection as default region+layer
    ok, filled = _bridge_post(port, host, "/fill", body)
    if not ok:
        return {"error": filled, "via": "bridge"}
    filled["via"] = "bridge"
    filled["selection_used"] = sel
    return filled


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


def tool_generate_map(
    preset: str = "grass_river_forest",
    seed: int = 11,
    out_name: str = "generated.tmx",
    render_preview: bool = True,
    pack: str | None = None,
) -> dict:
    """scripts/generate_map.py'yi subprocess ile çağırır."""
    if not GENERATOR_SCRIPT.exists():
        return {"error": f"Generator bulunamadı: {GENERATOR_SCRIPT}"}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / out_name

    cmd = [sys.executable, str(GENERATOR_SCRIPT),
           "--seed", str(seed),
           "--out", str(out_path)]
    if pack:
        cmd.extend(["--pack", pack])
    result = subprocess.run(cmd, capture_output=True, text=True,
                            cwd=str(GENERATOR_SCRIPT.parent))
    output = {
        "preset": preset,
        "seed": seed,
        "pack": pack,
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
     "Üretilmiş TMX'i alıp yalnızca kullanılan tile+prop'lardan oluşan "
     "tek atlas PNG + kendi kendine yeten TMX+TSX çıktısı üretir.",
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

    ("list_tilesets",
     "İndekslenmiş tileset'leri döndürür: pack_name, uid, isim, tile_count, "
     "boyut, is_collection. pack_name ile tek bir pakete filtrelenebilir.",
     {"type": "object",
      "properties": {"pack_name": {"type": "string",
                                   "description": "Opsiyonel pack filtresi"}}},
     lambda args: tool_list_tilesets(args.get("pack_name"))),

    ("list_wang_sets",
     "Wang set'lerini döndürür (terrain adjacency). "
     "pack_name ile filtrelenebilir.",
     {"type": "object",
      "properties": {"pack_name": {"type": "string"}}},
     lambda args: tool_list_wang_sets(args.get("pack_name"))),

    ("list_prop_categories",
     "Prop'ları kategoriye göre grupla (tree, bush, rock, ...). "
     "pack_name ile filtrelenebilir.",
     {"type": "object",
      "properties": {"pack_name": {"type": "string"}}},
     lambda args: tool_list_prop_categories(args.get("pack_name"))),

    ("list_animated_props",
     "Animasyon sprite'larını listele. category ve/veya pack_name ile "
     "filtrelenebilir.",
     {"type": "object",
      "properties": {
          "category": {"type": "string",
                       "description": "Optional: insect/fire/smoke/chest ..."},
          "pack_name": {"type": "string"},
      }},
     lambda args: tool_list_animated_props(
         args.get("category"), args.get("pack_name"))),

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
     "Preset: 'grass_river_forest' (40x40 çim+nehir+orman). "
     "pack parametresi ile kullanılacak paket (DB'deki pack_name) seçilebilir.",
     {"type": "object",
      "properties": {
          "preset": {"type": "string", "default": "grass_river_forest"},
          "seed": {"type": "integer", "default": 11},
          "out_name": {"type": "string", "default": "generated.tmx"},
          "render_preview": {"type": "boolean", "default": True},
          "pack": {"type": "string",
                   "description": "DB'deki pack_name (opsiyonel, default ERW Grass Land 2.0 v1.9)"},
      }},
     lambda args: tool_generate_map(
         preset=args.get("preset", "grass_river_forest"),
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
     "Bir tile layer'ının dikdörtgen bölgesini tek bir tile key ile doldurur "
     "(key=null ise bölgeyi siler). Koordinatlar inclusive — (x0,y0)..(x1,y1) "
     "kare dahil. Bridge açıksa broadcast olur, değilse doğrudan TMX'e yazar.",
     {"type": "object",
      "properties": {
          "tmx_path": {"type": "string"},
          "layer": {"type": "string"},
          "x0": {"type": "integer"},
          "y0": {"type": "integer"},
          "x1": {"type": "integer"},
          "y1": {"type": "integer"},
          "key": {"type": ["string", "null"],
                  "description": "Doldurulacak tile key; null ise bölgeyi siler"},
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      },
      "required": ["tmx_path", "layer", "x0", "y0", "x1", "y1", "key"]},
     lambda args: tool_fill_rect(
         tmx_path=args["tmx_path"],
         layer=args["layer"],
         x0=args["x0"], y0=args["y0"],
         x1=args["x1"], y1=args["y1"],
         key=args["key"],
         port=args.get("port", 3024),
         host=args.get("host", "127.0.0.1"),
     )),

    ("fill_selection",
     "Studio browser'da kullanıcının sürükleyerek seçtiği son dikdörtgeni "
     "verilen tile key ile doldurur (null ise siler). Bridge çalışıyor olmalı. "
     "Doğal dil akışı için tasarlandı: kullanıcı canvas'ta alan seçer, "
     "\"orayı çimle doldur\" der, Claude bu tool'u çağırır.",
     {"type": "object",
      "properties": {
          "key": {"type": ["string", "null"],
                  "description": "Doldurulacak tile key; null ise bölgeyi siler"},
          "port": {"type": "integer", "default": 3024},
          "host": {"type": "string", "default": "127.0.0.1"},
      },
      "required": ["key"]},
     lambda args: tool_fill_selection(
         key=args["key"],
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
