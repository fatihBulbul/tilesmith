---
name: create_map
description: Tiled (TMX) uyumlu 2D RPG haritası tasarla, üret, canlı editle ve tek atlas deliverable olarak teslim et. Kullanıcıyla soru-cevap yaparak boyut, bileşen (çim/toprak/nehir/orman) seç; yerleşim planı (ASCII preview) sun; kullanıcı onayladıktan sonra ham TMX + preview PNG üret ve Studio bridge'i aç; kullanıcı tarayıcıda iteratif editleyebilir (paint, fill_selection, place_props, wang); kullanıcı "bitti/teslim" dediğinde finalize_map ile tek-atlas deliverable çıkar. Kullanıcı "harita yap", "map oluştur", "create map", "yeni harita", "harita tasarla", "tmx üret", "rpg haritası", "grass land haritası", "oyun sahnesi" gibi ifadeler kullandığında bu skill tetiklenir.
---

# Harita Oluşturma (create_map)

Bu skill, 2D top-down RPG haritalarını soru-cevap üzerinden interaktif olarak tasarlar, tarayıcıda canlı editlenir ve son haliyle tek-atlas deliverable üretir. Adımlar sırasıyla uygulanmalı; atlanmamalı.

**v0.8 flow değişikliği:** `generate_map` artık atlas üretmiyor — ham TMX + preview PNG verir. Kullanıcı Studio'da iteratif editler. Atlas yalnızca `finalize_map` ile, kullanıcı "teslim" dediğinde üretilir.

## Akış

### 1. DB'yi doğrula

İlk adım `tilesmith` MCP server'ının `db_summary` tool'unu çağırmak. Eğer DB boşsa veya tileset sayısı 0 ise, kullanıcıya hangi asset klasörünü taraması gerektiğini sor (AskUserQuestion). Sonra `scan_folder` ile o klasörü indeksle. DB hazırsa bu adımı atla.

### 2. Harita tasarım soruları

Kullanıcıya **AskUserQuestion** ile sor (tek mesajda birden fazla soru):

1. **Boyut**: 20x20 / 30x30 / 40x40 / 60x60 (öneri: 40x40)
2. **Bileşenler** (multiSelect=true): çim (grass), toprak yaması (dirt), nehir (river), orman (forest)
3. **Seed**: 11 / 42 / 123 / rastgele

Kullanıcı ilk mesajında detay verdiyse (örn. "20x20 grass + nehir") tekrar sorma, doğrudan planı oluştur.

### 3. Yerleşim planını sun

`plan_map` tool'unu seçilen parametrelerle çağır. Dönen `ascii_preview` alanını kod bloğu olarak kullanıcıya göster:

```
Plan: 40x40, bileşenler: grass, river, forest
Legend: g=grass  d=dirt  ~=water  t=tree
...
```

Özet ver: "X zone planlandı: dirt yaması (sol-alt), nehir (dikey merkez), orman (sağ)".

### 4. Onay al

**AskUserQuestion** ile sor: "Bu yerleşimle haritayı üreteyim mi?"

- Evet, üret
- Parametreleri değiştir (boyut / bileşenler)
- İptal

Kullanıcı "evet" dediyse Adım 5'e geç.

### 5. Ham TMX üret (atlas YOK)

`generate_map` tool'unu preset + seed + pack ile çağır. Dönüş: `tmx_path` + `preview_path` (hızlı PNG preview). **Atlas üretilmez** — bu ham çalışma dosyası, Studio bunu düzenleyecek.

Kullanıcıya preview PNG'yi `computer://` linki olarak göster + "Bu ilk hali. Şimdi Studio'yu açıyorum, tarayıcıda istediğin gibi editleyebilirsin." de.

### 6. Studio'yu otomatik aç

`open_studio(tmx_path=<Adım 5 çıktısı>)` çağır. Dönen URL'i kullanıcıya ver:

> "Studio hazır: http://127.0.0.1:3024 — tarayıcıda aç. Paint/erase ile tile boya, seçim yap + `fill_selection` ile doldur, `place_props` ile ağaç/çalı scatter, `wang_paint` ile terrain geçişi çiz. Bittiğinde bana 'teslim' / 'finalize' / 'bitti' de."

### 7. Studio edit loop

Kullanıcıyla iteratif — her komutunda ilgili MCP tool'u çağır, Studio canlı yansıtır:

| Kullanıcı der | Agent çağırır |
|---|---|
| "şurayı çimle doldur" | `fill_selection` (selection zaten bridge'de) |
| "seçili alanı ağaçlandır" | `get_selection` → `place_props(category='tree', ...)` |
| "yolu taşlaştır" | `wang_fill_rect` veya `wang_paint` |
| "yanlış oldu geri al" | `studio_undo` |
| "şu objeyi sil" | `remove_objects` veya `patch_object` |

Edit loop'tan Adım 8'e geçiş tetikleyicileri: "bitti", "teslim", "finalize", "atlas yap", "ver bana", "tamam böyle kalsın".

### 8. Finalize + deliverable

`finalize_map(tmx_path=<Adım 5'teki aynı tmx_path>)` çağır. Bu:

- TMX'i okur, **son haliyle** kullanılan tile/prop GID'lerini toplar
- Shelf bin-packing ile tek atlas PNG üretir
- Self-contained TMX + TSX + sprites klasörü yazar
- Kullanılan her pack'in LICENSE/README excerpt'ini toplar (default `include_license_summary=True`)

### 9. Kullanıcıya teslim et

Aşağıdaki sırayla göster:

- `delivery.atlas_png` → `computer://` linki (**ANA deliverable**)
- `delivery.tmx` → `computer://` linki (self-contained TMX)
- `delivery.tsx` → `computer://` linki
- Kısa istatistik: "40x40 harita, X eşsiz tile + Y eşsiz prop. Atlas W×H px."
- `license_summary` varsa her pack için kısa satır: "ERW Grass Land 2.0: CC-BY 4.0 (see LICENSE.txt)"

## MCP Tool referansı

`tilesmith` server — v0.8.0+:

| Tool | Ne yapar |
|------|----------|
| `db_summary` | DB durumu |
| `scan_folder(path)` | Herhangi bir Tiled paketini recursive tarar ve DB'ye yazar |
| `list_tilesets`, `list_wang_sets`, `list_prop_categories`, `list_animated_props`, `list_characters`, `list_reference_layers`, `list_automapping_rules` | DB sorguları |
| `plan_map(w,h,components)` | Yerleşim planı + ASCII preview |
| `generate_map(preset, seed, out_name, pack)` | **Ham** TMX + preview PNG üretir (atlas YOK) |
| `open_studio(tmx_path)` | Browser Studio bridge'ini başlatır |
| `get_selection()` | Kullanıcının Studio'da çizdiği son rect'i döndürür |
| `paint_tiles`, `fill_rect`, `fill_selection` | Tile layer edit |
| `place_props`, `add_object`, `remove_objects` | ObjectGroup prop edit |
| `wang_paint`, `wang_fill_rect`, `wang_fill_selection` | Wang-aware terrain paint |
| `studio_undo`, `studio_redo` | Edit history |
| `finalize_map(tmx_path)` | **Tek atlas deliverable** + license summary |
| `consolidate_map(tmx_path)` | [DEPRECATED] finalize_map'e geç — backward compat için kalıyor |

## Kurallar

- Plan onaylanmadan TMX üretme. Kullanıcıya önce preview göster.
- `generate_map` sonrası **Studio'yu her zaman aç** (Adım 6) — kullanıcı editlemeyeceğini açıkça söylemedikçe.
- Atlas **yalnızca** `finalize_map` ile üretilir. Edit sırasında `consolidate_map` / `finalize_map` çağırma — kullanıcının iş akışını keser ve boşa compute harcar.
- Her zaman `computer://` linki ile atlas PNG + TMX göster.
- Asset klasörü DB'de yoksa önce `scan_folder` çağır, sonra tasarıma geç.
- Kullanıcı Türkçe yazıyorsa cevap Türkçe olsun. Tool output'ları İngilizce olabilir — kullanıcıya sunarken Türkçe'ye çevir.
