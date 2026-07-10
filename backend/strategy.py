"""FIB Hybrid strategy engine — the two regime-switching machines.

Given the 4H (HTF), 1D (DTF) and 15m (LTF) candles for one symbol plus the
current market regime, produce a signal dict describing the state:

    WATCHING  -> conditions not met
    ARMED     -> price tapped the golden zone + RSI trigger zone, waiting for
                 confirmation within CONFIRM_MAX_BARS
    ENTRY     -> confirmation candle printed (green+mini-BOS+RSI up+OBV up for
                 longs, mirror for shorts)

The dict also carries the exact checklist of rules (B/C sections) so the UI can
show *why* a signal fired, and a feature signature the learning engine keys on.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import config, indicators, tuning


# ----------------------------------------------------------------------------
# Impulse + fibonacci detection
# ----------------------------------------------------------------------------
def _latest_impulse(df: pd.DataFrame, atr_val: float, direction: str):
    """Find the most recent qualifying impulse on the HTF.

    For a bull impulse: swing low -> swing high (up move).
    For a bear impulse: swing high -> swing low (down move).
    Requirements: range >= IMPULSE_MIN_ATR * ATR, and the *ending* pivot is at
    most IMPULSE_MAX_AGE bars old.
    Returns (start_price, end_price, end_pos) or None.
    """
    piv_hi, piv_lo = indicators.find_pivots(df, config.PIVOT_LEN)
    n = len(df)
    hi_idx = [i for i in range(n) if piv_hi.iloc[i]]
    lo_idx = [i for i in range(n) if piv_lo.iloc[i]]
    if not hi_idx or not lo_idx:
        return None

    best = None
    if direction == "long":
        # swing low followed by a later swing high
        for h in reversed(hi_idx):
            if (n - 1 - h) > config.IMPULSE_MAX_AGE:
                break
            prior_lows = [l for l in lo_idx if l < h]
            if not prior_lows:
                continue
            l = max(prior_lows)
            lo_p = float(df["low"].iloc[l])
            hi_p = float(df["high"].iloc[h])
            rng = hi_p - lo_p
            if rng >= config.IMPULSE_MIN_ATR * atr_val:
                best = (lo_p, hi_p, h)
                break
    else:
        # swing high followed by a later swing low
        for l in reversed(lo_idx):
            if (n - 1 - l) > config.IMPULSE_MAX_AGE:
                break
            prior_highs = [h for h in hi_idx if h < l]
            if not prior_highs:
                continue
            h = max(prior_highs)
            hi_p = float(df["high"].iloc[h])
            lo_p = float(df["low"].iloc[l])
            rng = hi_p - lo_p
            if rng >= config.IMPULSE_MIN_ATR * atr_val:
                best = (hi_p, lo_p, l)
                break
    return best


def _fib_levels(start: float, end: float, direction: str) -> dict:
    """Retracement levels of the impulse (start->end)."""
    diff = end - start
    if direction == "long":  # start=low, end=high; retrace pulls price down
        return {
            "0.5": end - 0.5 * diff,
            "0.618": end - 0.618 * diff,
            "0.786": end - 0.786 * diff,
            "ext_1.272": end + 0.272 * diff,
        }
    else:  # start=high, end=low; retrace pulls price up
        diff = start - end
        return {
            "0.5": end + 0.5 * diff,
            "0.618": end + 0.618 * diff,
            "0.786": end + 0.786 * diff,
            "ext_1.272": end - 0.272 * diff,
        }


def _retrace_ratio(price: float, start: float, end: float, direction: str) -> float:
    rng = (end - start) if direction == "long" else (start - end)
    if rng == 0:
        return 0.0
    if direction == "long":
        return (end - price) / rng
    return (price - end) / rng


# ----------------------------------------------------------------------------
# Main evaluation
# ----------------------------------------------------------------------------
def evaluate(symbol: str, htf: pd.DataFrame, dtf: pd.DataFrame, ltf: pd.DataFrame,
             regime: dict) -> dict | None:
    """Run whichever machine matches the regime. Returns a signal dict or None."""
    if len(htf) < config.EMA_SLOW + 10 or len(ltf) < 30 or len(dtf) < config.EMA_SLOW + 5:
        return None

    machine = "long" if regime.get("regime") == "BULL" else \
              "short" if regime.get("regime") == "BEAR" else None
    if machine is None:
        return None

    # ---- HTF indicators ----
    h_close = htf["close"]
    ema50 = indicators.ema(h_close, config.EMA_FAST)
    ema200 = indicators.ema(h_close, config.EMA_SLOW)
    h_rsi = indicators.rsi(h_close, config.RSI_LEN)
    h_atr = indicators.atr(htf, config.ATR_LEN)
    ad = indicators.ad_line(htf)
    sar = indicators.parabolic_sar(htf)

    price = float(h_close.iloc[-1])
    atr_val = float(h_atr.iloc[-1])
    d_close = dtf["close"]
    d_ema200 = indicators.ema(d_close, config.EMA_SLOW)

    checklist: list[dict] = []

    def check(name: str, ok: bool, detail: str = ""):
        checklist.append({"rule": name, "ok": bool(ok), "detail": detail})
        return ok

    impulse = _latest_impulse(htf, atr_val, machine)
    if impulse is None:
        return None
    start_p, end_p, _end_i = impulse
    fib = _fib_levels(start_p, end_p, machine)
    ratio = _retrace_ratio(price, start_p, end_p, machine)

    if machine == "long":
        gz = check("Golden zone 0.5-0.618 (impuls naik 4H)",
                   config.FIB_ZONE_LO <= ratio <= config.FIB_ZONE_HI,
                   f"retrace={ratio:.3f}")
        check("Tidak batal (< 0.786)", ratio < config.FIB_INVALID, f"retrace={ratio:.3f}")
        check("close > EMA200 4H & EMA50 > EMA200 4H",
              price > ema200.iloc[-1] and ema50.iloc[-1] > ema200.iloc[-1])
        check("close > EMA200 1D", d_close.iloc[-1] > d_ema200.iloc[-1])
        check("RSI 4H > 50", h_rsi.iloc[-1] > 50, f"rsi={h_rsi.iloc[-1]:.1f}")
        ad_required = bool(tuning.get("require_ad", True))
        check("A/D Line naik (akumulasi)",
              indicators.slope_rising(ad, 3) or not ad_required,
              "" if ad_required else "tidak diwajibkan (tuning)")
        is_friday = datetime.now(timezone.utc).weekday() == 4
        check("Skip Jumat", not (config.SKIP_FRIDAY_LONG and is_friday),
              "hari Jumat" if is_friday else "")
    else:
        gz = check("Golden zone 0.5-0.618 (impuls turun 4H)",
                   config.FIB_ZONE_LO <= ratio <= config.FIB_ZONE_HI,
                   f"retrace={ratio:.3f}")
        check("Tidak batal (< 0.786)", ratio < config.FIB_INVALID, f"retrace={ratio:.3f}")
        check("close < EMA200 4H & EMA50 < EMA200 4H",
              price < ema200.iloc[-1] and ema50.iloc[-1] < ema200.iloc[-1])
        check("RSI 4H < 50", h_rsi.iloc[-1] < 50, f"rsi={h_rsi.iloc[-1]:.1f}")
        # USDT.D heading to (or at) resistance is enough — the regime already
        # decided SHORT from USDT.D, so accept "menuju resistance" too.
        usdtd_short_ok = bool(regime.get("usdtd_at_resistance") or regime.get("usdtd_bias") == "SHORT")
        check("USDT.D menuju/di resistance", usdtd_short_ok,
              f"{regime.get('usdtd_target')} · pos={regime.get('usdtd_pos')}")
        check("Parabolic SAR (close < SAR 4H)", price < sar.iloc[-1])

    htf_ok = all(c["ok"] for c in checklist)

    # ---- LTF trigger (Section B.8 / C.6) ----
    l_close = ltf["close"]
    l_rsi = indicators.rsi(l_close, config.RSI_LEN)
    l_obv = indicators.obv(ltf)
    last = ltf.iloc[-1]
    cur_rsi = float(l_rsi.iloc[-1])

    state = "WATCHING"
    trigger = []
    if machine == "long":
        armed = config.LONG_ARM_RSI[0] <= cur_rsi <= config.LONG_ARM_RSI[1]
        green = last["close"] > last["open"]
        mini_bos = last["close"] > ltf["high"].iloc[-2]
        rsi_up = l_rsi.iloc[-1] > l_rsi.iloc[-2]
        obv_up = indicators.slope_rising(l_obv, 2)
        trigger = [
            {"rule": "ARM: RSI 30-50", "ok": armed, "detail": f"rsi={cur_rsi:.1f}"},
            {"rule": "Candle hijau", "ok": bool(green)},
            {"rule": "Mini-BOS (close>high[1])", "ok": bool(mini_bos)},
            {"rule": "RSI naik", "ok": bool(rsi_up)},
            {"rule": "OBV naik", "ok": bool(obv_up)},
        ]
        confirm = green and mini_bos and rsi_up and obv_up
    else:
        armed = config.SHORT_ARM_RSI[0] <= cur_rsi <= config.SHORT_ARM_RSI[1]
        red = last["close"] < last["open"]
        mini_bos = last["close"] < ltf["low"].iloc[-2]
        rsi_dn = l_rsi.iloc[-1] < l_rsi.iloc[-2]
        obv_dn = not indicators.slope_rising(l_obv, 2)
        trigger = [
            {"rule": "ARM: RSI 50-70", "ok": armed, "detail": f"rsi={cur_rsi:.1f}"},
            {"rule": "Candle merah", "ok": bool(red)},
            {"rule": "Mini-BOS turun (close<low[1])", "ok": bool(mini_bos)},
            {"rule": "RSI turun", "ok": bool(rsi_dn)},
            {"rule": "OBV turun", "ok": bool(obv_dn)},
        ]
        confirm = red and mini_bos and rsi_dn and obv_dn

    if htf_ok and gz:
        if confirm and armed:
            state = "ENTRY"
        elif armed:
            state = "ARMED"

    # feature signature for the learning engine
    features = {
        "machine": machine,
        "regime": regime.get("regime"),
        "fib_bucket": "0.5-0.55" if ratio < 0.55 else "0.55-0.618" if ratio <= 0.618 else "deep",
        "rsi_htf_bucket": _bucket(float(h_rsi.iloc[-1]), [40, 50, 60, 70]),
        "rsi_ltf_bucket": _bucket(cur_rsi, [30, 40, 50, 60, 70]),
        "dow": datetime.now(timezone.utc).weekday(),
        "usdtd_pos_bucket": _bucket((regime.get("usdtd_pos") or 0.5) * 100, [30, 50, 70, 85]),
        "ad_rising": bool(indicators.slope_rising(ad, 3)) if machine == "long" else None,
        "sar_confirm": bool(price < sar.iloc[-1]) if machine == "short" else None,
    }

    # Last 40 HTF candles for the card chart: [ts, open, high, low, close].
    tail = htf.tail(40)
    candles = [
        [int(ts.timestamp()), float(o), float(h), float(l), float(c)]
        for ts, o, h, l, c in zip(
            tail.index, tail["open"], tail["high"], tail["low"], tail["close"]
        )
    ]

    return {
        "symbol": symbol,
        "machine": machine,
        "direction": "LONG" if machine == "long" else "SHORT",
        "state": state,
        "price": price,
        "atr": atr_val,
        "impulse_start": start_p,
        "impulse_end": end_p,
        "retrace_ratio": round(ratio, 3),
        "fib": {k: round(v, 8) for k, v in fib.items()},
        "checklist": checklist,
        "trigger": trigger,
        "htf_ok": htf_ok,
        "golden_zone": bool(gz),
        "features": features,
        "candles": candles,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def _bucket(value: float, edges: list[float]) -> str:
    prev = "-inf"
    for e in edges:
        if value < e:
            return f"{prev}-{e}"
        prev = str(e)
    return f"{prev}+"
