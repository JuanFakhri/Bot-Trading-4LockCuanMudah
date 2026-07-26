"""Research: does REMOVING the "BTC.D + arah BTC" market-filter component (the
btcd score, 5 pts) improve the SHORT (SMC) machine? A/B over real history with a
walk-forward OOS split. Also tests removing USDT.D for comparison. Baseline
reproduces the LIVE short (score_th 50, macro gate).

Read-only. Keep a change ONLY if it holds/raises win% AND does not hurt OOS.
Usage: RESEARCH_DAYS=1095 python -m scripts.research_btcd
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
    ("HAPUS BTC.D",           {"drop_btcd": True}),
    ("HAPUS USDT.D",          {"drop_usdtd": True}),
    ("HAPUS BTC.D + USDT.D",  {"drop_btcd": True, "drop_usdtd": True}),
]


async def main():
    print(f"[btcd] lookback={LOOKBACK_DAYS}d symbols={len(SYMBOLS)} base_th={BASE_TH}")
    usdtd = await _usdtd_timeline()
    if usdtd.empty:
        print("[btcd] no USDT.D — abort"); return
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
            print(f"[btcd] {sym} load error: {exc}")
    print(f"[btcd] loaded {len(data)} symbols\n")

    rows = []
    for name, extra in VARIANTS:
        params = {"allow_long": False, "allow_short": True, "score_th": BASE_TH}
        params.update(extra)
        trades = []
        for sym, (htf, dtf, ltf) in data.items():
            try:
                trades += smc_backtester.backtest_symbol_smc(sym, htf, dtf, ltf, usdtd, btcd, params)
            except Exception as exc:
                print(f"[btcd] {sym} {name} error: {exc}")
        trades = _macro_filter(trades, cpi_map)
        s = smc_backtester.summarize(trades)
        oos, oos_n = _oos(trades)
        rows.append((name, s, oos, oos_n))

    base = rows[0][1]
    print("\n=========== HAPUS KOMPONEN FILTER (BTC.D / USDT.D) — SHORT (1095d) ===========")
    print(f"{'variant':<24}{'trades':>7}{'win%':>7}{'PF':>6}{'totR':>8}{'DD':>7}"
          f" | {'OOSn':>5}{'OOSwin':>8}{'OOSpf':>7}{'OOStotR':>9}")
    print("-" * 90)
    for name, s, oos, oos_n in rows:
        print(f"{name:<24}{s['trades']:>7}{s['win_rate']:>7}{s['profit_factor']:>6}"
              f"{s['total_r']:>8}{s['max_drawdown_r']:>7} | {oos_n:>5}"
              f"{oos.get('win_rate', 0):>8}{oos.get('profit_factor', 0):>7}{oos.get('total_r', 0):>9}")
    print("=" * 90)
    print(f"BASE: {base['trades']} tr, win {base['win_rate']}%, PF {base['profit_factor']}, "
          f"{base['total_r']}R. Promosikan hanya jika win% naik/tetap DAN OOS tak turun.")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
