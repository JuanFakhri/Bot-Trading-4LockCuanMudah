"""Research: is the (proven) Phoenix engine robust LONG-ONLY, out-of-sample?

The Phoenix tab shows strong IN-SAMPLE portfolio numbers, but not a walk-forward
split. This script runs the SAME engine (backend.phoenix_backtester — the good
one, with the arm-then-confirm FIB timing) restricted to LONG, then splits the
trades 70/30 by time and reports the out-of-sample slice per engine. That OOS
number is what decides whether Phoenix-long earns a place on the live long side.

Research only — writes docs/data/phoenix_research.json, never touches live state.
Usage: PHOENIX_DAYS=1095 python -m scripts.research_phoenix
"""
from __future__ import annotations

import asyncio
import json
import os

import pandas as pd

from backend import config, data_feed, phoenix_backtester as phx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "data", "phoenix_research.json")

LOOKBACK_DAYS = int(os.getenv("PHOENIX_DAYS", "1095"))
_env_syms = os.getenv("PHOENIX_SYMBOLS", "").strip()
SYMBOLS = [s.strip().upper() for s in _env_syms.split(",") if s.strip()] or config.WATCHLIST
# engines to include on the long side (range is neutral-only, excluded here)
_env_eng = os.getenv("PHOENIX_ENGINES", "fib,breakout").strip()
ENGINES = [e.strip() for e in _env_eng.split(",") if e.strip()]


def _oos_split(trades, frac=0.7):
    srt = sorted(trades, key=lambda t: t["exit_ts"])
    cut = int(len(srt) * frac)
    train, test = srt[:cut], srt[cut:]
    out = {"train": phx._stats(train), "test": phx._stats(test),
           "cutoff_ts": test[0]["exit_ts"] if test else None}
    # per-engine on the OOS test slice
    out["test_by_engine"] = {
        e: phx._stats([t for t in test if t["engine"] == e]) for e in ENGINES}
    return out


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    print(f"[research] Phoenix LONG-only, engines={ENGINES}, {LOOKBACK_DAYS}d, {len(SYMBOLS)} symbols")

    btc_daily = await data_feed.get_klines_history("BTCUSDT", "1d", LOOKBACK_DAYS + 90)
    if btc_daily is None or btc_daily.empty:
        print("[research] no BTC data — aborting")
        await data_feed.close()
        return
    regime_daily = phx.btc_regime_daily(btc_daily)
    print(f"[research] regime days: {regime_daily.value_counts().to_dict()}")

    all_trades = []
    for sym in SYMBOLS:
        try:
            htf = await data_feed.get_klines_history(sym, config.HTF, LOOKBACK_DAYS)
            dtf = await data_feed.get_klines_history(sym, config.DTF, LOOKBACK_DAYS + 60)
            ltf = await data_feed.get_klines_history(sym, "1h", LOOKBACK_DAYS)
            if htf.empty or dtf.empty or ltf.empty:
                print(f"[research] {sym}: no data")
                continue
            trades = phx.backtest_symbol_phoenix(
                sym, htf, dtf, ltf, regime_daily, None,
                {"engines": ENGINES, "sides": ["LONG"]})
            all_trades.extend(trades)
            print(f"[research] {sym}: {len(trades)} long trades")
        except Exception as exc:
            print(f"[research] {sym} error: {exc}")

    overall = phx._stats(all_trades)
    by_engine = {e: phx._stats([t for t in all_trades if t["engine"] == e]) for e in ENGINES}
    oos = _oos_split(all_trades)

    report = {
        "generated_ts": pd.Timestamp.utcnow().isoformat(),
        "params": {"lookback_days": LOOKBACK_DAYS, "symbols": len(SYMBOLS),
                   "engines": ENGINES, "sides": ["LONG"], "demo": config.DEMO},
        "overall": overall, "by_engine": by_engine, "oos": oos,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))

    print(f"[research] OVERALL long: {overall['trades']} trades, win {overall['win_rate']}%, "
          f"PF {overall['profit_factor']}, total {overall['total_r']}R")
    for e in ENGINES:
        s = by_engine[e]
        print(f"[research]   {e:9s}: {s['trades']} trades, win {s['win_rate']}%, PF {s['profit_factor']}, {s['total_r']}R")
    tr, te = oos["train"], oos["test"]
    print(f"[research] WALK-FWD in-sample(train 70%): PF {tr['profit_factor']} win {tr['win_rate']}% ({tr['trades']} tr)")
    print(f"[research] WALK-FWD OUT-of-sample(test 30%): PF {te['profit_factor']} win {te['win_rate']}% ({te['trades']} tr)")
    for e in ENGINES:
        s = oos["test_by_engine"][e]
        print(f"[research]   OOS {e:9s}: PF {s['profit_factor']} win {s['win_rate']}% ({s['trades']} tr)")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
