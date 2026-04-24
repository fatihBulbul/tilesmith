# Tilesmith v0.8 Roadmap — "Prop-level Editing & Parametric Generation"

**Base:** v0.7.3 (self-bootstrap + prebuilt Studio frontend shipped)
**Source:** UX/API gap report (real user test, 2026-04) + maintainer priorities
**Goal:** Close the "önizleme mekaniği → düzenleme mekaniği" gap

---

## 0. Baseline (what works, must not regress)

- `scan_folder` 224k tile saniyeler içinde indeksliyor — dokunma.
- `wang_fill_rect` corner terrain geçişleri — dokunma.
- `consolidate_map` tek atlas PNG — dokunma (ama `generate_map` sonrası otomatik çağrılmayacak; bkz. §1.6).
- Studio bridge canlı WS broadcast + `last_selection` state — **zaten var**, sadece MCP yüzünde bir tool eksik.
- Tile animasyon pass-through (36 animated tile otomatik) — dokunma.

---

## Flow değişikliği — Lazy consolidate (onaylandı)

**Eski akış (v0.7.3):** `generate_map → consolidate_map (zorunlu) → kullanıcıya göster → edit → final consolidate` → ilk atlas boşa gider.

**Yeni akış (v0.8+):** `generate_map (ham TMX + preview PNG) → open_studio → iteratif edit → finalize_map → tek atlas deliverable`. Atlas sadece kullanıcı "bitti, teslim" dediğinde üretilir.

Gerekçe: Consolidate pahalı (tile toplama + PNG packing + TSX rewrite). Editleyeceği belli olan haritada ilk atlas her zaman boşa gidiyordu. Intent-driven bir `finalize_map` tool'u ile açıkça ayrılır.

---

## 1. Critical path — v0.8.0 ("Prop editing")

Bu milestone olmadan kullanıcının "seçili alanı ağaçlandır" promptu çalışmaz.

### 1.1. `get_selection` tool — **EN KOLAY KAZANIM**

Mevcut: `studio/bridge/server.py` zaten `STATE.last_selection` tutuyor ve `GET /selection` endpoint'i var (satır 552). Sadece MCP yüzüne wrap etmek gerekiyor.

```python
# mcp_server/server.py (yeni)
def tool_get_selection(port: int = 3024, host: str = "127.0.0.1") -> dict:
    # HTTP GET http://host:port/selection → {selection: {layer, x0, y0, x1, y1} | null}
```

**Output şeması:**
```json
{"layer": "forest", "x0": 12, "y0": 8, "x1": 28, "y1": 22,
 "width": 17, "height": 15, "tile_count": 255}
```

Bridge kapalıysa `{selection: null}` döner — agent "Studio'yu aç" demeli.

**Effort:** ~1 saat (1 tool def, 1 HTTP GET, 1 test).

### 1.2. `place_props` tool — **KRITIK**

Prop-aware region fill. Object layer'a (ObjectGroup) tree/bush/rock obje scatter'lar.

```python
def tool_place_props(
    tmx_path: str,
    layer: str,                    # ObjectGroup adı, örn "forest"
    region: dict | str,            # {x0,y0,x1,y1} veya "selection"
    category: str,                 # "tree" | "bush" | "rock" | ...
    variants: list[str] | str = "composite",  # ["composite"] | "all" | specific keys
    density: float = 0.3,          # 0.0-1.0, tile başına obje yoğunluğu
    min_distance: int = 2,         # Poisson disc min spacing (tile)
    pack: str | None = None,
    seed: int | None = None,
) -> dict:
    """Returns: {placed: int, skipped: int, variant_counts: {key: n}}"""
```

**İç tasarım:**
- Query: `SELECT key, width_px, height_px FROM props_auto WHERE category=? AND pack_name=? AND variant IN (...)`
- Poisson disc sampling (simple jitter-grid yeterli — saf Poisson gerek yok): region'u `min_distance` × `min_distance` cell grid'lere böl, her cell'de `density` olasılıkla random offset'li obje yerleştir.
- Variant seçimi: `variants="all"` → uniform random; list → weighted; single → sabit.
- Çakışma: aynı region içinde var olan aynı-kategori objelerle `min_distance`'dan yakına yerleştirme (O(n) check yeterli, n küçük).
- TMX'e yazım: `apply_object_patch` genişletilir veya yeni `apply_object_add` helper'ı yazılır.
- Bridge broadcast: `{type: "patch", op: "objects_added", objects: [...]}`

**Design decisions gereken noktalar** (sen onaylamadan kodu yazmayayım):
- Variant weighting: `variants=[("key1", 0.5), ("key2", 0.3)]` gibi tuple mı, yoksa `{variants: [...], weights: [...]}` dict mi? Ben tuple list öneriyorum, simetrik `fill_selection` ile.
- Collision policy: mevcut aynı-kategori objelerle çarpışma default hard-skip mi, yoksa `replace_existing: bool` parametre mi?
- Pixel vs tile coords: ObjectGroup objeleri Tiled'de pixel coord kullanır. Agent tile cinsinden `region` veriyor → iç dönüşüm `x_px = (x_tile + offset) * tile_w`. Obje anchor'ı Tiled default bottom-left, prop height > tile_h olursa şifrelenebiliyor; `metarials` pack'te tree = 128×160 px = 4×5 tile — bu çakışmayı dikkate alan bir "footprint check" gerek mi yoksa pure center-point check yeterli mi?

**Effort:** ~1-2 gün (scatter algoritması + DB query + TMX write + bridge broadcast + test).

### 1.3. `add_object` & `remove_objects` tools

```python
def tool_add_object(
    tmx_path: str, layer: str, prop_uid: str,
    x: int, y: int, rotation: float = 0.0,  # tile coords
) -> dict: ...

def tool_remove_objects(
    tmx_path: str, layer: str,
    region: dict | str,                     # "selection" supported
    category: str | None = None,            # filter
    prop_uid: str | None = None,            # filter
) -> dict:
    """Returns: {removed: int, remaining_in_layer: int}"""
```

`place_props` için gerekli infrastructure'ı paylaşırlar — birlikte yazılmalı.

**Effort:** ~0.5 gün (DB + TMX write; algoritma yok).

### 1.4. `fill_selection` multi-key / weighted

Mevcut: `fill_selection(key: str)` — tek key.
Yeni (backward compatible):

```python
def tool_fill_selection(
    ...,
    key: str | None = None,                 # mevcut, deprecated
    keys: list[str | tuple[str, float]] | None = None,  # YENİ
    seed: int | None = None,
) -> dict:
    """keys: ["key1", "key2"] (uniform) veya [("key1", 0.5), ("key2", 0.3)] (weighted)"""
```

Agent "her çeşit çim" gibi tile-level variety isteklerine cevap verir. Tile layer için `place_props`'ın tile eşdeğeri.

**Effort:** ~0.5 gün (mevcut fonksiyonu multi-key'e genişlet).

### 1.5. v0.8.0 kabul kriteri (prop editing kısmı)

Rapordaki test senaryosu Prompt #2 **çalışmalı:**
- Kullanıcı tarayıcıda seçim yapar
- `get_selection` → rect döner
- `place_props(layer="forest", region="selection", category="tree", variants="all", density=0.4)` → 20+ çeşitli ağaç eklenir
- Bridge canlı yansıtır
- `studio_undo` ile geri alınabilir

Rough effort (1.1-1.4): **3-4 gün** net implementasyon + test.

### 1.6. `finalize_map` tool + lazy consolidate — **FLOW FIX**

`generate_map`'in zorunlu `consolidate` bağı kesilir. Yeni explicit "teslim" tool'u:

```python
def tool_finalize_map(
    tmx_path: str,
    out_dir: str | None = None,
    out_stem: str = "final",
    include_license_summary: bool = True,   # YENİ
) -> dict:
    """
    Projeyi kilitler ve tek-atlas deliverable üretir. İç olarak consolidate_map
    çalıştırır; ek olarak kullanılan asset pack'lerinin license info'sunu
    toplar ve çıktıya ekler (varsa).
    Returns: {tmx_path, atlas_png, tsx_path, license_summary}
    """
```

**`generate_map` değişikliği:**
- Artık consolidate çağırmıyor.
- Çıktı: `{tmx_path, preview_png, stats}` (ham TMX + hızlı preview, atlas yok).
- `create_map` skill'i güncellenir: plan → generate (preview) → studio edit loop → **finalize** (atlas + deliverable).

**`consolidate_map` mevcut tool'u kalır** — geriye uyumluluk için, isteyen manuel çağırabilir. `finalize_map` üstüne wrap'lar.

**Neden v0.8.0'a aldık (v0.8.2 polish değil)?** Çünkü `place_props`/`fill_selection` gibi yeni editing tool'ları "edit → finalize" mental model'e ihtiyaç duyuyor — yoksa kullanıcı "editledim ama atlas hâlâ eski" confusion'ına düşer.

**Design kararı gereken tek nokta:** `include_license_summary` default `true` mu `false` mu? Ben `true` öneriyorum — Tiled paketleri çoğunlukla MIT/CC0 değil, deliverable'da license görünürlüğü önemli.

**Effort:** ~2 saat (rename + skill update + license scraper stub + test).

### 1.7. v0.8.0 full kabul kriteri

- Prompt #2 (prop editing) yeşil (§1.5)
- `generate_map` artık atlas üretmiyor, sadece preview PNG döndürüyor
- `finalize_map` → atlas + license summary üretiyor
- `create_map` skill akışı güncellenmiş: plan → generate → edit → finalize
- Eski `consolidate_map` tool'u hâlâ çalışıyor (backward compat)

Rough effort (full v0.8.0): **~4 gün**.

---

## 2. v0.8.1 — "Parametric generation"

### 2.1. `generate_map` parametrize

Mevcut: `preset="grass_river_forest"` — hardcoded 40×40, 7 tree.
Yeni (preset opsiyonel, backward compatible):

```python
def tool_generate_map(
    width: int = 40, height: int = 40,
    zones: list[dict] | None = None,        # [{type:"forest", rect:[x0,y0,x1,y1]}, ...]
    props: dict | None = None,              # {tree:{density:0.5, variety:"all"}, ...}
    river: dict | None = None,              # {path:[[x,y],...], width:3, animated:true}
    pack: str = "...",
    seed: int = 11,
    preset: str | None = None,              # mevcut, deprecated ama çalışır
    out_name: str = "generated.tmx",
) -> dict: ...
```

Plan output'u da aynı şemaya uyuyor — `generate_map(**plan.as_kwargs())` çalışır.

**Design decisions gereken noktalar:**
- `zones` çakışırsa precedence (alttan üste mi, z-order mı)?
- `river.path` (x,y) sequence mi yoksa start/end/style bırakıp cellular automata mı üretsin?

**Effort:** ~2 gün.

### 2.2. `plan_map` → `generate_map` zinciri

`plan_map` şu an sadece ASCII preview döndürüyor. Genişlet:

```python
def tool_plan_map(...) -> dict:
    return {
        "ascii": "...",
        "zones": [...],       # YENİ — generate_map zones ile aynı şema
        "width": w, "height": h,
        "estimated": {"trees": 500, "water_tiles": 180, ...}
    }
```

Agent workflow:
```
plan = plan_map(50, 40, components=[...])
# kullanıcıya ascii göster
tmx = generate_map(**plan, pack="metarials")   # zones/width/height plan'dan
```

**Effort:** ~0.5 gün (çıktı genişletme, generate_map tarafı 2.1'de halledildi).

### 2.3. `plan_map` için ek parametreler

Rapordaki "sık orman" probleminin kökü: `plan_map`'e density/coverage/layout override yok. En az:

```python
plan_map(
    width, height, components=[...],
    forest_coverage: float = 0.3,
    forest_density: float = 0.3,
    river_through: str | None = None,     # "forest" | "grass" | "center"
)
```

Daha karmaşık layout control'u v0.9'a ertele — `layout: dict` açmak surface area'yı büyütür.

**Effort:** ~0.5 gün.

### 2.4. v0.8.1 kabul kriteri

Rapordaki test senaryosu Prompt #1 **çalışmalı:**
- `plan_map(50, 40, components=["grass","forest","river"], forest_coverage=0.6, river_through="forest")` → forest zone haritanın %60'ı, river forest içinden
- `generate_map(**plan, props={tree:{density:0.5, variety:"all"}})` → 50×40 TMX, 500+ tree (hedef: kullanıcı "sık" hissetmeli)
- `open_studio` → canlı görünüm

Rough effort: **3 gün**.

---

## 3. v0.8.2 — "Polish & production readiness"

### 3.1. Pagination — `list_*` tool'larının hepsi

`list_animated_props` zaten 85,790 char dönüyor ve token limitini patlatıyor. Standard schema:

```python
list_animated_props(
    category: str | None = None,
    pack_name: str | None = None,
    limit: int = 50,                # YENİ, default 50
    offset: int = 0,                # YENİ
    format: str = "summary",        # "summary" | "full"
) -> {
    "total": int,
    "offset": int,
    "limit": int,
    "items": [...],
    "next_offset": int | None,
}
```

Aynı `list_tilesets`, `list_wang_sets`, `list_characters`, `list_prop_categories`'e de uygulanacak.

**Effort:** ~0.5 gün (tekrarlı pattern, hepsine uygula).

### 3.2. `get_map_state` detay

```python
get_map_state(tmx_path, summary_only: bool = True, include: list[str] | None = None)
```

`include=["animated_tiles_detail"]` → `[{layer, tile_key, frames, duration_ms}, ...]`
`include=["object_layers"]` → her ObjectGroup için `{name, object_count, categories: {tree: 500, bush: 80}}`

**Effort:** ~0.5 gün.

### 3.3. Locale tutarlılığı

Mevcut durum: tool descriptions Türkçe, bazı error message'lar İngilizce, bazı stdout Türkçe. Production karışıklık yaratır.

İki seçenek:
- (A) Tool description'ları İngilizce'ye çevir, stdout ve hint'ler de İngilizce yap. README Türkçe kalabilir.
- (B) `TILESMITH_LOCALE` env var: "tr" | "en", default "en".

Ben (A)'yı öneriyorum — ~90% daha basit, bakımı kolay. Senin tercihin?

**Effort:** (A) ~1 gün, (B) ~3 gün.

### 3.4. Seed determinism garantisi

Doc + test:
- `docs/DETERMINISM.md`: "Given same seed + same pack + same input params, `generate_map` produces byte-identical TMX."
- `tests/test_determinism.py`: 10 farklı preset × aynı seed iki kez → TMX hash eşit mi?

Gerçekte `random.Random(seed)` kullanıyoruz ama `set()` iteration order gibi yerlerde non-deterministic olabilir — test gerçek bug'ı yakalar.

**Effort:** ~0.5 gün (test + doc; bug çıkarsa +).

### 3.5. v0.8.2 kabul kriteri

- `list_animated_props()` (param'sız) artık crash değil, ilk 50 döner
- `get_map_state(..., include=["animated_tiles_detail"])` her animated tile'ın layer+key'ini bildirir
- Tool output locale tutarlı
- Seed determinism test suite geçiyor

Rough effort: **2-3 gün**.

---

## 4. Grand total

| Milestone | Effort | Kullanıcı değeri |
|---|---|---|
| v0.8.0 — Prop editing + lazy consolidate | ~4 gün | **Kritik** — "seçili alanı ağaçlandır" + atlas israfı çözülür |
| v0.8.1 — Parametric generation | ~3 gün | **Kritik** — "sık orman, ortadan dere" çalışır |
| v0.8.2 — Polish | ~3 gün | Önemli — token patlaması + production polish |
| **Toplam** | **~10 gün net dev** | Rapor test senaryoları uçtan uca yeşil |

---

## 5. Onaylanmış kararlar

Kullanıcı 2026-04-24'te aşağıdakileri onayladı:

1. **Başlangıç:** v0.8.0 — prop editing + lazy consolidate.
2. **`place_props` variant weighting API:** `list[tuple[str, float]]` (simetrik `fill_selection` ile).
3. **`place_props` collision policy:** default hard-skip, `replace_existing: bool = False` parametresi var.
4. **`generate_map` preset geri uyumluluğu:** deprecation warning ile çalışmaya devam — v0.9'da kaldırılır. Artık consolidate çağırmıyor.
5. **Locale:** (A) — hepsi İngilizce. Tool descriptions + stdout + hint. README/skill doc Türkçe kalabilir.
6. **Sürüm stratejisi:** 3 ayrı minor release — v0.8.0 / v0.8.1 / v0.8.2. Her biri bağımsız değer.
7. **Lazy consolidate:** `finalize_map` yeni tool, `generate_map` artık atlas üretmiyor. `consolidate_map` backward compat için kalıyor.
8. **`finalize_map` license summary:** default `include_license_summary=True`.

---

## 6. Explicitly out of scope for v0.8

Rapor bunları önermiyor ama eminim aklına geleceği için açık yazıyorum — **v0.9'a** bırak:
- Multi-pack mixing (bir haritada 2 pack birden)
- Natural language → zones (LLM-side iş, plugin'in işi değil)
- Real-time collaborative editing (bridge tek kullanıcı tasarımı)
- Isometric / hex grid support (şu an square grid)
- AI-assisted prop placement ("make this area feel scary") — interesting ama research

---

**Sonraki adım:** v0.8.0 implementasyonu başlıyor. İlk PR `get_selection` + test (~1 saat) — en küçük yürüyen parça, zincirin pattern'ini sabitliyor. Ardından `finalize_map` flow fix, sonra `place_props` ekibi, son olarak `fill_selection` multi-key.
