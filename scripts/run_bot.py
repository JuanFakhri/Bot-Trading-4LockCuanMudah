"""Headless bot runner — scan loop + executor + Telegram alerts, NO web dashboard.

Termux-friendly: the whole strategy runs without fastapi / uvicorn / pydantic
(so no Rust compile). Needs only numpy + pandas (from `pkg`) and httpx + ccxt
(pure-python, from `pip`). You watch the bot via Telegram alerts + the terminal
log instead of the web dashboard.

  python -m scripts.run_bot

Honours the same env as the full app (EXEC_ENABLED, LIVE_TRADING, EXEC_DRY_RUN,
EXEC_SIZE_MODE, TELEGRAM_TOKEN, ...). Ctrl-C to stop.
"""
from __future__ import annotations

import asyncio

from backend import config, data_feed, telegram, telegram_commands
from backend.engine import run_loop


async def main():
    print(f"[run_bot] NestSMC headless — scan tiap {config.SCAN_INTERVAL_SEC}s. "
          f"Ctrl-C untuk berhenti.")
    if telegram.enabled():
        await telegram.send("🤖 <b>NestSMC aktif</b> — memantau pasar &amp; siap kirim sinyal.")
    try:
        # scan loop + Telegram command listener run together
        await asyncio.gather(run_loop(), telegram_commands.poll_commands())
    finally:
        await data_feed.close()
        await telegram.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[run_bot] dihentikan.")
