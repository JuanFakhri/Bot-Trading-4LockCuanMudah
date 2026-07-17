"""Phoenix Hybrid — the LIVE LONG machine (adapter over the proven engine).

The bot runs two machines, chosen by the BTC-driven market regime
(see ``strategy_smc.evaluate`` — the router):

    BULL regime -> Phoenix Hybrid (this file)  — long
    BEAR regime -> classic SMC (``strategy_smc``) — short

The Phoenix *strategy* itself lives in :mod:`backend.phoenix_backtester` (the
research engine shown on the "Phoenix" tab). That engine is validated over 3
years — long-only walk-forward PF 1.36 out-of-sample (fib PF 1.33 / breakout
2.17). To guarantee the live signal never drifts from that backtest, this module
is a thin ADAPTER:

  * ``backtest_symbol_long`` delegates straight to
    ``phoenix_backtester.backtest_symbol_phoenix`` (sides=LONG).
  * ``evaluate_long`` is a faithful single-bar port of that engine's entry logic
    — the same arm-then-confirm FIB (wick into the 0.382-0.618 zone, then a
    2-of-3 trigger within 8 bars) and momentum-breakout rules — so the latest
    closed 1H bar is scored exactly as the backtester would enter it.

Exits are handled live by the engine (TP1/TP2 + breakeven), matching the
backtester's shared exit shape.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import config, indicators, phoenix_backtester as phx

ARM_EXPIRY = 8   # keep in sync with phoenix_backtester


def _live_engines() -> list:
    """Which long engines are enabled live (config-driven; both by default)."""
    eng = []
    if config.PHX_ENGINE_FIB:
        eng.append("fib")
    if config.PHX_ENGINE_BREAKOUT:
        eng.append("breakout")
    return eng


# --------------------------------------------------------------------------
# Backtest twin — delegate to the proven engine (single source of truth)
# --------------------------------------------------------------------------
def backtest_symbol_long(symbol, htf, dtf, ltf, usdtd_daily=None,
                         btcd_dir_daily=None, params=None) -> list[dict]:
    """Phoenix LONG trades for one symbol, produced by the proven research
    engine restricted to LONG. ``params['regime_daily']`` (BTC BULL/BEAR/NEUTRAL
    per day) is required — without it there is no regime and no trades."""
    params = params or {}
    regime_daily = params.get("regime_daily")
    if regime_daily is None:
        return []
    engines = params.get("engines") or _live_engines() or ["fib", "breakout"]
    return phx.backtest_symbol_phoenix(
        symbol, htf, dtf, ltf, regime_daily, usdtd_daily,
        {"engines": engines, "sides": ["LONG"]})


# --------------------------------------------------------------------------
# Live twin — faithful single-bar port of the engine's LONG entry
# --------------------------------------------------------------------------
def _prep(htf, dtf, ltf) -> dict:
    """Mirror the array set phoenix_backtester computes on the 1H frame."""
    h = ltf["high"].to_numpy(); l = ltf["low"].to_numpy()
    c = ltf["close"].to_numpy(); v = ltf["volume"].to_numpy()
    ts = ltf.index
    ema20 = indicators.ema(ltf["close"], 20).to_numpy()
    ema200 = indicators.ema(ltf["close"], config.EMA_SLOW).to_numpy()
    rsi1 = indicators.rsi(ltf["close"], config.RSI_LEN).to_numpy()
    atr1 = indicators.atr(ltf, config.ATR_LEN).to_numpy()
    vsma = ltf["volume"].rolling(20, min_periods=5).mean().to_numpy()
    ad = indicators.ad_line(ltf)
    ad_rising = (ad > ad.shift(3)).to_numpy()
    hi20 = pd.Series(h).rolling(config.PHX_BRK_LOOKBACK, min_periods=5).max().shift(1).to_numpy()
    piv_hi, piv_lo = indicators.find_pivots(ltf, config.PIVOT_LEN)

    def _align(series):
        return series.reindex(ts, method="ffill").to_numpy()
    h4_rsi = _align(indicators.rsi(htf["close"], config.RSI_LEN))
    h4_atr = indicators.atr(htf, config.ATR_LEN)
    h4_atr_pct = _align(h4_atr / htf["close"] * 100)
    d_ema200 = _align(indicators.ema(dtf["close"], config.EMA_SLOW))
    return {"h": h, "l": l, "c": c, "v": v, "ts": ts, "n": len(ltf),
            "ema20": ema20, "ema200": ema200, "rsi1": rsi1, "atr1": atr1,
            "vsma": vsma, "ad_rising": ad_rising, "hi20": hi20,
            "h4_rsi": h4_rsi, "h4_atr_pct": h4_atr_pct, "d_ema200": d_ema200,
            "piv_hi": piv_hi.to_numpy(), "piv_lo": piv_lo.to_numpy()}


def _scan_long(A: dict, engines: list) -> dict | None:
    """Reconstruct swing + FIB-arm state bar by bar (exactly as the backtester),
    and return the entry signal IF the latest bar (n-1) would open a long."""
    h, l, c, v = A["h"], A["l"], A["c"], A["v"]
    rsi1, atr1, vsma = A["rsi1"], A["atr1"], A["vsma"]
    hi20, ad_rising, ema200 = A["hi20"], A["ad_rising"], A["ema200"]
    h4_rsi, h4_atr_pct, d_ema200 = A["h4_rsi"], A["h4_atr_pct"], A["d_ema200"]
    piv_hi, piv_lo = A["piv_hi"], A["piv_lo"]
    n, pl = A["n"], config.PIVOT_LEN

    swH = swL = np.nan
    fib_arm = None
    last = n - 1
    for i in range(255, n):
        j = i - pl
        if j >= 0:
            if piv_hi[j]:
                swH = h[j]
            if piv_lo[j]:
                swL = l[j]
        if np.isnan(atr1[i]) or atr1[i] <= 0 or c[i] <= 0:
            continue
        # live long only fires in BULL + live volatility (trend-engine gate)
        if h4_atr_pct[i] < config.PHX_VOL_MIN_PCT:
            continue
        atrp = atr1[i] / c[i] * 100
        sig = None

        # ---- Engine 2: momentum breakout ----
        if "breakout" in engines and atrp > config.PHX_BRK_ATR_MIN:
            vol_ok = (not np.isnan(vsma[i])) and v[i] > config.PHX_BRK_VOL_MULT * vsma[i]
            if (not np.isnan(hi20[i]) and c[i] > hi20[i] and vol_ok
                    and c[i] > d_ema200[i] and h4_rsi[i] > config.PHX_BRK_RSI
                    and ad_rising[i]):
                entry = c[i]
                sl = min(l[i], swL if not np.isnan(swL) else l[i]) - config.PHX_SL_ATR * atr1[i]
                if entry - sl > 0:
                    sig = ("breakout", entry, sl, entry + 2 * (entry - sl))

        # ---- Engine 1: FIB retrace (arm in zone, confirm to enter) ----
        if "fib" in engines and not (np.isnan(swH) or np.isnan(swL)):
            impulse = swH - swL
            if impulse >= config.PHX_FIB_IMPULSE_ATR * atr1[i]:
                zlo = swH - config.PHX_FIB_ZONE_HI * impulse
                zhi = swH - config.PHX_FIB_ZONE_LO * impulse
                if l[i] <= zhi and l[i] >= zlo - 0.25 * impulse and c[i] > swL:
                    fib_arm = ("LONG", i, swH, swL)
            if fib_arm is not None:
                _, abar, aH, aL = fib_arm
                if i - abar > ARM_EXPIRY or c[i] < aL:
                    fib_arm = None
            if sig is None and fib_arm is not None:
                _, abar, aH, aL = fib_arm
                bos = int(c[i] > h[i - 1])
                rsi_turn = int(rsi1[i] > rsi1[i - 1] and rsi1[i] > 45)
                vol_up = int((not np.isnan(vsma[i])) and v[i] > vsma[i])
                if bos + rsi_turn + vol_up >= config.PHX_FIB_CONFIRM_MIN:
                    entry = c[i]
                    sl = aL - config.PHX_SL_ATR * atr1[i]
                    if entry - sl > 0:
                        sig = ("fib", entry, sl, entry + 2 * (entry - sl))
                        fib_arm = None   # consumed (as in the backtester)

        if sig is not None and i == last:
            engine, entry, sl, tp2 = sig
            risk = entry - sl
            return {"engine": engine, "entry": float(entry), "sl": float(sl),
                    "tp1": float(entry + risk), "tp2": float(tp2),
                    "tp3": float(entry + 3 * risk), "risk": float(risk),
                    "armed": fib_arm is not None}
    return None


def _armed_watch(A: dict, engines: list) -> bool:
    """True if a FIB arm is currently live at the last bar (for an ARMED card)."""
    h, l, c, atr1 = A["h"], A["l"], A["c"], A["atr1"]
    piv_hi, piv_lo = A["piv_hi"], A["piv_lo"]
    n, pl = A["n"], config.PIVOT_LEN
    swH = swL = np.nan
    fib_arm = None
    for i in range(255, n):
        j = i - pl
        if j >= 0:
            if piv_hi[j]:
                swH = h[j]
            if piv_lo[j]:
                swL = l[j]
        if np.isnan(atr1[i]) or atr1[i] <= 0 or A["h4_atr_pct"][i] < config.PHX_VOL_MIN_PCT:
            continue
        if "fib" in engines and not (np.isnan(swH) or np.isnan(swL)):
            impulse = swH - swL
            if impulse >= config.PHX_FIB_IMPULSE_ATR * atr1[i]:
                zlo = swH - config.PHX_FIB_ZONE_HI * impulse
                zhi = swH - config.PHX_FIB_ZONE_LO * impulse
                if l[i] <= zhi and l[i] >= zlo - 0.25 * impulse and c[i] > swL:
                    fib_arm = ("LONG", i, swH, swL)
            if fib_arm is not None:
                _, abar, aH, aL = fib_arm
                if i - abar > ARM_EXPIRY or c[i] < aL:
                    fib_arm = None
    return fib_arm is not None


def _features(engine: str, A: dict, i: int) -> dict:
    """Learning signature — same KEYS as the SMC machine so lessons interoperate."""
    h4 = A["h4_rsi"][i]
    return {
        "machine": "long", "regime": "BULL",
        "fib_bucket": "breakout" if engine == "breakout" else "golden",
        "rsi_htf_bucket": "hi" if h4 > 55 else "mid" if h4 > 45 else "lo",
        "rsi_ltf_bucket": "na", "dow": int(A["ts"][i].weekday()),
        "usdtd_pos_bucket": "na",
        "score_bucket": "85+" if engine == "breakout" else "70-85",
        "ad_rising": bool(A["ad_rising"][i]),
        "sar_confirm": None, "engine": engine,
    }


def evaluate_long(symbol: str, htf: pd.DataFrame, dtf: pd.DataFrame,
                  ltf: pd.DataFrame, regime: dict) -> dict | None:
    """Live Phoenix LONG signal for the latest closed 1H bar. Long only fires in
    a BULL market regime. Returns the SMC-shaped signal dict the engine/UI use."""
    if regime.get("regime") != "BULL":
        return None
    if len(ltf) < 260 or len(htf) < config.EMA_SLOW + 30 or len(dtf) < config.EMA_SLOW + 5:
        return None
    engines = _live_engines() or ["fib", "breakout"]
    A = _prep(htf, dtf, ltf)
    i = A["n"] - 1
    price = float(A["c"][i])
    atr_val = float(A["atr1"][i]) if np.isfinite(A["atr1"][i]) else 0.0

    e = _scan_long(A, engines)
    fired = e is not None
    if not fired:
        # informative ARMED/WATCHING card
        armed = _armed_watch(A, engines)
        state = "ARMED" if armed else "WATCHING"
        sl = price * (1 - config.SL_CAP_PCT)
        risk = max(price - sl, 1e-9)
        e = {"engine": "fib" if armed else "breakout", "entry": price, "sl": sl,
             "tp1": price + risk, "tp2": price + 2 * risk, "tp3": price + 3 * risk,
             "risk": risk, "armed": armed}
    else:
        state = "ENTRY"

    entry, sl = e["entry"], e["sl"]
    tp1, tp2, tp3 = e["tp1"], e["tp2"], e["tp3"]
    risk = e["risk"]
    be = entry * (1 + config.BE_BUFFER_PCT)
    atr_pct = (atr_val / price * 100) if price else 0.0
    vol_live = A["h4_atr_pct"][i] >= config.PHX_VOL_MIN_PCT
    vol_ok = (not np.isnan(A["vsma"][i])) and A["v"][i] > config.PHX_BRK_VOL_MULT * A["vsma"][i]

    checklist = [
        {"rule": "Regime pasar BULL (BTC EMA50 1D)", "ok": True},
        {"rule": f"Volatilitas hidup (ATR 4H ≥ {config.PHX_VOL_MIN_PCT}%)", "ok": bool(vol_live),
         "detail": f"{A['h4_atr_pct'][i]:.2f}%"},
        {"rule": "Harga > EMA200 1D", "ok": bool(price > A["d_ema200"][i])},
        {"rule": "RSI 4H > 55", "ok": bool(A["h4_rsi"][i] > config.PHX_BRK_RSI),
         "detail": f"{A['h4_rsi'][i]:.0f}"},
        {"rule": "A/D line naik", "ok": bool(A["ad_rising"][i])},
    ]
    trigger = [
        {"rule": "Mesin FIB — arm di zona 0.382-0.618, konfirmasi 2/3", "ok": e["engine"] == "fib" and fired,
         "detail": "armed" if e.get("armed") else ""},
        {"rule": "Mesin Breakout — tembus high 20-bar + volume 1.5×", "ok": e["engine"] == "breakout" and fired,
         "detail": "vol✓" if vol_ok else ""},
        {"rule": "Phoenix entry", "ok": bool(fired),
         "detail": e["engine"] if fired else "-"},
    ]

    plan = {
        "entry": round(entry, 8), "sl": round(sl, 8),
        "tp1": round(tp1, 8), "tp2": round(tp2, 8), "tp3": round(tp3, 8),
        "breakeven": round(be, 8), "risk_per_unit": round(risk, 8),
        "rr": round(abs(tp2 - entry) / risk, 2), "rr_ok": True,
        "tp_source": "phoenix",
        "position_size": round((1000.0 * config.PHX_RISK_TREND) / risk, 6),
        "risk_pct": config.PHX_RISK_TREND, "sl_pct": round(abs(entry - sl) / entry, 4),
    }
    features = _features(e["engine"], A, i)

    h_arr, l_arr = A["h"], A["l"]
    piv_hi, piv_lo = A["piv_hi"], A["piv_lo"]
    swing_highs = sorted(float(h_arr[k]) for k in range(A["n"]) if piv_hi[k] and h_arr[k] > price)
    swing_lows = sorted((float(l_arr[k]) for k in range(A["n"]) if piv_lo[k] and l_arr[k] < price), reverse=True)
    tail = htf.tail(40)
    candles = [[int(t.timestamp()), float(oo), float(hh), float(ll), float(cc)]
               for t, oo, hh, ll, cc in zip(tail.index, tail["open"], tail["high"],
                                            tail["low"], tail["close"])]

    return {
        "symbol": symbol, "machine": "long", "direction": "LONG",
        "state": state, "price": price, "atr": atr_val,
        "engine": e["engine"], "strategy": "phoenix",
        "impulse_start": price, "impulse_end": price,
        "retrace_ratio": None,
        "score": 85 if e["engine"] == "breakout" else 75,
        "fib": {"0.5": price, "ext_1.272": round(tp3, 8)},
        "swing_highs": [round(x, 8) for x in swing_highs[:6]],
        "swing_lows": [round(x, 8) for x in swing_lows[:6]],
        "checklist": checklist, "trigger": trigger,
        "htf_ok": bool(fired), "golden_zone": bool(e["engine"] == "fib" and fired),
        "features": features, "plan": plan, "candles": candles,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
