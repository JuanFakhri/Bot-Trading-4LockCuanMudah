"""Telegram COMMAND handler — makes the bot reply to /status, /pnl, etc.

Long-polls getUpdates and answers commands sent from the configured owner chat
(TELEGRAM_CHAT_ID). Info-only (bot stays running 24/7 — no pause/stop commands).
Runs alongside the scan loop (run_bot / main). No-op unless telegram is enabled.

Security: only the owner chat is obeyed; messages from anyone else are ignored.
"""
from __future__ import annotations

import asyncio
import os

from . import config, database as db, telegram
from .engine import engine

HELP = (
    "🤖 <b>Perintah Bot 4LockCuanMudah</b>\n"
    "/status  - ringkasan bot\n"
    "/balance - mode &amp; saldo\n"
    "/pnl     - statistik menang/kalah\n"
    "/position- posisi terbuka\n"
    "/diag    - kenapa belum ada sinyal\n"
    "/help    - bantuan ini"
)


def _gates():
    reg = engine.regime or {}
    regime = reg.get("regime", "NEUTRAL")
    cpi = reg.get("cpi_bias", "NETRAL")
    long_open = regime == "BULL"
    short_open = cpi != "BULLISH"
    return regime, cpi, long_open, short_open


def _status() -> str:
    regime, cpi, long_open, short_open = _gates()
    n = len(engine.signals or [])
    entries = sum(1 for s in (engine.signals or []) if s.get("state") == "ENTRY")
    st = db.stats_summary()
    scan = (engine.last_scan or "-")[:19].replace("T", " ")
    return (
        f"📊 <b>Status NestSMC</b>\n"
        f"Regime BTC: <b>{regime}</b> · CPI: <b>{cpi}</b>\n"
        f"Gerbang LONG: {'🔓 buka' if long_open else '🔒 kunci'} · "
        f"SHORT: {'🔓 buka' if short_open else '🔒 kunci'}\n"
        f"Sinyal aktif: <b>{n}</b> (ENTRY: {entries}) · Posisi terbuka: <b>{st['open']}</b>\n"
        f"Status: ▶️ jalan (realtime)\n"
        f"Scan terakhir (UTC): {scan}\n"
        f"Koin dipantau: {len(config.WATCHLIST)}"
    )


def _pnl() -> str:
    st = db.stats_summary()
    return (
        f"📈 <b>Statistik (paper/live)</b>\n"
        f"Selesai: <b>{st['resolved']}</b> · Menang: {st['wins']} · Kalah: {st['losses']}\n"
        f"Winrate: <b>{st['win_rate']}%</b> · PF: <b>{st['profit_factor']}</b>\n"
        f"Total R: <b>{st['total_r']}</b> · Posisi terbuka: {st['open']}"
    )


def _position() -> str:
    rows = db.open_trades()
    if not rows:
        return "📭 Tidak ada posisi terbuka."
    out = ["📌 <b>Posisi terbuka</b>"]
    for t in rows[:20]:
        out.append(f"• {t['symbol']} {t['direction']} @ {t['entry']} "
                   f"SL {t['sl']} TP2 {t['tp2']}")
    return "\n".join(out)


def _diag() -> str:
    regime, cpi, long_open, short_open = _gates()
    if long_open or short_open:
        return ("🔍 Gerbang terbuka, bot menunggu setup berkualitas yang lolos "
                "skor + filter. Ini normal — kualitas &gt; kuantitas.")
    return (
        "🔍 <b>Kenapa belum ada sinyal</b>\n"
        f"Kedua gerbang <b>terkunci</b> oleh kondisi pasar:\n"
        f"• LONG butuh regime <b>BULL</b> (sekarang {regime}).\n"
        f"• SHORT butuh CPI bukan BULLISH (sekarang {cpi} → jangan short lawan makro).\n"
        "Bot tetap memantau; begitu gerbang terbuka & setup lolos, sinyal dikirim."
    )


def _balance() -> str:
    if os.getenv("EXEC_ENABLED", "0") == "1":
        return "💰 Mode EKSEKUSI aktif. (Saldo bursa akan tampil saat terhubung.)"
    return ("💰 Mode <b>NOTIFIKASI</b> — bot belum membuka order di bursa, "
            "jadi tidak ada saldo bursa. (Aman, uang tak tersentuh.)")


async def _dispatch(cmd: str) -> str | None:
    cmd = cmd.lower().split("@")[0].strip()
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/status":
        return _status()
    if cmd == "/pnl":
        return _pnl()
    if cmd == "/position":
        return _position()
    if cmd == "/diag":
        return _diag()
    if cmd == "/balance":
        return _balance()
    return None


async def poll_commands():
    """Long-poll getUpdates and answer owner commands. Best-effort forever."""
    if not telegram.enabled():
        return
    offset = None
    await telegram.send("💬 Perintah aktif. Ketik /help untuk daftar perintah.")
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            r = await telegram._http().get(
                f"https://api.telegram.org/bot{telegram.TOKEN}/getUpdates", params=params)
            data = r.json()
            for u in data.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                chat = str((m.get("chat") or {}).get("id", ""))
                text = (m.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                if chat != str(telegram.CHAT_ID):     # only obey the owner
                    continue
                reply = await _dispatch(text)
                if reply:
                    await telegram.send(reply)
        except Exception as exc:
            print(f"[telegram-cmd] poll error: {exc}")
            await asyncio.sleep(5)
