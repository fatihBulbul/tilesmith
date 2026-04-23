"""
TMX write-path: tek bir TMX dosyasına atomik patch'ler uygular.

İki operasyon destekleniyor:
  - paint_tiles(layer_name, cells): bir tile layer'ında hücre üstüne yaz/sil
  - patch_object(group, id, op, **kw): object group içinde taşı/sil/güncelle

Key formatı tmx_state ile aynı: "{tileset_stem}__{local_id}".
"safe stem" dönüşümü bu modülde de aynı normalize kuralıyla yapılır.

Her patch, dosyayı atomik olarak yeniden yazar (temp file + os.replace)
ki yazılırken crash olursa dosya bozulmasın.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import io
import os
import re
import tempfile
import xml.etree.ElementTree as ET


def _safe_stem(tileset_stem: str) -> str:
    out = []
    for ch in tileset_stem:
        if ch.isalnum() or ch in "-_.":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def _parse_key(key: str) -> tuple[str, int]:
    """"{safe_stem}__{lid}" -> (safe_stem, lid)"""
    m = re.match(r"^(.+)__(\d+)$", key)
    if not m:
        raise ValueError(f"kötü tile key: {key!r}")
    return m.group(1), int(m.group(2))


@dataclass
class TilesetRef:
    firstgid: int
    safe_stem: str
    source: str  # orijinal relative path


def _load_tileset_refs(root: ET.Element, tmx_dir: Path) -> list[TilesetRef]:
    refs: list[TilesetRef] = []
    for ts_el in root.findall("tileset"):
        src = ts_el.get("source")
        if src is None:
            continue
        fgid = int(ts_el.get("firstgid", "0"))
        stem = Path(src).stem
        refs.append(TilesetRef(firstgid=fgid, safe_stem=_safe_stem(stem),
                               source=src))
    refs.sort(key=lambda r: -r.firstgid)  # büyük firstgid önce
    return refs


def _key_to_gid(refs: list[TilesetRef], key: str) -> int:
    stem, lid = _parse_key(key)
    for r in refs:
        if r.safe_stem == stem:
            return r.firstgid + lid
    raise ValueError(
        f"tileset bulunamadı: stem={stem!r} (bilinen: "
        f"{[r.safe_stem for r in refs]})"
    )


# ---------------------------------------------------------------------
# Paint
# ---------------------------------------------------------------------

@dataclass
class PaintCell:
    x: int
    y: int
    key: str | None  # None = erase


def _parse_csv_layer(text: str) -> list[list[int]]:
    rows: list[list[int]] = []
    for line in text.strip().split("\n"):
        line = line.strip().rstrip(",")
        if not line:
            continue
        rows.append([int(t.strip() or "0") for t in line.split(",")])
    return rows


def _format_csv_layer(grid: list[list[int]]) -> str:
    # One row per line, trailing comma, leading newline so opening tag is alone.
    lines = [",".join(str(g) for g in row) + "," for row in grid]
    return "\n" + "\n".join(lines) + "\n"


def apply_paint(
    tmx_path: str | Path,
    layer_name: str,
    cells: list[dict | PaintCell],
) -> dict:
    """Tile layer'ı üzerinde cells patch'ini uygular, TMX'i diske yazar.

    Args:
        tmx_path: TMX dosyası
        layer_name: hedef layer'ın name attribute'u
        cells: [{x, y, key|None}, ...] — key None ise erase

    Returns:
        {ok, layer, cells_applied, cells_skipped, width, height}
    """
    tmx = Path(tmx_path).resolve()
    tree = ET.parse(tmx)
    root = tree.getroot()
    refs = _load_tileset_refs(root, tmx.parent)

    layer_el = None
    for le in root.findall("layer"):
        if le.get("name") == layer_name:
            layer_el = le
            break
    if layer_el is None:
        raise ValueError(f"layer '{layer_name}' yok")

    map_w = int(root.get("width", "0"))
    map_h = int(root.get("height", "0"))
    lay_w = int(layer_el.get("width", str(map_w)))
    lay_h = int(layer_el.get("height", str(map_h)))

    data_el = layer_el.find("data")
    if data_el is None:
        raise ValueError(f"layer '{layer_name}' data yok")
    if (data_el.get("encoding") or "csv") != "csv":
        raise ValueError(f"sadece csv encoding destekleniyor")

    grid = _parse_csv_layer(data_el.text or "")
    # Pad if needed (should match layer_w x layer_h)
    while len(grid) < lay_h:
        grid.append([0] * lay_w)
    for row in grid:
        while len(row) < lay_w:
            row.append(0)

    applied = 0
    skipped = 0
    for raw in cells:
        if isinstance(raw, PaintCell):
            c = raw
        else:
            c = PaintCell(
                x=int(raw["x"]), y=int(raw["y"]),
                key=raw.get("key"),
            )
        if not (0 <= c.x < lay_w and 0 <= c.y < lay_h):
            skipped += 1
            continue
        if c.key is None:
            grid[c.y][c.x] = 0
        else:
            try:
                grid[c.y][c.x] = _key_to_gid(refs, c.key)
            except ValueError:
                skipped += 1
                continue
        applied += 1

    data_el.text = _format_csv_layer(grid)
    _atomic_write_xml(tree, tmx)

    return {
        "ok": True,
        "layer": layer_name,
        "cells_applied": applied,
        "cells_skipped": skipped,
        "width": lay_w, "height": lay_h,
    }


# ---------------------------------------------------------------------
# Object patches
# ---------------------------------------------------------------------

def apply_object_patch(
    tmx_path: str | Path,
    group_name: str,
    patch: dict,
) -> dict:
    """objectgroup içinde bir object'i taşı / sil / güncelle.

    patch şekilleri:
      {op: "move",   id: int, x: float, y: float}
      {op: "delete", id: int}
      {op: "set_key", id: int, key: str}    # tile objesinin gid'ini değiştir

    Dönüş:
      {ok, group, op, id, ...}
    """
    tmx = Path(tmx_path).resolve()
    tree = ET.parse(tmx)
    root = tree.getroot()

    og_el = None
    for og in root.findall("objectgroup"):
        if og.get("name") == group_name:
            og_el = og
            break
    if og_el is None:
        raise ValueError(f"objectgroup '{group_name}' yok")

    op = patch.get("op")
    obj_id = int(patch.get("id", -1))
    if obj_id < 0:
        raise ValueError("patch.id gerekli")

    target = None
    for o in og_el.findall("object"):
        if int(o.get("id", "-1")) == obj_id:
            target = o
            break
    if target is None and op != "create":
        raise ValueError(f"object id={obj_id} bulunamadı ({group_name})")

    result: dict[str, Any] = {"ok": True, "group": group_name, "op": op,
                              "id": obj_id}

    if op == "move":
        x = patch.get("x"); y = patch.get("y")
        if x is not None:
            target.set("x", _fmt_float(x))
            result["x"] = float(x)
        if y is not None:
            target.set("y", _fmt_float(y))
            result["y"] = float(y)

    elif op == "delete":
        og_el.remove(target)

    elif op == "set_key":
        key = patch.get("key")
        if not key:
            raise ValueError("set_key için key gerekli")
        refs = _load_tileset_refs(root, tmx.parent)
        gid = _key_to_gid(refs, key)
        target.set("gid", str(gid))
        result["key"] = key
        result["gid"] = gid

    else:
        raise ValueError(f"bilinmeyen op: {op!r}")

    _atomic_write_xml(tree, tmx)
    return result


def _fmt_float(v: float | int | str) -> str:
    f = float(v)
    if f.is_integer():
        return f"{int(f)}"
    return f"{f:g}"


# ---------------------------------------------------------------------
# Atomik dosya yazma
# ---------------------------------------------------------------------

def _atomic_write_xml(tree: ET.ElementTree, path: Path) -> None:
    # Tiled'in beklediği XML header ile yaz.
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=".tmx.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        os.close(tmp_fd)
        tree.write(tmp_name, encoding="utf-8", xml_declaration=True)
        # Tiled UTF-8 single-quote tercih eder ama ET çift tırnak yazar; Tiled
        # iki formu da kabul ediyor — dokunmuyoruz.
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------
# CLI (debug)
# ---------------------------------------------------------------------

if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 4:
        print("Usage:")
        print("  tmx_mutator.py paint <tmx> <layer> <json cells>")
        print("  tmx_mutator.py object <tmx> <group> <json patch>")
        sys.exit(1)
    mode = sys.argv[1]
    if mode == "paint":
        cells = json.loads(sys.argv[4])
        print(json.dumps(apply_paint(sys.argv[2], sys.argv[3], cells), indent=2))
    elif mode == "object":
        patch = json.loads(sys.argv[4])
        print(json.dumps(apply_object_patch(sys.argv[2], sys.argv[3], patch),
                         indent=2))
    else:
        print("unknown mode", mode)
        sys.exit(1)
