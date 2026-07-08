# 🚀 Deploy Gratis — FIB Hybrid Bot

Bot ini FastAPI + WebSocket, jadi bisa online 24/7 dan dibuka dari HP. Berikut
3 pilihan gratis. **Render** paling mudah (1-klik).

> ⚠️ Penting soal data: sinyal live butuh akses ke **Binance** & **CoinGecko**.
> Pastikan platform yang dipilih tidak memblokir kedua host itu (mayoritas tidak).
> Kalau ingin sekadar pratinjau UI tanpa data live, set `BOT_DEMO=1`.

---

## 1) Render.com — paling mudah (rekomendasi)

1. Push repo ke GitHub (lihat langkah di bawah).
2. Buka <https://render.com> → **New +** → **Blueprint**.
3. Pilih repo ini. Render membaca `render.yaml` otomatis → **Apply**.
4. Tunggu build selesai, buka URL `https://<nama>.onrender.com`.

Catatan free tier: service "tidur" setelah ~15 menit idle dan bangun lagi saat
diakses (loop pemindaian ikut jeda saat tidur). Database SQLite bersifat
sementara di free tier — pelajaran bisa ter-reset saat re-deploy. Untuk
persisten permanen, tambahkan **Disk** (berbayar) dan set
`BOT_DB_PATH=/var/data/bot.db`, atau pakai Fly.io (opsi 3).

---

## 2) Railway.app

1. Push repo ke GitHub.
2. <https://railway.app> → **New Project** → **Deploy from GitHub repo**.
3. Railway mendeteksi `Procfile` / `Dockerfile` otomatis. Deploy.
4. Buat domain publik di **Settings → Networking → Generate Domain**.

Railway memberi kredit gratis bulanan (cukup untuk bot ringan ini).

---

## 3) Fly.io — punya volume persisten (pelajaran tidak hilang)

```bash
# install flyctl: https://fly.io/docs/hands-on/install-flyctl/
fly auth login
fly launch --no-deploy            # pakai fly.toml yang sudah ada; ganti nama app bila diminta
fly volumes create botdata --size 1 --region sin
fly deploy
```

`fly.toml` sudah mengarahkan `BOT_DB_PATH=/data/bot.db` ke volume `botdata`,
jadi riwayat pembelajaran tetap tersimpan antar-deploy.

---

## Docker (host mana pun / VPS)

```bash
docker build -t fib-hybrid-bot .
docker run -d --name fib-bot -p 8000:8000 \
  -v fibdata:/app/data \
  -e SCAN_INTERVAL_SEC=60 \
  fib-hybrid-bot
```

Buka <http://localhost:8000>. Volume `fibdata` menjaga `data/bot.db` persisten.

---

## Push repo ini ke GitHub (jika belum)

```bash
git remote add origin https://github.com/<username>/<repo>.git
git push -u origin claude/fib-hybrid-trading-bot-elzm69
# lalu merge ke main lewat Pull Request, atau push langsung ke main:
# git push origin claude/fib-hybrid-trading-bot-elzm69:main
```

---

## Variabel lingkungan

| Variabel            | Default            | Fungsi                                        |
|---------------------|--------------------|-----------------------------------------------|
| `PORT`              | `8000`             | Port HTTP (di-set otomatis oleh host)         |
| `SCAN_INTERVAL_SEC` | `60`               | Interval pemindaian pasar (detik)             |
| `BOT_DB_PATH`       | `data/bot.db`      | Lokasi SQLite (arahkan ke volume utk persisten)|
| `BOT_DEMO`          | `0`                | `1` = pakai data sintetis (pratinjau tanpa API)|
