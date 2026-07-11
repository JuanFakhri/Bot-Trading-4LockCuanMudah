"""Run a historical backtest of the SMC + AI-Score strategy and let the bot LEARN.

Steps:
  1. Build a USDT.D position timeline (20-day range) + BTC.D direction (ETH/BTC
     proxy) for the macro components of the AI Score.
  2. Backtest every watchlist symbol on the 1H entry timeframe with 1D/4H bias.
  3. REBUILD the learning brain from the backtest: every resolved trade is fed
     into ``learning`` so losing patterns get blocked and winners get favoured.
  4. Write the report to ``docs/data/backtest.json`` (shown in the web UI) and
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

from backend import (config, data_feed, database as db, indicators,
                     learning, market_filter, smc_backtester)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "data", "state.json")
OUT_PATH = os.path.join(ROOT, "docs", "data", "backtest.json")

LOOKBACK_DAYS = int(os.getenv("BACKTEST_DAYS", "180"))

# Optional symbol override (comma-separated, e.g. "ETHUSDT,SOLUSDT"). Empty = all.
_env_syms = os.getenv("BACKTEST_SYMBOLS", "").strip()
SYMBOLS = [s.strip().upper() for s in _env_syms.split(",") if s.strip()] or config.WATCHLIST

# SMC AI-Score gate (defaults to the live value).
SCORE_TH = float(os.getenv("BACKTEST_SCORE_TH", str(config.SMC_SCORE_TH)))


def _dir_series(df: pd.Series | None, idx: pd.Index, invert: bool = False) -> pd.Series:
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
                # CoinGecko free only covers ~365d; span the full backtest index
                # (NaN before that -> the older period falls back to the BTC.D matrix)
                pos.index = pos.index.normalize()
                pos = pos[~pos.index.duplicated(keep="last")]
                return pos.reindex(idx)
    except Exception as exc:
        print(f"[backtest] usdtd history failed: {exc}")
    return pd.Series(np.nan, index=idx)


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)

    print(f"[backtest] lookback={LOOKBACK_DAYS}d, symbols={len(SYMBOLS)}, score_th={SCORE_TH}")
    usdtd_daily = await _usdtd_timeline()
    if usdtd_daily.empty:
        print("[backtest] no USDT.D data — aborting")
        return

    # BTC.D direction timeline (ETH/BTC proxy) for the SMC macro score
    ethbtc = await data_feed.get_klines_history("ETHBTC", "1d", LOOKBACK_DAYS + 80)
    btcd_dir_daily = _dir_series(ethbtc, usdtd_daily.index, invert=True)

    all_trades: list[dict] = []
    for sym in SYMBOLS:
        try:
            htf = await data_feed.get_klines_history(sym, config.HTF, LOOKBACK_DAYS)
            dtf = await data_feed.get_klines_history(sym, config.DTF, LOOKBACK_DAYS + 60)
            ltf = await data_feed.get_klines_history(sym, "1h", LOOKBACK_DAYS)  # 1H trigger
            if htf.empty or dtf.empty:
                print(f"[backtest] {sym}: no data")
                continue
            trades = smc_backtester.backtest_symbol_smc(sym, htf, dtf, ltf, usdtd_daily,
                                                        btcd_dir_daily, {"score_th": SCORE_TH})
            all_trades.extend(trades)
            print(f"[backtest] {sym}: {len(trades)} trades")
        except Exception as exc:
            print(f"[backtest] {sym} error: {exc}")

    summary = smc_backtester.summarize(all_trades)

    # ---- WALK-FORWARD: learn on train, apply the self-learning filter to the
    # unseen test split. This measures what the LIVE bot would actually trade
    # (it refuses blocked / low-confidence patterns), out-of-sample. ----
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

    # ---- REBUILD the learning brain from ALL trades (for the live bot) ----
    db.execute("DELETE FROM pattern_stats")
    db.execute("DELETE FROM lessons")
    for t in srt:
        learning.record_outcome(t["features"], t["r"] > 0.05, t["r"])

    blocked = [l for l in db.lessons(200) if l["kind"] == "BLOCK"]
    favored = [l for l in db.lessons(200) if l["kind"] == "FAVOR"]

    report = {
        "generated_ts": pd.Timestamp.utcnow().isoformat(),
        "params": {"lookback_days": LOOKBACK_DAYS, "htf": config.HTF, "ltf": "1h",
                   "symbols": len(SYMBOLS), "demo": config.DEMO, "strategy": "smc",
                   "score_th": SCORE_TH},
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
        "walkforward": walkforward,
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
