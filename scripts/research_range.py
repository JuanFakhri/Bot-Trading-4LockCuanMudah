"""Research: does the Phoenix RANGE engine have a real edge in NEUTRAL regimes?

Runs the range mean-reversion engine only (both sides) over 3y of real data,
using the same BTC-driven regime the live bot uses (range fires in NEUTRAL / low
vol). Reports overall + per-direction + a 70/30 walk-forward OOS split, plus a
funnel so we can see how often the range conditions actually trigger. This is the
"does it work on real crypto, not synthetic mean-reversion" check.

Research only — writes docs/data/range_research.json, never touches live state.
Usage: PHOENIX_DAYS=1095 python -m scripts.research_range
"""
from __future__ import annotations

import asyncio
import json
import os

import numpy as np
import pandas as pd

from backend import config, data_feed, indicators, phoenix_backtester as phx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "data", "range_research.json")
LOOKBACK_DAYS = int(os.getenv("PHOENIX_DAYS", "1095"))
SYMBOLS = config.WATCHLIST


def _oos(trades, frac=0.7):
    srt = sorted(trades, key=lambda t: t["exit_ts"])
    cut = int(len(srt) * frac)
    return phx._stats(srt[:cut]), phx._stats(srt[cut:])


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    print(f"[range] research, {LOOKBACK_DAYS}d, {len(SYMBOLS)} symbols")

    btc_daily = await data_feed.get_klines_history("BTCUSDT", "1d", LOOKBACK_DAYS + 90)
    if btc_daily is None or btc_daily.empty:
        print("[range] no BTC data — aborting"); await data_feed.close(); return
    regime_daily = phx.btc_regime_daily(btc_daily)
    rc = regime_daily.value_counts().to_dict()
    print(f"[range] BTC regime days: {rc}")

    all_trades = []
    for sym in SYMBOLS:
        try:
            htf = await data_feed.get_klines_history(sym, config.HTF, LOOKBACK_DAYS)
            dtf = await data_feed.get_klines_history(sym, config.DTF, LOOKBACK_DAYS + 60)
            ltf = await data_feed.get_klines_history(sym, "1h", LOOKBACK_DAYS)
            if htf.empty or dtf.empty or ltf.empty:
                continue
            tr = phx.backtest_symbol_phoenix(sym, htf, dtf, ltf, regime_daily, None,
                                             {"engines": ["range"], "sides": ["LONG", "SHORT"]})
            all_trades += tr
            print(f"[range] {sym}: {len(tr)} range trades")
        except Exception as exc:
            print(f"[range] {sym} error: {exc}")

    overall = phx._stats(all_trades)
    longs = phx._stats([t for t in all_trades if t["direction"] == "LONG"])
    shorts = phx._stats([t for t in all_trades if t["direction"] == "SHORT"])
    tr, te = _oos(all_trades)
    report = {"generated_ts": pd.Timestamp.utcnow().isoformat(),
              "params": {"lookback_days": LOOKBACK_DAYS, "symbols": len(SYMBOLS),
                         "regime_days": {str(k): int(v) for k, v in rc.items()},
                         "range_min_atr": config.PHX_RANGE_MIN_ATR, "range_rr": config.PHX_RANGE_RR,
                         "range_window": config.PHX_RANGE_WINDOW, "range_rsi_lo": config.PHX_RANGE_RSI_LO},
              "overall": overall, "long": longs, "short": shorts,
              "oos_train": tr, "oos_test": te}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))

    print(f"[range] OVERALL: {overall['trades']} tr, win {overall['win_rate']}%, PF {overall['profit_factor']}, {overall['total_r']}R")
    print(f"[range]   LONG {longs['trades']} tr PF {longs['profit_factor']} | SHORT {shorts['trades']} tr PF {shorts['profit_factor']}")
    print(f"[range]   IN-SAMPLE PF {tr['profit_factor']} ({tr['trades']} tr) | OOS PF {te['profit_factor']} win {te['win_rate']}% ({te['trades']} tr)")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
