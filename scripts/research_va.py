"""Research: do the user's proposed NEW backtestable modules help the SHORT (SMC)
machine? Tests Value-Area confluence (Volume Profile / Leviathan port: entry near
prior-session VAH for shorts) as a score bonus and as a hard gate, plus skip-Friday.
Walk-forward OOS split. Baseline reproduces the LIVE short (score_th 50, macro gate).

NOT tested (cannot be backtested): the Liquidity-Momentum Gauge (order book /depth +
aggTrades) — Binance provides no historical order book, so it is a live-only filter.

Read-only. Keep a variant ONLY if it holds/raises win% AND does not hurt OOS.
Usage: RESEARCH_DAYS=1095 python -m scripts.research_va
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("BACKTEST_DAYS", os.getenv("RESEARCH_DAYS", "1095"))

from backend import config, data_feed, smc_backtester  # noqa: E402
from scripts.backtest_live import (  # noqa: E402
    _usdtd_timeline, _dir_series, _cpi_dir_daily, LOOKBACK_DAYS, SYMBOLS)

BASE_TH = config.SMC_SHORT_SCORE_TH


def _macro_filter(trades, cpi_map):
    return [t for t in trades if not (
        t["direction"] == "SHORT" and cpi_map.get(t["entry_ts"][:10]) == "BULLISH")]


def _oos(trades):
    srt = sorted(trades, key=lambda x: x["exit_ts"])
    cut = int(len(srt) * 0.7)
    return smc_backtester.summarize(srt[cut:]), len(srt) - cut


VARIANTS = [
    ("S0 BASE (live short)",  {}),
    ("VA bonus +10",          {"va_bonus": 10}),
    ("VA bonus +15",          {"va_bonus": 15}),
    ("VA hard 0.5ATR",        {"va_hard": True, "va_tol_atr": 0.5}),
    ("VA hard 1.0ATR",        {"va_hard": True, "va_tol_atr": 1.0}),
    ("skip Friday",           {"skip_friday": True}),
    ("VA+10 + skipFri",       {"va_bonus": 10, "skip_friday": True}),
]


async def main():
    print(f"[va] lookback={LOOKBACK_DAYS}d symbols={len(SYMBOLS)} base_th={BASE_TH}")
    usdtd = await _usdtd_timeline()
    if usdtd.empty:
        print("[va] no USDT.D — abort"); return
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
            print(f"[va] {sym} load error: {exc}")
    print(f"[va] loaded {len(data)} symbols\n")

    rows = []
    for name, extra in VARIANTS:
        params = {"allow_long": False, "allow_short": True, "score_th": BASE_TH}
        params.update(extra)
        trades = []
        for sym, (htf, dtf, ltf) in data.items():
            try:
                trades += smc_backtester.backtest_symbol_smc(sym, htf, dtf, ltf, usdtd, btcd, params)
            except Exception as exc:
                print(f"[va] {sym} {name} error: {exc}")
        trades = _macro_filter(trades, cpi_map)
        s = smc_backtester.summarize(trades)
        oos, oos_n = _oos(trades)
        rows.append((name, s, oos, oos_n))

    base = rows[0][1]
    print("\n=========== VALUE-AREA / SKIP-FRIDAY SWEEP — SHORT (1095d) ===========")
    print(f"{'variant':<22}{'trades':>7}{'win%':>7}{'PF':>6}{'totR':>8}{'DD':>7}"
          f" | {'OOSn':>5}{'OOSwin':>8}{'OOSpf':>7}{'OOStotR':>9}")
    print("-" * 88)
    for name, s, oos, oos_n in rows:
        print(f"{name:<22}{s['trades']:>7}{s['win_rate']:>7}{s['profit_factor']:>6}"
              f"{s['total_r']:>8}{s['max_drawdown_r']:>7} | {oos_n:>5}"
              f"{oos.get('win_rate', 0):>8}{oos.get('profit_factor', 0):>7}{oos.get('total_r', 0):>9}")
    print("=" * 88)
    print(f"BASE: {base['trades']} tr, win {base['win_rate']}%, PF {base['profit_factor']}, "
          f"{base['total_r']}R. Promote only if win% holds/rises AND OOS not hurt.")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
