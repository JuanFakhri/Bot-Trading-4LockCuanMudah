"""FASE 1 connectivity check for Bybit futures — READ-ONLY, never places orders.

Run on your VPS (or anywhere with open network). Start on TESTNET:

  EXCHANGE_TESTNET=1 python -m scripts.bybit_check                 # public only
  EXCHANGE_TESTNET=1 BYBIT_API_KEY=... BYBIT_API_SECRET=... \
      python -m scripts.bybit_check                                # + your account

Without keys it verifies market data + that every watchlist symbol exists as a
Bybit linear USDT perp. With keys it also prints your USDT equity and open
positions. It does NOT send any order. Once this looks right on testnet, we build
Fase 2 (the executor).
"""
from __future__ import annotations

import asyncio

from backend import config
from backend.exchange_bybit import (BybitFutures, TESTNET, LIVE_TRADING,
                                    DRY_RUN, LEVERAGE, RISK_PCT, MAX_CONCURRENT)


async def main():
    api = BybitFutures()
    print(f"[bybit-check] testnet={TESTNET} LIVE_TRADING={LIVE_TRADING} "
          f"DRY_RUN={DRY_RUN} leverage={LEVERAGE}x risk={RISK_PCT*100:.1f}% "
          f"max_concurrent={MAX_CONCURRENT}")
    if LIVE_TRADING and not DRY_RUN and not TESTNET:
        print("[bybit-check] NOTE: LIVE real-money mode is configured (no orders "
              "are sent by THIS script, but the executor would trade for real).")
    try:
        await api.load()
        print(f"[bybit-check] markets loaded: {len(api.markets)}")
        print("[bybit-check] watchlist -> Bybit linear USDT perp:")
        missing = []
        for s in config.WATCHLIST:
            u = api.to_symbol(s)
            ok = u in api.markets
            if not ok:
                missing.append(s)
            print(f"   {s:10} -> {u:16} {'OK' if ok else 'MISSING'}")
        if missing:
            print(f"[bybit-check] WARNING: not tradable on Bybit linear perp: {missing}")
        else:
            print("[bybit-check] all watchlist symbols tradable on Bybit ✓")

        if api.ex.apiKey:
            eq = await api.equity_usdt()
            pos = await api.open_positions()
            print(f"[bybit-check] account USDT equity: {eq}")
            print(f"[bybit-check] open positions: {len(pos)}")
            for p in pos:
                print(f"   {p['symbol']} {p.get('side')} contracts={p.get('contracts')} "
                      f"entry={p.get('entryPrice')} uPnl={p.get('unrealizedPnl')}")
        else:
            print("[bybit-check] no API key set -> public check only "
                  "(set BYBIT_API_KEY/SECRET to also read balance & positions)")
        print("[bybit-check] done — READ-ONLY, no orders were placed.")
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
