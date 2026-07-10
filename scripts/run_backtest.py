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

from backend import backtester, config, data_feed, database as db, learning, optimizer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "data", "state.json")
TUNING_PATH = os.path.join(ROOT, "data", "tuning.json")
OUT_PATH = os.path.join(ROOT, "docs", "data", "backtest.json")

LOOKBACK_DAYS = int(os.getenv("BACKTEST_DAYS", "180"))

# Optional symbol override (comma-separated, e.g. "ETHUSDT,SOLUSDT"). Empty = all.
_env_syms = os.getenv("BACKTEST_SYMBOLS", "").strip()
SYMBOLS = [s.strip().upper() for s in _env_syms.split(",") if s.strip()] or config.WATCHLIST


def _regime_from_usdtd(usdtd: pd.Series) -> pd.Series:
    """Regime driven solely by USDT.D (same rule as live):
    heading to / at support -> BULL (long), heading to / at resistance -> BEAR."""
    hi = config.USDTD_POS_HI
    lo = 1 - hi
    diff = usdtd.diff().fillna(0.0)
    out = []
    for pos, dv in zip(usdtd, diff):
        if pos > hi:
            out.append("BEAR")
        elif pos < lo:
            out.append("BULL")
        elif dv > 0:            # rising -> toward resistance
            out.append("BEAR")
        else:                   # falling/flat -> toward support
            out.append("BULL")
    return pd.Series(out, index=usdtd.index)


async def _usdtd_timeline() -> pd.Series:
    idx = pd.date_range(end=pd.Timestamp.utcnow().normalize(),
                        periods=LOOKBACK_DAYS + 60, freq="D", tz="UTC")
    if config.DEMO:
        # synthetic oscillation so some short regimes appear in demo
        vals = 0.5 + 0.35 * np.sin(np.linspace(0, 6.28 * 3, len(idx)))
        return pd.Series(vals, index=idx)
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
    return pd.Series(0.5, index=idx)


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)

    print(f"[backtest] lookback={LOOKBACK_DAYS}d, symbols={len(SYMBOLS)}")
    usdtd_daily = await _usdtd_timeline()
    if usdtd_daily.empty:
        print("[backtest] no USDT.D data — aborting")
        return
    regime_daily = _regime_from_usdtd(usdtd_daily)   # regime driven by USDT.D (same as live)

    all_trades: list[dict] = []
    symbol_data: dict = {}
    for sym in SYMBOLS:
        try:
            htf = await data_feed.get_klines_history(sym, config.HTF, LOOKBACK_DAYS)
            dtf = await data_feed.get_klines_history(sym, config.DTF, LOOKBACK_DAYS + 60)
            if htf.empty or dtf.empty:
                print(f"[backtest] {sym}: no data")
                continue
            symbol_data[sym] = (htf, dtf)
            trades = backtester.backtest_symbol(sym, htf, dtf, regime_daily, usdtd_daily)
            all_trades.extend(trades)
            print(f"[backtest] {sym}: {len(trades)} trades")
        except Exception as exc:
            print(f"[backtest] {sym} error: {exc}")

    summary = backtester.summarize(all_trades)

    # ---- optimize: search for settings that turn losses into wins ----
    opt = optimizer.optimize(symbol_data, regime_daily, usdtd_daily)
    tuned = {"sl_atr": opt["params"]["sl_atr"], "min_rr": opt["params"]["min_rr"],
             "require_ad": opt["params"].get("require_ad", True),
             "accepted": opt["accepted"], "updated_ts": pd.Timestamp.utcnow().isoformat()}
    with open(TUNING_PATH, "w", encoding="utf-8") as f:
        json.dump(tuned, f, ensure_ascii=False, indent=0)
    print(f"[backtest] optimize: accepted={opt['accepted']} params={opt['params']} — {opt['reason']}")

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
                   "symbols": len(SYMBOLS), "demo": config.DEMO},
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
        "optimization": opt,
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
