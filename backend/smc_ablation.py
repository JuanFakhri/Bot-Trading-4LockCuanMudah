"""Ablation harness (round 2) — current live v1.1 + candidate indicators.

Tests the user's 4 proposed additions ONE AT A TIME on top of the live strategy
(v1.1 = SMC + AI-Score + ATR-expansion + volume, PF 1.79 / 41 trades / OOS 3.66),
so we only promote what actually improves the edge. OFFLINE ONLY — every flag
off reproduces v1.1 exactly.

Flags: stoch (#1) rsi (#2) stochrsi (#3) sr (#4 support/resistance room-to-run).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, indicators
from .smc_backtester import W, _align, _manage_smc, summarize  # reuse live pieces


def backtest_symbol_abl(symbol, htf, dtf, ltf, usdtd_daily, btcd_dir_daily,
                        flags=None, score_th=60.0, diag=None) -> list[dict]:
    flags = flags or set()
    F = lambda k: k in flags
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
    st_k, st_d = indicators.stochastic(ltf)                 # #1
    st_k = st_k.to_numpy(); st_d = st_d.to_numpy()
    sr_k, sr_d = indicators.stoch_rsi(ltf["close"])         # #3
    sr_k = sr_k.to_numpy(); sr_d = sr_d.to_numpy()
    piv_hi, piv_lo = indicators.find_pivots(ltf, config.PIVOT_LEN)
    piv_hi = piv_hi.to_numpy(); piv_lo = piv_lo.to_numpy()
    ts = ltf.index
    n = len(ltf)
    pl_ = config.PIVOT_LEN

    # confirmed pivot levels (idx = when the pivot becomes known) for S/R (#4)
    hi_piv = [(k + pl_, float(h[k])) for k in range(n) if piv_hi[k] and k + pl_ < n]
    lo_piv = [(k + pl_, float(l[k])) for k in range(n) if piv_lo[k] and k + pl_ < n]

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

    hours = ts.hour.to_numpy()
    in_session = (hours >= 7) & (hours < 22)

    swH = swL = np.nan
    fvg_bull_lo = fvg_bull_hi = np.nan
    fvg_bear_lo = fvg_bear_hi = np.nan
    trades: list[dict] = []
    pos = None
    cooldown_until = -1
    lowest = pd.Series(l).rolling(10, min_periods=3).min().shift(1).to_numpy()
    highest = pd.Series(h).rolling(10, min_periods=3).max().shift(1).to_numpy()

    def rej(key):
        if diag is not None:
            diag[key] = diag.get(key, 0) + 1
        return False

    for i in range(210, n):
        j = i - pl_
        if j >= 0:
            if piv_hi[j]:
                swH = h[j]
            if piv_lo[j]:
                swL = l[j]
        if i >= 2:
            if l[i] > h[i - 2]:
                fvg_bull_lo, fvg_bull_hi = h[i - 2], l[i]
            if h[i] < l[i - 2]:
                fvg_bear_lo, fvg_bear_hi = h[i], l[i - 2]

        if pos is not None:
            done = _manage_smc(pos, h[i], l[i], c[i], ema20_1[i])
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

        atr_pct = atr_1[i] / c[i] * 100 if c[i] else 0
        if not (0.3 <= atr_pct <= 8.0):
            continue
        if not in_session[i]:
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

        vol_ok = (not np.isnan(vsma[i])) and v[i] > config.SMC_VOL_MULT * vsma[i]
        atr_exp = (not np.isnan(atr_sma[i])) and atr_1[i] > atr_sma[i]
        adx_ok = adx_1[i] > 25
        score = (W["ema"] * ema_ok + W["rsi"] * rsi_ok + W["adx"] * adx_ok
                 + W["fib"] * in_fib + W["sweep"] * sweep + W["choch"] * choch
                 + W["bos"] * bos + W["fvg"] * fvg + W["ob"] * ob
                 + W["btcd"] * btcd_ok + W["usdtd"] * usdtd_ok)
        if not vol_ok or not atr_exp:
            continue
        if score < score_th:
            continue

        # -------- candidate indicators (each tested independently) --------
        if F("stoch"):
            if machine == "long" and not (st_k[i] < 85 and st_k[i] >= st_d[i]):
                rej("stoch"); continue
            if machine == "short" and not (st_k[i] > 15 and st_k[i] <= st_d[i]):
                rej("stoch"); continue
        if F("rsi"):
            if machine == "long" and not (45 <= rsi_1[i] <= 78):
                rej("rsi"); continue
            if machine == "short" and not (22 <= rsi_1[i] <= 55):
                rej("rsi"); continue
        if F("stochrsi"):
            if machine == "long" and not (sr_k[i] < 0.85 and sr_k[i] >= sr_d[i]):
                rej("stochrsi"); continue
            if machine == "short" and not (sr_k[i] > 0.15 and sr_k[i] <= sr_d[i]):
                rej("stochrsi"); continue
        if F("sr"):
            room = 1.5 * atr_1[i]   # nearest opposing S/R must give room to TP
            if machine == "long":
                res = min((p for (ci, p) in hi_piv if ci <= i and p > c[i]), default=None)
                if res is not None and (res - c[i]) < room:
                    rej("sr"); continue
            else:
                sup = max((p for (ci, p) in lo_piv if ci <= i and p < c[i]), default=None)
                if sup is not None and (c[i] - sup) < room:
                    rej("sr"); continue

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
