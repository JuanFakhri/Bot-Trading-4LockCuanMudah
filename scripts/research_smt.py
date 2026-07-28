"""Research: does ICT "SMT divergence" improve the LIVE short machine?

SMT (Smart Money Technique) divergence compares a coin to a correlated reference
(BTC; ETH for BTC itself). When the two DISAGREE on making a fresh higher-high
(bearish SMT) or lower-low (bullish SMT), smart money is likely distributing /
accumulating — a reversal tell. Here it is added as a CONFIRMATION filter on top
of the live short config (triple-TF bearish + score + vol/ATR/ADX + macro gate):
a SHORT is only taken when bearish SMT is present.

Runs standalone (Phoenix long excluded), read-only, with a walk-forward OOS
split — same harness as research_short. Compares:
  S0  live short (no SMT)                 <- baseline
  M1  live short + SMT confirmation
  M2  live short + SMT, score_th 50 (let SMT replace some score strictness)
and a long-machine peek (SMT on longs) to see if it rescues the losing long side.

Usage: RESEARCH_DAYS=1095 python -m scripts.research_smt
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


# (label, extra params, macro-gate on?)
VARIANTS = [
    ("S0 live short (no SMT)",  {"allow_short": True, "allow_long": False},                     True),
    ("M1 short + SMT",          {"allow_short": True, "allow_long": False, "smt_filter": True}, True),
    ("M2 short + SMT, th50",    {"allow_short": True, "allow_long": False, "smt_filter": True,
                                 "score_th": 50},                                               True),
    ("L0 long (no SMT)",        {"allow_short": False, "allow_long": True},                     False),
    ("L1 long + SMT",           {"allow_short": False, "allow_long": True, "smt_filter": True}, False),
]


async def main():
    print(f"[smt-research] lookback={LOOKBACK_DAYS}d symbols={len(SYMBOLS)}")
    usdtd = await _usdtd_timeline()
    if usdtd.empty:
        print("[smt-research] no USDT.D data — abort")
        return
    ethbtc = await data_feed.get_klines_history("ETHBTC", "1d", LOOKBACK_DAYS + 80)
    btcd = _dir_series(ethbtc, usdtd.index, invert=True)
    cpi = await _cpi_dir_daily(usdtd.index)
    cpi_map = {d.strftime("%Y-%m-%d"): v for d, v in cpi.items()}

    # SMT reference series: BTC 1H high/low (ETH for BTC itself).
    btc_ltf = await data_feed.get_klines_history("BTCUSDT", "1h", LOOKBACK_DAYS)
    eth_ltf = await data_feed.get_klines_history("ETHUSDT", "1h", LOOKBACK_DAYS)
    ref = {
        "BTCUSDT": (eth_ltf["high"], eth_ltf["low"]),
        "_default": (btc_ltf["high"], btc_ltf["low"]),
    }

    data = {}
    for sym in SYMBOLS:
        try:
            htf = await data_feed.get_klines_history(sym, config.HTF, LOOKBACK_DAYS)
            dtf = await data_feed.get_klines_history(sym, config.DTF, LOOKBACK_DAYS + 60)
            ltf = await data_feed.get_klines_history(sym, "1h", LOOKBACK_DAYS)
            if not (htf.empty or dtf.empty or ltf.empty):
                data[sym] = (htf, dtf, ltf)
        except Exception as exc:
            print(f"[smt-research] {sym} load error: {exc}")
    print(f"[smt-research] loaded {len(data)} symbols\n")

    rows = []
    for name, extra, macro_on in VARIANTS:
        params_base = {"score_th": config.SMC_SCORE_TH, "short_align": "triple"}
        params_base.update(extra)
        trades = []
        for sym, (htf, dtf, ltf) in data.items():
            params = dict(params_base)
            if params.get("smt_filter"):
                rh, rl = ref.get(sym, ref["_default"])
                params["smt_ref_high"], params["smt_ref_low"] = rh, rl
            try:
                trades += smc_backtester.backtest_symbol_smc(
                    sym, htf, dtf, ltf, usdtd, btcd, params)
            except Exception as exc:
                print(f"[smt-research] {sym} {name} error: {exc}")
        trades = _macro_filter(trades, cpi_map, macro_on)
        s = smc_backtester.summarize(trades)
        oos, oos_n = _oos(trades)
        rows.append((name, s, oos, oos_n))

    print("\n=========== SMT DIVERGENCE FILTER vs LIVE MACHINE (1095d) ===========")
    print(f"{'variant':<24}{'trades':>7}{'win%':>7}{'PF':>6}{'totR':>8}"
          f" | {'OOSn':>5}{'OOSwin':>8}{'OOSpf':>7}")
    print("-" * 74)
    for name, s, oos, oos_n in rows:
        print(f"{name:<24}{s['trades']:>7}{s['win_rate']:>7}{s['profit_factor']:>6}"
              f"{s['total_r']:>8} | {oos_n:>5}{oos.get('win_rate', 0):>8}"
              f"{oos.get('profit_factor', 0):>7}")
    print("=" * 74)
    print("Keep SMT only if it lifts win%/PF/OOS vs S0 without gutting trade count.")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
