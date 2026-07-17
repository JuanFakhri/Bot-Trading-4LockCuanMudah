"""Live SMC + AI-Score evaluator — the profitable strategy, wired for signals.

This is the *live* twin of ``smc_backtester.py``. The backtester walks every
1H bar historically; here we evaluate only the **latest closed 1H bar** and emit
a signal dict the engine, risk guard, learning brain and web UI consume directly.

Entry requires the same confluence the backtest validated (PF 1.44 over 212
trades, walk-forward OOS PF 2.09): multi-TF trend alignment (1D+4H+1H),
premium/discount, ADX>25, volume spike, an ATR band, London/NY session, the SMC
signals (sweep → CHOCH → BOS → FVG → OB) and the weighted Setup-Score gate.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import config, indicators, phoenix

# Setup-Score weights (#15) — identical to the backtester.
W = {"ema": 10, "rsi": 5, "adx": 10, "fib": 15, "sweep": 15, "choch": 15,
     "bos": 10, "fvg": 10, "ob": 5, "btcd": 5, "usdtd": 5}


def _last_pivot(piv: np.ndarray, values: np.ndarray, n: int, pl: int):
    """Return (price, index) of the most recently *confirmed* pivot, else NaN."""
    for j in range(n - 1 - pl, -1, -1):
        if piv[j]:
            return values[j], j
    return np.nan, -1


def _direction(htf: pd.DataFrame, dtf: pd.DataFrame, ltf: pd.DataFrame) -> str | None:
    """Pick the machine from multi-TF trend alignment (#19): all three of
    1D / 4H / 1H EMA50-vs-EMA200 must agree, else no trade."""
    if len(ltf) < 250 or len(htf) < config.EMA_SLOW + 30 or len(dtf) < config.EMA_SLOW + 5:
        return None
    d_bull = indicators.ema(dtf["close"], config.EMA_FAST).iloc[-1] \
        > indicators.ema(dtf["close"], config.EMA_SLOW).iloc[-1]
    h4_bull = indicators.ema(htf["close"], config.EMA_FAST).iloc[-1] \
        > indicators.ema(htf["close"], config.EMA_SLOW).iloc[-1]
    h1_bull = indicators.ema(ltf["close"], config.EMA_FAST).iloc[-1] \
        > indicators.ema(ltf["close"], config.EMA_SLOW).iloc[-1]
    if d_bull and h4_bull and h1_bull:
        return "long"
    if (not d_bull) and (not h4_bull) and (not h1_bull):
        return "short"
    return None


def evaluate(symbol: str, htf: pd.DataFrame, dtf: pd.DataFrame, ltf: pd.DataFrame,
             regime: dict) -> dict | None:
    """Two-machine ROUTER — the single entry point the engine calls.

        BULL market regime -> Phoenix Hybrid  (``phoenix.evaluate_long``)
        BEAR / short setup  -> classic SMC     (``evaluate_smc_machine`` below)

    The long side is gated by the BTC-driven market regime (BULL) because that is
    exactly how the Phoenix engine was backtested; the short side keeps the
    validated SMC per-symbol trend alignment.
    """
    reg = regime.get("regime")
    cpi_bias = regime.get("cpi_bias", "NETRAL")
    gate = config.MACRO_GATE
    # LONG — Phoenix, only in a BULL market regime (matches phoenix_backtester).
    if reg == "BULL" and config.SMC_ALLOW_LONG:
        if gate and cpi_bias == "BEARISH":       # don't long into a rising-inflation backdrop
            return None
        return phoenix.evaluate_long(symbol, htf, dtf, ltf, regime)
    # SHORT — classic SMC, on a per-symbol bearish alignment (the validated edge).
    if config.SMC_ALLOW_SHORT and _direction(htf, dtf, ltf) == "short":
        if gate and cpi_bias == "BULLISH":       # don't short into a falling-inflation backdrop
            return None
        return evaluate_smc_machine(symbol, htf, dtf, ltf, regime)
    return None


def evaluate_smc_machine(symbol: str, htf: pd.DataFrame, dtf: pd.DataFrame,
                         ltf: pd.DataFrame, regime: dict) -> dict | None:
    """The SMC machine (short side). Evaluates the latest 1H bar. ``ltf`` MUST
    be 1H candles (SMC entry TF)."""
    if len(ltf) < 250 or len(htf) < config.EMA_SLOW + 30 or len(dtf) < config.EMA_SLOW + 5:
        return None

    o = ltf["open"].to_numpy(); h = ltf["high"].to_numpy()
    l = ltf["low"].to_numpy(); c = ltf["close"].to_numpy()
    v = ltf["volume"].to_numpy()
    n = len(ltf)
    i = n - 1                                   # latest closed bar
    pl = config.PIVOT_LEN

    ema50_1 = indicators.ema(ltf["close"], config.EMA_FAST).to_numpy()
    ema200_1 = indicators.ema(ltf["close"], config.EMA_SLOW).to_numpy()
    ema20_1 = indicators.ema(ltf["close"], 20).to_numpy()
    atr_1 = indicators.atr(ltf, config.ATR_LEN).to_numpy()
    atr_sma = pd.Series(atr_1).rolling(20, min_periods=5).mean().to_numpy()   # v1.1 #4
    adx_1 = indicators.adx(ltf, 14).to_numpy()
    vsma = ltf["volume"].rolling(20, min_periods=5).mean().to_numpy()
    piv_hi, piv_lo = indicators.find_pivots(ltf, pl)
    piv_hi = piv_hi.to_numpy(); piv_lo = piv_lo.to_numpy()

    # higher-TF trend (latest values)
    d_e50 = indicators.ema(dtf["close"], config.EMA_FAST).iloc[-1]
    d_e200 = indicators.ema(dtf["close"], config.EMA_SLOW).iloc[-1]
    d_bull = d_e50 > d_e200
    h4_e50 = indicators.ema(htf["close"], config.EMA_FAST).iloc[-1]
    h4_e200 = indicators.ema(htf["close"], config.EMA_SLOW).iloc[-1]
    h4_bull = h4_e50 > h4_e200
    h4_rsi = float(indicators.rsi(htf["close"], config.RSI_LEN).iloc[-1])
    h1_bull = ema50_1[i] > ema200_1[i]

    price = float(c[i])
    atr_val = float(atr_1[i])

    # ---- SMC is the SHORT machine: require a fully bearish 1D+4H+1H alignment ----
    if d_bull or h4_bull or h1_bull:
        return None

    # ---- swings / bearish FVG / liquidity ----
    swH, _ = _last_pivot(piv_hi, h, n, pl)
    swL, _ = _last_pivot(piv_lo, l, n, pl)
    if np.isnan(swH) or np.isnan(swL) or (swH - swL) <= 0:
        return None
    fvg_bear_lo = fvg_bear_hi = np.nan
    for k in range(2, n):
        if h[k] < l[k - 2]:
            fvg_bear_lo, fvg_bear_hi = h[k], l[k - 2]
    highest = float(pd.Series(h[:i]).rolling(10, min_periods=3).max().iloc[-1]) if i >= 3 else np.nan

    # macro from regime
    usdtd_rising = bool(regime.get("usdtd_rising"))
    btcd_dir = regime.get("btcd_dir", "STABIL")

    premium = price > (swH + swL) / 2

    # fib golden zone of the last swing (short side)
    ratio = (price - swL) / (swH - swL)
    in_fib = config.FIB_ZONE_LO <= ratio <= config.FIB_ZONE_HI

    # ---- SMC signals (short, heuristic — same as backtest) ----
    sweep = (not np.isnan(highest)) and h[i] > highest and c[i] < highest
    choch = c[i] < swL and c[i - 1] >= swL
    bos = c[i] < swL
    fvg = (not np.isnan(fvg_bear_lo)) and h[i] >= fvg_bear_lo and c[i] <= fvg_bear_hi
    ob = c[i - 1] > o[i - 1] and bos
    ema_ok = c[i] < ema200_1[i] and ema50_1[i] < ema200_1[i]
    rsi_ok = h4_rsi < 50
    btcd_ok = btcd_dir == "NAIK"
    usdtd_ok = usdtd_rising

    # v1.1: ablation-validated filters (PF 1.41->2.60, win 62->72%, DD -6->-3.4R)
    vol_ok = (not np.isnan(vsma[i])) and v[i] > config.SMC_VOL_MULT * vsma[i]   # #5 volume 1.5x
    atr_exp = (not np.isnan(atr_sma[i])) and atr_1[i] > atr_sma[i]              # #4 ATR expansion
    adx_ok = adx_1[i] > 25
    atr_pct = atr_val / price * 100 if price else 0.0
    atr_ok = config.SMC_ATR_MIN <= atr_pct <= config.SMC_ATR_MAX
    hours = ltf.index[i].hour
    in_session = 7 <= hours < 22
    pd_ok = premium

    score = (W["ema"] * ema_ok + W["rsi"] * rsi_ok + W["adx"] * adx_ok
             + W["fib"] * in_fib + W["sweep"] * sweep + W["choch"] * choch
             + W["bos"] * bos + W["fvg"] * fvg + W["ob"] * ob
             + W["btcd"] * btcd_ok + W["usdtd"] * usdtd_ok)
    score = int(score)
    score_th = float(config.SMC_SCORE_TH)

    # ---- checklist (why) for the UI ----
    checklist = [
        {"rule": "Trend align 1D+4H+1H", "ok": True, "detail": "bear"},
        {"rule": "Premium zone", "ok": bool(pd_ok)},
        {"rule": "ADX > 25 (trending)", "ok": bool(adx_ok), "detail": f"adx={adx_1[i]:.0f}"},
        {"rule": f"Volume > {config.SMC_VOL_MULT}x SMA20", "ok": bool(vol_ok)},
        {"rule": "ATR expansion (> SMA20)", "ok": bool(atr_exp)},
        {"rule": f"ATR {config.SMC_ATR_MIN}-{config.SMC_ATR_MAX}%", "ok": bool(atr_ok),
         "detail": f"{atr_pct:.2f}%"},
        {"rule": "Sesi London/NY", "ok": bool(in_session), "detail": f"{hours:02d}:00 UTC"},
        {"rule": "Golden zone 0.5-0.618", "ok": bool(in_fib), "detail": f"{ratio:.3f}"},
    ]
    trigger = [
        {"rule": "Liquidity sweep", "ok": bool(sweep)},
        {"rule": "CHOCH", "ok": bool(choch)},
        {"rule": "BOS", "ok": bool(bos)},
        {"rule": "FVG retest", "ok": bool(fvg)},
        {"rule": "Order block", "ok": bool(ob)},
        {"rule": f"Skor Setup >= {int(score_th)}", "ok": score >= score_th,
         "detail": f"score={score}"},
    ]

    # hard pre-reqs that MUST hold for a fire (mirror the backtest's `continue`s)
    hard_ok = atr_ok and in_session and pd_ok and vol_ok and atr_exp
    fire = hard_ok and score >= score_th
    if fire:
        state = "ENTRY"
    elif score >= score_th - 15 and hard_ok:
        state = "ARMED"
    else:
        state = "WATCHING"

    # ---- build the trade plan (SMC short: SL beyond swing +1 ATR, cap 6%; 1/2/3R) ----
    entry = price
    sl = min(max(swH, entry) + atr_val, entry * (1 + config.SL_CAP_PCT))
    risk = sl - entry
    if risk <= 0:
        return None
    tp1, tp2, tp3 = entry - risk, entry - 2 * risk, entry - 3 * risk
    direction = "SHORT"
    be = entry * (1 - config.BE_BUFFER_PCT)
    plan = {
        "entry": round(entry, 8), "sl": round(sl, 8),
        "tp1": round(tp1, 8), "tp2": round(tp2, 8), "tp3": round(tp3, 8),
        "breakeven": round(be, 8), "risk_per_unit": round(risk, 8),
        "rr": round(abs(tp3 - entry) / risk, 2), "rr_ok": True,
        "tp_source": "smc", "position_size": round((1000.0 * config.RISK_PER_TRADE) / risk, 6),
        "risk_pct": config.RISK_PER_TRADE, "sl_pct": round(abs(entry - sl) / entry, 4),
    }

    # feature signature (identical keys/buckets to the backtest so lessons transfer)
    features = {
        "machine": "short", "regime": "BEAR",
        "fib_bucket": "0.5-0.55" if ratio < 0.55 else "0.55-0.618" if ratio <= 0.618 else "deep",
        "rsi_htf_bucket": "hi" if h4_rsi > 55 else "mid" if h4_rsi > 45 else "lo",
        "rsi_ltf_bucket": "na", "dow": int(ltf.index[i].weekday()),
        "usdtd_pos_bucket": "na",
        "score_bucket": "85+" if score >= 85 else "70-85" if score >= 70 else "lo",
        "ad_rising": None,
        "sar_confirm": bool(sweep and choch),
    }

    # opposing liquidity levels for the UI (swing highs above / lows below)
    highs_arr = ltf["high"].to_numpy(); lows_arr = ltf["low"].to_numpy()
    swing_highs = sorted(float(highs_arr[k]) for k in range(n)
                         if piv_hi[k] and highs_arr[k] > price)
    swing_lows = sorted((float(lows_arr[k]) for k in range(n)
                         if piv_lo[k] and lows_arr[k] < price), reverse=True)

    # last 40 4H candles for the card chart
    tail = htf.tail(40)
    candles = [[int(ts.timestamp()), float(oo), float(hh), float(ll), float(cc)]
               for ts, oo, hh, ll, cc in zip(tail.index, tail["open"], tail["high"],
                                             tail["low"], tail["close"])]

    return {
        "symbol": symbol, "machine": "short", "direction": direction,
        "state": state, "price": price, "atr": atr_val,
        "impulse_start": float(swH),
        "impulse_end": float(swL),
        "retrace_ratio": round(ratio, 3),
        "score": score,
        "fib": {"0.5": round((swH + swL) / 2, 8), "ext_1.272": round(tp3, 8)},
        "swing_highs": [round(x, 8) for x in swing_highs[:6]],
        "swing_lows": [round(x, 8) for x in swing_lows[:6]],
        "checklist": checklist, "trigger": trigger,
        "htf_ok": bool(hard_ok), "golden_zone": bool(in_fib),
        "features": features, "plan": plan, "candles": candles,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
