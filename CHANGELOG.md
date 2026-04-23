# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.1] - 2026-04-23

### Added — Edge-type wang resolver

- **`WangEdgeState` dataclass** — corner state'in edge muadili. İki grid tutuyor: `h_edges[(H+1)×W]` (yatay kenarlar — N/S paylaşımı için) ve `v_edges[H×(W+1)]` (dikey kenarlar — W/E paylaşımı için). `paint_cell(x,y,color)` hücrenin 4 kenarını da boyar; komşu hücrelerle kenarlar **sadece** 4-orthogonal (N/E/S/W) yönde paylaşılır — diagonal komşuluk yok (bu corner'a göre bariz fark).
- **`resolve_wang_tile_edge(wangset_uid, n, e, s, w)`** — DB'den `wang_tiles.(c_n, c_e, c_s, c_w)` üzerinden arama. En düşük `local_id` deterministic olarak seçilir.
- **`apply_wang_paint_edge` + dispatcher** — `apply_wang_paint` artık wangset'in `type` kolonuna bakıp corner/edge branch'ine yönleniyor. 5-hücreli artı-şekilli neighborhood (self + N/E/S/W) hesaplanıp her hücrenin 4 kenarı okunarak tile resolve ediliyor. Mixed-type ve unknown wangset'ler hâlâ `ValueError` atıyor. State sınıfının type uyumsuzluğu da açıkça hata veriyor (`WangCornerState` + edge wangset → ValueError ve vice versa).
- **`seed_edges_from_layer`** — edge muadili seed fonksiyonu. TMX'teki existing tile'lardan `c_n/c_e/c_s/c_w` bilgisini alıp grid'leri dolduruyor; wang boyaması mevcut haritayı "tanıyor".
- **Studio Bridge entegrasyonu** — `_get_or_seed_wang` artık `get_wangset_type()` ile sorgu yapıp doğru state sınıfını seçiyor; `self.wang_states` tipi `dict[..., WangCornerState | WangEdgeState]` olarak güncellendi.
- **`SUPPORTED_WANG_TYPES = {"corner", "edge"}`** — `list_wangsets_for_tilesets` artık edge wangset'leri `supported=true` olarak raporluyor. Bu sayede Studio palette / catalog UI'si fence, half-sized wall gibi edge-typed tile'ları filtre etmeden gösterebiliyor.

### Added — Testler

- **`scripts/test_wang_unit.py`** 57 assertion'a genişletildi — `WangEdgeState` grid shape, 4-orthogonal paylaşım, diagonal paylaşmama, `resolve_wang_tile_edge`, `apply_wang_paint` dispatcher her iki yönde de type mismatch rejection, `seed_edges_from_layer`, 16-edge-tile kombinasyon synthetic fixture.
- **`scripts/test_studio_wang_edge.py`** (yeni, 16 assertion) — gerçek `half-sized wall` edge wangset'i (ERW Grass Land 2.0 v1.9) ile bridge e2e. Tek nokta paint → 5-hücre plus neighborhood; merkez `lid 818` (tek full-edge tile); N/S/E/W komşuları doğru yönde single-edge variant; diagonal komşular dokunulmuyor; `fill_rect` interior cell'i full-edge çözüyor; MCP `wang_paint` via=bridge edge wangset üzerinde çalışıyor; erase hem merkezi hem 4 komşuyu temizliyor.

### Kapsam dışı (bilinçli)

- **Mixed-type wangset'ler** hâlâ `SUPPORTED_WANG_TYPES` dışında (DB'de sadece 2 adet var ve semantics corner ∪ edge kombinasyonu; ileriki bir phase'de `WangMixedState` + hem 8-cell hem 5-cell neighborhood union'la ele alınabilir).

## [0.7.0] - 2026-04-23

### Added — Undo / Redo (Phase 5)

- **Bridge-tarafı global history stack'leri** — `StudioState` artık `undo_stack: list[dict]` ve `redo_stack: list[dict]` tutuyor (N=100, TMX (re)load'unda temizleniyor). Her `patch_paint` çağrısı mutation'ı uygulamadan önce mevcut `data[y][x]` değerlerinden `inverse` patch üretip stack'e `{kind,"paint",layer,forward,inverse,meta}` olarak push ediyor. Redo branch yeni mutation geldiğinde otomatik temizleniyor.
- **Wang-aware**: `wang_paint` artık üretilen 3×3 neighborhood paint cells'ini `patch_paint` üzerinden yazdığı için undo otomatik olarak tüm komşuluğu (pure-wang + transition tile'lar) tek adımda geri alıyor. Wang meta bilgisi (wangset_uid + color + erase) undo entry'sine eklenen `meta` field'ı aracılığıyla taşınıyor — debugging ve ileriki replay senaryoları için.
- **fill_rect = 1 undo adımı** — `fill_rect`/`wang_fill_rect` internal olarak tek bir `patch_paint` call'ına açıldığı için dikdörtgen doldurma 16 veya 400 hücre olsun tek Ctrl+Z ile geri alınıyor.
- **Stale wang corner cache invalidation** — her paint mutation'dan sonra `wang_states = {}` set edilerek undo/redo sonrası bir sonraki wang paint'in güncel layer'dan re-seed olması sağlanıyor.

### Added — HTTP endpoints

- **`POST /undo`** — Stack'teki en son paint patch'ini geri alır, broadcast olarak `patch op:"paint"` + `undo:true` yayar. Stack boşsa `{ok:true, applied:false}` döner (no-op). Inverse patch cold-reload gerektiriyorsa `map_loaded` re-broadcast edilir.
- **`POST /redo`** — Son undo edilen entry'yi tekrar uygular, `redo:true` flag'iyle broadcast eder. Boş redo branch → no-op.
- **`GET /history`** — `{undo_depth, redo_depth, undo_max}` — UI ve debug için.

### Added — MCP tool'ları (23 → 25)

- **`studio_undo(port?, host?)`** — Bridge'deki `/undo` endpoint'ini tetikler. Bridge-only (history bridge'de tutuluyor). Doğal dil akışı: kullanıcı `"son değişikliği geri al"` → Claude bu tool'u çağırır → tüm bağlı client'lar canlı broadcast'le senkron olur.
- **`studio_redo(port?, host?)`** — Son undo'yu tekrar uygular. `"yeniden uygula"` gibi komutlar için.

### Added — Studio UI

- **Header: `Undo` + `Redo` butonları** — disabled state stack derinliğine göre otomatik güncelleniyor (`/history` poll, WS event'leriyle refresh).
- **Klavye kısayolları**: `Ctrl+Z` (Mac'te `Cmd+Z`) → undo, `Ctrl+Shift+Z` veya `Ctrl+Y` → redo. Input/select/textarea odaklıyken bypass ediliyor.

### Added — e2e testi

- **`scripts/test_studio_undo.py`** — 20 assertion: history bootstrap, single-cell paint undo/redo round-trip, new mutation → redo branch clears, `fill_rect` = 1 undo step, wang_paint undo tüm 36-cell neighborhood'u reverses, MCP `studio_undo`/`studio_redo` via=bridge, empty-stack undo no-op.

### Notes

- Object patch'leri (move/delete/set_key) şu an history'ye girmiyor — paint/erase/wang dışındaki mutation tiplerinin undo desteği sonraki phase'e bırakıldı.
- Paint stroke'ları per-cell recording yapıyor (her `patch_paint` call'ı bir undo entry). Drag ile uzun stroke'ta Ctrl+Z bir süre aynı isteğe tekabül ediyor. Client-side "batch_id" ile per-stroke coalescing ileride eklenebilir (istek gelirse).

## [0.6.2] - 2026-04-23

### Added — Wang rect/selection fill (Phase 4.1)

- **`POST /wang/fill_rect`** — `{layer?, wangset_uid, color?, x0,y0,x1,y1, erase?, use_selection?}` payload'ıyla wang-aware dikdörtgen doldurma. Hücre hücre `wang_paint` göndermeye gerek kalmadan tek HTTP call ile bir alan doldurulabiliyor. `x0>x1` / `y0>y1` otomatik normalize ediliyor; rect layer bounds'a clip'leniyor. Sonuç yine `patch op:"paint"` broadcast'i olarak tüm client'lara yayılıyor ve response'a `rect: {x0,y0,x1,y1}` meta eklenmiş oluyor.
- **`use_selection=true` flag'i veya eksik koordinatlar** → bridge `STATE.last_selection`'ı kullanır (drag-select'ten gelen rect). Layer verilmezse selection'ın layer'ı uygulanır. Böylece "canvas'ta alan seç → 'Fill selection' butonuna bas" akışı tek endpoint'le çözülüyor.
- **`StudioState.wang_fill_rect(layer, x0,y0,x1,y1, wangset_uid, color, *, erase)`** — normalize + clip + cell-list expansion + delegate-to-`wang_paint` pipeline'ı.
- **MCP tool'ları (21 → 23)**:
  - **`wang_fill_rect(wangset_uid, x0,y0,x1,y1, color=1, layer?, erase?, tmx_path?, port?, host?)`** — bridge çalışıyorsa `/wang/fill_rect`'e POST eder; yoksa rect'i hücre listesine açıp `tool_wang_paint`'in direct fallback yoluna devreder.
  - **`wang_fill_selection(wangset_uid, color=1, erase?, port?, host?)`** — bridge'den `/selection` okur, `/wang/fill_rect` + `use_selection=true` ile doldurur. Bridge-only (direct fallback'te selection bilgisi yok); doğal dil akışı için: kullanıcı Studio'da alan seçer → "orayı toprak wang'i ile doldur" der → Claude bu tool'u çağırır.

### Added — frontend "Fill selection (wang)" butonu

- **`#wang-fill-selection` butonu** Wang panelinin altına eklendi. Hem bir wang rengi hem de bir selection aktifken etkinleşiyor; tıklama `POST /wang/fill_rect` yaparak server-side rect-fill'i tetikliyor. `ToolController._refreshWangFillButton()` helper'ı `setWang()` ve `setSelection()`'dan çağrılarak enable/disable state'ini senkron tutuyor. Hata durumunda status bar'a görünür bir mesaj yazıyor.

### Added — e2e testi

- **`scripts/test_studio_wang_fill_rect.py`** — 23 assertion; explicit rect, ters-sıralı koordinat normalize, eksik `wangset_uid` → 400, boş selection + `use_selection=true` → 400, stored selection round-trip, `use_selection=true` rect metadata, MCP `tool_wang_fill_rect` + `tool_wang_fill_selection` (via=bridge), erase yoluyla rect temizleme.

## [0.6.1] - 2026-04-23

### Added — hygiene + coverage

- **`requirements.txt`** artık `fastapi>=0.110` + `uvicorn[standard]>=0.29` içeriyor. Studio bridge'i README'nin belirttiği gereksinimlerle doğrudan kurulabiliyor.
- **CI güncellemesi** (`.github/workflows/ci.yml`):
  - `wang`, `tmx_state`, `tmx_mutator` + `studio/bridge/server` import smoke testleri eklendi.
  - Yeni Playwright-free unit test `scripts/test_wang_unit.py` matrix içinde çalışıyor (33 assertion, in-memory SQLite ile synthetic wangset, fikstür gerektirmiyor).
  - `marketplace.json` validate edilmeye eklendi.
  - Yeni **`frontend-build` job**'ı: Node 20 setup + `studio/frontend` içinde `npm ci && npm run build` (tsc --noEmit + vite build).
- **`scripts/test_wang_unit.py`** — `wang.py` core algoritmasını fikstürsüz doğrulayan yeni test harness'i.

### Changed — wangset type safety

- **`list_wangsets_for_tilesets` artık `supported: bool` alanı döndürüyor.** Şu an sadece `corner` tipi desteklendiği için edge/mixed wangset'ler `supported=false` olarak işaretleniyor.
- **`apply_wang_paint` artık non-corner wangset'leri reddediyor** (`ValueError: wangset type '<tip>' not supported ...`). Bridge HTTP 400 + WS `error` mesajı olarak surface ediyor (zaten yakalıyordu). Önceden edge/mixed tip seçilince sessizce tüm hücreleri silerdi — artık net hata.
- **Yeni `get_wangset_type(db_path, wangset_uid)` helper'ı** ve `SUPPORTED_WANG_TYPES = {"corner"}` sabiti.
- **Frontend**: Wang dropdown artık sadece `supported` setleri listeliyor, gizlenen edge/mixed setler için bilgilendirici satır ekleniyor. `supported` alanı yoksa (eski bridge) `type === "corner"` fallback'i uygulanıyor.

### Notes

- Mevcut DB'de 283 corner-type, 9 edge-type, 2 mixed-type wangset tespit edildi. Edge/mixed desteği (çitler, duvarlar gibi side-adjacency pattern'ler için) sonraki bir phase'e bırakıldı; o zaman `resolve_wang_tile` N/E/S/W slotlarına bakan bir varyant ve `SUPPORTED_WANG_TYPES` genişletmesi gerekecek.

## [0.6.0] - 2026-04-23

### Added — Wang-aware autotile paint (Phase 4)

- **`mcp_server/wang.py`**: Corner-wang autotile motoru. `WangCornerState` her layer-wangset çifti için `(H+1)×(W+1)` corner grid'i tutar; `paint_cell(x,y,color)` bir hücrenin 4 köşesini de `color` değerine set eder, bu da diagonal komşulardaki paylaşılan köşeleri otomatik etkiler. `apply_wang_paint()` bir stroke'taki her hücre için 3×3 komşuluğu tarar, her hücrenin (NW,NE,SW,SE) köşe renklerine bakarak DB'deki `wang_tiles` tablosundan deterministik olarak (en düşük `local_id`) eşleşen tile'ı seçer ve `[{x,y,key|None}, ...]` döndürür. No-match ise hücre silinir (TMX bozulmaz). Cache'li (köşe tuple'ı başına tek DB sorgusu).
- **Key format bridge**: DB `tile_uid = "{pack}::{raw_stem}::{local_id}"` ↔ Studio `key = "{safe_stem}__{local_id}"`. `tile_uid_to_studio_key()` fonksiyonu `_safe_stem()` ile non-alnum normalize ederek bu iki formatı birbirine çevirir.
- **Lazy corner seeding**: `seed_corners_from_layer()` mevcut TMX içeriğinden köşe durumunu çıkarır (her tile'ın `local_id`'sini DB'de reverse-lookup yapıp 4 köşe rengine bakar) — böylece mevcut bir haritayı wang-repaint ederken komşu köşe uyumu korunur.

### Added — bridge endpoints + WS protocol

- **`GET /wang/sets`** — yüklü TMX'in tileset stem'lerini çıkarıp DB'deki wangset listesini nested color array'iyle döndürür: `{sets: [{wangset_uid, tileset, name, type, color_count, tile_count, colors: [{color_index, name, color_hex}, ...]}, ...]}`.
- **`GET /wang/tiles/{wangset_uid:path}`** — bir wangset'in tüm tile'ları (`tile_uid`, `studio_key`, 8 corner slot) — debug/inspection için.
- **`POST /wang/paint`** — `{layer?, wangset_uid, color, cells: [{x,y}], erase?}` payload'ıyla wang-aware paint tetikler. İçerde `WangCornerState` (gerekirse lazy seed edilir) + `apply_wang_paint` çalıştırır, sonucu mevcut `patch_paint` akışına verir (atomic TMX write + broadcast).
- **WS `wang_paint`** mesajı — aynı yükü WS üzerinden kabul eder, broadcast `{type:"patch", op:"paint", layer, cells, wang:{wangset_uid, color, cells_touched, erase?}}` olarak tüm clientlara yayılır.
- `StudioState.wang_states: dict[(layer, wangset_uid), WangCornerState]` — bridge per-pair corner grid'i cache'ler. Yeni TMX yüklenince temizlenir.

### Added — MCP tool'ları (19 → 21)

- **`list_wangsets_for_tmx(tmx_path?, port?, host?)`** — Yüklü (veya verilen) TMX'te referans edilen tileset'ler için mevcut wangset'leri döndürür. Bridge çalışıyorsa HTTP üzerinden alır, yoksa doğrudan DB'den okur.
- **`wang_paint(wangset_uid, cells, color=1, layer?, erase?, tmx_path?, port?, host?)`** — Wang-aware paint tool'u. Bridge varsa WS/HTTP endpoint'ine gönderir (broadcast); yoksa doğrudan bir `WangCornerState` kurar, `seed_corners_from_layer` ile mevcut TMX'ten köşe durumunu okur ve `apply_wang_paint` ile hücre değerlerini hesaplayıp `tmx_mutator.apply_paint` ile atomic yazar.

### Added — Studio UI (Wang tab)

- **Toolbar**: Yeni **Wang (W)** modu — `mode='wang'`. 'W' klavye kısayolu da mode'u toggle eder.
- **Sağ panel — Wang**: `populateWangPanel()` ilk `/state` sonrasında `GET /wang/sets` çağırır, select dropdown'a tüm wangset'leri doldurur (format: `{tileset} — {name} ({type}, {N}c)`). Seçilen set için renk swatch'ları (24×24 div'ler, `background-color` = `color_hex`) yan yana dizilir. Swatch'a tıklamak `ctl.state.wang = {wangset_uid, color, color_hex, color_name}` set eder ve otomatik wang mode'una geçer. Aktif swatch mavi border + iç gölge ile vurgulanır.
- **Status bar**: `#wang-active` div'i aktif wang seçimini gösterir (`wang: **dirt** color=1 ●`).
- **Paint dispatch**: Wang modunda canvas'ta tıkla+sürükle `ws.send({type:"wang_paint", layer, wangset_uid, color, cells:[{x,y}]})` yollar. Dedup cell-only (wang resolver kendi 3×3 komşuluğunu yönetir).

### Added — e2e testler

- **`scripts/test_studio_wang.py`**: Backend smoke — `/wang/sets` döner mi, `POST /wang/paint` 4×4 stroke doğru pure-wang tile'a (local_id 1887) çözer mi, TMX mtime bump, out-of-stroke hücre korunuyor mu, MCP `tool_list_wangsets_for_tmx` + `tool_wang_paint` (erase path + direct-fallback) çalışıyor mu. 16/16 assertion geçiyor.
- **`scripts/test_studio_wang_browser.py`**: Full UI round-trip — Playwright headless Chromium, `/wang/sets` panel populasyonu, swatch click → `mode='wang'` + state doğrulama, WS `wang_paint` gönderme, interior 9 hücrenin hem Konva grid hem `/state` + TMX disk'te painted olması. 11/11 assertion geçiyor.

### Requirements

- Mevcut gereksinimler yeterli. `wang.py` sadece stdlib (`sqlite3`, `pathlib`, `dataclasses`) kullanıyor.

## [0.5.0] - 2026-04-23

### Added — Tilesmith Studio (canlı browser viewer + editor)

- **`studio/` dizini**. FastAPI köprü sunucu + Vite/TypeScript/Konva tabanlı SPA. Bridge `GET /state` ile MapState JSON, `GET /sprite/{key}.png` ile anında ayrıştırılmış tile PNG'leri, `POST /open` ile yeni TMX yükleme ve `/ws` üzerinden canlı patch broadcast sağlıyor. Birden fazla browser aynı TMX'e bağlanıp birbirini gerçek zamanlı görebiliyor.
- **Phase 1 — Viewer**. Konva tabanlı stage üzerinde tüm tile layer'lar + object group'lar, sprite önbelleğiyle birlikte render. Pan (drag), zoom (scroll), fit-to-view butonu, per-layer görünürlük/opacity slider'ları. Animasyonlu tile'lar (`<animation>`) tek bir RAF clock'u ile kare değiştirir; 60 fps'te 600+ animasyon sürdürülür.
- **Phase 2 — Paint & Erase**. Toolbar üzerinden View (V) / Paint (B) / Erase (E) modları, sağ panelde klikle seçilebilen tile palette (animasyonlu tile'lar öne alınıyor). Tıklama + sürükleme ile tek-hücre paint yapılır; aynı hücre üzerinde sürüklendiğinde WS trafiği dedupe edilir. Yolculuğun tamamı `client → WS → bridge → tmx_mutator (atomic write) → broadcast → applyPaintPatch`. Tüm clientlar state'in single source of truth olan bridge'e göre güncellenir.
- **Phase 3 — Rectangle selection + fill_selection**. Yeni Select (R) modu, drag-rect ile canvas üstünde seçim yapılır, dashed overlay çizilir, status bar'da `[x0,y0]-[x1,y1] (WxH)` gösterilir. Esc seçimi temizler. Bridge `last_selection`'ı tutar ve WS ile diğer clientlara mirror eder; `GET /selection` ve `POST /selection` endpoint'leriyle sorgulanabilir. `POST /fill` endpoint'i stored selection'ı varsayılan bölge olarak kullanır.

### Added — MCP tool'ları (17 → 19)

- **`open_studio(tmx_path, port?, host?)`**: bridge'i subprocess olarak başlatır, URL döner.
- **`close_studio(port?)`**: bridge'i kapatır.
- **`paint_tiles(tmx_path, layer, cells, port?, host?)`**: `cells:[{x,y,key|null}]` ile bir tile layer'a paint/erase patch uygular. Bridge çalışıyorsa HTTP üzerinden gönderir (broadcast), değilse doğrudan `tmx_mutator.apply_paint` ile TMX'e atomic yazar.
- **`patch_object(tmx_path, group, op, id, x?, y?, key?, ...)`**: object group içinde `move` / `delete` / `set_key` operasyonları. Aynı bridge-first/direct-fallback akışı.
- **`fill_rect(tmx_path, layer, x0, y0, x1, y1, key, port?, host?)`**: tile layer'ının dikdörtgen bölgesini tek key ile doldurur (null → siler). Koordinatlar inclusive.
- **`fill_selection(key, port?, host?)`**: browser'da sürükle-seç ile işaretlenmiş son dikdörtgeni okur ve verilen tile key ile doldurur. Doğal dil akışı için: kullanıcı canvas'ta alan seçer, "orayı çimle doldur" der, Claude bu tool'u çağırır.

### Added — yeni Python modülleri

- **`mcp_server/tmx_state.py`**: TMX'i parse edip MapState JSON ve sprite dict üretir. Pack-scoped URL-safe key formatı (`{tileset_stem}__{local_id}`, non-alnum normalize). Object tile'ları Tiled'in sol-alt anchor'ından node koordinatlarına `(x, y - h)` ile çevirir.
- **`mcp_server/tmx_mutator.py`**: `apply_paint(tmx_path, layer, cells)` ve `apply_object_patch(tmx_path, group, patch)` — her ikisi `tempfile.mkstemp + os.replace` ile atomic rewrite yapar, TMX bozulmaz. CSV encoded data grid parse/format, `key→gid` resolver.
- **`studio/bridge/server.py`**: FastAPI + uvicorn sunucu, `asyncio.Lock` ile WS state çakışmasını serialize eder.
- **`studio/frontend/`**: Vite + TypeScript + Konva SPA. `src/canvas.ts` (renderer + anim loop + paint + select overlay), `src/tools.ts` (toolbar + palette + kbd shortcuts), `src/ws.ts` (auto-reconnect), `src/main.ts` (wiring), `src/sprites.ts` (async sprite önbellek).

### Added — e2e test harness

- **`scripts/test_studio_e2e.py`**: Phase 1 smoke — headless Chromium (Playwright), canvas + fps + anim + layer panel doğrulamaları.
- **`scripts/test_studio_phase2.py`**: Paint + erase round-trip, Konva grid + state + TMX file mutation, keyboard shortcut smoke.
- **`scripts/test_studio_multiclient.py`**: İki paralel browser, A paint → B görüyor ve B erase → A görüyor; error surfacing (invalid layer).
- **`scripts/test_studio_selection.py`**: Rect selection + MCP `fill_selection` → 5×3 bölge, tüm probe noktaları painted, dış hücreler untouched, on-disk TMX mutated.

### Added — bridge WS protocol

- **Client → server**:
  - `{type:"ping"}` → sunucu `{type:"pong"}`
  - `{type:"patch", op:"paint"|"erase", layer, cells}` → TMX mutasyon + tüm clientlara `{type:"patch", op:"paint", layer, cells}` broadcast
  - `{type:"patch", op:"object", group, patch}` → aynı şekilde object patch broadcast
  - `{type:"selection", selection: {...}|null}` → bridge `last_selection`'ı günceller, sender hariç herkese echo
- **Server → client**:
  - `{type:"map_loaded", state}` — bağlanınca ve reload gerekince
  - `{type:"patch", op:"paint"|"object", ...}` — canlı değişiklikler
  - `{type:"selection", selection}` — başka bir client seçim değiştirdiğinde mirror
  - `{type:"error", message}` — malformed patch / unknown layer vb.

### Added — frontend UX

- `V` → view (pan), `B` → paint, `E` → erase, `R` → rect select, `Esc` → seçimi temizle.
- Status bar: `WS: connected/error — {msg}`, `{n} fps`, `{n} anim`, `mode: {m}`, `selection: terrain [10,10]-[14,12] (5×3)`.
- Sağ panel: Info (tile/object sayıları) + Cursor tile + Palette (tile thumbnails, animasyonluları öne).
- Drag dedup: aynı hücre üzerinde mouse hareket ederken tekrar paint göndermez.

### Requirements

- `studio/bridge/server.py` için: `fastapi`, `uvicorn[standard]`, `Pillow` (scan/generate için zaten gerekli).
- Frontend build: `studio/frontend/` içinde `npm install && npm run build`. Bridge `dist/` varsa onu serve eder; yoksa dev bilgisi verir.
- e2e test için: `playwright install chromium` (ya da headless shell).

## [0.4.0] - 2026-04-23

### Changed (BREAKING — DB schema)
- **Multi-pack aware şema.** Her asset kayıtı artık `pack_name` sütunuyla scope'lanıyor. Aynı isimli tileset'ler (ör. `Atlas-Props`) farklı paketlerde birbirini ezmiyor. 13 paketlik tarama sonrası 190 tileset korunuyor (önceki şemada ~150 sağlanıyordu).
- **`_auto` + `_overrides` + merged VIEW.** Her asset türü için 3 katman: scanner `_auto`'ya yazar (pack bazlı wipe + rescan), kullanıcı `_overrides`'a yazar (asla silinmez), okurlar unprefixed VIEW'ı sorgular (COALESCE ile override kazanır). Etkilenen türler: `tilesets`, `tiles`, `wang_sets`, `wang_colors`, `wang_tiles`, `props`, `animated_props`, `characters`, `character_animations`, `reference_maps`, `reference_layers`, `automapping_rule_sets`, `automapping_rules`.
- **UID'lere pack prefix.** `tile_uid`, `tileset_uid`, `wangset_uid`, `prop_uid`, `char_uid`, `map_uid`, `ruleset_uid`, `rule_uid`, `aprop_uid`, `canim_uid` artık `PackName::...` formatında. Farklı paket içerikleri asla çakışmaz.
- **Absolute asset path'ler.** `source_path` ve `image_path` tüm tablolarda mutlak resolved path. Relative path'ler `_resolve_asset_path()` ile anchor'a göre absolute'a çevriliyor.
- **Idempotent rescan.** `scan_folder()` artık pack bazlı `DELETE FROM *_auto WHERE pack_name = ?` ile başlıyor; aynı paketi tekrar taramak çift kayıt üretmiyor. `_overrides` tablolarına dokunulmuyor.
- **Yeni `scan_folder(folder, db_path, pack_name=None)` imzası.** MCP tool'u ve CLI, opsiyonel pack adı parametresi kabul ediyor. Varsayılan kök klasör adı.

### Changed (server.py)
- Tüm list tool'ları (`list_tilesets`, `list_wang_sets`, `list_prop_categories`, `list_animated_props`, `list_characters`, `list_reference_layers`, `list_automapping_rules`) opsiyonel `pack_name` filtresini alır ve döndürülen satırlarda `pack_name` sütununu gösterir.
- `db_summary`: merged view üzerinden sayım; ek olarak `packs` listesi (pack başına tileset sayısı) döndürüyor.
- `scan_folder` tool'u `pack_name` parametresini accept ediyor.

### Changed (scripts/indexer/query.py)
- Tüm sorgular merged VIEW'ları okuyor (artık `_auto` değil). Dolayısıyla kullanıcı override'ları otomatik uygulanıyor.
- `has_override`, `material`, `filename`, `has_animation` gibi eski şema-özel sütun referansları temizlendi; yeni şemayla hizalandı.
- `get_transitions()` kaldırıldı (şemada karşılığı yok).
- Tüm sorgular opsiyonel `pack_name` filtresi kabul ediyor.

### Migration
Bu sürüm şemayı kırıyor. Mevcut `data/tiles.db` silinmeli ve paketler tekrar taranmalı:
```
rm tilesmith/data/tiles.db
# Her pack için:
scan_folder path=/path/to/pack
```

## [0.3.1] - 2026-04-23

### Changed
- **Self-contained repo**: `generate_map_v3.py` ve `preview_map.py` artık `ai-mapgen-demo/` harici klasöründen `tilesmith/scripts/` altına taşındı. Repo klon'u tek başına çalışıyor, harici script dosyasına ihtiyaç yok.
- `tilesmith/scripts/indexer/query.py` DB_PATH hardcode'u kaldırıldı; `TILESMITH_DB_PATH` env var veya repo-içi `data/tiles.db` default'u kullanılıyor.
- Default `GENERATOR_SCRIPT` ve `PREVIEW_SCRIPT` path'leri `${CLAUDE_PLUGIN_ROOT}/scripts/`'ye güncellendi.
- README ENV tablosu ve docstring'ler güncel path'leri yansıtacak şekilde düzeltildi.

### Removed
- Workspace temizliği: rename öncesi artifaktlar (`erw-mapgen/`, `erw-mapgen-repo/`, `erw-mapgen.plugin` v0.2.0 zip), `ai-mapgen-demo/` klasörü (gerekli kısımlar `scripts/`'e taşındı), `.DS_Store`, `__pycache__/`.

## [0.3.0] - 2026-04-17

### Changed (BREAKING)
- **Proje adı: `erw-mapgen` → `tilesmith`** — Plugin adı, MCP server adı, marketplace adı ve ENV prefix'leri değişti. Bu ad artık projenin generic Tiled pack desteğini ve atlas forge odağını daha iyi yansıtıyor.
- ENV variable prefix: `ERW_*` → `TILESMITH_*`. Eski `ERW_*` isimleri hâlâ fallback olarak çalışıyor (non-breaking for existing users).
- MCP config'te server key: `erw-mapgen` → `tilesmith`.
- Marketplace key: `erw-mapgen-marketplace` → `tilesmith-marketplace`.

### Migration
Mevcut kurulum için:
```
/plugin uninstall erw-mapgen@erw-mapgen-marketplace
/plugin marketplace remove erw-mapgen-marketplace
/plugin marketplace add <kullaniciadi>/tilesmith
/plugin install tilesmith@tilesmith-marketplace
```

## [0.2.0] - 2026-04-17

### Added
- **Generic scanner** (`scanner.py`): `scan_folder(path)` tool'u artık herhangi bir Tiled paketini recursive tarar. Atlas/collection tileset, wang set, prop, animated prop, karakter ve automapping rule'ları otomatik tespit eder.
- **Consolidate tool** (`consolidate.py`): Üretilen TMX'teki kullanılan GID'leri toplar, shelf bin-packing ile tek bir atlas PNG üretir ve self-contained TMX + TSX (collection) yazar.
- **`plan_map` tool**: Harita üretilmeden önce ASCII yerleşim planı sunar.
- **`create_map` skill**: 7 adımlı interaktif akış (DB doğrula → sor → plan → onay → üret → konsolide → sun).

### Changed
- Skill `harita-yap` → `create_map` olarak yeniden adlandırıldı.
- Plugin.json açıklaması ERW'ye özel olmaktan çıkıp generic Tiled pack desteğini yansıtıyor.
- Server.py ENV handling portable path'lere taşındı (`${CLAUDE_PLUGIN_ROOT}` tabanlı).

## [0.1.0] - 2026-04-17

### Added
- İlk sürüm. ERW - Grass Land 2.0 v1.9'a özel MCP server.
- 9 DB sorgu tool'u + `generate_map` preset tool.
- SQLite `tiles.db` şeması: `_auto` + `_overrides` tablolar.
- `grass_river_forest` preset'i.
