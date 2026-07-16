# 📈 SMC Bot — Self-Learning Trading Signal (Web, Gratis)

Bot sinyal trading crypto berbasis web yang **belajar sendiri dari kesalahannya**.
Dibangun dari nol mengikuti **Strategi FIB — Hybrid**: entry di 15M/1H, bias &
fibonacci dari 4H/1D, dengan **2 mesin regime-switch** (BULL→LONG, BEAR→SHORT).

Semua sumber data **gratis tanpa API key** (Binance public API + CoinGecko),
UI real-time via WebSocket, dan tersedia **mode siang/malam**.

---

## ✨ Fitur

- **Filter market (Bagian A)** — bias ALT dibaca berurutan: **① USDT.D**
  (support = risk-on) → **② arah BTC + BTC.D** via matriks dominance (BTC.D turun
  + BTC naik = alt naik) → **③ bias akhir** menentukan mesin LONG/SHORT. SL/TP
  lalu diambil dari likuiditas (swing ±ATR).
- **Mesin BULL (fib-long)** & **Mesin BEAR (fib-short)** lengkap dengan seluruh
  aturan checklist (golden zone 0.5–0.618, EMA200 4H/1D, RSI, A/D line, Parabolic
  SAR, USDT.D di resistance, skip Jumat, trigger ARM→konfirmasi 15M/1H).
- **Exit & risk (Bagian D & E)** — SL 1×ATR di luar swing (cap 6%), TP1 +1R lalu
  breakeven, TP2 fib 1.272 dengan syarat RR≥2, risiko 2%/trade, cooldown 16 bar,
  maks 3 trade/hari, circuit breaker (−8%/hari atau 2 SL).
- **🧠 Self-learning** — setiap sinyal + hasilnya dicatat ke SQLite. Bot menghitung
  win-rate per *pola* dan **otomatis memblokir pola yang berulang rugi** serta
  memprioritaskan pola yang menang. Karena tersimpan di database, **bot tidak
  pernah lupa** — pelajaran bertahan meski di-restart.
- **UI real-time** — kartu sinyal + **grafik candlestick 4H** (golden zone,
  garis Entry/SL/TP), KPI (win rate, profit factor), panel pasar, manajemen
  risiko, daftar pelajaran, jurnal trade. Mode siang/malam.
- **🔬 Backtest + belajar dari sejarah** — tab **Backtest** menjalankan strategi
  pada data historis Binance (via GitHub Actions), menampilkan Win Rate/PF/kurva
  ekuitas, dan **men-feed hasilnya ke mesin pembelajaran** sehingga pola yang
  terbukti rugi di masa lalu langsung diblokir untuk sinyal live.
- **🌐 Screening Makro (berita → crypto)** — tab **Makro** membaca kalender
  **High Impact Expected** ForexFactory dan menilai tiap rilis untuk crypto lewat
  jalur *likuiditas / kebijakan moneter*: **CPI/inflasi turun**, **suku bunga
  dipotong**, atau tenaga kerja melemah → bank sentral cenderung melonggarkan →
  likuiditas naik → **BAGUS** untuk crypto (dan sebaliknya **BURUK** saat inflasi
  memanas / suku bunga naik). Disertai **backtest 3 tahun** (event study FRED +
  Binance) yang mengukur hit-rate arah dan retur BTC +1/+3/+7 hari per bias. Ini
  **murni layer analisa** — tidak mengeksekusi maupun memblokir trade live.
- **⚙️ Optimasi otomatis (kekalahan → kemenangan)** — selain memblokir pola rugi,
  backtest mencari parameter (jarak SL, RR minimal, syarat A/D) yang meningkatkan
  ekspektasi, **memvalidasinya out-of-sample** (train/test) agar tidak overfit,
  lalu **menerapkannya otomatis ke sinyal live** via `data/tuning.json`.
- **Deploy gratis** — GitHub Actions + Pages (tanpa server), atau Render /
  Railway / Fly.io / Docker (lihat [DEPLOY.md](DEPLOY.md)).

---

## 🚀 Cara Menjalankan

```bash
bash run.sh
```

Lalu buka **http://localhost:8000**.

Atau manual:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Variabel opsional: `PORT`, `SCAN_INTERVAL_SEC` (default 60), `BOT_DB_PATH`.

### 🔍 Mode demo (pratinjau tanpa internet)

Untuk melihat UI terisi penuh tanpa akses API (mis. di lingkungan yang memblokir
Binance/CoinGecko), jalankan dengan data sintetis yang tetap melewati pipeline
asli (regime → strategi → sinyal → pembelajaran):

```bash
BOT_DEMO=1 python -m backend.demo_seed        # (opsional) isi riwayat pembelajaran
BOT_DEMO=1 uvicorn backend.main:app --port 8000
```

Mode ini murni untuk pratinjau; mode live memakai data Binance/CoinGecko nyata.

---

## 🧠 Bagaimana bot "belajar dari kesalahan"

1. Saat sinyal ENTRY lolos semua filter, bot membuka **paper trade** dan menyimpan
   *pattern signature*-nya (mesin, zona fib, RSI, USDT.D, A/D, SAR, hari).
2. Bot melacak harga sampai trade kena TP/SL, lalu mencatat hasil (WIN/LOSS + R).
3. `learning.py` memperbarui statistik per pola (Bayesian-smoothed win-rate).
4. Jika sebuah pola punya win-rate < 35% pada ≥5 sampel → pola **DIBLOKIR**;
   sinyal serupa berikutnya tidak akan dieksekusi dan ditandai di UI. Pola dengan
   win-rate ≥65% **diprioritaskan**. Semua pelajaran tampil di panel "Yang
   Dipelajari Bot" dan tersimpan permanen di `data/bot.db`.

---

## 📁 Struktur

```
backend/
  config.py         parameter strategi & risk
  data_feed.py      OHLCV Binance + USDT.D CoinGecko (gratis)
  indicators.py     EMA, RSI, ATR, OBV, A/D, Parabolic SAR, pivot, fib
  market_filter.py  Bagian A — regime BULL/BEAR
  macro_news.py     screening berita ekonomi → bias crypto (CPI/rate/jobs)
  strategy.py       Bagian B & C — mesin long/short + trigger 15M
  risk.py           Bagian D & E — SL/TP, sizing, circuit breaker
  learning.py       mesin self-learning (pola, blokir, prioritas)
  database.py       persistensi SQLite (trade, pola, pelajaran)
  engine.py         orkestrator scan loop + tracking hasil
  main.py           FastAPI: REST + WebSocket + serve frontend
frontend/
  index.html, style.css, app.js   dashboard mode siang/malam
```

---

## ⚠️ Catatan

- Ini alat **edukasi & sinyal**, **bukan nasihat finansial**. Trade dieksekusi
  sebagai *paper trade* untuk pembelajaran, tidak menaruh order sungguhan.
- USDT.D dihitung dari data gratis CoinGecko (proxy 20-hari); untuk presisi penuh
  gunakan sumber data USDT.D khusus bila tersedia.
