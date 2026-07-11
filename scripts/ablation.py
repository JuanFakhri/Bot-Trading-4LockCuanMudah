"""Ablation study: v1 baseline + each proposed improvement ONE AT A TIME.

Runs the whole matrix in a single job so we can see which single change raises
the edge over the validated v1 (PF 1.41 / OOS 1.90 / 69 trades, 730d). Writes
``docs/data/ablation.json`` and prints a ranked table.

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
BASE_TH = float(os.getenv("BASE_SCORE_TH", "60"))

# (label, flags-set, score_th) — each is v1 + exactly one change.
EXPERIMENTS = [
    ("v1 baseline",        set(),                60),
    ("#1 double_bos",      {"double_bos"},       60),
    ("#2 ema_slope",       {"ema_slope"},        60),
    ("#3 adx_rising",      {"adx_rising"},       60),
    ("#4 atr_expansion",   {"atr_exp"},          60),
    ("#5 volume_1.5x",     {"vol15"},            60),
    ("#6 score>=70",       set(),                70),
    ("#6 score>=80",       set(),                80),
    ("#7 rsi_zone",        {"rsi_zone"},         60),
    ("#8 macro_4confirm",  {"macro4"},           60),
    ("#9 eth_trend",       {"eth_trend"},        60),
    ("#10 candle_confirm", {"candle"},           60),
    ("#11 tight_session",  {"tight_session"},    60),
    ("#12 chandelier_exit", {"chandelier"},      60),
    ("#14 range_off",      {"range_off"},        60),
    ("#16 antifake_retest", {"antifake"},        60),
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
    """Out-of-sample: last 30% of trades by exit time (no learning filter)."""
    srt = sorted(trades, key=lambda t: t["exit_ts"])
    test = srt[int(len(srt) * 0.7):]
    return v1.summarize(test), len(test)


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    print(f"[abl] days={DAYS}, symbols={len(config.WATCHLIST)}")

    usdtd = await _usdtd_timeline(DAYS + 60)
    ethbtc = await data_feed.get_klines_history("ETHBTC", "1d", DAYS + 80)
    btc_d = await data_feed.get_klines_history("BTCUSDT", "1d", DAYS + 80)
    eth_d = await data_feed.get_klines_history("ETHUSDT", "1d", DAYS + 80)
    btcd_dir = _dir_series(ethbtc, usdtd.index, invert=True)
    eth_bull = None
    if eth_d is not None and not eth_d.empty:
        eb = (indicators.ema(eth_d["close"], config.EMA_FAST)
              > indicators.ema(eth_d["close"], config.EMA_SLOW)).astype(float)
        eb.index = eb.index.normalize()
        eth_bull = eb[~eb.index.duplicated(keep="last")]
    macro = {"btc_dir": _dir_series(btc_d, usdtd.index),
             "eth_dir": _dir_series(eth_d, usdtd.index), "eth_bull": eth_bull}

    # fetch per-symbol data once
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
        trades = []
        for sym, (htf, dtf, ltf) in data.items():
            trades += abl.backtest_symbol_abl(sym, htf, dtf, ltf, usdtd, btcd_dir,
                                              macro, flags, th)
        s = v1.summarize(trades)
        oos, oos_n = _oos(trades)
        results.append({
            "label": label, "flags": sorted(flags), "score_th": th,
            "trades": s["trades"], "win_rate": s["win_rate"],
            "profit_factor": s["profit_factor"], "expectancy_r": s["expectancy_r"],
            "total_r": s["total_r"], "max_drawdown_r": s["max_drawdown_r"],
            "oos_pf": oos["profit_factor"], "oos_win": oos["win_rate"], "oos_n": oos_n,
            "long_n": s["long"]["n"], "short_n": s["short"]["n"],
        })
        print(f"[abl] {label:22} trades={s['trades']:3} win={s['win_rate']:5}% "
              f"PF={s['profit_factor']:4} exp={s['expectancy_r']:+.3f}R "
              f"DD={s['max_drawdown_r']:6}R | OOS PF={oos['profit_factor']:4} n={oos_n}")

    base = next(r for r in results if r["label"] == "v1 baseline")
    for r in results:
        r["d_pf"] = round(r["profit_factor"] - base["profit_factor"], 2)
        r["helps"] = bool(r["profit_factor"] >= base["profit_factor"]
                          and r["oos_pf"] >= base["oos_pf"] and r["trades"] >= 20)

    report = {
        "generated_ts": pd.Timestamp.utcnow().isoformat(),
        "days": DAYS, "baseline": base,
        "results": sorted(results, key=lambda x: -x["profit_factor"]),
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[abl] wrote {OUT_PATH}")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
