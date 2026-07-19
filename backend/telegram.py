"""Telegram notifier — pings a chat when the bot signals an entry or an exit.

Notification ONLY: it never touches orders or funds. It is a no-op unless BOTH
TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are set, so the GitHub Actions scan (which
has no secrets) and any unconfigured run stay silent. Configure on the VPS/uDroid
via .env. Failures are swallowed so a Telegram hiccup never breaks the scan.
"""
from __future__ import annotations

import os

import httpx

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

_client: httpx.AsyncClient | None = None


def enabled() -> bool:
    return bool(TOKEN and CHAT_ID)


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client


async def send(text: str):
    """Send an HTML message to the configured chat (best-effort)."""
    if not enabled():
        return
    try:
        await _http().post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
        )
    except Exception as exc:
        print(f"[telegram] send failed: {exc}")


async def close():
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        finally:
            _client = None


# --------------------------------------------------------------- message text
def entry_msg(sig: dict, plan: dict) -> str:
    d = sig.get("direction", "?")
    arrow = "🟢 LONG" if d == "LONG" else "🔴 SHORT"
    sym = sig.get("symbol", "?")
    conf = int(round((sig.get("confidence") or 0) * 100))
    entry = plan.get("entry"); sl = plan.get("sl")
    tp1 = plan.get("tp1"); tp2 = plan.get("tp2")
    mach = sig.get("machine", "")
    return (
        f"{arrow}  <b>{sym}</b>  ({mach})\n"
        f"Masuk: <b>{entry}</b>\n"
        f"SL: {sl}  ·  TP1: {tp1}  ·  TP2: {tp2}\n"
        f"Keyakinan: {conf}%  ·  margin $ sesuai setelan (7×)"
    )


def exit_msg(symbol: str, direction: str, outcome: str, r: float, price) -> str:
    icon = "✅" if r > 0.05 else "❌" if r < -0.05 else "➖"
    return (f"{icon} <b>{outcome}</b>  {symbol} {direction}  "
            f"{'+' if r >= 0 else ''}{r:.2f}R\nKeluar: {price}")
