"""Research: do the HIGH-PROBABILITY confluence filters requested by the user
improve the SHORT (SMC) machine? Tests, one at a time and in combos, over the
real-data window with a walk-forward OOS split:

  - MACD (12/26/9) momentum confirm      (use_macd / macd_as_score)
  - EMA20 fast-trend gate                (use_ema20_gate)
  - RSI as a hard gate                   (rsi_hard)
  - Breakout + RETEST entry              (use_retest)
  - "High probability only" = raise the Setup-Score threshold

Baseline S0 reproduces the LIVE short exactly (triple-TF align, score_th=50,
macro/CPI gate on). Read-only: never writes the learning brain or dashboard JSON,
so it is safe to run against the live branch. Keep a variant ONLY if it holds or
raises win-rate AND does not wreck OOS / total-R for the trades it drops.

Usage: RESEARCH_DAYS=1095 python -m scripts.research_criteria
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("BACKTEST_DAYS", os.getenv("RESEARCH_DAYS", "1095"))

from backend import config, data_feed, smc_backtester  # noqa: E402
from scripts.backtest_live import (  # noqa: E402
    _usdtd_timeline, _dir_series, _cpi_dir_daily, LOOKBACK_DAYS, SYMBOLS)

BASE_TH = config.SMC_SHORT_SCORE_TH   # live short threshold (50)


def _macro_filter(trades, cpi_map):
    """Live macro/CPI gate: drop SHORTs whose entry-day CPI bias is BULLISH."""
    return [t for t in trades if not (
        t["direction"] == "SHORT" and cpi_map.get(t["entry_ts"][:10]) == "BULLISH")]


def _oos(trades):
    srt = sorted(trades, key=lambda x: x["exit_ts"])
    cut = int(len(srt) * 0.7)
    return smc_backtester.summarize(srt[cut:]), len(srt) - cut


# (label, extra params over the live short config). All layer on the SAME base.
VARIANTS = [
    ("S0 BASE (live short)",   {}),
    # --- single filters ---
    ("A MACD hard-gate",       {"use_macd": True}),
    ("B MACD as score(+10)",   {"macd_as_score": True}),
    ("C EMA20 gate",           {"use_ema20_gate": True}),
    ("D RSI hard-gate",        {"rsi_hard": True}),
    ("E Retest entry",         {"use_retest": True}),
    # --- "high probability only": raise the score bar ---
    ("F score_th 60",          {"score_th": 60}),
    ("G score_th 65",          {"score_th": 65}),
    ("H score_th 70",          {"score_th": 70}),
    # --- promising combos ---
    ("I MACD + EMA20",         {"use_macd": True, "use_ema20_gate": True}),
    ("J MACD + RSI",           {"use_macd": True, "rsi_hard": True}),
    ("K MACD-score + th60",    {"macd_as_score": True, "score_th": 60}),
    ("L MACD + EMA20 + th60",  {"use_macd": True, "use_ema20_gate": True, "score_th": 60}),
    ("M all-gates HP",         {"use_macd": True, "use_ema20_gate": True,
                                "rsi_hard": True}),
]


async def main():
    print(f"[criteria] lookback={LOOKBACK_DAYS}d symbols={len(SYMBOLS)} base_th={BASE_TH}")
    usdtd = await _usdtd_timeline()
    if usdtd.empty:
        print("[criteria] no USDT.D data — abort")
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
            print(f"[criteria] {sym} load error: {exc}")
    print(f"[criteria] loaded {len(data)} symbols\n")

    rows = []
    for name, extra in VARIANTS:
        params = {"allow_long": False, "allow_short": True, "score_th": BASE_TH}
        params.update(extra)
        trades = []
        for sym, (htf, dtf, ltf) in data.items():
            try:
                trades += smc_backtester.backtest_symbol_smc(
                    sym, htf, dtf, ltf, usdtd, btcd, params)
            except Exception as exc:
                print(f"[criteria] {sym} {name} error: {exc}")
        trades = _macro_filter(trades, cpi_map)
        s = smc_backtester.summarize(trades)
        oos, oos_n = _oos(trades)
        rows.append((name, s, oos, oos_n))

    base = rows[0][1]
    print("\n============ HIGH-PROBABILITY CRITERIA SWEEP — SHORT (1095d) ============")
    print(f"{'variant':<24}{'trades':>7}{'win%':>7}{'PF':>6}{'totR':>8}{'DD':>7}"
          f" | {'OOSn':>5}{'OOSwin':>8}{'OOSpf':>7}{'OOStotR':>9}")
    print("-" * 90)
    for name, s, oos, oos_n in rows:
        print(f"{name:<24}{s['trades']:>7}{s['win_rate']:>7}{s['profit_factor']:>6}"
              f"{s['total_r']:>8}{s['max_drawdown_r']:>7} | {oos_n:>5}"
              f"{oos.get('win_rate', 0):>8}{oos.get('profit_factor', 0):>7}"
              f"{oos.get('total_r', 0):>9}")
    print("=" * 90)
    print(f"BASE: {base['trades']} tr, win {base['win_rate']}%, PF {base['profit_factor']}, "
          f"{base['total_r']}R.  Promote only if win% holds/rises AND OOS win/PF not hurt.")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
