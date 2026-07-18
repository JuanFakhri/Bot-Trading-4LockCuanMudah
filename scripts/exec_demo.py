"""FASE 2 demo — show what the executor WOULD send, fully offline.

Feeds two synthetic ENTRY signals (one LONG, one SHORT) through the executor in
DRY_RUN and prints the resulting order plans (sizing from 2% risk, leverage 7x).
No API key, no network, no orders — just proves the sizing + order logic.

    EXEC_DRY_EQUITY=1000 python -m scripts.exec_demo
"""
from __future__ import annotations

import asyncio

from backend import executor as ex
from backend import exchange_bybit as xb


async def main():
    print(f"[demo] DRY_RUN={xb.DRY_RUN} LIVE_TRADING={xb.LIVE_TRADING} "
          f"leverage={xb.LEVERAGE:g}x risk={xb.RISK_PCT*100:.1f}% "
          f"dry_equity={ex.DRY_EQUITY}\n")

    # executor WITHOUT an exchange client -> pure offline sizing (no market rounding)
    engine = ex.Executor(api=None)

    long_sig = {
        "symbol": "ETHUSDT", "direction": "LONG",
        "plan": {"entry": 3000.0, "sl": 2940.0, "tp1": 3060.0, "tp2": 3120.0},
    }
    short_sig = {
        "symbol": "SOLUSDT", "direction": "SHORT",
        "plan": {"entry": 150.0, "sl": 153.0, "tp1": 147.0, "tp2": 144.0},
    }

    for sig in (long_sig, short_sig):
        await engine.on_entry(sig)
        print()

    print("[demo] ^ Ini yang AKAN dikirim ke Bybit saat Fase 3 aktif. "
          "Sekarang cuma log — nol order, nol uang bergerak.")


if __name__ == "__main__":
    asyncio.run(main())
