"""Research: do BTC.D / USDT.D / macro-CPI hard entry-gates improve the live bot?

The live bot = Phoenix long (BULL) + SMC short. These extra gates would BLOCK an
entry when the dominance / macro backdrop fights the trade:
  * USDT.D gate  — block LONG when USDT.D rising (risk-off); block SHORT when
                   USDT.D falling (risk-on).   [USDT.D history ~1y only]
  * BTC.D gate   — block LONG when BTC.D rising; block SHORT when BTC.D falling.
  * Macro gate   — block LONG when CPI bias BEARISH (inflation rising); block
                   SHORT when BULLISH (inflation falling).   [FRED, 3y]

A gate is an entry filter, so we measure it by POST-FILTERING trades (drop the
ones whose entry day violates the gate) — equivalent to a hard gate for win-rate
/ PF / OOS. Tested one-by-one and all combined, over 3y with a 70/30 split.

Research only — writes docs/data/gates_research.json, never touches live.
Usage: BACKTEST_DAYS=1095 python -m scripts.research_gates
"""
from __future__ import annotations

import asyncio
import io
import json
import os

import numpy as np
import pandas as pd

from backend import config, data_feed, phoenix, phoenix_backtester as phx, smc_backtester
from scripts.backtest_live import _usdtd_timeline, _dir_series, LOOKBACK_DAYS, SYMBOLS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "data", "gates_research.json")

VARIANTS = [
    ("baseline (tanpa gate)", set()),
    ("USDT.D gate", {"usdtd"}),
    ("BTC.D gate", {"btcd"}),
    ("Macro/CPI gate", {"macro"}),
    ("GABUNGAN (semua gate)", {"usdtd", "btcd", "macro"}),
]


def _oos(trades, frac=0.7):
    srt = sorted(trades, key=lambda t: t["exit_ts"])
    cut = int(len(srt) * frac)
    return phx._stats(srt[cut:])


async def _cpi_dir_daily(idx):
    """Monthly CPI YoY direction (BULLISH=disinflation / BEARISH=rising), lagged
    ~45d for the release, forward-filled to a daily index."""
    if config.DEMO:
        return pd.Series("NETRAL", index=idx)
    try:
        r = await data_feed._client.get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL", timeout=30.0)
        df = pd.read_csv(io.StringIO(r.text)); df.columns = ["date", "value"]
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna().set_index("date").sort_index()
        yoy = df["value"] / df["value"].shift(12) - 1
        delta = yoy.diff()
        d = pd.Series("NETRAL", index=yoy.index)
        d[delta < -0.0005] = "BULLISH"
        d[delta > 0.0005] = "BEARISH"
        d.index = d.index + pd.Timedelta(days=45)          # release lag (no lookahead)
        d.index = d.index.tz_localize("UTC")
        return d.reindex(idx, method="ffill").fillna("NETRAL")
    except Exception as exc:
        print(f"[gates] cpi failed: {exc}")
        return pd.Series("NETRAL", index=idx)


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    print(f"[gates] {LOOKBACK_DAYS}d, {len(SYMBOLS)} symbols")

    idx = pd.date_range(end=pd.Timestamp.utcnow().normalize(),
                        periods=LOOKBACK_DAYS + 60, freq="D", tz="UTC")
    btc_daily = await data_feed.get_klines_history("BTCUSDT", "1d", LOOKBACK_DAYS + 90)
    regime_daily = phx.btc_regime_daily(btc_daily)
    ethbtc = await data_feed.get_klines_history("ETHBTC", "1d", LOOKBACK_DAYS + 80)
    btcd_daily = _dir_series(ethbtc, idx, invert=True)                 # NAIK/TURUN/STABIL
    usdtd_pos = await _usdtd_timeline()                                # ~1y position (0-1)
    usdtd_rising = (usdtd_pos > usdtd_pos.shift(5))                    # bool (NaN pre-history)
    cpi_daily = await _cpi_dir_daily(idx)

    # daily lookups keyed by YYYY-MM-DD
    btcd_map = {d.strftime("%Y-%m-%d"): v for d, v in btcd_daily.items()}
    cpi_map = {d.strftime("%Y-%m-%d"): v for d, v in cpi_daily.items()}
    ur_map = {d.strftime("%Y-%m-%d"): (None if pd.isna(v) else bool(v))
              for d, v in usdtd_rising.items()}

    # ---- generate the live trades (Phoenix long + SMC short) ----
    trades = []
    for sym in SYMBOLS:
        try:
            htf = await data_feed.get_klines_history(sym, config.HTF, LOOKBACK_DAYS)
            dtf = await data_feed.get_klines_history(sym, config.DTF, LOOKBACK_DAYS + 60)
            ltf = await data_feed.get_klines_history(sym, "1h", LOOKBACK_DAYS)
            if htf.empty or dtf.empty or ltf.empty:
                continue
            trades += smc_backtester.backtest_symbol_smc(
                sym, htf, dtf, ltf, usdtd_pos, btcd_daily,
                {"score_th": config.SMC_SCORE_TH, "allow_long": False, "allow_short": True})
            trades += phoenix.backtest_symbol_long(
                sym, htf, dtf, ltf, usdtd_pos, btcd_daily, {"regime_daily": regime_daily})
        except Exception as exc:
            print(f"[gates] {sym} error: {exc}")
    print(f"[gates] {len(trades)} base trades")

    # USDT.D coverage over the trade set
    cov = sum(1 for t in trades if ur_map.get(t["entry_ts"][:10]) is not None)
    print(f"[gates] USDT.D coverage: {cov}/{len(trades)} trades ({round(100*cov/max(1,len(trades)))}%)")

    def blocked(t, gate):
        long = t["direction"] == "LONG"
        day = t["entry_ts"][:10]
        if "usdtd" in gate:
            ur = ur_map.get(day)
            if ur is not None:
                if long and ur: return True
                if (not long) and (not ur): return True
        if "btcd" in gate:
            bd = btcd_map.get(day, "STABIL")
            if long and bd == "NAIK": return True
            if (not long) and bd == "TURUN": return True
        if "macro" in gate:
            cb = cpi_map.get(day, "NETRAL")
            if long and cb == "BEARISH": return True
            if (not long) and cb == "BULLISH": return True
        return False

    results = []
    for name, gate in VARIANTS:
        kept = [t for t in trades if not blocked(t, gate)]
        overall = phx._stats(kept)
        te = _oos(kept)
        results.append({"name": name, "gate": sorted(gate), "overall": overall, "oos_test": te})
        print(f"[gates] {name:24s} | kept {overall['trades']:>4}/{len(trades)} | "
              f"PF {overall['profit_factor']:<4} win {overall['win_rate']:<4}% totR {overall['total_r']:<8} | "
              f"OOS PF {te['profit_factor']:<4} win {te['win_rate']}%")

    report = {"generated_ts": pd.Timestamp.utcnow().isoformat(),
              "params": {"lookback_days": LOOKBACK_DAYS, "symbols": len(SYMBOLS),
                         "base_trades": len(trades), "usdtd_coverage_pct": round(100 * cov / max(1, len(trades)))},
              "results": results}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))
    print("[gates] done")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
