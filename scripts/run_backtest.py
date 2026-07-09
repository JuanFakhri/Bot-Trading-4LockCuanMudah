"""Run a historical backtest of the FIB Hybrid strategy and let the bot LEARN.

Steps:
  1. Build a regime timeline from BTC's 1D EMA50 slope.
  2. Build a USDT.D position timeline (20-day range) from CoinGecko history.
  3. Backtest every watchlist symbol on 4H history with 1D filters.
  4. REBUILD the learning brain from the backtest: every resolved trade is fed
     into ``learning`` so losing patterns get blocked and winners get favoured.
  5. Write the report to ``docs/data/backtest.json`` (shown in the web UI) and
     persist the updated learning to ``data/state.json``.

Usage: ``BOT_DEMO=1 python -m scripts.run_backtest``  (or without BOT_DEMO on a
host that can reach Binance/CoinGecko, e.g. GitHub Actions).
"""
from __future__ import annotations

import asyncio
import json
import os

import numpy as np
import pandas as pd

from backend import backtester, config, data_feed, database as db, indicators, learning

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "data", "state.json")
OUT_PATH = os.path.join(ROOT, "docs", "data", "backtest.json")

LOOKBACK_DAYS = int(os.getenv("BACKTEST_DAYS", "180"))


async def _regime_timeline() -> pd.Series:
    btc = await data_feed.get_klines_history("BTCUSDT", "1d", LOOKBACK_DAYS + 60)
    if btc.empty:
        return pd.Series(dtype=object)
    ema50 = indicators.ema(btc["close"], config.EMA_FAST)
    rising = ema50 > ema50.shift(3)
    return rising.map(lambda x: "BULL" if x else "BEAR")


async def _usdtd_timeline(index: pd.Index) -> pd.Series:
    if config.DEMO:
        # synthetic oscillation so some short setups can arm in demo
        vals = 0.5 + 0.35 * np.sin(np.linspace(0, 6.28 * 3, len(index)))
        return pd.Series(vals, index=index)
    try:
        h = await data_feed._client.get(
            config.COINGECKO_BASE + "/coins/tether/market_chart",
            params={"vs_currency": "usd", "days": "365", "interval": "daily"},
        )
        if h.status_code == 200:
            caps = h.json().get("market_caps", [])
            if len(caps) > 25:
                s = pd.Series([c[1] for c in caps],
                              index=pd.to_datetime([c[0] for c in caps], unit="ms", utc=True))
                lo = s.rolling(config.USDTD_LOOKBACK, min_periods=5).min()
                hi = s.rolling(config.USDTD_LOOKBACK, min_periods=5).max()
                pos = ((s - lo) / (hi - lo).replace(0, np.nan)).clip(0, 1).fillna(0.5)
                return pos
    except Exception as exc:
        print(f"[backtest] usdtd history failed: {exc}")
    return pd.Series(0.5, index=index)


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)

    print(f"[backtest] lookback={LOOKBACK_DAYS}d, symbols={len(config.WATCHLIST)}")
    regime_daily = await _regime_timeline()
    if regime_daily.empty:
        print("[backtest] no BTC data — aborting")
        return
    usdtd_daily = await _usdtd_timeline(regime_daily.index)

    all_trades: list[dict] = []
    for sym in config.WATCHLIST:
        try:
            htf = await data_feed.get_klines_history(sym, config.HTF, LOOKBACK_DAYS)
            dtf = await data_feed.get_klines_history(sym, config.DTF, LOOKBACK_DAYS + 60)
            if htf.empty or dtf.empty:
                print(f"[backtest] {sym}: no data")
                continue
            trades = backtester.backtest_symbol(sym, htf, dtf, regime_daily, usdtd_daily)
            all_trades.extend(trades)
            print(f"[backtest] {sym}: {len(trades)} trades")
        except Exception as exc:
            print(f"[backtest] {sym} error: {exc}")

    summary = backtester.summarize(all_trades)

    # ---- REBUILD the learning brain from the backtest ----
    db.execute("DELETE FROM pattern_stats")
    db.execute("DELETE FROM lessons")
    for t in sorted(all_trades, key=lambda x: x["exit_ts"]):
        learning.record_outcome(t["features"], t["r"] > 0.05, t["r"])

    blocked = [l for l in db.lessons(200) if l["kind"] == "BLOCK"]
    favored = [l for l in db.lessons(200) if l["kind"] == "FAVOR"]

    report = {
        "generated_ts": pd.Timestamp.utcnow().isoformat(),
        "params": {"lookback_days": LOOKBACK_DAYS, "htf": config.HTF,
                   "symbols": len(config.WATCHLIST), "demo": config.DEMO},
        "summary": summary,
        "recent_trades": [
            {k: t[k] for k in ("symbol", "direction", "entry", "exit_price",
                               "outcome", "r", "rr", "entry_ts", "exit_ts")}
            for t in sorted(all_trades, key=lambda x: x["exit_ts"], reverse=True)[:40]
        ],
        "learned": {
            "blocked_count": len(blocked),
            "favored_count": len(favored),
            "lessons": db.lessons(40),
        },
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(db.export_state(), f, ensure_ascii=False, indent=0)

    s = summary
    print(f"[backtest] DONE: {s['trades']} trades, win {s['win_rate']}%, "
          f"PF {s['profit_factor']}, expectancy {s['expectancy_r']}R, "
          f"maxDD {s['max_drawdown_r']}R | learned {len(blocked)} blocks, {len(favored)} favors")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
