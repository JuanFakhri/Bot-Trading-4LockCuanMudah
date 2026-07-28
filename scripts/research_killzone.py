"""Research: does an ICT "Kill Zones" time filter beat our current 07-22 UTC
session window on the SHORT (SMC) machine?

The tested method (from a trading video) says entries should only happen inside
the high-liquidity kill zones. Converted to UTC (video used New York time, EDT
= UTC-4):
  * London Open kill zone : 02-05 NY  -> ~06-09 UTC
  * New York AM kill zone  : 07-10 NY  -> ~11-14 UTC
  * London Close kill zone : 10-12 NY  -> ~14-16 UTC

NOT testable on 1H data / out of scope here (reported honestly, not silently
dropped): the minute "macros" (3AM/9AM/9:30/11AM windows) need sub-hour candles,
and SMT divergence needs a correlated-pair feed — a separate build.

Runs the short machine STANDALONE (Phoenix long excluded) over the same real-data
window across the time-window variants, with a walk-forward OOS split. Read-only:
never writes the learning brain or dashboard JSON — safe on the live branch.

Usage: RESEARCH_DAYS=1095 python -m scripts.research_killzone
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("BACKTEST_DAYS", os.getenv("RESEARCH_DAYS", "1095"))

from backend import config, data_feed, smc_backtester  # noqa: E402
from scripts.backtest_live import (  # noqa: E402
    _usdtd_timeline, _dir_series, _cpi_dir_daily, LOOKBACK_DAYS, SYMBOLS)


def _macro_filter(trades, cpi_map, on):
    if not on:
        return trades
    return [t for t in trades if not (
        t["direction"] == "SHORT" and cpi_map.get(t["entry_ts"][:10]) == "BULLISH")]


def _oos(trades):
    srt = sorted(trades, key=lambda x: x["exit_ts"])
    cut = int(len(srt) * 0.7)
    return smc_backtester.summarize(srt[cut:]), len(srt) - cut


# Kill-zone UTC hour sets (see module docstring).
KZ_LONDON_OPEN = [6, 7, 8]
KZ_NY_AM = [11, 12, 13]
KZ_LONDON_CLOSE = [14, 15]
KZ_ALL = sorted(set(KZ_LONDON_OPEN + KZ_NY_AM + KZ_LONDON_CLOSE))       # 6-8,11-15
KZ_NY_ONLY = KZ_NY_AM + KZ_LONDON_CLOSE                                  # 11-15
KZ_OPENS = KZ_LONDON_OPEN + KZ_NY_AM                                     # 6-8,11-13

# (label, extra params over the live short config, macro-gate on?)
VARIANTS = [
    ("S0 BASE 07-22 UTC (live)", {},                          True),
    ("K1 kill zones (all 3)",    {"kz_hours": KZ_ALL},        True),
    ("K2 NY AM + Lon close",     {"kz_hours": KZ_NY_ONLY},    True),
    ("K3 Lon-open + NY-AM",      {"kz_hours": KZ_OPENS},      True),
    ("K4 NY AM only",            {"kz_hours": KZ_NY_AM},      True),
    ("K5 Lon open only",         {"kz_hours": KZ_LONDON_OPEN},True),
    ("K6 kill zones, no-macro",  {"kz_hours": KZ_ALL},        False),
]


async def main():
    print(f"[kz-research] lookback={LOOKBACK_DAYS}d symbols={len(SYMBOLS)}")
    usdtd = await _usdtd_timeline()
    if usdtd.empty:
        print("[kz-research] no USDT.D data — abort")
        return
    ethbtc = await data_feed.get_klines_history("ETHBTC", "1d", LOOKBACK_DAYS + 80)
    btcd = _dir_series(ethbtc, usdtd.index, invert=True)
    cpi = await _cpi_dir_daily(usdtd.index)
    cpi_map = {d.strftime("%Y-%m-%d"): v for d, v in cpi.items()}

    data = {}
    for sym in SYMBOLS:
        try:
            htf = await data_feed.get_klines_history(sym, config.HTF, LOOKBACK_DAYS)
            dtf = await data_feed.get_klines_history(sym, config.DTF, LOOKBACK_DAYS + 60)
            ltf = await data_feed.get_klines_history(sym, "1h", LOOKBACK_DAYS)
            if not (htf.empty or dtf.empty or ltf.empty):
                data[sym] = (htf, dtf, ltf)
        except Exception as exc:
            print(f"[kz-research] {sym} load error: {exc}")
    print(f"[kz-research] loaded {len(data)} symbols\n")

    rows = []
    for name, extra, macro_on in VARIANTS:
        params = {"allow_long": False, "allow_short": True,
                  "short_align": "triple", "score_th": config.SMC_SCORE_TH,
                  "use_session": True}
        params.update(extra)
        trades = []
        for sym, (htf, dtf, ltf) in data.items():
            try:
                trades += smc_backtester.backtest_symbol_smc(
                    sym, htf, dtf, ltf, usdtd, btcd, params)
            except Exception as exc:
                print(f"[kz-research] {sym} {name} error: {exc}")
        trades = _macro_filter(trades, cpi_map, macro_on)
        s = smc_backtester.summarize(trades)
        oos, oos_n = _oos(trades)
        rows.append((name, s, oos, oos_n))

    print("\n========== SHORT MACHINE — ICT KILL-ZONE TIME FILTER (1095d) ==========")
    print(f"{'variant':<27}{'trades':>7}{'win%':>7}{'PF':>6}{'totR':>8}"
          f" | {'OOSn':>5}{'OOSwin':>8}{'OOSpf':>7}")
    print("-" * 76)
    for name, s, oos, oos_n in rows:
        print(f"{name:<27}{s['trades']:>7}{s['win_rate']:>7}{s['profit_factor']:>6}"
              f"{s['total_r']:>8} | {oos_n:>5}{oos.get('win_rate', 0):>8}"
              f"{oos.get('profit_factor', 0):>7}")
    print("=" * 76)
    print("Keep kill zones only if win% holds/rises AND OOS win/PF not hurt vs S0.")
    print("NOTE: minute macros (3AM/9AM/9:30/11AM) need <1H candles — untested here.")
    print("      SMT divergence needs a correlated-pair feed — separate build.")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
