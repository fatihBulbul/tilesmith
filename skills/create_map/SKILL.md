---
name: create_map
description: Tiled (TMX) uyumlu 2D RPG haritası tasarla ve üret. Kullanıcıyla soru-cevap yaparak boyut, bileşen (çim/toprak/nehir/orman) seç; yerleşim planı (ASCII preview) sun; kullanıcı onayladıktan sonra TMX + tek atlas PNG üret. Kullanıcı "harita yap", "map oluştur", "create map", "yeni harita", "harita tasarla", "tmx üret", "rpg haritası", "grass land haritası", "oyun sahnesi" gibi ifadeler kullandığında bu skill tetiklenir. Gerekirse önce scan_folder ile asset klasörünü taratır, sonra plan_map ile plan sunar, onay sonrası generate_map + consolidate_map ile tek-atlas deliverable verir.
---

# Harita Oluşturma (create_map)

Bu skill, 2D top-down RPG haritalarını soru-cevap üzerinden interaktif olarak tasarlar ve üretir. Adımlar sırasıyla uygulanmalı; atlanmamalı.

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

gggggggggggggggggggggg~~wwwww~~tttttttttt
gggggggggggggggggggggg~~wwwww~~tttttttttt
...
```

Özet ver: "X zone planlandı: dirt yaması (sol-alt), nehir (dikey merkez), orman (sağ)".

### 4. Onay al

**AskUserQuestion** ile sor: "Bu yerleşimle haritayı üreteyim mi?"

- Evet, üret
- Parametreleri değiştir (boyut / bileşenler)
- İptal

Kullanıcı "evet" dediyse Adım 5'e geç.

### 5. TMX üret

`generate_map` tool'unu preset ve seed ile çağır. Şu an `grass_river_forest` preset'i implement. Dönen `tmx_path`'i kullanıcıya GÖSTERMEDEN ÖNCE Adım 6'yı çalıştır.

### 6. Consolidate (tek atlas PNG)

`consolidate_map` tool'unu çağır (`tmx_path` = az önce üretilen TMX). Bu:

- TMX'i okur, kullanılan tüm GID'leri toplar
- Shelf bin-packing ile tek bir atlas PNG üretir
- Kullanılan asset'leri individual sprite PNG'lere ayırır (Tiled collection format gereği)
- Yeni bir TMX yazar: tek bir collection tileset, atlas PNG'yi referans alır

Sonuç: kullanıcı tek PNG + self-contained TMX + TSX alır. Orijinal tileset paketine bağımlı değil.

### 7. Kullanıcıya sun

Aşağıdaki sırayla göster:

- Consolidate edilmiş TMX dosyasına `computer://` linki
- Atlas PNG'ye `computer://` linki (bu ANA deliverable)
- Sprite klasörü yolu (Tiled'ın collection tileset'i açabilmesi için referans)
- Kısa istatistik: "40x40 harita, X eşsiz tile + Y eşsiz prop = Z toplam asset. Atlas boyutu W×H piksel."

## MCP Tool referansı

`tilesmith` server:

| Tool | Ne yapar |
|------|----------|
| `db_summary` | DB durumu |
| `scan_folder(path)` | Herhangi bir Tiled paketini recursive tarar ve DB'ye yazar |
| `list_tilesets`, `list_wang_sets`, `list_prop_categories`, `list_animated_props`, `list_characters`, `list_reference_layers`, `list_automapping_rules` | DB sorguları |
| `plan_map(w,h,components)` | Yerleşim planı + ASCII preview |
| `generate_map(preset, seed, out_name)` | Preset tabanlı ham TMX üretir (multi-tileset) |
| `consolidate_map(tmx_path)` | Tek atlas PNG + self-contained TMX çıktısı |

## Kurallar

- Plan onaylanmadan TMX üretme. Kullanıcıya önce preview göster.
- Consolidate ADIMI ZORUNLU. Ham multi-tileset TMX kullanıcıya verilmez; her zaman önce `consolidate_map` ile tek-atlas hale getir.
- Her zaman `computer://` link'i ile atlas PNG ve TMX'i göster.
- Asset klasörü DB'de yoksa önce `scan_folder` çağır, sonra tasarıma geç.
- Kullanıcı Türkçe yazıyorsa cevap Türkçe olsun.
