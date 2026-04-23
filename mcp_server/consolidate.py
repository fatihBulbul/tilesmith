"""
Consolidate a generated TMX into a single-atlas deliverable.

Input:  A TMX file with multiple tilesets (atlas + collection).
Output: A new TMX file + a single PNG atlas containing ONLY the tiles
        and props actually used in the map. The new TMX references the
        single atlas via one tileset.

Approach (shelf bin-packing):
  1. Parse TMX, collect all GIDs used in tile layers + object layers.
  2. Resolve each GID to (tileset_source_path, local_id) using the
     tileset <source="X.tsx" firstgid="N"/> entries.
  3. For atlas tilesets, extract the tile's pixel rectangle from the
     tileset PNG; for collection tilesets, load the tile's per-tile PNG.
  4. Shelf-pack all images (sorted by height desc) into one atlas PNG.
  5. Write a new TMX whose <tileset> is an inline "collection of images"
     pointing to the single atlas PNG, with each tile's <image> carrying
     the final width/height. NOTE: Tiled collection tilesets don't natively
     support sub-rectangles, so for maximum compatibility we emit:
       - A consolidated atlas PNG (one file, the deliverable)
       - Individual per-tile PNG files in <atlas_stem>_sprites/ for the
         collection TSX to reference (each file is a crop of the atlas).
     This preserves the "one atlas PNG" deliverable while staying 100%
     Tiled-compatible.

Output files (all next to <out_tmx>):
  <out_stem>.tmx
  <out_stem>.tsx               (collection tileset)
  <out_stem>.png               (the single consolidated atlas)
  <out_stem>_sprites/*.png     (individual sprites extracted from atlas)
"""

from __future__ import annotations
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from PIL import Image


def _load_tileset(tsx_abs: Path) -> dict:
    """Return dict describing a tileset: atlas vs collection + image map."""
    tree = ET.parse(tsx_abs)
    root = tree.getroot()
    tw = int(root.get("tilewidth", 32))
    th = int(root.get("tileheight", 32))
    columns = int(root.get("columns", 0))
    img_el = root.find("image")
    info: dict = {
        "tsx_path": tsx_abs, "tile_w": tw, "tile_h": th,
        "columns": columns, "tsx_dir": tsx_abs.parent,
    }
    if img_el is not None:
        info["type"] = "atlas"
        info["image_path"] = (tsx_abs.parent / img_el.get("source")).resolve()
    else:
        info["type"] = "collection"
        info["collection"] = {}
        for tile_el in root.findall("tile"):
            tid = int(tile_el.get("id"))
            timg = tile_el.find("image")
            if timg is not None:
                info["collection"][tid] = (
                    tsx_abs.parent / timg.get("source")
                ).resolve()
    # Animations: anchor_tid -> [(frame_tid, duration_ms), ...]
    info["animations"] = {}
    for tile_el in root.findall("tile"):
        tid = int(tile_el.get("id"))
        anim_el = tile_el.find("animation")
        if anim_el is None:
            continue
        frames: list[tuple[int, int]] = []
        for fr in anim_el.findall("frame"):
            frames.append((
                int(fr.get("tileid", "0")),
                int(fr.get("duration", "100")),
            ))
        if frames:
            info["animations"][tid] = frames
    return info


def _get_tile_image(ts_info: dict, local_id: int) -> Image.Image:
    """Return PIL Image for the tile at local_id in the given tileset."""
    if ts_info["type"] == "atlas":
        cols = ts_info["columns"]
        tw, th = ts_info["tile_w"], ts_info["tile_h"]
        col = local_id % cols
        row = local_id // cols
        box = (col * tw, row * th, (col + 1) * tw, (row + 1) * th)
        with Image.open(ts_info["image_path"]) as im:
            return im.convert("RGBA").crop(box)
    else:
        p = ts_info["collection"].get(local_id)
        if p is None:
            raise KeyError(f"collection tile {local_id} not found")
        with Image.open(p) as im:
            return im.convert("RGBA").copy()


def _shelf_pack(items: list[tuple[str, Image.Image]],
                max_width: int = 1024) -> tuple[Image.Image, dict]:
    """Simple shelf bin-packing.

    items: list of (key, PIL.Image)
    Returns: (atlas_image, placements)
      placements: {key: (x, y, w, h)}
    """
    # Sort by height desc, then width desc
    sorted_items = sorted(items, key=lambda kv: (-kv[1].height, -kv[1].width))
    placements: dict = {}
    x = 0
    y = 0
    shelf_h = 0
    rows: list[list[tuple[str, Image.Image, int, int]]] = []
    cur_row: list[tuple[str, Image.Image, int, int]] = []
    for key, im in sorted_items:
        w, h = im.size
        if w > max_width:
            # Oversized: force its own row at max_width (just place at x=0)
            if cur_row:
                rows.append(cur_row)
                cur_row = []
                y += shelf_h
            placements[key] = (0, y, w, h)
            y += h
            shelf_h = 0
            continue
        if x + w > max_width:
            rows.append(cur_row)
            cur_row = []
            y += shelf_h
            x = 0
            shelf_h = 0
        placements[key] = (x, y, w, h)
        cur_row.append((key, im, x, y))
        x += w
        shelf_h = max(shelf_h, h)
    if cur_row:
        rows.append(cur_row)
        y += shelf_h

    atlas_w = max_width
    atlas_h = y if y > 0 else 1
    # Trim unused width
    used_w = 0
    for (kx, ky, kw, kh) in placements.values():
        used_w = max(used_w, kx + kw)
    atlas_w = used_w if used_w > 0 else 1

    atlas = Image.new("RGBA", (atlas_w, atlas_h), (0, 0, 0, 0))
    for key, (px, py, _, _) in placements.items():
        im = dict(items)[key]
        atlas.paste(im, (px, py))
    return atlas, placements


def consolidate(tmx_path: str | Path, out_dir: str | Path,
                out_stem: str = "consolidated",
                max_atlas_width: int = 1024) -> dict:
    """Consolidate a TMX into a single-atlas self-contained TMX.

    Returns dict with paths and stats.
    """
    tmx_path = Path(tmx_path).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Parse TMX
    tree = ET.parse(tmx_path)
    mroot = tree.getroot()
    map_w = int(mroot.get("width"))
    map_h = int(mroot.get("height"))
    tile_w = int(mroot.get("tilewidth"))
    tile_h = int(mroot.get("tileheight"))

    # 2. Collect tileset sources (firstgid, path)
    tsx_refs = []
    for ts in mroot.findall("tileset"):
        fgid = int(ts.get("firstgid"))
        src = ts.get("source")
        if src is None:
            continue  # inline tilesets unsupported here
        tsx_abs = (tmx_path.parent / src).resolve()
        tsx_refs.append((fgid, tsx_abs))
    tsx_refs.sort(key=lambda x: x[0], reverse=True)

    def resolve_gid(gid: int) -> tuple[Path, int] | None:
        gid = gid & 0x1FFFFFFF  # strip flip flags
        if gid == 0:
            return None
        for fgid, tsx in tsx_refs:
            if gid >= fgid:
                return (tsx, gid - fgid)
        return None

    # Cache loaded tilesets
    ts_cache: dict[Path, dict] = {}

    def load_ts(tsx: Path) -> dict:
        if tsx not in ts_cache:
            ts_cache[tsx] = _load_tileset(tsx)
        return ts_cache[tsx]

    # 3. Collect used (tileset, local_id) pairs from tile layers
    used_terrain: set[tuple[Path, int]] = set()
    for layer in mroot.findall("layer"):
        data = layer.find("data")
        if data is None:
            continue
        encoding = data.get("encoding")
        if encoding != "csv":
            continue
        txt = (data.text or "").replace("\n", "").replace(" ", "")
        for tok in txt.split(","):
            tok = tok.strip()
            if not tok:
                continue
            gid = int(tok)
            r = resolve_gid(gid)
            if r is not None:
                used_terrain.add(r)

    # 4. Collect used object gids (props)
    used_props: set[tuple[Path, int]] = set()
    for og in mroot.findall("objectgroup"):
        for obj in og.findall("object"):
            gid_s = obj.get("gid")
            if gid_s is None:
                continue
            r = resolve_gid(int(gid_s))
            if r is not None:
                used_props.add(r)

    # 4b. Expand animated anchors: if any used (tsx, lid) has animation
    # metadata, all frame tileids must also be packed into the atlas.
    def expand_anims(used_set: set[tuple[Path, int]]) -> set[tuple[Path, int]]:
        result = set(used_set)
        frontier = list(used_set)
        while frontier:
            (tsx, lid) = frontier.pop()
            ts_info = load_ts(tsx)
            anim = ts_info.get("animations", {}).get(lid)
            if not anim:
                continue
            for frame_tid, _dur in anim:
                entry = (tsx, frame_tid)
                if entry not in result:
                    result.add(entry)
                    frontier.append(entry)
        return result

    used_terrain = expand_anims(used_terrain)
    used_props = expand_anims(used_props)

    # 5. Extract images for all used entries
    items: list[tuple[str, Image.Image]] = []
    key_to_meta: dict[str, dict] = {}
    for (tsx, lid) in sorted(used_terrain | used_props,
                             key=lambda kv: (str(kv[0]), kv[1])):
        ts_info = load_ts(tsx)
        try:
            im = _get_tile_image(ts_info, lid)
        except Exception as e:
            continue
        key = f"{tsx.stem}__{lid}"
        items.append((key, im))
        key_to_meta[key] = {"tsx": tsx, "local_id": lid,
                            "w": im.width, "h": im.height,
                            "is_terrain": (tsx, lid) in used_terrain,
                            "is_prop": (tsx, lid) in used_props}

    # 6. Shelf-pack into single atlas
    atlas_img, placements = _shelf_pack(items, max_width=max_atlas_width)

    # 7. Write outputs
    atlas_png = out_dir / f"{out_stem}.png"
    sprites_dir = out_dir / f"{out_stem}_sprites"
    sprites_dir.mkdir(exist_ok=True)
    atlas_img.save(atlas_png)

    # Individual per-tile PNGs (for collection TSX) - cropped from atlas
    for key, (px, py, pw, ph) in placements.items():
        sprite_img = atlas_img.crop((px, py, px + pw, py + ph))
        sprite_img.save(sprites_dir / f"{key}.png")

    # 8. Build new TSX (collection of images pointing to sprite PNGs)
    tsx_out = out_dir / f"{out_stem}.tsx"

    # Pass 1: Assign new local IDs in packing order
    new_lid_map: dict[tuple[Path, int], int] = {}
    for new_lid, key in enumerate(placements.keys()):
        meta = key_to_meta[key]
        new_lid_map[(meta["tsx"], meta["local_id"])] = new_lid

    # Pass 2: Build tile XML, attaching animation metadata where present
    tile_entries_xml = []
    anim_count = 0
    for new_lid, key in enumerate(placements.keys()):
        meta = key_to_meta[key]
        rel = (sprites_dir.name + "/" + key + ".png")
        ts_info = load_ts(meta["tsx"])
        anim = ts_info.get("animations", {}).get(meta["local_id"])
        anim_xml = ""
        if anim:
            frame_lines = []
            for frame_tid, duration in anim:
                frame_new_lid = new_lid_map.get((meta["tsx"], frame_tid))
                if frame_new_lid is None:
                    continue
                frame_lines.append(
                    f'    <frame tileid="{frame_new_lid}" '
                    f'duration="{duration}"/>'
                )
            if frame_lines:
                anim_xml = (
                    "   <animation>\n"
                    + "\n".join(frame_lines)
                    + "\n   </animation>\n"
                )
                anim_count += 1
        tile_entries_xml.append(
            f'  <tile id="{new_lid}">\n'
            f'    <image width="{meta["w"]}" height="{meta["h"]}" '
            f'source="{rel}"/>\n'
            f'{anim_xml}'
            f'  </tile>'
        )
    tile_count = len(placements)
    tsx_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<tileset version="1.9" tiledversion="1.9.2" '
        f'name="{out_stem}" tilewidth="{tile_w}" tileheight="{tile_h}" '
        f'tilecount="{tile_count}" columns="0">\n'
        f' <grid orientation="orthogonal" width="1" height="1"/>\n'
        + "\n".join(tile_entries_xml) + "\n"
        '</tileset>\n'
    )
    tsx_out.write_text(tsx_xml, encoding="UTF-8")

    # 9. Build new TMX with single tileset + remapped layers
    new_map = ET.Element("map", {
        "version": "1.9", "tiledversion": "1.9.2",
        "orientation": "orthogonal", "renderorder": "right-down",
        "width": str(map_w), "height": str(map_h),
        "tilewidth": str(tile_w), "tileheight": str(tile_h),
        "infinite": "0",
        "nextlayerid": mroot.get("nextlayerid", "10"),
        "nextobjectid": mroot.get("nextobjectid", "100"),
    })
    ET.SubElement(new_map, "tileset", {
        "firstgid": "1", "source": tsx_out.name,
    })

    # Remap tile layers
    for layer in mroot.findall("layer"):
        new_layer = ET.SubElement(new_map, "layer", {
            "id": layer.get("id", "1"),
            "name": layer.get("name", "layer"),
            "width": str(map_w), "height": str(map_h),
        })
        data = layer.find("data")
        new_data = ET.SubElement(new_layer, "data", {"encoding": "csv"})
        new_rows = []
        txt = (data.text or "").strip().replace(" ", "")
        lines = [ln for ln in txt.split("\n") if ln]
        for ri, line in enumerate(lines):
            toks = [t for t in line.split(",") if t != ""]
            remapped = []
            for t in toks:
                g = int(t)
                r = resolve_gid(g)
                if r is None:
                    remapped.append("0")
                    continue
                new_lid = new_lid_map.get(r)
                if new_lid is None:
                    remapped.append("0")
                else:
                    remapped.append(str(new_lid + 1))  # firstgid=1
            suffix = "," if ri < len(lines) - 1 else ""
            new_rows.append(",".join(remapped) + suffix)
        new_data.text = "\n" + "\n".join(new_rows) + "\n"

    # Remap object layers
    for og in mroot.findall("objectgroup"):
        new_og = ET.SubElement(new_map, "objectgroup", {
            "id": og.get("id", "99"),
            "name": og.get("name", "objects"),
        })
        for obj in og.findall("object"):
            gid_s = obj.get("gid")
            if gid_s is None:
                continue
            g = int(gid_s)
            r = resolve_gid(g)
            if r is None:
                continue
            new_lid = new_lid_map.get(r)
            if new_lid is None:
                continue
            new_obj = ET.SubElement(new_og, "object", {
                "id": obj.get("id", "1"),
                "name": obj.get("name", ""),
                "gid": str(new_lid + 1),
                "x": obj.get("x", "0"),
                "y": obj.get("y", "0"),
                "width": obj.get("width", "0"),
                "height": obj.get("height", "0"),
            })

    out_tmx = out_dir / f"{out_stem}.tmx"
    new_tree = ET.ElementTree(new_map)
    ET.indent(new_tree, space=" ")
    new_tree.write(out_tmx, encoding="UTF-8", xml_declaration=True)

    return {
        "tmx": str(out_tmx),
        "tsx": str(tsx_out),
        "atlas_png": str(atlas_png),
        "sprites_dir": str(sprites_dir),
        "stats": {
            "unique_tiles": len(used_terrain),
            "unique_props": len(used_props),
            "total_unique_assets": len(placements),
            "animated_tiles": anim_count,
            "atlas_width": atlas_img.width,
            "atlas_height": atlas_img.height,
        },
    }


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 3:
        print("Usage: consolidate.py <input.tmx> <output_dir> [stem]")
        sys.exit(1)
    stem = sys.argv[3] if len(sys.argv) > 3 else "consolidated"
    r = consolidate(sys.argv[1], sys.argv[2], stem)
    print(json.dumps(r, indent=2, ensure_ascii=False))
