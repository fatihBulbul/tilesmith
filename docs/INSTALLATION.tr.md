# Kurulum Rehberi

> 🇬🇧 English version: [INSTALLATION.md](./INSTALLATION.md)

## Ön Koşullar

- Python 3.10 veya üstü
- pip
- Claude Code CLI veya Cowork
- Node.js 18+ (yalnızca Studio frontend'ini kaynaktan yeniden build etmek istiyorsan)

## 1. Bağımlılıkları Yükle

```bash
pip install -r requirements.txt
```

Studio frontend'i önceden build edilmiş olarak `studio/frontend/dist/` içinde gelir. Sadece viewer'ı değiştirmek istiyorsan Node gerekir:

```bash
cd studio/frontend
npm install
npm run build
```

## 2. Plugin'i Claude Code'a Ekle

### Yöntem A: GitHub'dan marketplace ile kur

Claude Code içinde:

```
/plugin marketplace add <kullaniciadi>/tilesmith
/plugin install tilesmith@tilesmith-marketplace
```

Kısa form GitHub URL'sine genişler. Tam URL de verilebilir:

```
/plugin marketplace add https://github.com/<kullaniciadi>/tilesmith
```

### Yöntem B: Yerel klasörden kur (push'tan önce test)

```
/plugin marketplace add /path/to/tilesmith
/plugin install tilesmith@tilesmith-marketplace
```

### Yöntem C: `.plugin` dosyasından kur

GitHub Releases sayfasından `.plugin` dosyasını indir, sonra Claude Code'da ilgili komutla yükle.

## 3. Cowork / Claude Code üzerinden kullan

Plugin yüklendikten sonra Claude'a doğal dilde yaz:

```
"Şu klasörü tara: /path/to/my-tiled-pack"
```

Claude `scan_folder` tool'unu tetikleyip DB'yi doldurur.

Sonra:

```
"40x40 grass + nehir + orman haritası yap"
```

`create_map` skill'i soru-cevap akışını başlatır (boyut → biyom karışımı → bileşen planı → onay → TMX).

Canlı düzenleme için:

```
"Haritayı Studio'da aç."
"Seçili alanı toprakla doldur."
"Geri al."
```

## 4. Manuel Doğrulama

Plugin'in doğru yüklendiğini kontrol etmek için:

```bash
# MCP server'ı elle çalıştır (hata çıkıyorsa stderr'a yazar)
python3 tilesmith/mcp_server/server.py --help 2>&1 | head
```

Claude Code içinde:

```
/plugin        # kurulu plugin'leri listele
/mcp           # aktif MCP server'ları listele — tilesmith "connected" olmalı
```

DB konumunu değiştirmek istersen `.mcp.json` içindeki `TILESMITH_DB_PATH`'i güncelle.

## Sorun Giderme

**"ERROR: mcp paketi yüklü değil"** → `pip install mcp` çalıştır.

**"Atlas PNG boş / kırık"** → `scan_folder` önce çalıştırıldı mı kontrol et; DB boşsa `consolidate_map` tile'ları bulamaz.

**"Tiled TMX'i açtığında missing image"** → Sprite klasörü (`<stem>_sprites/`) TMX ile aynı dizinde olmalı; taşıma yapıldıysa birlikte taşı.

**"scan_folder taramıyor"** → Klasör yolunun mutlak (absolute) olduğuna emin ol. `~` veya göreli yol çalışmayabilir.

**"Studio'da hiçbir şey görünmüyor"** → Vite frontend en az bir kez build edilmiş olmalı. `cd studio/frontend && npm install && npm run build`. Bundle `studio/frontend/dist/` altında servis edilir ve bilinçli olarak gitignore'dadır.

**Eski `ERW_*` ENV kullananlar** → Backward compat sağlanıyor, dokunmadan çalışır. Yeni kurulumlar için `TILESMITH_*` öneki tercih et.
