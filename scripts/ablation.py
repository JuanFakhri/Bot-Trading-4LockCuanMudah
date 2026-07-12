"""Ablation (round 2): live v1.1 + each candidate indicator, one at a time.

Runs baseline + {stochastic, rsi, stoch_rsi, support/resistance} over the same
real data, reports trades/win/PF/expectancy/DD + out-of-sample PF, writes
docs/data/ablation.json and prints a ranked table.

Usage: ``BACKTEST_DAYS=730 python -m scripts.ablation``
"""
from __future__ import annotations

import asyncio
import json
import os

import numpy as np
import pandas as pd

from backend import (config, data_feed, indicators, smc_backtester as v1,
                     smc_ablation as abl)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "data", "ablation.json")
DAYS = int(os.getenv("BACKTEST_DAYS", "730"))

EXPERIMENTS = [
    ("v1.1 baseline",         set(),            60),
    ("#1 stochastic",         {"stoch"},        60),
    ("#2 rsi",                {"rsi"},          60),
    ("#3 stoch_rsi",          {"stochrsi"},     60),
    ("#4 support_resistance", {"sr"},           60),
    ("combo all-4",           {"stoch", "rsi", "stochrsi", "sr"}, 60),
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
        print(f"[abl] usdtd failed: {exc}")
    return pd.Series(np.nan, index=idx)


def _oos(trades):
    srt = sorted(trades, key=lambda t: t["exit_ts"])
    return v1.summarize(srt[int(len(srt) * 0.7):])


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    print(f"[abl] days={DAYS}, symbols={len(config.WATCHLIST)}")
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
    print(f"[abl] loaded {len(data)} symbols")

    results = []
    for label, flags, th in EXPERIMENTS:
        trades, diag = [], {}
        for sym, (htf, dtf, ltf) in data.items():
            trades += abl.backtest_symbol_abl(sym, htf, dtf, ltf, usdtd, btcd_dir, flags, th, diag)
        s = v1.summarize(trades); oos = _oos(trades)
        results.append({
            "label": label, "flags": sorted(flags), "trades": s["trades"],
            "win_rate": s["win_rate"], "profit_factor": s["profit_factor"],
            "expectancy_r": s["expectancy_r"], "max_drawdown_r": s["max_drawdown_r"],
            "oos_pf": oos["profit_factor"], "oos_n": oos["trades"],
            "long_n": s["long"]["n"], "short_n": s["short"]["n"], "rejected": diag,
        })
        print(f"[abl] {label:22} trades={s['trades']:3} win={s['win_rate']:5}% "
              f"PF={s['profit_factor']:5} exp={s['expectancy_r']:+.3f}R DD={s['max_drawdown_r']:6}R "
              f"| OOS PF={oos['profit_factor']:5} n={oos['trades']:2} | rej={diag}")

    base = next(r for r in results if r["label"] == "v1.1 baseline")
    for r in results:
        r["d_pf"] = round(r["profit_factor"] - base["profit_factor"], 2)
        r["helps"] = bool(r["profit_factor"] >= base["profit_factor"]
                          and r["oos_pf"] >= base["oos_pf"] and r["trades"] >= 20)

    report = {"generated_ts": pd.Timestamp.utcnow().isoformat(), "days": DAYS,
              "baseline": base, "results": sorted(results, key=lambda x: -x["profit_factor"])}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[abl] wrote {OUT_PATH}")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
