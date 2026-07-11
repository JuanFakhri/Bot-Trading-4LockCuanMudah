"""Ablation harness — v1 SMC backtester + independent improvement toggles.

Purpose: measure each proposed change ONE AT A TIME against the validated v1
baseline (PF 1.41 / win 62% / OOS 1.90 / 69 trades over 730d) so we only promote
the changes that actually raise the edge. Every flag defaults OFF, so an empty
``flags`` set reproduces v1 exactly (given the same score_th).

Flags (map to the user's numbered proposals):
  double_bos (#1) ema_slope (#2) adx_rising (#3) atr_exp (#4) vol15 (#5)
  rsi_zone (#7) macro4 (#8) eth_trend (#9) candle (#10) tight_session (#11)
  chandelier (#12) range_off (#14) antifake (#16)
Score threshold (#6/#15) is tested via the ``score_th`` param, not a flag.

This module is OFFLINE ONLY — it never touches the live engine.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, indicators
from .smc_backtester import W, _align


def _bull_confirm(o, h, l, c):
    rng = (h - l) or 1e-9
    body = abs(c - o)
    strong = (c - l) / rng >= 0.7 and c > o
    hammer = (min(o, c) - l) >= 2 * body and (c - l) / rng >= 0.6
    return bool(strong or hammer)


def _bear_confirm(o, h, l, c):
    rng = (h - l) or 1e-9
    body = abs(c - o)
    strong = (h - c) / rng >= 0.7 and c < o
    shoot = (h - max(o, c)) >= 2 * body and (h - c) / rng >= 0.6
    return bool(strong or shoot)


def backtest_symbol_abl(symbol, htf, dtf, ltf, usdtd_daily, btcd_dir_daily,
                        macro=None, flags=None, score_th=60.0) -> list[dict]:
    flags = flags or set()
    macro = macro or {}
    F = lambda k: k in flags
    slope_min = 0.001              # #2 EMA50 slope over 5 bars (~ min angle)

    if ltf is None or len(ltf) < 250 or len(htf) < config.EMA_SLOW + 30:
        return []

    o = ltf["open"].to_numpy(); h = ltf["high"].to_numpy()
    l = ltf["low"].to_numpy(); c = ltf["close"].to_numpy()
    v = ltf["volume"].to_numpy()
    ema50_1 = indicators.ema(ltf["close"], config.EMA_FAST).to_numpy()
    ema200_1 = indicators.ema(ltf["close"], config.EMA_SLOW).to_numpy()
    ema20_1 = indicators.ema(ltf["close"], 20).to_numpy()
    rsi_1 = indicators.rsi(ltf["close"], config.RSI_LEN).to_numpy()
    atr_1 = indicators.atr(ltf, config.ATR_LEN).to_numpy()
    atr_sma = pd.Series(atr_1).rolling(20, min_periods=5).mean().to_numpy()
    adx_1 = indicators.adx(ltf, 14).to_numpy()
    vsma = ltf["volume"].rolling(20, min_periods=5).mean().to_numpy()
    ch_long = (pd.Series(h).rolling(22, min_periods=5).max() - 3 * pd.Series(atr_1)).to_numpy()
    ch_short = (pd.Series(l).rolling(22, min_periods=5).min() + 3 * pd.Series(atr_1)).to_numpy()
    piv_hi, piv_lo = indicators.find_pivots(ltf, config.PIVOT_LEN)
    piv_hi = piv_hi.to_numpy(); piv_lo = piv_lo.to_numpy()
    ts = ltf.index
    n = len(ltf)
    pl_ = config.PIVOT_LEN

    d_bull = _align((indicators.ema(dtf["close"], config.EMA_FAST)
                     > indicators.ema(dtf["close"], config.EMA_SLOW)).astype(float), ts)
    h4_bull = _align((indicators.ema(htf["close"], config.EMA_FAST)
                      > indicators.ema(htf["close"], config.EMA_SLOW)).astype(float), ts)
    h4_rsi = _align(indicators.rsi(htf["close"], config.RSI_LEN), ts)
    h1_bull = (ema50_1 > ema200_1).astype(float)

    usdtd = _align(usdtd_daily, ts)
    usdtd_prev = _align(usdtd_daily.shift(5), ts)
    btcd_dir = pd.Series(btcd_dir_daily).reindex(ts, method="ffill").fillna("STABIL").to_numpy() \
        if btcd_dir_daily is not None else np.array(["STABIL"] * n)

    def macro_arr(key, default):
        s = macro.get(key)
        if s is None:
            return np.array([default] * n, dtype=object)
        return pd.Series(s).reindex(ts, method="ffill").fillna(default).to_numpy()
    btc_dir = macro_arr("btc_dir", "STABIL")
    eth_dir = macro_arr("eth_dir", "STABIL")
    eth_bull = (pd.Series(macro["eth_bull"]).reindex(ts, method="ffill").fillna(1).to_numpy()
                if macro.get("eth_bull") is not None else np.ones(n))

    hours = ts.hour.to_numpy()
    wide_session = (hours >= 7) & (hours < 22)
    tight_session = np.isin(hours, [7, 8, 13, 14])

    swH = swL = np.nan
    swH_prev = swL_prev = np.nan
    fvg_bull_lo = fvg_bull_hi = np.nan
    fvg_bear_lo = fvg_bear_hi = np.nan

    trades: list[dict] = []
    pos = None
    cooldown_until = -1
    lowest = pd.Series(l).rolling(10, min_periods=3).min().shift(1).to_numpy()
    highest = pd.Series(h).rolling(10, min_periods=3).max().shift(1).to_numpy()

    for i in range(210, n):
        j = i - pl_
        if j >= 0:
            if piv_hi[j]:
                swH_prev, swH = swH, h[j]
            if piv_lo[j]:
                swL_prev, swL = swL, l[j]
        if i >= 2:
            if l[i] > h[i - 2]:
                fvg_bull_lo, fvg_bull_hi = h[i - 2], l[i]
            if h[i] < l[i - 2]:
                fvg_bear_lo, fvg_bear_hi = h[i], l[i - 2]

        if pos is not None:
            done = _manage_abl(pos, h[i], l[i], c[i], ema20_1[i],
                               ch_long[i] if pos["machine"] == "long" else ch_short[i],
                               F("chandelier"))
            if done is not None:
                pos.update(outcome=("WIN" if done > 0.05 else "LOSS" if done < -0.05 else "BE"),
                           r=round(done, 3), exit_price=c[i], exit_ts=ts[i].isoformat())
                trades.append(pos)
                cooldown_until = i + (2 if done > 0 else 8)
                pos = None
            continue
        if i < cooldown_until:
            continue

        long_ok = d_bull[i] > 0 and h4_bull[i] > 0 and h1_bull[i] > 0
        short_ok = d_bull[i] == 0 and h4_bull[i] == 0 and h1_bull[i] == 0
        machine = "long" if long_ok else "short" if short_ok else None
        if machine is None:
            continue

        # #9 ETH trend correlation
        if F("eth_trend"):
            if machine == "long" and eth_bull[i] <= 0:
                continue
            if machine == "short" and eth_bull[i] > 0:
                continue

        atr_pct = atr_1[i] / c[i] * 100 if c[i] else 0
        if not (0.3 <= atr_pct <= 8.0):
            continue
        # #11 session (wide default, tight when flagged)
        sess = tight_session if F("tight_session") else wide_session
        if not sess[i]:
            continue
        # #14 regime range filter (ADX < 20 = range = off)
        if F("range_off") and (np.isnan(adx_1[i]) or adx_1[i] < 20):
            continue
        if np.isnan(swH) or np.isnan(swL) or (swH - swL) <= 0:
            continue
        mid = (swH + swL) / 2
        discount = c[i] < mid
        premium = c[i] > mid
        if machine == "long" and not discount:
            continue
        if machine == "short" and not premium:
            continue

        if machine == "long":
            ratio = (swH - c[i]) / (swH - swL)
        else:
            ratio = (c[i] - swL) / (swH - swL)
        in_fib = config.FIB_ZONE_LO <= ratio <= config.FIB_ZONE_HI

        if machine == "long":
            sweep = (not np.isnan(lowest[i])) and l[i] < lowest[i] and c[i] > lowest[i]
            choch = c[i] > swH and c[i - 1] <= swH
            bos = c[i] > swH
            fvg = (not np.isnan(fvg_bull_lo)) and l[i] <= fvg_bull_hi and c[i] >= fvg_bull_lo
            ob = c[i - 1] < o[i - 1] and bos
            ema_ok = c[i] > ema200_1[i] and ema50_1[i] > ema200_1[i]
            rsi_ok = h4_rsi[i] > 50
            btcd_ok = btcd_dir[i] == "TURUN"
            usdtd_ok = usdtd[i] < usdtd_prev[i]
            double_bos = bos and (not np.isnan(swH_prev)) and swH > swH_prev
            btc_ok = btc_dir[i] == "NAIK"; eth_ok = eth_dir[i] == "NAIK"
        else:
            sweep = (not np.isnan(highest[i])) and h[i] > highest[i] and c[i] < highest[i]
            choch = c[i] < swL and c[i - 1] >= swL
            bos = c[i] < swL
            fvg = (not np.isnan(fvg_bear_lo)) and h[i] >= fvg_bear_lo and c[i] <= fvg_bear_hi
            ob = c[i - 1] > o[i - 1] and bos
            ema_ok = c[i] < ema200_1[i] and ema50_1[i] < ema200_1[i]
            rsi_ok = h4_rsi[i] < 50
            btcd_ok = btcd_dir[i] == "NAIK"
            usdtd_ok = usdtd[i] > usdtd_prev[i]
            double_bos = bos and (not np.isnan(swL_prev)) and swL < swL_prev
            btc_ok = btc_dir[i] == "TURUN"; eth_ok = eth_dir[i] == "TURUN"

        vol_ok = (not np.isnan(vsma[i])) and v[i] > vsma[i]
        adx_ok = adx_1[i] > 25

        score = (W["ema"] * ema_ok + W["rsi"] * rsi_ok + W["adx"] * adx_ok
                 + W["fib"] * in_fib + W["sweep"] * sweep + W["choch"] * choch
                 + W["bos"] * bos + W["fvg"] * fvg + W["ob"] * ob
                 + W["btcd"] * btcd_ok + W["usdtd"] * usdtd_ok)
        if not vol_ok:
            continue

        # ---------------- improvement toggles (each independent) ----------------
        if F("double_bos") and not double_bos:                         # #1
            continue
        if F("ema_slope"):                                             # #2
            slope = (ema50_1[i] - ema50_1[i - 5]) / (abs(ema50_1[i - 5]) or 1e-9)
            if (machine == "long" and slope < slope_min) or (machine == "short" and slope > -slope_min):
                continue
        if F("adx_rising") and not (adx_1[i] > adx_1[i - 3]):           # #3
            continue
        if F("atr_exp") and not ((not np.isnan(atr_sma[i])) and atr_1[i] > atr_sma[i]):  # #4
            continue
        if F("vol15") and not (v[i] > 1.5 * vsma[i]):                   # #5
            continue
        if F("rsi_zone"):                                              # #7
            if machine == "long" and not (55 <= rsi_1[i] <= 70):
                continue
            if machine == "short" and not (30 <= rsi_1[i] <= 45):
                continue
        if F("macro4"):                                               # #8 (4 confirmations)
            if not (btcd_ok and usdtd_ok and btc_ok and eth_ok):
                continue
        if F("candle"):                                              # #10
            if machine == "long" and not _bull_confirm(o[i], h[i], l[i], c[i]):
                continue
            if machine == "short" and not _bear_confirm(o[i], h[i], l[i], c[i]):
                continue
        if F("antifake"):                                            # #16
            body = abs(c[i] - o[i]); rng = (h[i] - l[i]) or 1e-9
            if machine == "long" and c[i] < o[i] and body / rng > 0.6:
                continue
            if machine == "short" and c[i] > o[i] and body / rng > 0.6:
                continue
        if score < score_th:                                         # #6 / #15
            continue

        entry = c[i]
        if machine == "long":
            sl = max(min(swL, entry) - atr_1[i], entry * (1 - config.SL_CAP_PCT))
            risk = entry - sl
            if risk <= 0:
                continue
            tp1, tp2, tp3 = entry + risk, entry + 2 * risk, entry + 3 * risk
        else:
            sl = min(max(swH, entry) + atr_1[i], entry * (1 + config.SL_CAP_PCT))
            risk = sl - entry
            if risk <= 0:
                continue
            tp1, tp2, tp3 = entry - risk, entry - 2 * risk, entry - 3 * risk

        features = {
            "machine": machine, "regime": "BULL" if machine == "long" else "BEAR",
            "fib_bucket": "0.5-0.55" if ratio < 0.55 else "0.55-0.618" if ratio <= 0.618 else "deep",
            "rsi_htf_bucket": "hi" if (h4_rsi[i] > 55) else "mid" if h4_rsi[i] > 45 else "lo",
            "rsi_ltf_bucket": "na", "dow": int(ts[i].weekday()), "usdtd_pos_bucket": "na",
            "score_bucket": "85+" if score >= 85 else "70-85" if score >= 70 else "lo",
            "ad_rising": bool(sweep and choch) if machine == "long" else None,
            "sar_confirm": bool(sweep and choch) if machine == "short" else None,
        }
        pos = {
            "symbol": symbol, "direction": "LONG" if machine == "long" else "SHORT",
            "machine": machine, "entry": float(entry), "sl": float(sl),
            "tp1": float(tp1), "tp2": float(tp2), "tp3": float(tp3),
            "rr": round(abs(tp3 - entry) / risk, 2), "risk": float(risk),
            "score": int(score), "tp1_hit": False, "tp2_hit": False,
            "rem": 1.0, "realized": 0.0, "stop": float(sl), "tp_source": "abl",
            "entry_ts": ts[i].isoformat(), "features": features,
        }
    return trades


def _manage_abl(pos, bar_high, bar_low, close, ema20, chandelier, use_chandelier):
    """v1 30/30/40 exit; runner trailed by EMA20 (default) or Chandelier (#12)."""
    entry, risk = pos["entry"], pos["risk"] or 1e-9
    long = pos["direction"] == "LONG"

    def hit(level):
        return bar_high >= level if long else bar_low <= level

    stopped = bar_low <= pos["stop"] if long else bar_high >= pos["stop"]
    if stopped:
        r_stop = (pos["stop"] - entry) / risk if long else (entry - pos["stop"]) / risk
        pos["realized"] += pos["rem"] * r_stop
        return pos["realized"]

    if not pos["tp1_hit"] and hit(pos["tp1"]):
        pos["realized"] += 0.30
        pos["rem"] -= 0.30
        pos["tp1_hit"] = True
        pos["stop"] = entry * (1 + config.BE_BUFFER_PCT) if long else entry * (1 - config.BE_BUFFER_PCT)
    if pos["tp1_hit"] and not pos["tp2_hit"] and hit(pos["tp2"]):
        pos["realized"] += 0.30 * 2.0
        pos["rem"] -= 0.30
        pos["tp2_hit"] = True
    if pos["tp2_hit"]:
        if use_chandelier and not np.isnan(chandelier):
            pos["stop"] = max(pos["stop"], chandelier) if long else min(pos["stop"], chandelier)
            return None
        if hit(pos["tp3"]):
            r3 = (pos["tp3"] - entry) / risk if long else (entry - pos["tp3"]) / risk
            pos["realized"] += pos["rem"] * r3
            return pos["realized"]
        trail_break = close < ema20 if long else close > ema20
        if trail_break:
            r_exit = (close - entry) / risk if long else (entry - close) / risk
            pos["realized"] += pos["rem"] * r_exit
            return pos["realized"]
    return None
