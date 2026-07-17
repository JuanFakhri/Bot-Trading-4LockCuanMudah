"""News/macro-impact study (offline research): does US macro move crypto?

Tests the hypothesis "inflation falling / rate cut -> crypto up" empirically over
~3 years, using FREE historical data (no API key):
  * CPI (CPIAUCSL) and Core CPI (CPILFESL) — FRED free CSV
  * Fed Funds rate (FEDFUNDS) — FRED free CSV
  * BTC & ETH daily closes — Binance

For each macro "event" we look at BTC/ETH forward returns (1/3/7/14 days) after
the approximate release, then compare conditions (disinflation vs rising CPI,
rate cut vs hike/hold). Timing is approximate (FRED gives the reference month,
not the exact release timestamp) — this is a directional study, not intraday.

Writes docs/data/news_impact.json. NEVER touches the live bot.
Usage: BACKTEST_DAYS=1095 python -m scripts.news_impact
"""
from __future__ import annotations

import asyncio
import io
import json
import os

import numpy as np
import pandas as pd

from backend import config, data_feed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "data", "news_impact.json")
DAYS = int(os.getenv("BACKTEST_DAYS", "1095"))
HORIZONS = [1, 3, 7, 14]
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="


async def _fred(series: str) -> pd.Series:
    """Fetch a FRED series as a monthly Series (date -> value). Free, no key."""
    r = await data_feed._client.get(FRED + series, timeout=30.0)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna().set_index("date")["value"]


def _fwd_returns(px: pd.Series, event_dates: list[pd.Timestamp]) -> dict:
    """Mean BTC/ETH forward return (%) at each horizon after the event dates."""
    out = {}
    px = px.sort_index()
    for h in HORIZONS:
        rets = []
        for d in event_dates:
            # first close on/after the (approx) release date
            after = px[px.index >= d]
            if len(after) < h + 1:
                continue
            p0 = after.iloc[0]
            p1 = after.iloc[min(h, len(after) - 1)]
            if p0 > 0:
                rets.append((p1 / p0 - 1) * 100)
        out[f"{h}d"] = round(float(np.mean(rets)), 2) if rets else None
    out["n"] = len([d for d in event_dates])
    return out


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    start = pd.Timestamp.utcnow().normalize() - pd.Timedelta(days=DAYS)
    print(f"[news] window since {start.date()}")

    btc = await data_feed.get_klines_history("BTCUSDT", "1d", DAYS)
    eth = await data_feed.get_klines_history("ETHUSDT", "1d", DAYS)
    btc_c = btc["close"]; eth_c = eth["close"]
    btc_c.index = pd.to_datetime(btc_c.index, utc=True)
    eth_c.index = pd.to_datetime(eth_c.index, utc=True)

    cpi = await _fred("CPIAUCSL")        # headline CPI (index level, monthly)
    ff = await _fred("FEDFUNDS")         # effective fed funds rate (monthly)

    # CPI YoY per reference month, restricted to window
    cpi_yoy = (cpi / cpi.shift(12) - 1) * 100
    cpi_yoy = cpi_yoy.dropna()
    # approximate release date: reference month + ~1 month + 12 days
    def release(idx):
        return [d + pd.DateOffset(months=1, days=12) for d in idx]

    # --- disinflation (CPI YoY falling) vs rising ---
    ycur = cpi_yoy
    yprev = cpi_yoy.shift(1)
    fall_mask = (ycur < yprev) & (ycur.index >= start)
    rise_mask = (ycur > yprev) & (ycur.index >= start)
    ev_fall = release(ycur.index[fall_mask])
    ev_rise = release(ycur.index[rise_mask])

    # --- rate cut vs hike (FEDFUNDS change vs prior month) ---
    ff_chg = ff.diff()
    ff_ev = ff.index[ff.index >= start]
    cut = [d + pd.Timedelta(days=15) for d in ff_ev if ff_chg.get(d, 0) < -0.02]
    hike = [d + pd.Timedelta(days=15) for d in ff_ev if ff_chg.get(d, 0) > 0.02]

    report = {
        "generated_ts": pd.Timestamp.utcnow().isoformat(), "days": DAYS,
        "note": "Timing perkiraan (FRED = bulan referensi, bukan jam rilis). Studi arah, bukan intraday.",
        "cpi_disinflation": {"BTC": _fwd_returns(btc_c, ev_fall), "ETH": _fwd_returns(eth_c, ev_fall)},
        "cpi_rising":       {"BTC": _fwd_returns(btc_c, ev_rise), "ETH": _fwd_returns(eth_c, ev_rise)},
        "rate_cut":         {"BTC": _fwd_returns(btc_c, cut),     "ETH": _fwd_returns(eth_c, cut)},
        "rate_hike":        {"BTC": _fwd_returns(btc_c, hike),    "ETH": _fwd_returns(eth_c, hike)},
    }

    def show(name, d):
        b = d["BTC"]; e = d["ETH"]
        print(f"[news] {name:20} n={b['n']:2} | BTC " +
              " ".join(f"{h}={b[h]}" for h in ["1d","3d","7d","14d"]) +
              " | ETH " + " ".join(f"{h}={e[h]}" for h in ["1d","3d","7d","14d"]))
    show("CPI disinflation", report["cpi_disinflation"])
    show("CPI rising",       report["cpi_rising"])
    show("Rate CUT",         report["rate_cut"])
    show("Rate HIKE",        report["rate_hike"])

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[news] wrote {OUT_PATH}")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
