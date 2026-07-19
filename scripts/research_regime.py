"""Research: should the BTC market regime look at 4H and 1H too, not just 1D?

The live regime = BTC EMA50 1D (+/-band over 5d) -> BULL/BEAR/NEUTRAL, and Phoenix
LONG only fires in BULL. When 1D is flat (NEUTRAL) the long machine idles. This
sweep rebuilds the regime from BTC 1D+4H+1H under several combining rules and
re-runs the LIVE two-machine backtest (Phoenix long gated by the new regime + SMC
short unchanged + macro gate), 3y with a walk-forward OOS split, so we adopt a
looser regime ONLY if it adds LONG trades without wrecking win-rate / PF / OOS.

Read-only. Usage: RESEARCH_DAYS=1095 python -m scripts.research_regime
"""
from __future__ import annotations

import asyncio
import os

import pandas as pd

os.environ.setdefault("BACKTEST_DAYS", os.getenv("RESEARCH_DAYS", "1095"))

from backend import config, data_feed, indicators, phoenix, smc_backtester  # noqa: E402
from scripts.backtest_live import (  # noqa: E402
    _usdtd_timeline, _dir_series, _cpi_dir_daily, LOOKBACK_DAYS, SYMBOLS)


def _tf_regime(df: pd.DataFrame, bars: int, band: float) -> pd.Series:
    """BULL/BEAR/NEUTRAL from EMA50 change over `bars`, flat within +/-band."""
    ema = indicators.ema(df["close"], config.EMA_FAST)
    prev = ema.shift(bars)
    chg = (ema - prev) / prev.abs()
    r = pd.Series("NEUTRAL", index=ema.index)
    r[chg > band] = "BULL"
    r[chg < -band] = "BEAR"
    return r


def build_regime(btc1d, btc4h, btc1h, rule: str) -> pd.Series:
    """Combine per-TF regimes into one timeline. '1d' reproduces the live rule.
    5 days == 5x 1D bars == 30x 4H bars == 120x 1H bars."""
    band = config.PHX_NEUTRAL_BAND
    d = config.PHX_NEUTRAL_DAYS
    r1d = _tf_regime(btc1d, d, band)
    if rule == "1d":
        return r1d
    r4h = _tf_regime(btc4h, d * 6, band)
    r1h = _tf_regime(btc1h, d * 24, band)
    idx = btc1h.index
    a = r1d.reindex(idx, method="ffill"); b = r4h.reindex(idx, method="ffill")
    c = r1h.reindex(idx, method="ffill")
    bull = (a == "BULL").astype(int) + (b == "BULL").astype(int) + (c == "BULL").astype(int)
    bear = (a == "BEAR").astype(int) + (b == "BEAR").astype(int) + (c == "BEAR").astype(int)
    out = pd.Series("NEUTRAL", index=idx)
    if rule == "any2":                       # >=2 of 3 TFs agree
        out[bull >= 2] = "BULL"; out[bear >= 2] = "BEAR"
    elif rule == "1d_or_4h1h":               # 1D trend OR (4H & 1H agree)
        out[(a == "BULL") | ((b == "BULL") & (c == "BULL"))] = "BULL"
        out[(a == "BEAR") | ((b == "BEAR") & (c == "BEAR"))] = "BEAR"
    elif rule == "4h1h":                      # ignore 1D, need 4H & 1H
        out[(b == "BULL") & (c == "BULL")] = "BULL"
        out[(b == "BEAR") & (c == "BEAR")] = "BEAR"
    elif rule == "all3":                      # strictest: all 3 agree
        out[bull == 3] = "BULL"; out[bear == 3] = "BEAR"
    return out


def _oos(trades):
    srt = sorted(trades, key=lambda x: x["exit_ts"])
    test = srt[int(len(srt) * 0.7):]
    return smc_backtester.summarize(test), len(test)


RULES = ["1d", "all3", "any2", "1d_or_4h1h", "4h1h"]


async def main():
    print(f"[regime-research] lookback={LOOKBACK_DAYS}d symbols={len(SYMBOLS)}")
    usdtd = await _usdtd_timeline()
    ethbtc = await data_feed.get_klines_history("ETHBTC", "1d", LOOKBACK_DAYS + 80)
    btcd = _dir_series(ethbtc, usdtd.index, invert=True)
    cpi = await _cpi_dir_daily(usdtd.index)
    cpi_map = {d.strftime("%Y-%m-%d"): v for d, v in cpi.items()}

    btc1d = await data_feed.get_klines_history("BTCUSDT", "1d", LOOKBACK_DAYS + 90)
    btc4h = await data_feed.get_klines_history("BTCUSDT", "4h", LOOKBACK_DAYS + 20)
    btc1h = await data_feed.get_klines_history("BTCUSDT", "1h", LOOKBACK_DAYS + 10)

    # preload per-symbol data + fixed SMC shorts (same for every rule)
    data, shorts_by_sym = {}, {}
    for sym in SYMBOLS:
        try:
            htf = await data_feed.get_klines_history(sym, config.HTF, LOOKBACK_DAYS)
            dtf = await data_feed.get_klines_history(sym, config.DTF, LOOKBACK_DAYS + 60)
            ltf = await data_feed.get_klines_history(sym, "1h", LOOKBACK_DAYS)
            if htf.empty or dtf.empty or ltf.empty:
                continue
            data[sym] = (htf, dtf, ltf)
            shorts_by_sym[sym] = smc_backtester.backtest_symbol_smc(
                sym, htf, dtf, ltf, usdtd, btcd,
                {"score_th": config.SMC_SHORT_SCORE_TH, "allow_long": False, "allow_short": True})
        except Exception as exc:
            print(f"[regime-research] {sym} load error: {exc}")
    print(f"[regime-research] loaded {len(data)} symbols\n")

    def macro(trades):
        return [t for t in trades if not (
            (t["direction"] == "LONG" and cpi_map.get(t["entry_ts"][:10]) == "BEARISH") or
            (t["direction"] == "SHORT" and cpi_map.get(t["entry_ts"][:10]) == "BULLISH"))]

    rows = []
    for rule in RULES:
        regime = build_regime(btc1d, btc4h, btc1h, rule)
        bull_frac = float((regime == "BULL").mean()) * 100
        trades = []
        for sym, (htf, dtf, ltf) in data.items():
            longs = phoenix.backtest_symbol_long(
                sym, htf, dtf, ltf, usdtd, btcd, {"regime_daily": regime})
            trades += longs + shorts_by_sym.get(sym, [])
        trades = macro(trades)
        longs_only = [t for t in trades if t["direction"] == "LONG"]
        s = smc_backtester.summarize(trades)
        ls = smc_backtester.summarize(longs_only)
        oos, oos_n = _oos(trades)
        rows.append((rule, bull_frac, len(longs_only), ls, s, oos, oos_n))

    print("\n===================== BTC REGIME RULE SWEEP (1095d) =====================")
    print(f"{'rule':<13}{'%BULL':>6}{'longs':>6}{'Lwin':>6}{'Lpf':>6} | "
          f"{'blend_n':>7}{'win':>6}{'PF':>6}{'totR':>8} | {'OOSn':>5}{'OOSwin':>7}{'OOSpf':>6}")
    print("-" * 84)
    for rule, bf, ln, ls, s, oos, oos_n in rows:
        print(f"{rule:<13}{bf:>6.0f}{ln:>6}{ls.get('win_rate',0):>6}{ls.get('profit_factor',0):>6} | "
              f"{s['trades']:>7}{s['win_rate']:>6}{s['profit_factor']:>6}{s['total_r']:>8} | "
              f"{oos_n:>5}{oos.get('win_rate',0):>7}{oos.get('profit_factor',0):>6}")
    print("=" * 84)
    print("Adopt a looser rule ONLY if it adds longs and keeps blend + OOS win/PF.")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
