"""
TMX -> PNG preview render'ı (Tiled olmadan).

Destekler:
  - Çoklu tileset (image veya collection)
  - Birden fazla tile layer (sıralı, şeffaflıkla)
  - Object layer (tile objects için: gid'den prop resmi çöz)

Tiled object Y-konvansiyonu: object.y = objenin ALT kenarının Y piksel koordinatı.

Kullanım:
  python3 preview_map.py output/demo-river-forest.tmx --scale 1
"""

from __future__ import annotations
import argparse
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image


# --- TSX -------------------------------------------------------------

class ImageTileset:
    """Tek atlas PNG'den kesilen tileset."""
    def __init__(self, tsx_path: Path):
        root = ET.parse(tsx_path).getroot()
        self.tile_w = int(root.get("tilewidth"))
        self.tile_h = int(root.get("tileheight"))
        self.columns = int(root.get("columns", 0))
        self.tile_count = int(root.get("tilecount", 0))
        image_el = root.find("image")
        self.image_path = (tsx_path.parent / image_el.get("source")).resolve()
        self._atlas: Image.Image | None = None
        self.is_collection = False

    def atlas(self) -> Image.Image:
        if self._atlas is None:
            self._atlas = Image.open(self.image_path).convert("RGBA")
        return self._atlas

    def get_tile(self, local_id: int) -> Image.Image | None:
        if self.columns == 0:
            return None
        col = local_id % self.columns
        row = local_id // self.columns
        sx = col * self.tile_w
        sy = row * self.tile_h
        a = self.atlas()
        if sx + self.tile_w > a.width or sy + self.tile_h > a.height:
            return None
        return a.crop((sx, sy, sx + self.tile_w, sy + self.tile_h))


class CollectionTileset:
    """Her tile'ın kendi PNG'si olan tileset."""
    def __init__(self, tsx_path: Path):
        root = ET.parse(tsx_path).getroot()
        self.tile_w = int(root.get("tilewidth", 0))
        self.tile_h = int(root.get("tileheight", 0))
        self.images: dict[int, Path] = {}
        self.sizes: dict[int, tuple[int, int]] = {}
        for tile_el in root.findall("tile"):
            local_id = int(tile_el.get("id"))
            img_el = tile_el.find("image")
            if img_el is not None:
                path = (tsx_path.parent / img_el.get("source")).resolve()
                self.images[local_id] = path
                w = int(img_el.get("width", 0))
                h = int(img_el.get("height", 0))
                self.sizes[local_id] = (w, h)
        self._cache: dict[int, Image.Image] = {}
        self.is_collection = True

    def get_tile(self, local_id: int) -> Image.Image | None:
        if local_id not in self.images:
            return None
        if local_id not in self._cache:
            self._cache[local_id] = Image.open(
                self.images[local_id]
            ).convert("RGBA")
        return self._cache[local_id]

    def get_size(self, local_id: int) -> tuple[int, int]:
        if local_id in self.sizes:
            return self.sizes[local_id]
        img = self.get_tile(local_id)
        return (img.width, img.height) if img else (0, 0)


def load_tileset(tsx_path: Path):
    root = ET.parse(tsx_path).getroot()
    if root.find("image") is not None:
        return ImageTileset(tsx_path)
    return CollectionTileset(tsx_path)


# --- TMX -------------------------------------------------------------

class TMX:
    def __init__(self, tmx_path: Path):
        self.path = tmx_path
        root = ET.parse(tmx_path).getroot()
        self.width = int(root.get("width"))
        self.height = int(root.get("height"))
        self.tile_w = int(root.get("tilewidth"))
        self.tile_h = int(root.get("tileheight"))

        # Tilesets (firstgid azalan sırada saklıyoruz, gid->tileset çözümü için)
        self.tilesets: list[tuple[int, object]] = []  # (firstgid, tileset)
        for ts_el in root.findall("tileset"):
            firstgid = int(ts_el.get("firstgid"))
            source = ts_el.get("source")
            tsx_path = (tmx_path.parent / source).resolve()
            ts = load_tileset(tsx_path)
            self.tilesets.append((firstgid, ts))
        self.tilesets.sort(key=lambda x: -x[0])  # büyükten küçüğe

        # Tile layers (sıralı)
        self.tile_layers: list[tuple[str, list[list[int]]]] = []
        # Object layers
        self.obj_layers: list[tuple[str, list[dict]]] = []

        for child in root:
            if child.tag == "layer":
                name = child.get("name", "")
                data = child.find("data")
                rows: list[list[int]] = []
                for row_text in (data.text or "").strip().split("\n"):
                    row_text = row_text.strip().rstrip(",")
                    if not row_text:
                        continue
                    rows.append([int(v) for v in row_text.split(",")])
                self.tile_layers.append((name, rows))
            elif child.tag == "objectgroup":
                name = child.get("name", "")
                objs: list[dict] = []
                for o in child.findall("object"):
                    objs.append({
                        "gid": int(o.get("gid", 0)),
                        "x": float(o.get("x", 0)),
                        "y": float(o.get("y", 0)),
                        "w": float(o.get("width", 0)),
                        "h": float(o.get("height", 0)),
                    })
                self.obj_layers.append((name, objs))

    def resolve_gid(self, gid: int):
        for firstgid, ts in self.tilesets:
            if gid >= firstgid:
                return firstgid, ts
        return None, None


# --- Render ---------------------------------------------------------

def render(tmx_path: Path, out_path: Path, scale: int = 1) -> None:
    tmx = TMX(tmx_path)
    canvas = Image.new(
        "RGBA", (tmx.width * tmx.tile_w, tmx.height * tmx.tile_h), (0, 0, 0, 255)
    )

    # 1) Tile layers
    for name, grid in tmx.tile_layers:
        for y, row in enumerate(grid):
            for x, gid in enumerate(row):
                if gid == 0:
                    continue
                firstgid, ts = tmx.resolve_gid(gid)
                if ts is None:
                    continue
                tile = ts.get_tile(gid - firstgid)
                if tile is None:
                    continue
                canvas.paste(tile, (x * tmx.tile_w, y * tmx.tile_h), tile)

    # 2) Object layers (tile objects)
    for name, objs in tmx.obj_layers:
        # y'ye göre sırala (aşağıdaki objeler önce basılsın? hayır, üstten alta)
        for o in sorted(objs, key=lambda a: a["y"]):
            gid = o["gid"]
            if gid == 0:
                continue
            firstgid, ts = tmx.resolve_gid(gid)
            if ts is None:
                continue
            tile = ts.get_tile(gid - firstgid)
            if tile is None:
                continue
            # Tiled: object.x = sol, object.y = ALT kenar
            px = int(round(o["x"]))
            py = int(round(o["y"])) - tile.height
            canvas.paste(tile, (px, py), tile)

    if scale != 1:
        canvas = canvas.resize(
            (canvas.width * scale, canvas.height * scale), Image.NEAREST
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    print(f"Yazıldı: {out_path}  ({canvas.size[0]}x{canvas.size[1]})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tmx", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--scale", type=int, default=1)
    args = ap.parse_args()

    out = args.out or args.tmx.with_suffix(".preview.png")
    render(args.tmx, out, scale=args.scale)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
