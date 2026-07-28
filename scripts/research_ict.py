"""Research: how good is a PURE ICT strategy on its own?

This does NOT bolt anything onto our bot. It runs the canonical ICT model as a
STANDALONE strategy and measures ITS OWN win-rate, so we can judge whether ICT
is worth pursuing before comparing it to anything.

The ICT model tested (per bar, on 1H):
  1. Time inside the London/NY "kill zones" (or all-day, as a control)
  2. Price in the premium (for shorts) / discount (for longs) array
  3. Liquidity SWEEP of the prior swing (grab stops)
  4. Market-Structure-Shift (choch) — break of the last opposing swing
  5. FVG retrace entry
  SL beyond the swept swing +/-1 ATR (cap 6%); TP 1R/2R; risk 1%.

It deliberately ignores our composite Setup Score and the non-ICT confluences
(EMA/RSI/ADX/volume/ATR/fib/USDT.D) — that is the whole point of an isolated test.

NOT modeled (reported honestly, not silently dropped):
  * minute "macros" (3AM/9AM/9:30/11AM) — need <1H candles
  * SMT divergence — needs a correlated-pair feed (separate build)

Usage: RESEARCH_DAYS=1095 python -m scripts.research_ict
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("BACKTEST_DAYS", os.getenv("RESEARCH_DAYS", "1095"))

from backend import config, data_feed, smc_backtester  # noqa: E402
from scripts.backtest_live import (  # noqa: E402
    _usdtd_timeline, _dir_series, _cpi_dir_daily, LOOKBACK_DAYS, SYMBOLS)


def _oos(trades):
    srt = sorted(trades, key=lambda x: x["exit_ts"])
    cut = int(len(srt) * 0.7)
    return smc_backtester.summarize(srt[cut:]), len(srt) - cut


# kill-zone UTC hours (video used NY time / EDT = UTC-4):
#   London open 02-05 NY -> 06-08 UTC ; NY AM 07-10 NY -> 11-13 UTC ;
#   London close 10-12 NY -> 14-15 UTC
KZ_ALL = [6, 7, 8, 11, 12, 13, 14, 15]

# (label, extra params). Each is a fully standalone ICT config.
VARIANTS = [
    ("ICT short — all day",   {"ict_pure": True, "allow_short": True,  "allow_long": False,
                               "use_session": False}),
    ("ICT short — killzones", {"ict_pure": True, "allow_short": True,  "allow_long": False,
                               "use_session": True, "kz_hours": KZ_ALL}),
    ("ICT long  — all day",   {"ict_pure": True, "allow_short": False, "allow_long": True,
                               "use_session": False}),
    ("ICT long  — killzones", {"ict_pure": True, "allow_short": False, "allow_long": True,
                               "use_session": True, "kz_hours": KZ_ALL}),
]


async def main():
    print(f"[ict-research] lookback={LOOKBACK_DAYS}d symbols={len(SYMBOLS)}")
    usdtd = await _usdtd_timeline()
    if usdtd.empty:
        print("[ict-research] no USDT.D data — abort")
        return
    ethbtc = await data_feed.get_klines_history("ETHBTC", "1d", LOOKBACK_DAYS + 80)
    btcd = _dir_series(ethbtc, usdtd.index, invert=True)

    data = {}
    for sym in SYMBOLS:
        try:
            htf = await data_feed.get_klines_history(sym, config.HTF, LOOKBACK_DAYS)
            dtf = await data_feed.get_klines_history(sym, config.DTF, LOOKBACK_DAYS + 60)
            ltf = await data_feed.get_klines_history(sym, "1h", LOOKBACK_DAYS)
            if not (htf.empty or dtf.empty or ltf.empty):
                data[sym] = (htf, dtf, ltf)
        except Exception as exc:
            print(f"[ict-research] {sym} load error: {exc}")
    print(f"[ict-research] loaded {len(data)} symbols\n")

    rows = []
    for name, extra in VARIANTS:
        params = {"score_th": config.SMC_SCORE_TH}
        params.update(extra)
        trades = []
        for sym, (htf, dtf, ltf) in data.items():
            try:
                trades += smc_backtester.backtest_symbol_smc(
                    sym, htf, dtf, ltf, usdtd, btcd, params)
            except Exception as exc:
                print(f"[ict-research] {sym} {name} error: {exc}")
        s = smc_backtester.summarize(trades)
        oos, oos_n = _oos(trades)
        rows.append((name, s, oos, oos_n))

    print("\n=============== PURE ICT STRATEGY — STANDALONE (1095d) ===============")
    print(f"{'variant':<24}{'trades':>7}{'win%':>7}{'PF':>6}{'totR':>8}"
          f" | {'OOSn':>5}{'OOSwin':>8}{'OOSpf':>7}")
    print("-" * 74)
    for name, s, oos, oos_n in rows:
        print(f"{name:<24}{s['trades']:>7}{s['win_rate']:>7}{s['profit_factor']:>6}"
              f"{s['total_r']:>8} | {oos_n:>5}{oos.get('win_rate', 0):>8}"
              f"{oos.get('profit_factor', 0):>7}")
    print("=" * 74)
    print("This is ICT ALONE (no score, no EMA/RSI/ADX/vol/fib/USDT.D confluence).")
    print("NOT modeled: minute macros (need <1H candles), SMT divergence (separate).")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
