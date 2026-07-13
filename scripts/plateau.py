"""Plateau / curve-fit test — sweep each core parameter around its live value.

For each of the 4 core "4-Lock" knobs, re-run the 730-day backtest at values
around the current setting (others held fixed) and compare win rate / profit
factor / total R. A flat neighbourhood = plateau = real edge; a lone spike =
curve-fit. Writes docs/data/plateau.json and prints a table.

Mapping (SMC implementation -> user's components):
  score_th   = Structure confluence threshold (AI Score gate)
  ema_fast   = HTF-LTF momentum window (EMA fast period)
  pivot_len  = LTF trigger sensitivity (swing/BOS detection window)
  rsi_len    = momentum window (RSI length)

OFFLINE ONLY — never touches the live bot.
"""
from __future__ import annotations

import asyncio
import json
import os

import numpy as np
import pandas as pd

from backend import config, data_feed, indicators, smc_backtester as bt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "data", "plateau.json")
DAYS = int(os.getenv("BACKTEST_DAYS", "730"))

# (component label, config attribute, is-score-param, [values], current)
SWEEPS = [
    ("Structure confluence (score_th)", "SMC_SCORE_TH", True,  [50, 55, 60, 65, 70], 60),
    ("Momentum window (EMA fast)",       "EMA_FAST",     False, [40, 45, 50, 55, 60], 50),
    ("LTF trigger (pivot len)",          "PIVOT_LEN",    False, [3, 4, 5, 6, 7],       5),
    ("Momentum (RSI len)",               "RSI_LEN",      False, [10, 12, 14, 16, 18], 14),
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
        print(f"[plateau] usdtd failed: {exc}")
    return pd.Series(np.nan, index=idx)


def _run(data, usdtd, btcd_dir, score_th):
    trades = []
    for sym, (htf, dtf, ltf) in data.items():
        trades += bt.backtest_symbol_smc(sym, htf, dtf, ltf, usdtd, btcd_dir, {"score_th": score_th})
    return bt.summarize(trades)


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    print(f"[plateau] days={DAYS}, symbols={len(config.WATCHLIST)}")
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
    print(f"[plateau] loaded {len(data)} symbols")

    report = {"generated_ts": pd.Timestamp.utcnow().isoformat(), "days": DAYS, "sweeps": []}
    for label, attr, is_score, values, current in SWEEPS:
        rows = []
        saved = getattr(config, attr)
        for val in values:
            if is_score:
                score_th = float(val)
            else:
                setattr(config, attr, val)
                score_th = config.SMC_SCORE_TH
            s = _run(data, usdtd, btcd_dir, score_th)
            rows.append({"value": val, "is_current": val == current, "trades": s["trades"],
                         "win_rate": s["win_rate"], "profit_factor": s["profit_factor"],
                         "total_r": s["total_r"], "max_drawdown_r": s["max_drawdown_r"]})
            print(f"[plateau] {label:34} {attr}={val:<4} trades={s['trades']:3} "
                  f"win={s['win_rate']:5} PF={s['profit_factor']:5} totalR={s['total_r']:+.2f}")
        setattr(config, attr, saved)   # restore
        # plateau metric: coefficient of variation of PF across the neighbourhood
        pfs = [r["profit_factor"] for r in rows]
        mean = sum(pfs) / len(pfs)
        std = (sum((x - mean) ** 2 for x in pfs) / len(pfs)) ** 0.5
        cv = round(std / mean, 3) if mean else None
        report["sweeps"].append({"label": label, "attr": attr, "current": current,
                                 "rows": rows, "pf_cv": cv})
        print(f"[plateau] -> {label}: PF spread CV={cv} (kecil = plateau/kokoh)")

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[plateau] wrote {OUT_PATH}")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
