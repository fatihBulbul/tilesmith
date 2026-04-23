# Katkıda Bulunma Rehberi

tilesmith'e ilgi gösterdiğin için teşekkürler!

> 🇬🇧 English version: [CONTRIBUTING.md](./CONTRIBUTING.md)

## Geliştirme Ortamı

```bash
git clone https://github.com/<kullaniciadi>/tilesmith.git
cd tilesmith
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Studio frontend Vite + TypeScript kullanır:

```bash
cd studio/frontend
npm install
npm run build          # production bundle → studio/frontend/dist/
npm run dev            # canlı dev server (MCP bridge'e proxy)
```

## Projenin Yapısı

- `mcp_server/server.py` — MCP stdio server; tüm tool'ları `TOOL_DEFS` içinde expose eder.
- `mcp_server/scanner.py` — Generic Tiled pack scanner + SQLite DDL.
- `mcp_server/consolidate.py` — Atlas bin-packer + TMX rewriter.
- `mcp_server/wang.py` — Corner/edge wang resolver'ları, paint dispatcher, state sınıfları, seed yardımcıları.
- `studio/bridge/server.py` — FastAPI/WebSocket köprüsü; viewer'ı sunar ve canlı patch'leri yönetir.
- `studio/frontend/` — Vite + Konva viewer.
- `skills/create_map/SKILL.md` — Claude'un etkileşimli tasarım akışında kullandığı skill talimatları.
- `scripts/test_*.py` — Regresyon takımı (unit + e2e).

## Testler

Backend takımı (Studio process başlatmaz):

```bash
python3 scripts/test_wang_unit.py
python3 scripts/test_studio_wang.py           # corner-type e2e
python3 scripts/test_studio_wang_edge.py      # edge-type e2e
```

Scanner smoke test:

```bash
python3 -c "
import sys; sys.path.insert(0, 'mcp_server')
from scanner import scan_folder
result = scan_folder('/path/to/tiled-pack', 'data/tiles.db')
print(result)
"
```

Consolidate smoke test:

```bash
python3 -c "
import sys; sys.path.insert(0, 'mcp_server')
from consolidate import consolidate
result = consolidate('path/to/input.tmx', 'output/', 'test')
print(result)
"
```

## Pull Request Akışı

1. Feature branch aç: `git checkout -b feature/my-feature`.
2. Commit'leri küçük ve odaklı tut; bir logical change = bir commit.
3. Commit mesajlarında **neden**'i **ne**'den daha çok açıkla.
4. Yeni preset eklediysen: `tool_generate_map` içindeki mapping'i güncelle, README'ye ekle, CHANGELOG.md'ye not düş.
5. Yeni parser heuristic'i eklediysen: `scanner.py`'deki ilgili fonksiyonları (`is_automapping_rule`, `is_character_path`, wangset tespiti vb.) güncelle ve bir regresyon testi yaz.
6. Wang resolver'a dokunduysan: `scripts/test_wang_unit.py` ve ilgili e2e testine case ekle.

## Kod Stili

- Python 3.10+ type hints kullan.
- Her modülü `from __future__ import annotations` ile başlat.
- Kısa ve spesifik docstring'ler tercih et.
- Sihirli sayılar yerine isimli sabitler kullan.
- Public MCP tool şemalarını (`TOOL_DEFS`) sabit tut — buradaki değişiklikler plugin'i script'leyen herkes için breaking'tir.

## İstenen Katkılar

- **Yeni preset'ler**: `desert_oasis`, `snow_forest`, `cave_lava`, `dungeon` ve benzeri biyomlar.
- **Atlas packing algoritmaları**: guillotine, MAXRECTS (şu anki implementasyon basit bir shelf packer).
- **Ek parser konvansiyonları**: daha çok asset pack layout'unu out-of-the-box tanıyan heuristic'ler.
- **Daha geniş Tiled kapsamı**: image layers, object templates, animation frames (TSX `<animation>` tag), staggered/hex orientations.
- **Mixed-type wangsets**: bugün indexlenip paint dispatcher'da reddediliyor — gerçek bir resolver daha çok paketi açar.

## Sorun Bildirme

Issue açarken lütfen ekle:

- Python sürümü + OS.
- Hangi tileset paketini kullanıyorsun (public ise link).
- Tam hata mesajı + stack trace.
- Minimal tekrar-üretme (örn: çalıştırdığın `scan_folder` çağrısı + `wang_paint` argümanları).
- Görsel bir sorun ise Tiled'in ve Tilesmith Studio'nun yan yana screenshot'ı çok yardımcı olur.
