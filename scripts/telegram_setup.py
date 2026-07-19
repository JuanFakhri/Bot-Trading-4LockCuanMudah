"""Find your Telegram chat_id + send a test message.

1) In Telegram, open your bot (e.g. @SimpleCuan_bot) and tap START / send any message.
2) Run:  TELEGRAM_TOKEN=xxxxx python3 -m scripts.telegram_setup
3) It prints your chat_id and sends a test message. Put the id in .env:
       TELEGRAM_CHAT_ID=<the number>
"""
from __future__ import annotations

import asyncio
import os

import httpx

TOKEN = os.getenv("TELEGRAM_TOKEN", "")


async def main():
    if not TOKEN:
        print("Set TELEGRAM_TOKEN first, e.g.:\n  TELEGRAM_TOKEN=xxxx python3 -m scripts.telegram_setup")
        return
    async with httpx.AsyncClient(timeout=20.0) as c:
        me = (await c.get(f"https://api.telegram.org/bot{TOKEN}/getMe")).json()
        if not me.get("ok"):
            print(f"Token invalid: {me}")
            return
        print(f"Bot OK: @{me['result'].get('username')}")
        upd = (await c.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates")).json()
        chats = {}
        for u in upd.get("result", []):
            m = u.get("message") or u.get("channel_post") or {}
            ch = m.get("chat") or {}
            if ch.get("id"):
                chats[ch["id"]] = ch.get("username") or ch.get("title") or ch.get("first_name") or "?"
        if not chats:
            print("Belum ada chat. Kirim /start (atau pesan apa pun) ke bot-mu dulu, "
                  "lalu jalankan skrip ini lagi.")
            return
        print("Chat ditemukan:")
        for cid, name in chats.items():
            print(f"   chat_id = {cid}   ({name})")
        cid = list(chats)[0]
        await c.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                     json={"chat_id": cid, "text": "✅ NestSMC terhubung ke Telegram!"})
        print(f"\nTest terkirim ke {cid}. Cek Telegram-mu.")
        print(f"Sekarang set di .env:  TELEGRAM_CHAT_ID={cid}")


if __name__ == "__main__":
    asyncio.run(main())
