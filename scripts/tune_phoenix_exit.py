"""Exit-tuning sweep for the Phoenix LONG machine (research only).

Entries are held FIXED (the validated arm-then-confirm FIB + breakout); only the
EXIT is varied. Each variant is scored over 3y of real data with a 70/30
walk-forward split, so we optimize exits without curve-fitting entries. Data is
fetched once per symbol and reused across every variant.

Writes docs/data/phoenix_exit_tune.json and prints a comparison table. Nothing
here touches the live bot — a winner is applied only after review.

Usage: PHOENIX_DAYS=1095 python -m scripts.tune_phoenix_exit
"""
from __future__ import annotations

import asyncio
import json
import os

import pandas as pd

from backend import config, data_feed, phoenix_backtester as phx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "data", "phoenix_exit_tune.json")
LOOKBACK_DAYS = int(os.getenv("PHOENIX_DAYS", "1095"))
SYMBOLS = config.WATCHLIST
ENGINES = ["fib", "breakout"]

# Principled exit variants (entries identical). Baseline = current live exit.
VARIANTS = [
    ("V0 baseline (tp2 2R, tp1 50%@1R, SL0.8, ts12)", {}),
    ("V1 runner 3R",                       {"tp2_r": 3.0}),
    ("V2 pure trailing (no fixed TP2)",    {"tp2_r": 0.0}),
    ("V3 TP1 33% (keep more for runner)",  {"tp1_frac": 0.33}),
    ("V4 TP1 33% + runner 3R",             {"tp1_frac": 0.33, "tp2_r": 3.0}),
    ("V5 TP1 33% + pure trailing",         {"tp1_frac": 0.33, "tp2_r": 0.0}),
    ("V6 wider SL 1.0 ATR",                {"sl_atr": 1.0}),
    ("V7 no time-stop",                    {"time_stop": 0}),
]


def _oos(trades, frac=0.7):
    srt = sorted(trades, key=lambda t: t["exit_ts"])
    cut = int(len(srt) * frac)
    return phx._stats(srt[:cut]), phx._stats(srt[cut:])


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    print(f"[tune] Phoenix exit sweep, {LOOKBACK_DAYS}d, {len(SYMBOLS)} symbols, {len(VARIANTS)} variants")

    btc_daily = await data_feed.get_klines_history("BTCUSDT", "1d", LOOKBACK_DAYS + 90)
    if btc_daily is None or btc_daily.empty:
        print("[tune] no BTC data — aborting"); await data_feed.close(); return
    regime_daily = phx.btc_regime_daily(btc_daily)

    # fetch each symbol's frames ONCE
    data = {}
    for sym in SYMBOLS:
        try:
            htf = await data_feed.get_klines_history(sym, config.HTF, LOOKBACK_DAYS)
            dtf = await data_feed.get_klines_history(sym, config.DTF, LOOKBACK_DAYS + 60)
            ltf = await data_feed.get_klines_history(sym, "1h", LOOKBACK_DAYS)
            if not (htf.empty or dtf.empty or ltf.empty):
                data[sym] = (htf, dtf, ltf)
        except Exception as exc:
            print(f"[tune] {sym} fetch error: {exc}")
    print(f"[tune] fetched {len(data)} symbols")

    results = []
    for name, ex in VARIANTS:
        trades = []
        for sym, (htf, dtf, ltf) in data.items():
            trades += phx.backtest_symbol_phoenix(
                sym, htf, dtf, ltf, regime_daily, None,
                {"engines": ENGINES, "sides": ["LONG"], "exit": ex})
        overall = phx._stats(trades)
        tr, te = _oos(trades)
        results.append({"name": name, "exit": ex, "overall": overall,
                        "oos_train": tr, "oos_test": te})
        print(f"[tune] {name:44s} | all PF {overall['profit_factor']:<4} win {overall['win_rate']:<4}% "
              f"totR {overall['total_r']:<7} | OOS PF {te['profit_factor']:<4} win {te['win_rate']:<4}% totR {te['total_r']}")

    # rank by OOS total_r (robust forward profit), then OOS PF
    ranked = sorted(results, key=lambda r: (r["oos_test"]["total_r"], r["oos_test"]["profit_factor"]), reverse=True)
    base = next(r for r in results if r["name"].startswith("V0"))
    report = {"generated_ts": pd.Timestamp.utcnow().isoformat(),
              "params": {"lookback_days": LOOKBACK_DAYS, "symbols": len(data), "engines": ENGINES},
              "baseline": base, "results": results, "ranked_by_oos_totalr": [r["name"] for r in ranked]}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))

    print("\n[tune] === RANK by OOS total R ===")
    for r in ranked:
        te = r["oos_test"]; o = r["overall"]
        flag = "  <= BASELINE" if r["name"].startswith("V0") else ""
        print(f"  OOS totR {te['total_r']:<7} PF {te['profit_factor']:<4} win {te['win_rate']:<4}% | "
              f"all totR {o['total_r']:<7} PF {o['profit_factor']:<4} | {r['name']}{flag}")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
