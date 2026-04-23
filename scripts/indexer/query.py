"""
Query API — DB üzerindeki merged view'ları okuyan yardımcı fonksiyonlar.

Bu modül, `generate_map.py` ve benzeri script'ler için basit bir okuma
arayüzü sağlar. Scanner `_auto` tablolarına yazar, kullanıcı `_overrides`
tablolarına; buradaki fonksiyonların tamamı merged VIEW'ları (örn. `tiles`,
`props`, `wang_sets`) sorguladığı için override'lar otomatik uygulanır.

Şema v0.4.0 ile geldi (bkz. ``mcp_server/scanner.py``).
"""

import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager

# DB path: önce TILESMITH_DB_PATH / ERW_DB_PATH env var, yoksa repo-içi default.
# Bu dosya tilesmith/scripts/indexer/query.py; repo kökü 2 parents yukarı.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(
    os.environ.get("TILESMITH_DB_PATH")
    or os.environ.get("ERW_DB_PATH")
    or str(_REPO_ROOT / "data" / "tiles.db")
)


@contextmanager
def db():
    """Context manager - bağlantıyı otomatik kapatır."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # dict-benzeri erişim
    try:
        yield conn
    finally:
        conn.close()


def _rows(cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]


# --- ÇEKİRDEK SORGU TOOL'LARI ----------------------------------------

def search_tiles(semantic: str | None = None,
                 biome: str | None = None,
                 role: str | None = None,
                 walkable: bool | None = None,
                 pack_name: str | None = None,
                 limit: int = 50) -> list[dict]:
    """Verilen kriterlere uyan tile'ları listele.

    Örn.: search_tiles(semantic="grass", role="fill")
    """
    clauses = []
    params: list = []
    if semantic is not None:
        clauses.append("semantic = ?")
        params.append(semantic)
    if biome is not None:
        clauses.append("biome = ?")
        params.append(biome)
    if role is not None:
        clauses.append("role = ?")
        params.append(role)
    if walkable is not None:
        clauses.append("walkable = ?")
        params.append(1 if walkable else 0)
    if pack_name is not None:
        clauses.append("pack_name = ?")
        params.append(pack_name)

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    sql = f"""
        SELECT pack_name, tile_uid, tileset_uid, tileset, local_id,
               semantic, biome, role, walkable, variant_group, probability,
               atlas_row, atlas_col, image_path
        FROM tiles
        {where}
        LIMIT ?
    """
    params.append(limit)

    with db() as conn:
        cur = conn.execute(sql, params)
        return _rows(cur)


def get_tile_info(tile_uid: str) -> dict | None:
    """Bir tile'ın tüm efektif bilgisini getir."""
    with db() as conn:
        cur = conn.execute(
            "SELECT * FROM tiles WHERE tile_uid = ?", (tile_uid,)
        )
        rows = _rows(cur)
        return rows[0] if rows else None


def get_tile_variants(semantic: str,
                      biome: str | None = None,
                      pack_name: str | None = None) -> list[dict]:
    """Bir semantic için kullanılabilecek tüm varyantları getir."""
    return (search_tiles(semantic=semantic, biome=biome, role="fill",
                         pack_name=pack_name)
            + search_tiles(semantic=semantic, biome=biome,
                           role="fill_variant", pack_name=pack_name))


def list_tilesets(pack_name: str | None = None) -> list[dict]:
    """İndekslenmiş tüm tileset'leri listele."""
    with db() as conn:
        if pack_name:
            cur = conn.execute("""
                SELECT pack_name, tileset_uid, name, tile_count, columns,
                       is_collection, indexed_at
                FROM tilesets
                WHERE pack_name = ?
                ORDER BY name
            """, (pack_name,))
        else:
            cur = conn.execute("""
                SELECT pack_name, tileset_uid, name, tile_count, columns,
                       is_collection, indexed_at
                FROM tilesets
                ORDER BY pack_name, name
            """)
        return _rows(cur)


def count_by_semantic() -> list[dict]:
    """Her semantic kategoride kaç tile var? Debug/istatistik için."""
    with db() as conn:
        cur = conn.execute("""
            SELECT semantic, COUNT(*) AS n
            FROM tiles
            GROUP BY semantic ORDER BY n DESC
        """)
        return _rows(cur)


def count_by_role(semantic: str) -> list[dict]:
    """Bir semantic'in rol dağılımı."""
    with db() as conn:
        cur = conn.execute("""
            SELECT role, COUNT(*) AS n
            FROM tiles
            WHERE semantic = ?
            GROUP BY role ORDER BY n DESC
        """, (semantic,))
        return _rows(cur)


# --- WANG TABANLI SORGULAR -------------------------------------------
# Harita üreticisinin asıl beyni: "main grass dolgu tile'ı ver",
# "dirt1->grass2 kuzey sınırı tile'ı ver" gibi sorular.

def list_wang_sets(tileset: str | None = None,
                   pack_name: str | None = None) -> list[dict]:
    """Tüm wang set'leri, renk/tile sayısıyla listele."""
    clauses = []
    params: list = []
    if tileset is not None:
        clauses.append("tileset = ?")
        params.append(tileset)
    if pack_name is not None:
        clauses.append("pack_name = ?")
        params.append(pack_name)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    sql = f"""
        SELECT pack_name, wangset_uid, tileset, name, type,
               color_count, tile_count
        FROM wang_sets
        {where}
        ORDER BY pack_name, tileset, name
    """
    with db() as conn:
        cur = conn.execute(sql, params)
        return _rows(cur)


def list_wang_colors(wangset_uid: str) -> list[dict]:
    """Bir wang set içindeki renkleri (transition konseptlerini) getir."""
    with db() as conn:
        cur = conn.execute("""
            SELECT color_index, name, color_hex
            FROM wang_colors WHERE wangset_uid = ?
            ORDER BY color_index
        """, (wangset_uid,))
        return _rows(cur)


def find_wangset(name_like: str,
                 pack_name: str | None = None) -> list[dict]:
    """İsmine göre wang set ara ('dirt', 'sand' vs.)."""
    clauses = ["LOWER(name) LIKE LOWER(?)"]
    params: list = [f"%{name_like}%"]
    if pack_name is not None:
        clauses.append("pack_name = ?")
        params.append(pack_name)
    where = " WHERE " + " AND ".join(clauses)
    sql = f"""
        SELECT pack_name, wangset_uid, tileset, name, type,
               color_count, tile_count
        FROM wang_sets
        {where}
        ORDER BY pack_name, name
    """
    with db() as conn:
        cur = conn.execute(sql, params)
        return _rows(cur)


def find_pure_wang_tiles(wangset_uid: str, color_index: int) -> list[dict]:
    """4 köşesi de aynı renkte olan wang tile'ları getir.

    Bu tile'lar 'bölgenin iç dolgusu' olarak kullanılır. Wangset tipine göre
    aktif slotları kontrol eder (corner: köşeler; edge: kenarlar; mixed: 8 slot).
    """
    with db() as conn:
        row = conn.execute(
            "SELECT type FROM wang_sets WHERE wangset_uid = ?",
            (wangset_uid,),
        ).fetchone()
        if row is None:
            return []
        ws_type = row["type"]

        if ws_type == "corner":
            where_extra = ("wt.c_nw=? AND wt.c_ne=? "
                           "AND wt.c_sw=? AND wt.c_se=?")
            params_extra = [color_index] * 4
        elif ws_type == "edge":
            where_extra = ("wt.c_n=? AND wt.c_e=? "
                           "AND wt.c_s=? AND wt.c_w=?")
            params_extra = [color_index] * 4
        else:  # mixed
            where_extra = ("wt.c_n=? AND wt.c_ne=? AND wt.c_e=? AND wt.c_se=? "
                           "AND wt.c_s=? AND wt.c_sw=? AND wt.c_w=? AND wt.c_nw=?")
            params_extra = [color_index] * 8

        sql = f"""
            SELECT wt.tile_uid, wt.c_n, wt.c_ne, wt.c_e, wt.c_se,
                   wt.c_s, wt.c_sw, wt.c_w, wt.c_nw,
                   t.tileset, t.local_id, t.atlas_row, t.atlas_col,
                   t.semantic, t.biome, t.role
            FROM wang_tiles wt
            JOIN tiles t ON t.tile_uid = wt.tile_uid
            WHERE wt.wangset_uid = ? AND {where_extra}
        """
        cur = conn.execute(sql, [wangset_uid, *params_extra])
        return _rows(cur)


def find_wang_tiles_by_corners(wangset_uid: str,
                               nw: int | None = None,
                               ne: int | None = None,
                               sw: int | None = None,
                               se: int | None = None) -> list[dict]:
    """Belirtilen köşe renklerini taşıyan wang tile'ları getir.

    Corner type wang set'lerde sadece NW/NE/SW/SE kullanılır (N,E,S,W=0).
    None geçilen köşeler wildcard olarak muamele görür.
    """
    clauses = ["wt.wangset_uid = ?"]
    params: list = [wangset_uid]
    for col, val in (("c_nw", nw), ("c_ne", ne), ("c_sw", sw), ("c_se", se)):
        if val is not None:
            clauses.append(f"wt.{col} = ?")
            params.append(val)
    where = " AND ".join(clauses)
    sql = f"""
        SELECT wt.tile_uid, wt.c_nw, wt.c_ne, wt.c_sw, wt.c_se,
               t.tileset, t.local_id, t.atlas_row, t.atlas_col
        FROM wang_tiles wt
        JOIN tiles t ON t.tile_uid = wt.tile_uid
        WHERE {where}
    """
    with db() as conn:
        cur = conn.execute(sql, params)
        return _rows(cur)


def find_boundary_wang_tiles(wangset_uid: str,
                             inside_color: int,
                             outside_color: int = 0) -> dict:
    """Verilen rengin bölgesini çerçeveleyen 8+4 yön için boundary tile'ları.

    Sonuç { "N": [tile...], "NE_outer": [...], ... } şeklinde.

    inside_color = dolgu rengi (ör. main grass color_index)
    outside_color = "dışarısı" (wang set'te 0 = renksiz = dolgu dışı)

    Bu fonksiyon harita üreticisinin sınır çizme aracıdır.
    """
    I, O = inside_color, outside_color
    patterns = {
        "N":       (I, I, O, O),
        "S":       (O, O, I, I),
        "W":       (I, O, I, O),
        "E":       (O, I, O, I),
        "NW_outer": (O, O, O, I),  # sadece SE köşesi dolu
        "NE_outer": (O, O, I, O),  # sadece SW köşesi dolu
        "SW_outer": (O, I, O, O),  # sadece NE köşesi dolu
        "SE_outer": (I, O, O, O),  # sadece NW köşesi dolu
        "NW_inner": (O, I, I, I),  # NW hariç hepsi dolu
        "NE_inner": (I, O, I, I),  # NE hariç hepsi dolu
        "SW_inner": (I, I, O, I),  # SW hariç hepsi dolu
        "SE_inner": (I, I, I, O),  # SE hariç hepsi dolu
    }

    result: dict[str, list[dict]] = {}
    for direction, (nw, ne, sw, se) in patterns.items():
        result[direction] = find_wang_tiles_by_corners(
            wangset_uid, nw=nw, ne=ne, sw=sw, se=se
        )
    return result


def get_wang_tile_corners(tile_uid: str) -> list[dict]:
    """Bir tile'ın hangi wang set(ler)inde olduğunu ve köşe renklerini getir."""
    with db() as conn:
        cur = conn.execute("""
            SELECT wt.wangset_uid, ws.name AS wangset_name, ws.type,
                   wt.c_n, wt.c_ne, wt.c_e, wt.c_se,
                   wt.c_s, wt.c_sw, wt.c_w, wt.c_nw
            FROM wang_tiles wt
            JOIN wang_sets ws ON ws.wangset_uid = wt.wangset_uid
            WHERE wt.tile_uid = ?
        """, (tile_uid,))
        return _rows(cur)


# --- PROP / OBJE SORGULARI -------------------------------------------

def search_props(category: str | None = None,
                 pack_name: str | None = None,
                 variant: str | None = "composite",
                 limit: int = 50) -> list[dict]:
    """Prop'ları ara. category='tree', 'bush' vs.

    `variant` default olarak 'composite' — yerleştirmeye hazır tam assetleri
    döndürür. Parça varyantları (trunk/foliage/base/shadow) elle compose
    eden kullanıcılar için `variant=None` ile tüm satırlar alınabilir, ya da
    spesifik varyant adıyla filtrelenebilir.
    """
    clauses = []
    params: list = []
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    if pack_name is not None:
        clauses.append("pack_name = ?")
        params.append(pack_name)
    if variant is not None:
        clauses.append("variant = ?")
        params.append(variant)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    sql = f"""
        SELECT pack_name, prop_uid, tileset_uid, tileset, local_id,
               category, variant, biome_tags, size_w, size_h, image_path
        FROM props
        {where}
        LIMIT ?
    """
    params.append(limit)
    with db() as conn:
        cur = conn.execute(sql, params)
        return _rows(cur)


def count_prop_categories(pack_name: str | None = None) -> list[dict]:
    """Her prop kategoride kaç varlık var?"""
    with db() as conn:
        if pack_name:
            cur = conn.execute("""
                SELECT category, COUNT(*) AS n
                FROM props
                WHERE pack_name = ?
                GROUP BY category ORDER BY n DESC
            """, (pack_name,))
        else:
            cur = conn.execute("""
                SELECT category, COUNT(*) AS n
                FROM props
                GROUP BY category ORDER BY n DESC
            """)
        return _rows(cur)


# --- BİLEŞİK: harita üretici için yüksek seviyeli sorgular -----------

def pick_basic_grass_fillers(tileset: str = "Tileset-Terrain-new grass",
                             local_ids: list[int] | None = None,
                             pack_name: str | None = None) -> list[dict]:
    """Temel çim dolgu tile'ları.

    İki mod:
      1. local_ids verilirse: sadece o ID'leri getir.
      2. local_ids=None: verilen tileset'te wang set'te OLMAYAN tile'ları getir.
    """
    with db() as conn:
        pack_clause = "AND t.pack_name = ?" if pack_name else ""
        pack_params: list = [pack_name] if pack_name else []
        if local_ids:
            placeholders = ",".join(["?"] * len(local_ids))
            sql = f"""
                SELECT t.pack_name, t.tile_uid, t.tileset, t.local_id,
                       t.atlas_row, t.atlas_col, t.semantic, t.role
                FROM tiles t
                WHERE t.tileset = ?
                  AND t.local_id IN ({placeholders})
                  {pack_clause}
                ORDER BY t.local_id
            """
            params = [tileset, *local_ids, *pack_params]
        else:
            sql = f"""
                SELECT t.pack_name, t.tile_uid, t.tileset, t.local_id,
                       t.atlas_row, t.atlas_col, t.semantic, t.role
                FROM tiles t
                WHERE t.tileset = ?
                  AND t.tile_uid NOT IN (SELECT tile_uid FROM wang_tiles)
                  {pack_clause}
                ORDER BY t.tile_uid
            """
            params = [tileset, *pack_params]
        cur = conn.execute(sql, params)
        return _rows(cur)


# --- CLI: hızlı test için --------------------------------------------

if __name__ == "__main__":
    print("=== Tileset listesi ===")
    for ts in list_tilesets():
        print(f"  [{ts['pack_name']}] {ts['name']}: "
              f"{ts['tile_count']} tile, {ts['columns']} col")

    print("\n=== Semantic dağılımı ===")
    for row in count_by_semantic():
        print(f"  {row['semantic']}: {row['n']}")

    print("\n=== Grass rol dağılımı ===")
    for row in count_by_role("grass"):
        print(f"  {row['role']}: {row['n']}")

    print("\n=== Örnek sorgu: fill rolündeki çim tile'ları (ilk 10) ===")
    for r in search_tiles(semantic="grass", role="fill", limit=10):
        print(f"  {r['tile_uid']}  (atlas: r{r['atlas_row']} c{r['atlas_col']})")
