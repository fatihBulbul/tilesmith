# tilesmith

> **Tiled (TMX) uyumlu, sohbetten düzenlenebilir 2D top-down RPG harita otoritme aracı — Claude Code / Cowork üzerinde çalışır.**

Tilesmith, Tiled tileset paketleri klasörünü indexli, wang-aware bir harita yazım backend'ine dönüştürür. `.tsx` / `.tmx` / `.png` dosyalarını recursive tarar; tüm tileset'leri, wang set'leri, prop'ları, animasyonlu prop'ları, karakterleri ve automap kurallarını SQLite'a indeksler. Daha sonra sohbet üzerinden (doğal dil) ya da **Tilesmith Studio** adlı tarayıcı tabanlı canlı editörden harita tasarlamanı sağlar.

English version: [README.md](./README.md)

---

## İçindekiler

1. [Ne yapar](#ne-yapar)
2. [Sanatçı sözleşmesi — wangset kalitesi neden önemli](#sanatçı-sözleşmesi)
3. [Kurulum](#kurulum)
4. [Hızlı başlangıç](#hızlı-başlangıç)
5. [Tilesmith Studio (tarayıcı editörü)](#tilesmith-studio)
6. [MCP tool referansı](#mcp-tool-referansı)
7. [Mimari](#mimari)
8. [Sorun giderme](#sorun-giderme)
9. [Katkı](#katkı)
10. [Lisans](#lisans)

---

## Ne yapar

Tilesmith üç şey arasında köprüdür: diskteki bir Tiled asset paketi, o paketin içindekilerin SQLite kataloğu ve Claude'un harita tasarlamak/düzenlemek/render etmek/export etmek için çağırabileceği MCP tool'ları.

**İndeksleme.** Her paket için bir kez `scan_folder("paket/yolu")` çalıştır. Scanner klasörü recursive tarar, her `.tsx` tileset'ini (hem atlas hem collection tipi) ve TMX içindeki inline `<tileset>`'leri parse eder; tüm wang set'leri, terrain renkleri, prop varyantlarını, animasyonları ve automap kurallarını çıkarır. Her satır `pack_name` ile namespace'lenir — aynı isimli tileset'i paylaşan paketler birbirini ezmez.

**Tasarım.** `create_map` skill'i kısa bir soru-cevap akışı yürütür — boyut, biome karışımı, seed, stil tercihleri — sonra ASCII yerleşim planı önerir. Onayladığında wang-doğru geçişler, prop'lar, animasyonlar ve spawn object'leri ile dolu TMX üretir.

**Düzenleme.** `open_studio(tmx_path=...)` FastAPI + WebSocket bridge'i ile Konva tabanlı tarayıcı canvas'ı başlatır. Paint, erase, rect select, wang-fill, undo/redo yaparsın — tüm bağlı tarayıcı sekmelerine canlı broadcast olur. Claude da aynı haritayı MCP tool'ları ile (`paint_tiles`, `wang_fill_selection`, `studio_undo`, …) chat'ten düzenleyebilir; sonucu anında görürsün.

**Export.** `consolidate_map` bir TMX'i self-contained tek-atlas biçimine dönüştürür. Kullanılan tüm GID'leri tarar, referans verilen tile'ları shelf bin-packing ile tek PNG'ye paketler, TMX'i sadece o atlas'ı referans edecek şekilde yeniden yazar ve çıktıyı haritanın yanına koyar. Sonuç artık orijinal paketin klasör yapısına bağlı değildir — istediğin yere ship edebilirsin.

---

## Sanatçı sözleşmesi

Tilesmith, **Tiled'de tile artist'in ne bildirdiyse onu eksiksiz render eden bir araçtır**. Tahmin yürütmez, uydurmaz, boşlukları kapatmaz. Bu bilinçli bir tasarım kararıdır ve keskin bir sonucu vardır:

> **Haritalarının kalitesi, wangset'lerinin kalitesiyle sınırlıdır.**

Eğer tileset paketin eksik veya tutarsız wang set'lerle gelirse, tilesmith eksik veya tutarsız haritalar üretir. Hiçbir prompt bunu düzeltemez çünkü eksik bilgi zaten indekste yoktur. Bu plugin ile güzel haritalar yazabilmen için, önce tile artist'in Tiled'de wangset çalışmasını **doğru şekilde** yapmış olması lazım.

### Tilesmith'in bir wangset'ten beklediği

Tiled üç wangset tipi destekler: **corner** (köşe), **edge** (kenar) ve **mixed** (karma). Haritandaki her hücre ya 4 köşeye, ya 4 kenara, ya da her ikisine sahiptir ve her biri wangset'in bildirdiği renklerden birini taşır (`0` indeksi "dış / wildcard" demektir).

N renk bildirilmiş bir wangset için tam tile kataloğu:

| Wangset tipi | Sanatçının sağlaması gereken kombinasyonlar |
|---|---|
| corner — örn. çim-toprak geçişi | wildcard dahil `(N+1)⁴`; pratikte renk çifti başına `2⁴ = 16` kombinasyon |
| edge — örn. çitler, yarım-duvarlar, uçurumlar | aynı matematik: renk çifti başına `2⁴ = 16` oryantasyon |
| mixed — corner + edge aynı anda | ikisinin birleşimi (tilesmith'in resolver'ı şu an desteklemiyor) |

Tilesmith'in resolver'ı etkilenen her hücre için indekse tek bir soru sorar: *"Verilen bu 4 köşe/kenar rengine göre buraya hangi tile gelmeli?"* En düşük `local_id`'ye sahip eşleşeni alır. Eğer kombinasyon wangset'te yoksa, **resolver `None` döner ve hücre boş kalır.** O boş hücre haritada görünür bir delik olarak ortaya çıkar.

### Wangset özensizse karşılaşacağın hata modları

1. **Eksik kombinasyon → görünür delikler.** Sanatçı bir çim-toprak wang'ının 16 corner kombinasyonundan sadece 12'sini boyadıysa, haritada o 4 eksik kombinasyona denk gelen her hücre boş çıkar. Kaynaktaki boşluklar çıktıda boşluk olarak görünür.
2. **Tileset'ler arası tutarsız renk indeksi.** A paketi `1` indeksini "grass" için, B paketi `2` indeksini "grass" için kullanıyorsa tilesmith bunların aynı şey olduğunu bilemez. İki tileset'in tile'ları arasındaki geçişler tamamen ilgisiz terrain'ler gibi ele alınır.
3. **Yanlış etiketlenmiş tile'lar.** Sanatçı bir tile'ın NW köşesini sprite görsel olarak toprak gösterirken "grass" olarak işaretlediyse, haritada görsel kopukluk çıkar — tilesmith bug'ı gibi görünür ama bug `.tsx`'tedir.
4. **Saf corner ya da edge olabilecekken "mixed" seçilmiş wangset.** Tilesmith v0.7.1 resolver'ı corner ve edge'i destekler. Mixed wangset'ler listelenir ama `supported: false` olarak işaretlenir ve otomatik boyanamaz. Bu genelde kaçınılabilir bir seçimdir: çoğu wangset kavramsal olarak ya corner-sharing (terrain gradyanı) ya edge-sharing'dir (çit, duvar) ve birinden biri olarak yazılabilir.
5. **Hiçbir tile tarafından kullanılmayan renkler.** Palette'de bildirilmiş ama hiçbir tile'ın 4-corner veya 4-edge imzasında yer almayan. UI swatch'ı gösterir ama onunla paint her zaman `None` döner.

### Tile artist paketi ship etmeden önce ne yapmalı

1. Renk paletini baştan belirle. Her ayrı terrain / materyal tipi bir renk indeksi alır. Aynı tileset'teki ilişkili wangset'lerde tutarlı isimler kullan.
2. Wangset'teki her renk çifti için **16 kombinasyonu da** boya — köşe ya da kenar. Yavaş iştir, kestirmesi yok. Tiled'in wang-editor'ü 2D wang fırçasıyla boyayıp palette'den renk auto-assign yaparak hızlandırır.
3. Bittiğinde Tiled'de boş bir harita aç ve wangset ile paint yapmayı dene. Herhangi bir kombinasyon visual glitch veriyorsa ya da Tiled boşluk bırakıyorsa, o kombinasyon ya eksiktir ya yanlış etiketlenmiştir — paketi ship etmeden önce `.tsx`'te düzelt.
4. Wangset'leri odaklı tut. 7 renkli tek bir wangset, 7 tane 2-renkli wangset'ten çok daha zor tamamlanır. Mümkünse karmaşık terrain sistemlerini küçük, ikişerli wangset'lere böl.
5. Tipi bilinçli seç: görsel olarak harmanlanması gereken gradyanlar (çimden toprağa, sudan kuma) **corner**'dır. Grid'e snap'leyen sert sınırlar (çit, duvar, uçurum tepesi, patika) **edge**'dir. Birini seç ve tutarlı kal.

### Wangset sorunlarını yakalamana tilesmith nasıl yardım eder

`list_wangsets_for_tmx` TMX'teki her wangset'i color count, tile count ve `supported` flag'i ile döner. `2⁴ × (color_count choose 2)` beklentisinin çok altında tile sayısı olan wangset neredeyse kesin eksiktir. İndeksi doğrudan sorgulayabilirsin:

```sql
-- Wangset başına wang tile sayısı; renk çifti başına 2^4 = 16 beklenir.
SELECT ws.wangset_uid, ws.type, ws.color_count, COUNT(*) AS tile_count
  FROM wang_sets ws
  JOIN wang_tiles wt ON wt.wangset_uid = ws.wangset_uid
 GROUP BY ws.wangset_uid
 ORDER BY tile_count;
```

4 renk bildiren bir paketin wangset'lerinde sadece 6-7 tile görüyorsan, o paketten temiz haritalar çıkmaz. Ya o wangset'leri kullanmayı reddet, ya da kaynak `.tsx`'i kendin düzelt.

---

## Kurulum

### Gereksinimler

- Python 3.10 veya daha yeni
- Node.js 20+ (Studio frontend build için — bir kez)
- Sahip olduğun ya da lisans hakkın olan bir Tiled asset paketi (tilesmith **hiçbir** tileset art'ı bundle etmez)

### Claude Code / Cowork plugin'i olarak

Repo, Claude Code plugin marketplace yapısında düzenlenmiştir:

```text
/plugin marketplace add <github-kullanici-adin>/tilesmith
/plugin install tilesmith@tilesmith-marketplace
```

Hepsi bu. Claude Code `.mcp.json`, `skills/` ve MCP server'ı auto-discover eder. İlk açılışta plugin kendi klasörü içinde izole bir `.venv` kurar ve Python bağımlılıklarını oraya yükler — sen `pip install` çalıştırmazsın, plugin sistem Python'una hiç dokunmaz. İlk başlangıç ~30 sn sürer; sonraki başlangıçlar anında. Bu yaklaşım macOS (Homebrew) ve Debian/Ubuntu'daki PEP 668 (`externally-managed-environment`) kısıtlamasını aşar.

### Standalone kurulum

```bash
git clone https://github.com/<github-kullanici-adin>/tilesmith.git
cd tilesmith
pip install -r requirements.txt
cd studio/frontend && npm install && npm run build && cd ../..
```

`TILESMITH_DB_PATH`'i yazılabilir bir dizine işaret et (default `./data/tiles.db`) ve MCP server'ı doğrudan çalıştır:

```bash
python3 mcp_server/server.py
```

---

## Hızlı başlangıç

### 1. Asset paketini tara

Chat'te:

> Tilesmith ile `/yol/ERW-GrassLand-v1.9` tileset paketini tara.

Claude `scan_folder` çağırır ve SQLite indeksini doldurur. Orta boy bir paketin (~20 tileset, ~500 wang tile) ilk taraması birkaç saniye sürer.

### 2. Harita üret

> Bana 60×60 boyutunda, kuzey-güney akan nehri ve doğu kenarında toprak patikası olan bir grass-land haritası yap.

`create_map` skill'i birkaç netleştirme sorusu sorar, ASCII yerleşim planı gösterir, onayı bekler ve `output/`'a TMX çıkarır.

### 3. Studio'da aç

> Oluşturulan haritayı tilesmith studio'da aç.

Bridge `http://127.0.0.1:3024/` adresinde başlar. Tarayıcıda aç. Canlı Konva canvas'ında tüm layer'lar, object'ler ve animasyonlar render olur.

### 4. Chat'ten düzenle

> Seçtiğim dikdörtgeni dirt wang'ıyla doldur.

Canvas'ta dikdörtgen seç (**R** tuşuna bas, sürükle), sonra bu mesajı gönder. Claude `wang_fill_selection` çağırır — autotile geçişleri canlı belirir.

### 5. Self-contained versiyonu export et

> Bu haritayı tek atlas'a consolidate et.

`consolidate_map` TMX'i sadece üretilen tek bir PNG'ye bağımlı olacak şekilde yeniden yazar. Klasörü ship edersin, tek başına çalışır.

---

## Tilesmith Studio

Studio, tarayıcıda Konva tabanlı single-page app çalıştıran bir FastAPI + WebSocket bridge'dir. Seninle Claude'un aynı TMX'i aynı anda düzenlemesinin yoludur.

### Klavye & fare

| Tuş / hareket | Tool |
|---|---|
| `V` + sürükle, scroll | Pan / zoom |
| `Fit` butonu | Haritayı viewport'a sığdır |
| `B` | Paint — palette'den tile seç, canvas'ta tıkla ya da sürükle |
| `E` | Erase — hücre sil |
| `R` + sürükle | Rect select; **Esc** temizler |
| `W` | Wang mode — wangset ve renk seç, tıkla/sürükle ile autotile |
| `Ctrl/Cmd + Z` | Undo (history derinliği 100) |
| `Ctrl/Cmd + Shift + Z` / `Ctrl + Y` | Redo |

### Corner vs. edge wang modu pratikte

Wang modunda tek hücreye tıkladığında etkilenen neighborhood wangset tipine göre değişir:

**Corner wangset** (çimden toprağa, sudan kuma): tıklaman hedef hücrenin 4 köşesini seçtiğin renkle boyar. Tool sonra 3×3 komşuluğu (8 komşu + self) yeniden resolve eder, çünkü o köşeler 8 komşunun hepsiyle paylaşılır. Tıklama etrafında pürüzsüz gradyan geçişler belirir.

**Edge wangset** (çitler, yarım-duvarlar, uçurumlar): tıklaman hedef hücrenin 4 kenarını boyar. Tool 5-hücreli artı-şekilli neighborhood'u (self + N / E / S / W) yeniden resolve eder çünkü kenarlar sadece 4 ortogonal komşuyla paylaşılır. Diagonal komşular dokunulmaz — bu tam olarak istediğin davranıştır, bir çit diagonal çaprazdaki başka bir çite bağlanmamalı.

### Multi-client canlı düzenleme

İki tarayıcı sekmesinde `http://127.0.0.1:3024/` aç. Birinde paint yap — diğeri milisaniyeler içinde WebSocket ile güncellenir. Claude'un MCP çağrıları da aynı broadcast'ten geçer, chat tetiklediğin düzenlemeler iki sekmede de görünür.

---

## MCP tool referansı

Tilesmith 25 MCP tool expose eder. Tam set `mcp_server/server.py`'de; öne çıkanlar:

**İndeksleme & sorgu**

| Tool | Amaç |
|---|---|
| `scan_folder(path)` | Bir Tiled asset paketini SQLite'a indeksle |
| `list_packs()` | İndekste bulunan tüm paketler |
| `list_tilesets`, `list_wangsets`, `list_props`, `list_characters`, `list_animations` | Katalog sorguları |

**Harita üretimi & export**

| Tool | Amaç |
|---|---|
| `create_map_preset(name, ...)` | Built-in preset'lerden birini tetikle (grassland, rich-80, …) |
| `consolidate_map(tmx_path, out_dir)` | TMX'i self-contained tek-atlas dosyasına dönüştür |

**Studio bridge**

| Tool | Amaç |
|---|---|
| `open_studio(tmx_path, port?, host?)` | Bridge + browser URL |
| `close_studio(port?)` | Bridge'i durdur |
| `paint_tiles(tmx_path, layer, cells, port?)` | Tile layer'a `cells: [{x, y, key|null}]` patch uygula |
| `patch_object(...)` | Object'lerde move / delete / set-key |
| `fill_rect(tmx_path, layer, x0, y0, x1, y1, key, port?)` | Tek tile key ile dikdörtgen doldur |
| `fill_selection(key, port?)` | Studio'daki son rect seçimini doldur |
| `list_wangsets_for_tmx(tmx_path?, port?)` | TMX'teki tüm wangset'ler, renkleri + `supported` flag |
| `wang_paint(wangset_uid, cells, color=1, layer?, erase?)` | Wang-aware autotile paint (corner + edge) |
| `wang_fill_rect(wangset_uid, x0, y0, x1, y1, color=1, layer?, erase?)` | Wang-aware dikdörtgen doldurma |
| `wang_fill_selection(wangset_uid, color=1, erase?)` | Wang-aware seçim doldurma |
| `studio_undo(port?)`, `studio_redo(port?)` | History navigasyonu (derinlik 100) |

Tüm studio tool'ları önce çalışan bridge'e HTTP üzerinden erişmeye çalışır, her bağlı tarayıcıya broadcast eder; bridge yoksa doğrudan atomic file write'a düşer. Her iki durumda da disk TMX'in tutarlı kalır.

---

## Mimari

```
  Tiled asset paketi (.tsx / .tmx / .png)
              │
              ▼
  ┌────────────────────────┐
  │  scanner.py            │  parse + normalize + SQLite DDL
  │  (auto + overrides +   │
  │   VIEW COALESCE)       │
  └───────────┬────────────┘
              ▼
     data/tiles.db  ◄─────────── sorgular (server.py, wang.py, generator)
              │
              ▼
  ┌─────────────────────────────────────────────────┐
  │  MCP server (stdio)         Studio bridge (HTTP+WS) │
  │  25 tool                    FastAPI + Konva frontend│
  │  create_map skill           single-page app         │
  └──────┬──────────────────────────┬─────────────────┘
         │                          │
         ▼                          ▼
   Disk TMX  ◄── atomic write ──────┘
```

Üç katmanlı katalog — `<kind>_auto` (her taramada bu pack için temizlenip yeniden kurulur), `<kind>_overrides` (elle düzenlenir, dokunulmaz) ve bunları `COALESCE` ile birleştiren `<kind>` VIEW — herhangi bir indeks satırını (örn. yanlış etiketlenmiş wang rengini düzeltme) bir sonraki taramada kaybetmeden ince ayar yapabilmeni sağlar.

Tüm state değişiklikleri Studio bridge'deki tek bir chokepoint'ten (`patch_paint`) geçer. Wang paint, rect fill, selection fill ve undo hepsi o tek fonksiyondan geçer — inverse-patch history'si tutarlı, broadcast semantiği tek tip kalır.

---

## Sorun giderme

**"Bir wangset ile paint yaptım, hücrelerin yarısı boş çıktı."**
Wangset'te corner/edge kombinasyonu eksik. [Sanatçı sözleşmesi bölümündeki](#sanatçı-sözleşmesi) SQL sorgusunu çalıştır, wangset başına tile sayısına bak. Muhtemelen renk sayısının gerektirdiği tile'ların çok altında kalan bir wangset göreceksin.

**"İki tileset'im arasındaki geçişler bozuk görünüyor."**
İki tileset muhtemelen aynı kavramsal terrain için farklı renk indeksleri kullanıyor. Tilesmith renk indekslerini opak integer olarak ele alır — A tileset'indeki `1` indeksi, B tileset'indeki `1` indeksiyle otomatik olarak aynı değildir. Tutarlı hale getirmek için `.tsx` dosyalarını (ya da `wang_colors_overrides`'da override satırı) düzenlemen gerek.

**"Wang paint'i undo edince delikler çıkıyor."**
v0.7.0+ sonrası bu olmamalı — undo path inverse patch'i uygulamadan önce corner/edge cache'i invalidate ediyor. Yine de görürsen, kesin repro (wangset tipi, layer, paint sekansı) ile issue aç.

**"Mixed-type wangset'ler grileşmiş."**
Doğru — v0.7.1'de listeleniyorlar ama `supported: false`. Sadece corner ve edge implemente. Çoğu mixed wangset Tiled'de saf corner ya da saf edge olarak yeniden yazılabilir.

**"Büyük pakette `scan_folder` sonsuza çalışıyor."**
Scanner single-threaded. Orta boy pakette (~20 tileset, yüzlerce tile) birkaç saniye sürer; çok büyükte (100+ tileset) bir dakika olabilir. Gerçekten takılırsa genelde bir `.tsx` circular template reference'ı ya da çok büyük embedded base64 image'ı içeriyordur.

---

## Katkı

[CONTRIBUTING.tr.md](./CONTRIBUTING.tr.md)'ye bak (English: [CONTRIBUTING.md](./CONTRIBUTING.md)). Issue ve PR'lar her zaman açık, özellikle:

- ek wangset tipleri (mixed, 2-edge, custom),
- Studio canvas'ında object drag-and-drop,
- automap rule engine (`automap_rules` tablosu indexli ama henüz tool kullanmıyor),
- daha fazla `create_map` preset.

Tam test suite `scripts/`'de. `test_wang_unit.py` fixture-siz saf unit test, saniyeden kısa çalışır — oradan başla.

---

## Lisans

MIT — bkz. [LICENSE](./LICENSE).

Tilesmith Tiled tileset paketlerini indeksler ve render eder; **hiçbir** tile art'ı bundle etmez veya yeniden dağıtmaz. Taradığın herhangi bir paketin sahipliği veya lisansı senin sorumluluğundadır. Popüler ticari paketler (örn. ERW ailesi) ilgili yazarlarınca ayrıca satılır.
