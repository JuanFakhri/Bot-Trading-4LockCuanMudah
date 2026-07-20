#!/usr/bin/env bash
# NestSMC — setup VPS (Ubuntu/Debian). Jalankan dari root repo: bash deploy/vps/setup.sh
set -e
echo "[*] apt: python3, venv, pip, git..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
echo "[*] membuat virtualenv .venv ..."
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
echo "[*] memasang library (numpy, pandas, httpx)..."
.venv/bin/pip install --quiet numpy pandas httpx
echo
echo "[OK] Selesai. Berikutnya:"
echo "  1) cp deploy/vps/nestsmc.env.example deploy/vps/nestsmc.env"
echo "  2) isi TELEGRAM_TOKEN & TELEGRAM_CHAT_ID di file itu"
echo "  3) tes: set -a; . deploy/vps/nestsmc.env; set +a; .venv/bin/python -m scripts.run_bot"
