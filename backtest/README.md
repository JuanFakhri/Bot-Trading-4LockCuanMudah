# 🧪 Backtest — FIB Hybrid + SMC Confluence

`FIB_Hybrid_SMC.pine` adalah **strategi Pine Script v6** untuk **menguji** usulan
tambahan (SMC, ADX, ATR, volume, premium/discount, BTC.D/USDT.D/DXY/TOTAL3, sesi,
multi-TF) **di atas** strategi FIB Hybrid — **sebelum** dimasukkan ke bot live.

## Cara backtest di TradingView
1. Buka [TradingView](https://www.tradingview.com) → **Pine Editor**.
2. Tempel isi `FIB_Hybrid_SMC.pine` → **Save** → **Add to chart**.
3. Set chart ke **1H** pada pair USDT (mis. `BINANCE:ETHUSDT`).
4. Buka **Strategy Tester** (bawah) → lihat Net Profit, **Win Rate**, **Profit Factor**, Max Drawdown.

## Cara memilih komponen yang benar-benar menambah edge
Nyalakan/matikan tiap toggle di grup **"2) Filter"** satu per satu, lalu bandingkan
metrik. Pertahankan yang **menaikkan Profit Factor & Win Rate tanpa membuat jumlah
trade terlalu sedikit** (< ~30 trade = tidak signifikan).

Uji juga **out-of-sample**: jalankan pada rentang tanggal berbeda (mis. 2022–2023
lalu 2024–2025). Kalau hasil bagus di satu periode tapi jelek di periode lain →
kemungkinan *overfit*, jangan dipakai.

## Rekomendasi awal (hipotesis untuk diuji)
- **Kandidat kuat:** Multi-TF, Premium/Discount, ADX, ATR filter, Volume, exit
  30/30/40 + trailing EMA20, risk 1%, cooldown.
- **Uji terpisah:** paket SMC (Sweep+CHOCH+BOS), FVG, Order Block — kuat tapi
  menurunkan jumlah trade & deteksinya heuristik.
- **Opsional/makro:** BTC.D, USDT.D, DXY, TOTAL3, sesi London/NY.
- **AI Score (#15):** gerbang skor ≥ 85 menggantikan "wajib semua kondisi" (#20)
  yang terlalu ketat. Turunkan ambang bila trade terlalu sedikit.
- **News filter (#13):** TIDAK di Pine (tak ada feed kalender) — diterapkan di bot
  live memakai API kalender ekonomi.

> ⚠️ Deteksi SMC di Pine bersifat perkiraan; hasil backtest untuk perbandingan
> relatif, bukan janji hasil live. Selalu validasi out-of-sample.
