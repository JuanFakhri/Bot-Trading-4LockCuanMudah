"""Research: can the SHORT (SMC) machine be LOOSENED to trade MORE without
losing win-rate? Runs the short machine STANDALONE (Phoenix long excluded) over
the same real-data window across several relaxation variants, with a walk-forward
OOS split, and prints a comparison. Read-only: never writes the live learning
brain or the dashboard JSON — so it is safe to run against the live branch.

Usage: RESEARCH_DAYS=1095 python -m scripts.research_short
"""
from __future__ import annotations

import asyncio
import os

# _usdtd_timeline / _cpi_dir_daily in backtest_live read BACKTEST_DAYS at import.
os.environ.setdefault("BACKTEST_DAYS", os.getenv("RESEARCH_DAYS", "1095"))

from backend import config, data_feed, smc_backtester  # noqa: E402
from scripts.backtest_live import (  # noqa: E402
    _usdtd_timeline, _dir_series, _cpi_dir_daily, LOOKBACK_DAYS, SYMBOLS)


def _macro_filter(trades, cpi_map, on):
    """Live macro/CPI gate: drop SHORTs whose entry-day CPI bias is BULLISH
    (disinflation) — same post-filter used by backtest_live / the live router."""
    if not on:
        return trades
    return [t for t in trades if not (
        t["direction"] == "SHORT" and cpi_map.get(t["entry_ts"][:10]) == "BULLISH")]


def _oos(trades):
    """Walk-forward OOS = the last 30% of trades (chronological), same split as
    backtest_live. Measures the edge on unseen tail data."""
    srt = sorted(trades, key=lambda x: x["exit_ts"])
    cut = int(len(srt) * 0.7)
    test = srt[cut:]
    return smc_backtester.summarize(test), len(test)


# (label, extra params over the live short config, macro-gate on?)
VARIANTS = [
    ("S0 BASE (live)",         {"short_align": "triple"},                        True),
    ("S1 score_th 55",         {"short_align": "triple", "score_th": 55},        True),
    ("S2 score_th 50",         {"short_align": "triple", "score_th": 50},        True),
    ("S3 dual 1D+4H (drop1H)", {"short_align": "dual_dh4"},                      True),
    ("S4 dual 4H+1H (drop1D)", {"short_align": "dual_h4h1"},                     True),
    ("S5 session OFF",         {"short_align": "triple", "use_session": False},  True),
    ("S6 vol filter OFF",      {"short_align": "triple", "short_vol_mult": 0.0}, True),
    ("S7 macro gate OFF",      {"short_align": "triple"},                        False),
    ("S8 55 + dual_dh4",       {"short_align": "dual_dh4", "score_th": 55},      True),
    ("S9 55 + dual + no-macro", {"short_align": "dual_dh4", "score_th": 55},     False),
]


async def main():
    print(f"[short-research] lookback={LOOKBACK_DAYS}d symbols={len(SYMBOLS)}")
    usdtd = await _usdtd_timeline()
    if usdtd.empty:
        print("[short-research] no USDT.D data — abort")
        return
    ethbtc = await data_feed.get_klines_history("ETHBTC", "1d", LOOKBACK_DAYS + 80)
    btcd = _dir_series(ethbtc, usdtd.index, invert=True)
    cpi = await _cpi_dir_daily(usdtd.index)
    cpi_map = {d.strftime("%Y-%m-%d"): v for d, v in cpi.items()}

    # preload klines once per symbol (reused across every variant)
    data = {}
    for sym in SYMBOLS:
        try:
            htf = await data_feed.get_klines_history(sym, config.HTF, LOOKBACK_DAYS)
            dtf = await data_feed.get_klines_history(sym, config.DTF, LOOKBACK_DAYS + 60)
            ltf = await data_feed.get_klines_history(sym, "1h", LOOKBACK_DAYS)
            if not (htf.empty or dtf.empty or ltf.empty):
                data[sym] = (htf, dtf, ltf)
        except Exception as exc:
            print(f"[short-research] {sym} load error: {exc}")
    print(f"[short-research] loaded {len(data)} symbols\n")

    rows = []
    for name, extra, macro_on in VARIANTS:
        params = {"allow_long": False, "allow_short": True,
                  "score_th": config.SMC_SCORE_TH}
        params.update(extra)
        trades = []
        for sym, (htf, dtf, ltf) in data.items():
            try:
                trades += smc_backtester.backtest_symbol_smc(
                    sym, htf, dtf, ltf, usdtd, btcd, params)
            except Exception as exc:
                print(f"[short-research] {sym} {name} error: {exc}")
        trades = _macro_filter(trades, cpi_map, macro_on)
        s = smc_backtester.summarize(trades)
        oos, oos_n = _oos(trades)
        rows.append((name, s, oos, oos_n))

    print("\n============== SHORT MACHINE — RELAXATION SWEEP (1095d) ==============")
    print(f"{'variant':<26}{'trades':>7}{'win%':>7}{'PF':>6}{'totR':>8}"
          f" | {'OOSn':>5}{'OOSwin':>8}{'OOSpf':>7}")
    print("-" * 74)
    for name, s, oos, oos_n in rows:
        print(f"{name:<26}{s['trades']:>7}{s['win_rate']:>7}{s['profit_factor']:>6}"
              f"{s['total_r']:>8} | {oos_n:>5}{oos.get('win_rate', 0):>8}"
              f"{oos.get('profit_factor', 0):>7}")
    print("=" * 74)
    print("Keep a variant only if it beats S0 on trades AND does not drop OOS win/PF.")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
