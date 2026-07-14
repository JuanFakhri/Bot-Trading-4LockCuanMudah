"""Variant test (offline research): short-only and drop-ETH/DOGE over 3 years.

Runs the current v1.1 strategy in a few configurations on the same real data so
we can see which improves the edge before touching live:
  1. Baseline (all symbols, both directions)
  2. Short-only (all symbols)
  3. Drop ETH+DOGE (both directions)
  4. Short-only + drop ETH+DOGE

Writes docs/data/variants.json and prints a table. Live untouched.
Usage: BACKTEST_DAYS=1095 python -m scripts.variants
"""
from __future__ import annotations

import asyncio
import json
import os

import numpy as np
import pandas as pd

from backend import config, data_feed, indicators, smc_backtester as bt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "data", "variants.json")
DAYS = int(os.getenv("BACKTEST_DAYS", "1095"))
DROP = {"ETHUSDT", "DOGEUSDT"}
KEEP = [s for s in config.WATCHLIST if s not in DROP]

# (label, symbol list, allow_long, allow_short)
VARIANTS = [
    ("Baseline (semua, 2 arah)",        config.WATCHLIST, True,  True),
    ("Short-only (semua)",              config.WATCHLIST, False, True),
    ("Drop ETH+DOGE (2 arah)",          KEEP,             True,  True),
    ("Short-only + drop ETH+DOGE",      KEEP,             False, True),
]


def _dir_series(df, idx, invert=False):
    if df is None or df.empty:
        return pd.Series("STABIL", index=idx)
    ema = indicators.ema(df["close"], config.EMA_FAST)
    pct = (ema - ema.shift(3)) / ema.shift(3).abs()
    d = pd.Series("STABIL", index=ema.index)
    d[pct > 0.005] = "NAIK"; d[pct < -0.005] = "TURUN"
    if invert:
        d = d.map({"NAIK": "TURUN", "TURUN": "NAIK", "STABIL": "STABIL"})
    return d.reindex(idx, method="ffill").fillna("STABIL")


async def _usdtd_timeline(idx_len):
    idx = pd.date_range(end=pd.Timestamp.utcnow().normalize(), periods=idx_len, freq="D", tz="UTC")
    if config.DEMO:
        return pd.Series(0.5 + 0.35 * np.sin(np.linspace(0, 18.8, len(idx))), index=idx)
    try:
        h = await data_feed._client.get(
            config.COINGECKO_BASE + "/coins/tether/market_chart",
            params={"vs_currency": "usd", "days": "365", "interval": "daily"})
        if h.status_code == 200:
            caps = h.json().get("market_caps", [])
            if len(caps) > 25:
                s = pd.Series([x[1] for x in caps],
                              index=pd.to_datetime([x[0] for x in caps], unit="ms", utc=True))
                lo = s.rolling(config.USDTD_LOOKBACK, min_periods=5).min()
                hi = s.rolling(config.USDTD_LOOKBACK, min_periods=5).max()
                pos = ((s - lo) / (hi - lo).replace(0, np.nan)).clip(0, 1).fillna(0.5)
                pos.index = pos.index.normalize()
                return pos[~pos.index.duplicated(keep="last")].reindex(idx)
    except Exception as exc:
        print(f"[var] usdtd failed: {exc}")
    return pd.Series(np.nan, index=idx)


def _oos(trades):
    srt = sorted(trades, key=lambda t: t["exit_ts"])
    return bt.summarize(srt[int(len(srt) * 0.7):])


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    print(f"[var] days={DAYS}")
    usdtd = await _usdtd_timeline(DAYS + 60)
    ethbtc = await data_feed.get_klines_history("ETHBTC", "1d", DAYS + 80)
    btcd_dir = _dir_series(ethbtc, usdtd.index, invert=True)

    data = {}
    for sym in config.WATCHLIST:
        htf = await data_feed.get_klines_history(sym, config.HTF, DAYS)
        dtf = await data_feed.get_klines_history(sym, config.DTF, DAYS + 60)
        ltf = await data_feed.get_klines_history(sym, "1h", DAYS)
        if not htf.empty and not dtf.empty and ltf is not None and not ltf.empty:
            data[sym] = (htf, dtf, ltf)
    print(f"[var] loaded {len(data)} symbols")

    results = []
    for label, syms, al, ash in VARIANTS:
        trades = []
        for sym in syms:
            if sym not in data:
                continue
            htf, dtf, ltf = data[sym]
            trades += bt.backtest_symbol_smc(sym, htf, dtf, ltf, usdtd, btcd_dir,
                                             {"score_th": 60, "allow_long": al, "allow_short": ash})
        s = bt.summarize(trades); oos = _oos(trades)
        results.append({"label": label, "trades": s["trades"], "win_rate": s["win_rate"],
                        "profit_factor": s["profit_factor"], "expectancy_r": s["expectancy_r"],
                        "total_r": s["total_r"], "max_drawdown_r": s["max_drawdown_r"],
                        "oos_pf": oos["profit_factor"], "oos_win": oos["win_rate"], "oos_n": oos["trades"],
                        "long_n": s["long"]["n"], "short_n": s["short"]["n"]})
        print(f"[var] {label:30} trades={s['trades']:3} win={s['win_rate']:5} PF={s['profit_factor']:5} "
              f"totalR={s['total_r']:+.2f} DD={s['max_drawdown_r']:6} | OOS PF={oos['profit_factor']:5} n={oos['trades']}")

    report = {"generated_ts": pd.Timestamp.utcnow().isoformat(), "days": DAYS, "results": results}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[var] wrote {OUT_PATH}")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
