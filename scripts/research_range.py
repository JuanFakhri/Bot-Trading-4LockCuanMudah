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


# Strict range variants — give mean-reversion a fair last shot. The key new
# lever is adx_max (only trade when the market is genuinely NOT trending).
VARIANTS = [
    ("R0 baseline (no ADX filter)",        {}),
    ("R1 +ADX<20 only",                    {"adx_max": 20}),
    ("R2 strict (ADX18, wide, RSI25, RR2)", {"adx_max": 18, "min_atr": 3.5, "rsi_lo": 25,
                                             "near_frac": 0.10, "rr_min": 2.0, "cooldown": 24}),
    ("R3 ultra (ADX15, very wide, RSI20)",  {"adx_max": 15, "min_atr": 4.0, "rsi_lo": 20,
                                             "near_frac": 0.08, "rr_min": 2.0, "cooldown": 48}),
]


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

    # fetch each symbol's frames once, reuse across variants
    data = {}
    for sym in SYMBOLS:
        try:
            htf = await data_feed.get_klines_history(sym, config.HTF, LOOKBACK_DAYS)
            dtf = await data_feed.get_klines_history(sym, config.DTF, LOOKBACK_DAYS + 60)
            ltf = await data_feed.get_klines_history(sym, "1h", LOOKBACK_DAYS)
            if not (htf.empty or dtf.empty or ltf.empty):
                data[sym] = (htf, dtf, ltf)
        except Exception as exc:
            print(f"[range] {sym} fetch error: {exc}")
    print(f"[range] fetched {len(data)} symbols")

    results = []
    for name, rg in VARIANTS:
        trades = []
        for sym, (htf, dtf, ltf) in data.items():
            trades += phx.backtest_symbol_phoenix(
                sym, htf, dtf, ltf, regime_daily, None,
                {"engines": ["range"], "sides": ["LONG", "SHORT"], "range": rg})
        overall = phx._stats(trades)
        tr, te = _oos(trades)
        results.append({"name": name, "range": rg, "overall": overall,
                        "oos_train": tr, "oos_test": te})
        print(f"[range] {name:38s} | all {overall['trades']:>5} tr PF {overall['profit_factor']:<4} "
              f"win {overall['win_rate']:<4}% totR {overall['total_r']:<8} | OOS PF {te['profit_factor']:<4} totR {te['total_r']}")

    report = {"generated_ts": pd.Timestamp.utcnow().isoformat(),
              "params": {"lookback_days": LOOKBACK_DAYS, "symbols": len(data),
                         "regime_days": {str(k): int(v) for k, v in rc.items()}},
              "results": results}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))
    print("[range] done")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
