"""Research: are the ABCD / ABC swing patterns (from the user's reference images)
worth trading? Runs the pivot-based ABCD detector STANDALONE over real 1H history
for the live watchlist, with a walk-forward OOS split, and prints a comparison.

Compared against the LIVE edge we already have (for context):
  SHORT SMC (live): 60.4% win, PF 1.61 | blended 37-coin: 59.1% win, PF 1.48.

Read-only: writes nothing. Promote a variant ONLY if it clears the live edge
(win% and PF and positive OOS) — otherwise it is not worth adding.

Usage: RESEARCH_DAYS=1095 python -m scripts.research_abcd
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("BACKTEST_DAYS", os.getenv("RESEARCH_DAYS", "1095"))

from backend import config, data_feed, abcd_backtester as ab  # noqa: E402
from backend.smc_backtester import summarize                   # noqa: E402
from scripts.backtest_live import LOOKBACK_DAYS, SYMBOLS        # noqa: E402


def _oos(trades):
    srt = sorted(trades, key=lambda x: x["exit_ts"])
    cut = int(len(srt) * 0.7)
    return summarize(srt[cut:]), len(srt) - cut


VARIANTS = [
    ("A strict both",        {"mode": "strict"}),
    ("B strict SHORT-only",  {"mode": "strict", "allow_long": False}),
    ("C strict LONG-only",   {"mode": "strict", "allow_short": False}),
    ("D loose both",         {"mode": "loose"}),
    ("E loose SHORT-only",   {"mode": "loose", "allow_long": False}),
    ("F abc continuation",   {"mode": "abc"}),
    ("G strict + fib-TP",    {"mode": "strict", "fib_tp": True}),
    ("H strict big-legs",    {"mode": "strict", "min_leg_atr": 2.0}),
]


async def main():
    print(f"[abcd] lookback={LOOKBACK_DAYS}d symbols={len(SYMBOLS)}")
    data = {}
    for sym in SYMBOLS:
        try:
            ltf = await data_feed.get_klines_history(sym, "1h", LOOKBACK_DAYS)
            if not ltf.empty:
                data[sym] = ltf
        except Exception as exc:
            print(f"[abcd] {sym} load error: {exc}")
    print(f"[abcd] loaded {len(data)} symbols\n")

    rows = []
    for name, extra in VARIANTS:
        trades = []
        for sym, ltf in data.items():
            try:
                trades += ab.backtest_symbol_abcd(sym, ltf, extra)
            except Exception as exc:
                print(f"[abcd] {sym} {name} error: {exc}")
        s = summarize(trades)
        oos, oos_n = _oos(trades)
        rows.append((name, s, oos, oos_n))

    print("\n================= ABCD / ABC PATTERN SWEEP — STANDALONE (1095d) =================")
    print(f"{'variant':<22}{'trades':>7}{'win%':>7}{'PF':>6}{'totR':>8}{'DD':>7}"
          f" | {'OOSn':>5}{'OOSwin':>8}{'OOSpf':>7}{'OOStotR':>9}")
    print("-" * 88)
    for name, s, oos, oos_n in rows:
        print(f"{name:<22}{s['trades']:>7}{s['win_rate']:>7}{s['profit_factor']:>6}"
              f"{s['total_r']:>8}{s['max_drawdown_r']:>7} | {oos_n:>5}"
              f"{oos.get('win_rate', 0):>8}{oos.get('profit_factor', 0):>7}"
              f"{oos.get('total_r', 0):>9}")
    print("=" * 88)
    print("LIVE edge to beat: SHORT 60.4% / PF 1.61; blended 59.1% / PF 1.48. "
          "Promote only if a variant clears that AND OOS stays positive.")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
