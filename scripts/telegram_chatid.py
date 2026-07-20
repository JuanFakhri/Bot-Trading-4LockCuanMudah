"""Print ONLY the numeric Telegram chat_id (nothing else) so a launcher script can
capture it. Uses TELEGRAM_TOKEN and the latest chat that messaged the bot. Prints
an empty line if no chat is found yet (user hasn't pressed START).

  TELEGRAM_TOKEN=xxxx python -m scripts.telegram_chatid
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx

TOKEN = os.getenv("TELEGRAM_TOKEN", "")


async def main():
    if not TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            upd = (await c.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates")).json()
    except Exception:
        return
    last = None
    for u in upd.get("result", []):
        m = u.get("message") or u.get("channel_post") or {}
        ch = m.get("chat") or {}
        if ch.get("id"):
            last = ch["id"]        # most recent chat wins
    if last is not None:
        sys.stdout.write(str(last))


if __name__ == "__main__":
    asyncio.run(main())
