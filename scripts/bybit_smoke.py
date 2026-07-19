"""FASE 3 smoke test — validate the REAL order path on Bybit TESTNET only.

Opens ONE tiny position on a chosen symbol, attaches SL + TP1 + TP2 exactly like
the bot would, prints the resulting orders/position, waits a few seconds, then
closes everything. Use this to confirm your testnet keys + order params work
before letting the bot trade on its own.

REQUIRES (all must be set, or it refuses to run):
  EXCHANGE_TESTNET=1  LIVE_TRADING=1  EXEC_DRY_RUN=0
  BYBIT_API_KEY / BYBIT_API_SECRET   (testnet keys)

Usage:
  EXCHANGE_TESTNET=1 LIVE_TRADING=1 EXEC_DRY_RUN=0 \
      BYBIT_API_KEY=... BYBIT_API_SECRET=... \
      python -m scripts.bybit_smoke ETHUSDT
"""
from __future__ import annotations

import asyncio
import os
import sys

from backend import exchange_bybit as xb
from backend import executor, position_manager


async def main():
    if not (xb.TESTNET and xb.LIVE_TRADING and not xb.DRY_RUN):
        print("[smoke] refuse: need EXCHANGE_TESTNET=1 LIVE_TRADING=1 EXEC_DRY_RUN=0")
        return
    watch = (sys.argv[1] if len(sys.argv) > 1 else "ETHUSDT").upper()
    api = xb.BybitFutures()
    pm = position_manager.PositionManager(api)
    try:
        await api.load()
        symbol = xb.BybitFutures.to_symbol(watch)
        last = await api.price(watch)
        # tiny synthetic trade: SL 1% away, TP1 +1%, TP2 +2% (LONG)
        entry, sl, tp1, tp2 = last, last * 0.99, last * 1.01, last * 1.02
        eq = await api.equity_usdt()
        print(f"[smoke] {symbol} last={last} testnet equity={eq} USDT")
        plan = executor.build_plan(symbol, "LONG", entry, sl, tp1, tp2, eq)
        print(executor.format_plan(plan))
        print("[smoke] placing entry + SL/TP on TESTNET ...")
        st = await pm.open_position(plan)
        if not st:
            print("[smoke] open refused/failed — check qty/min-notional above")
            return
        await asyncio.sleep(4)
        size, avg = await api.position_size(symbol)
        print(f"[smoke] position now: size={size} entry={avg}")
        print("[smoke] closing everything (cancel orders + market close) ...")
        await pm.close_all()
        size, _ = await api.position_size(symbol)
        print(f"[smoke] done. residual size={size} (should be 0)")
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
