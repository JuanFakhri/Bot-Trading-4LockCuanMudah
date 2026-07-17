"""Backtest the LIVE two-machine bot: Phoenix Hybrid (LONG) + classic SMC (SHORT).

This mirrors exactly what the live engine trades now (see strategy_smc.evaluate):
  * LONG  entries come from backend.phoenix.backtest_symbol_long
  * SHORT entries come from smc_backtester.backtest_symbol_smc (allow_long=False)

It builds the real-data pipeline (USDT.D + BTC.D timelines, Binance klines),
combines both machines, and reports the blended win-rate / PF plus a
per-machine breakdown so the long (Phoenix) and short (SMC) edges are visible
separately. Walk-forward (train 70% / test 30%) gives the honest OOS number.

Not to be confused with scripts/backtest_phoenix.py — that is a separate research
portfolio simulator. This one backtests what the LIVE bot actually signals.

Usage: ``BACKTEST_DAYS=1095 python -m scripts.backtest_live``
Env: BACKTEST_DAYS, BACKTEST_SYMBOLS, BACKTEST_NO_PERSIST (1 = don't touch the
live learning brain).
"""
from __future__ import annotations

import asyncio
import json
import os

import pandas as pd

import numpy as np
import pandas as pd

from backend import (config, data_feed, database as db, indicators, learning,
                     phoenix, phoenix_backtester as phx, smc_backtester)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "data", "state.json")
OUT_PATH = os.path.join(ROOT, "docs", "data", "live_backtest.json")

LOOKBACK_DAYS = int(os.getenv("BACKTEST_DAYS", "180"))
_env_syms = os.getenv("BACKTEST_SYMBOLS", "").strip()
SYMBOLS = [s.strip().upper() for s in _env_syms.split(",") if s.strip()] or config.WATCHLIST
NO_PERSIST = os.getenv("BACKTEST_NO_PERSIST", "0") == "1"


def _dir_series(df, idx, invert=False):
    """Per-day NAIK/TURUN/STABIL from an EMA50 slope (deadband 0.5%), aligned to idx."""
    if df is None or df.empty:
        return pd.Series("STABIL", index=idx)
    ema = indicators.ema(df["close"], config.EMA_FAST)
    pct = (ema - ema.shift(3)) / ema.shift(3).abs()
    d = pd.Series("STABIL", index=ema.index)
    d[pct > 0.005] = "NAIK"
    d[pct < -0.005] = "TURUN"
    if invert:
        d = d.map({"NAIK": "TURUN", "TURUN": "NAIK", "STABIL": "STABIL"})
    return d.reindex(idx, method="ffill").fillna("STABIL")


async def _usdtd_timeline():
    """USDT.D 20-day range position over the backtest window (CoinGecko free)."""
    idx = pd.date_range(end=pd.Timestamp.utcnow().normalize(),
                        periods=LOOKBACK_DAYS + 60, freq="D", tz="UTC")
    if config.DEMO:
        vals = 0.5 + 0.35 * np.sin(np.linspace(0, 6.28 * 3, len(idx)))
        return pd.Series(vals, index=idx)
    try:
        h = await data_feed._client.get(
            config.COINGECKO_BASE + "/coins/tether/market_chart",
            params={"vs_currency": "usd", "days": "365", "interval": "daily"})
        if h.status_code == 200:
            caps = h.json().get("market_caps", [])
            if len(caps) > 25:
                s = pd.Series([c[1] for c in caps],
                              index=pd.to_datetime([c[0] for c in caps], unit="ms", utc=True))
                lo = s.rolling(config.USDTD_LOOKBACK, min_periods=5).min()
                hi = s.rolling(config.USDTD_LOOKBACK, min_periods=5).max()
                pos = ((s - lo) / (hi - lo).replace(0, np.nan)).clip(0, 1).fillna(0.5)
                pos.index = pos.index.normalize()
                pos = pos[~pos.index.duplicated(keep="last")]
                return pos.reindex(idx)
    except Exception as exc:
        print(f"[live] usdtd history failed: {exc}")
    return pd.Series(np.nan, index=idx)


def _machine_summary(trades, machine):
    return smc_backtester.summarize([t for t in trades if t.get("machine") == machine])


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)

    print(f"[live] lookback={LOOKBACK_DAYS}d, symbols={len(SYMBOLS)} "
          f"(LONG=Phoenix, SHORT=SMC)")
    usdtd_daily = await _usdtd_timeline()
    if usdtd_daily.empty:
        print("[live] no USDT.D data — aborting")
        return
    ethbtc = await data_feed.get_klines_history("ETHBTC", "1d", LOOKBACK_DAYS + 80)
    btcd_dir_daily = _dir_series(ethbtc, usdtd_daily.index, invert=True)
    # BTC-driven regime for the Phoenix long machine (BULL/BEAR/NEUTRAL per day)
    btc_daily = await data_feed.get_klines_history("BTCUSDT", "1d", LOOKBACK_DAYS + 90)
    regime_daily = phx.btc_regime_daily(btc_daily) if btc_daily is not None and not btc_daily.empty else None

    all_trades: list[dict] = []
    for sym in SYMBOLS:
        try:
            htf = await data_feed.get_klines_history(sym, config.HTF, LOOKBACK_DAYS)
            dtf = await data_feed.get_klines_history(sym, config.DTF, LOOKBACK_DAYS + 60)
            ltf = await data_feed.get_klines_history(sym, "1h", LOOKBACK_DAYS)
            if htf.empty or dtf.empty or ltf.empty:
                print(f"[live] {sym}: no data")
                continue
            # SHORT machine — classic SMC, longs disabled
            shorts = smc_backtester.backtest_symbol_smc(
                sym, htf, dtf, ltf, usdtd_daily, btcd_dir_daily,
                {"score_th": config.SMC_SCORE_TH, "allow_long": False, "allow_short": True})
            # LONG machine — Phoenix Hybrid (proven engine, regime-driven)
            longs = phoenix.backtest_symbol_long(
                sym, htf, dtf, ltf, usdtd_daily, btcd_dir_daily,
                {"regime_daily": regime_daily})
            all_trades.extend(shorts)
            all_trades.extend(longs)
            print(f"[live] {sym}: {len(longs)} long (Phoenix) + {len(shorts)} short (SMC)")
        except Exception as exc:
            print(f"[live] {sym} error: {exc}")

    summary = smc_backtester.summarize(all_trades)
    long_sum = _machine_summary(all_trades, "long")
    short_sum = _machine_summary(all_trades, "short")

    # ---- walk-forward OOS (learn on 70%, filter the unseen 30%) ----
    srt = sorted(all_trades, key=lambda x: x["exit_ts"])
    cut = int(len(srt) * 0.7)
    train, test = srt[:cut], srt[cut:]
    db.execute("DELETE FROM pattern_stats")
    db.execute("DELETE FROM lessons")
    for t in train:
        learning.record_outcome(t["features"], t["r"] > 0.05, t["r"])
    kept = [t for t in test if learning.evaluate(t["features"])["allowed"]
            and learning.evaluate(t["features"])["confidence"] >= config.CONFIDENCE_FLOOR]
    walkforward = {
        "cutoff_ts": test[0]["exit_ts"] if test else None,
        "test_all": smc_backtester.summarize(test),
        "test_filtered": smc_backtester.summarize(kept),
        "kept": len(kept), "test_n": len(test),
    }

    # ---- rebuild the learning brain from ALL trades (both machines) ----
    db.execute("DELETE FROM pattern_stats")
    db.execute("DELETE FROM lessons")
    for t in srt:
        learning.record_outcome(t["features"], t["r"] > 0.05, t["r"])
    blocked = [l for l in db.lessons(200) if l["kind"] == "BLOCK"]
    favored = [l for l in db.lessons(200) if l["kind"] == "FAVOR"]

    # engine split within the long (Phoenix) machine
    long_trades = [t for t in all_trades if t.get("machine") == "long"]
    engine_split = {}
    for eng in ("fib", "breakout"):
        sub = [t for t in long_trades if t.get("engine") == eng]
        if sub:
            engine_split[eng] = smc_backtester.summarize(sub)

    report = {
        "generated_ts": pd.Timestamp.utcnow().isoformat(),
        "params": {"lookback_days": LOOKBACK_DAYS, "htf": config.HTF, "ltf": "1h",
                   "symbols": len(SYMBOLS), "demo": config.DEMO,
                   "strategy": "phoenix-long + smc-short", "score_th": config.SMC_SCORE_TH},
        "summary": summary,
        "by_machine": {"long_phoenix": long_sum, "short_smc": short_sum},
        "by_long_engine": engine_split,
        "recent_trades": [
            {**{k: t.get(k) for k in ("symbol", "direction", "machine", "entry",
                                      "exit_price", "outcome", "r", "rr", "entry_ts", "exit_ts")},
             "engine": t.get("engine", "smc")}
            for t in sorted(all_trades, key=lambda x: x["exit_ts"], reverse=True)[:40]
        ],
        "learned": {"blocked_count": len(blocked), "favored_count": len(favored),
                    "lessons": db.lessons(40)},
        "walkforward": walkforward,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))
    if not NO_PERSIST:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(db.export_state(), f, ensure_ascii=False, indent=0)
    else:
        print("[live] NO_PERSIST — live learning state left untouched")

    wf = walkforward.get("test_filtered", {})
    s = summary
    print(f"[live] BLENDED: {s['trades']} trades, win {s['win_rate']}%, "
          f"PF {s['profit_factor']}, exp {s['expectancy_r']}R, maxDD {s['max_drawdown_r']}R")
    print(f"[live]   LONG  (Phoenix): {long_sum['trades']} trades, "
          f"win {long_sum['win_rate']}%, PF {long_sum['profit_factor']}, total {long_sum['total_r']}R")
    print(f"[live]   SHORT (SMC):     {short_sum['trades']} trades, "
          f"win {short_sum['win_rate']}%, PF {short_sum['profit_factor']}, total {short_sum['total_r']}R")
    print(f"[live]   OOS(walk-forward): PF {wf.get('profit_factor')} "
          f"win {wf.get('win_rate')}% trades {wf.get('trades')}")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
