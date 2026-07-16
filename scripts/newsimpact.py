"""Macro-news impact study (offline research): does disinflation / rate cuts help BTC?

The user asked whether high-impact macro news (CPI/inflation down, rate cuts) is
good or bad for crypto, over ~3 years. ForexFactory has no free multi-year
history, so we use FRED (free, no API key) for the actual macro data and Binance
for BTC, then measure BTC's reaction.

Data (FRED CSV, no key):
  CPIAUCSL  — CPI (headline)         -> YoY inflation
  CPILFESL  — Core CPI               -> YoY core inflation
  FEDFUNDS  — effective Fed Funds    -> rate cut / hike / hold

Method (monthly):
  - inflation "cooling" = CPI YoY lower than the prior month's YoY.
  - rate action = MoM change in Fed Funds (cut < -0.05, hike > +0.05, else hold).
  - a macro print for month M is released in M+1, so we align to BTC's return in
    the FOLLOWING month (the reaction window).
  - report mean BTC next-month return conditioned on each macro state.

Honest limits: only ~36 monthly prints in 3y (tiny sample), monthly granularity
(not event-timestamped), correlation != causation. Writes docs/data/newsimpact.json.
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
OUT_PATH = os.path.join(ROOT, "docs", "data", "newsimpact.json")
DAYS = int(os.getenv("BACKTEST_DAYS", "1095"))
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="


async def _fred(series: str) -> pd.Series:
    """Fetch a FRED series as a monthly-indexed float Series (no API key)."""
    r = await data_feed._client.get(FRED + series, timeout=20.0)
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", "val"]
    df["date"] = pd.to_datetime(df["date"])
    df["val"] = pd.to_numeric(df["val"], errors="coerce")
    s = df.dropna().set_index("date")["val"]
    s.index = s.index.to_period("M")
    return s


def _stats(x: pd.Series) -> dict:
    x = x.dropna()
    return {"n": int(len(x)), "mean_pct": round(float(x.mean()) * 100, 2),
            "median_pct": round(float(x.median()) * 100, 2),
            "win_rate": round(float((x > 0).mean()) * 100, 1) if len(x) else 0.0}


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    print(f"[news] window ~{DAYS}d")

    cpi = await _fred("CPIAUCSL")
    core = await _fred("CPILFESL")
    ff = await _fred("FEDFUNDS")

    # BTC monthly returns from Binance daily closes
    btc = await data_feed.get_klines_history("BTCUSDT", "1d", DAYS + 60)
    if btc.empty:
        print("[news] no BTC data — aborting")
        return
    bm = btc["close"].resample("ME").last()
    bm.index = bm.index.to_period("M")
    btc_ret = bm.pct_change()                      # return during month M
    btc_next = btc_ret.shift(-1)                    # return in month M+1 (reaction)

    # unify everything onto ONE monthly PeriodIndex so booleans align safely
    M = pd.DataFrame({"cpi": cpi, "core": core, "ff": ff}).sort_index()
    M["btc_ret"] = btc_ret
    M["btc_next"] = btc_next
    M["cpi_yoy"] = M["cpi"].pct_change(12)
    M["core_yoy"] = M["core"].pct_change(12)

    infl_cooling = M["cpi_yoy"].diff() < 0         # YoY falling = disinflation
    core_cooling = M["core_yoy"].diff() < 0
    ff_chg = M["ff"].diff()
    cut = ff_chg < -0.05
    hike = ff_chg > 0.05
    hold = (~cut) & (~hike)

    # restrict to the study window (last ~DAYS)
    start = pd.Timestamp.utcnow().to_period("M") - int(DAYS / 30)
    mask = pd.Series(M.index >= start, index=M.index)

    def cond(flag):
        idx = M.index[flag.fillna(False) & mask]
        return _stats(M["btc_next"].reindex(idx))

    out = {
        "generated_ts": pd.Timestamp.utcnow().isoformat(),
        "window_months": int(DAYS / 30),
        "note": "BTC return in the month AFTER the macro print (reaction window).",
        "inflation": {
            "cooling (CPI YoY turun)": cond(infl_cooling),
            "rising  (CPI YoY naik)": cond(~infl_cooling),
            "core_cooling": cond(core_cooling),
            "core_rising": cond(~core_cooling),
        },
        "rates": {
            "cut (Fed Funds turun)": cond(cut),
            "hold": cond(hold),
            "hike (Fed Funds naik)": cond(hike),
        },
        "baseline_all_months": _stats(M["btc_next"][mask]),
    }

    def verdict():
        c = out["inflation"]["cooling (CPI YoY turun)"]; r = out["inflation"]["rising  (CPI YoY naik)"]
        cutv = out["rates"]["cut (Fed Funds turun)"]
        parts = []
        if c["n"] >= 5 and r["n"] >= 5:
            parts.append("Inflasi TURUN -> BTC +{:.1f}% vs inflasi NAIK {:+.1f}% (bulan berikutnya)".format(c["mean_pct"], r["mean_pct"]))
        if cutv["n"] >= 3:
            parts.append("Suku bunga DIPOTONG -> BTC {:+.1f}% (n={})".format(cutv["mean_pct"], cutv["n"]))
        return " | ".join(parts) or "Sampel terlalu kecil untuk simpulan."
    out["verdict"] = verdict()

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("[news] inflation:", out["inflation"])
    print("[news] rates:", out["rates"])
    print("[news] baseline:", out["baseline_all_months"])
    print("[news] verdict:", out["verdict"])
    print(f"[news] wrote {OUT_PATH}")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
