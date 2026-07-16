"""3-year macro-news event study — does high-impact data actually move crypto?

This backtest answers the trader's question with numbers, WITHOUT touching the
live trade engine:

    "Kalau CPI turun / inflasi turun / suku bunga dipotong, historisnya crypto
     naik atau tidak?"

Method
------
1. Build ~3 years of high-impact US macro *releases* with an approximate release
   date and the print vs the prior print:
     * Inflation      — CPI & Core CPI month-over-month rate (FRED CPIAUCSL /
                        CPILFESL). Cooler = bullish crypto.
     * Policy rate     — effective Fed funds rate (FEDFUNDS). Cut = bullish.
     * Unemployment    — UNRATE. Higher = dovish = bullish.
     * Payrolls        — non-farm payroll MoM change (PAYEMS). Hotter = bearish.
     * PPI             — producer prices (PPIACO). Cooler = bullish.
   Data source is FRED's free CSV endpoint (no API key). Offline / demo mode
   synthesises a plausible macro + price world so the pipeline is explorable.

2. Score every release with ``backend.macro_news`` → a signed crypto bias.

3. Event study on BTC daily prices (Binance history, demo-synthetic offline):
   for each release measure the forward return at +1d / +3d / +7d, grouped by
   the predicted bias (RISK_ON vs RISK_OFF). Report hit-rate (did the sign of
   the prediction match the sign of the move) and average forward return.

4. A toy strategy: hold BTC for H days after a RISK_ON print, stay flat after a
   RISK_OFF print — reported as a cumulative equity curve so the edge (if any)
   is visible. This is a *screen*, not a live signal.

Output → ``docs/data/news_backtest.json`` (rendered in the Makro tab).

Usage
-----
  BOT_DEMO=1 python -m scripts.backtest_news          # offline synthetic
  python -m scripts.backtest_news                     # real (FRED + Binance)
"""
from __future__ import annotations

import asyncio
import io
import json
import os
from datetime import timedelta

import numpy as np
import pandas as pd

from backend import config, data_feed, macro_news

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "data", "news_backtest.json")

YEARS = float(os.getenv("NEWS_BACKTEST_YEARS", "3"))
DAYS = int(YEARS * 365)
HOLD_DAYS = int(os.getenv("NEWS_HOLD_DAYS", "3"))   # strategy hold window
HORIZONS = (1, 3, 7)                                # forward-return horizons

# FRED series → (macro_news title used for classification, release-day offset
# from the reference month's first day, "level" vs "mom_rate" transform).
#   * mom_rate: convert an index into its month-over-month % change (inflation).
#   * level:    use the value as-is (rates, unemployment).
#   * mom_chg:  month-over-month change in the level (payrolls, in thousands).
FRED_SERIES = {
    "CPIAUCSL": ("CPI m/m", 43, "mom_rate"),        # released ~13th next month
    "CPILFESL": ("Core CPI m/m", 43, "mom_rate"),
    "PPIACO":   ("PPI m/m", 44, "mom_rate"),
    "FEDFUNDS": ("Federal Funds Rate", 32, "level"),
    "UNRATE":   ("Unemployment Rate", 37, "level"),  # released 1st Fri next month
    "PAYEMS":   ("Non-Farm Employment Change", 37, "mom_chg"),
}


# --------------------------------------------------------------------------
# Data acquisition
# --------------------------------------------------------------------------
async def _fetch_fred(series_id: str) -> pd.Series | None:
    """Monthly FRED series via the free CSV endpoint (no API key)."""
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    try:
        r = await data_feed._client.get(url, params={"id": series_id})
        if r.status_code != 200:
            print(f"[news-bt] FRED {series_id} HTTP {r.status_code}")
            return None
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = ["date", "value"]
        df["date"] = pd.to_datetime(df["date"], utc=True)
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        s = df.dropna().set_index("date")["value"]
        return s
    except Exception as exc:
        print(f"[news-bt] FRED {series_id} failed: {exc}")
        return None


def _synth_macro_events(start: pd.Timestamp, end: pd.Timestamp, rng) -> list[dict]:
    """Synthetic monthly macro releases with a slow inflation/rate cycle so the
    demo shows a realistic mix of RISK_ON / RISK_OFF prints."""
    months = pd.date_range(start=start, end=end, freq="MS", tz="UTC")
    events: list[dict] = []
    # a slow easing/tightening cycle over the window
    infl = 6.0
    rate = 5.0
    for i, m in enumerate(months):
        phase = np.sin(i / 6.0)
        infl = max(1.5, infl + (-0.25 + 0.15 * phase) + rng.normal(0, 0.2))
        rate = max(0.25, rate + (0.1 * phase) + rng.normal(0, 0.1))
        unemp = 3.8 + 0.6 * (-phase) + rng.normal(0, 0.1)
        nfp = 180 - 60 * phase + rng.normal(0, 40)
        rel = m + timedelta(days=13)
        events.append({"ts": rel, "title": "CPI m/m", "actual": round(infl / 12, 2),
                       "previous": round((infl + 0.3) / 12, 2)})
        events.append({"ts": rel, "title": "Federal Funds Rate", "actual": round(rate, 2),
                       "previous": round(rate + 0.1 * phase, 2)})
        events.append({"ts": m + timedelta(days=5), "title": "Unemployment Rate",
                       "actual": round(unemp, 1), "previous": round(unemp - 0.1 * phase, 1)})
        events.append({"ts": m + timedelta(days=5), "title": "Non-Farm Employment Change",
                       "actual": round(nfp), "previous": round(nfp + 20 * phase)})
    return events


def _transform(series_id: str, s: pd.Series, kind: str) -> pd.Series:
    if kind == "mom_rate":
        return (s.pct_change() * 100).dropna()
    if kind == "mom_chg":
        return s.diff().dropna()
    return s


async def _build_events(start: pd.Timestamp, end: pd.Timestamp, rng) -> list[dict]:
    """Return [{ts, title, actual, previous}] over the window (real or synthetic)."""
    if config.DEMO:
        print("[news-bt] DEMO — synthesising macro events")
        return _synth_macro_events(start, end, rng)

    events: list[dict] = []
    for sid, (title, offset_days, kind) in FRED_SERIES.items():
        raw = await _fetch_fred(sid)
        if raw is None or raw.empty:
            continue
        transformed = _transform(sid, raw, kind)
        prev = None
        for ref_date, val in transformed.items():
            rel = ref_date + timedelta(days=offset_days)
            if rel < start or rel > end:
                prev = val
                continue
            events.append({"ts": pd.Timestamp(rel).tz_convert("UTC"),
                           "title": title,
                           "actual": float(val),
                           "previous": float(prev) if prev is not None else None})
            prev = val
        print(f"[news-bt] {sid}: {sum(1 for e in events if e['title'] == title)} releases")
    events.sort(key=lambda e: e["ts"])
    return events


async def _btc_daily(rng) -> pd.Series:
    """BTC daily close over the window (Binance history; synthetic in demo)."""
    df = await data_feed.get_klines_history("BTCUSDT", "1d", DAYS + 30)
    if df is not None and not df.empty:
        return df["close"]
    return pd.Series(dtype=float)


# --------------------------------------------------------------------------
# Event study
# --------------------------------------------------------------------------
def _fwd_return(prices: pd.Series, when: pd.Timestamp, horizon: int) -> float | None:
    """Return over ``horizon`` calendar days starting at the first price on/after
    ``when``."""
    idx = prices.index
    pos = idx.searchsorted(when)
    if pos >= len(idx):
        return None
    p0 = prices.iloc[pos]
    tgt = idx[pos] + pd.Timedelta(days=horizon)
    pos2 = idx.searchsorted(tgt)
    if pos2 >= len(idx):
        pos2 = len(idx) - 1
    if pos2 <= pos:
        return None
    p1 = prices.iloc[pos2]
    if p0 <= 0:
        return None
    return float(p1 / p0 - 1.0)


def _group_stats(rows: list[dict], horizon: int) -> dict:
    key = f"fwd_{horizon}d"
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return {"n": 0, "avg_ret": 0.0, "median_ret": 0.0, "win_rate": 0.0}
    arr = np.array(vals)
    return {
        "n": len(vals),
        "avg_ret": round(float(arr.mean()) * 100, 3),      # %
        "median_ret": round(float(np.median(arr)) * 100, 3),
        "win_rate": round(float((arr > 0).mean()) * 100, 1),
    }


def _hit_rate(rows: list[dict], horizon: int) -> dict:
    """Fraction of directional prints where sign(score) == sign(forward return)."""
    key = f"fwd_{horizon}d"
    hits = tot = 0
    for r in rows:
        if r["score"] == 0 or r.get(key) is None:
            continue
        tot += 1
        if (r["score"] > 0) == (r[key] > 0):
            hits += 1
    return {"n": tot, "hit_rate": round(hits / tot * 100, 1) if tot else 0.0}


def _strategy_curve(daily_bias: pd.Series, prices: pd.Series, hold: int) -> dict:
    """Long BTC for ``hold`` days after RISK_ON, flat after RISK_OFF; buy&hold
    benchmark for comparison. Returns cumulative % curves + summary."""
    idx = prices.index
    ret = prices.pct_change().fillna(0.0)
    # target exposure per day: +1 during a RISK_ON hold window, else 0
    exposure = pd.Series(0.0, index=idx)
    for ts, bias in daily_bias.items():
        if bias <= 0:
            continue
        pos = idx.searchsorted(ts)
        for j in range(pos, min(pos + hold, len(idx))):
            exposure.iloc[j] = 1.0
    strat_ret = (exposure.shift(1).fillna(0.0) * ret)
    strat_eq = (1 + strat_ret).cumprod()
    bh_eq = (1 + ret).cumprod()

    # sample the curve to keep the JSON small (~150 points)
    step = max(1, len(idx) // 150)
    curve = [{"ts": idx[i].isoformat(),
              "strat": round(float(strat_eq.iloc[i] - 1) * 100, 2),
              "bh": round(float(bh_eq.iloc[i] - 1) * 100, 2)}
             for i in range(0, len(idx), step)]
    days_in = float((exposure > 0).mean())
    return {
        "curve": curve,
        "strat_return_pct": round(float(strat_eq.iloc[-1] - 1) * 100, 2),
        "buyhold_return_pct": round(float(bh_eq.iloc[-1] - 1) * 100, 2),
        "exposure_pct": round(days_in * 100, 1),
        "hold_days": hold,
    }


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    rng = np.random.default_rng(20260716)

    end = pd.Timestamp.utcnow().normalize()
    start = end - pd.Timedelta(days=DAYS)
    print(f"[news-bt] window {start.date()} … {end.date()} ({YEARS}y), hold={HOLD_DAYS}d, demo={config.DEMO}")

    prices = await _btc_daily(rng)
    if prices.empty:
        print("[news-bt] no BTC price data — aborting")
        await data_feed.close()
        return
    prices = prices[prices.index >= start]

    events = await _build_events(start, end, rng)
    if not events:
        print("[news-bt] no macro events — aborting")
        await data_feed.close()
        return

    # ---- score every event + attach forward returns ----
    rows: list[dict] = []
    for ev in events:
        a = macro_news.assess_event(ev["title"], actual=ev.get("actual"),
                                    previous=ev.get("previous"))
        if not a.matched:
            continue
        row = {"ts": pd.Timestamp(ev["ts"]).isoformat(), "title": ev["title"],
               "type": a.type_label, "bias": a.bias, "verdict": a.verdict,
               "score": round(a.score, 3), "actual": ev.get("actual"),
               "previous": ev.get("previous"), "reason": a.reason}
        for h in HORIZONS:
            row[f"fwd_{h}d"] = _fwd_return(prices, pd.Timestamp(ev["ts"]), h)
        rows.append(row)

    risk_on = [r for r in rows if r["bias"] == "RISK_ON"]
    risk_off = [r for r in rows if r["bias"] == "RISK_OFF"]

    by_horizon = {}
    for h in HORIZONS:
        by_horizon[f"{h}d"] = {
            "risk_on": _group_stats(risk_on, h),
            "risk_off": _group_stats(risk_off, h),
            "all_hit": _hit_rate(rows, h),
        }

    # per event-type breakdown (which release actually predicts crypto best)
    by_type: dict[str, dict] = {}
    for r in rows:
        b = by_type.setdefault(r["type"], {"rows": []})
        b["rows"].append(r)
    type_summary = {}
    for t, b in by_type.items():
        type_summary[t] = {
            "n": len(b["rows"]),
            "hit_3d": _hit_rate(b["rows"], 3)["hit_rate"],
            "avg_ret_3d": _group_stats(b["rows"], 3)["avg_ret"],
        }

    # ---- daily aggregated bias → strategy curve ----
    day_scores: dict[pd.Timestamp, float] = {}
    for r in rows:
        d = pd.Timestamp(r["ts"]).normalize()
        day_scores[d] = day_scores.get(d, 0.0) + r["score"]
    daily_bias = pd.Series(day_scores).sort_index()
    daily_bias.index = daily_bias.index.tz_convert("UTC") if daily_bias.index.tz else daily_bias.index.tz_localize("UTC")
    strategy = _strategy_curve(daily_bias.reindex(daily_bias.index), prices, HOLD_DAYS)

    report = {
        "generated_ts": pd.Timestamp.utcnow().isoformat(),
        "params": {"years": YEARS, "days": DAYS, "hold_days": HOLD_DAYS,
                   "horizons": list(HORIZONS), "demo": config.DEMO,
                   "source": "synthetic" if config.DEMO else "FRED + Binance",
                   "n_events": len(rows), "n_risk_on": len(risk_on),
                   "n_risk_off": len(risk_off),
                   "window": {"start": start.isoformat(), "end": end.isoformat()}},
        "by_horizon": by_horizon,
        "by_type": type_summary,
        "strategy": strategy,
        "recent_events": sorted(rows, key=lambda r: r["ts"], reverse=True)[:60],
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))

    h3 = by_horizon["3d"]
    print(f"[news-bt] DONE: {len(rows)} events "
          f"(RISK_ON {len(risk_on)}, RISK_OFF {len(risk_off)}) | "
          f"3d hit-rate {h3['all_hit']['hit_rate']}% | "
          f"RISK_ON avg +{h3['risk_on']['avg_ret']}% vs RISK_OFF {h3['risk_off']['avg_ret']}% | "
          f"strategy {strategy['strat_return_pct']}% vs buy&hold {strategy['buyhold_return_pct']}%")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
