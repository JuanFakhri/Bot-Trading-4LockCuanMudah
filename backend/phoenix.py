"""Phoenix Hybrid — the LONG machine.

The bot runs TWO machines, chosen by multi-timeframe trend alignment
(see ``strategy_smc.evaluate`` — the router):

    LONG  -> Phoenix Hybrid  (this file)
    SHORT -> classic SMC      (``strategy_smc``)

Why: a 3-year backtest showed the plain SMC *long* machine loses money (it kept
buying EMA-aligned dips that never resumed), while SMC *short* is the real edge.
Rather than switch longs off entirely, the long side now runs the Phoenix
Hybrid — two independent entry engines that only fire in a BULL regime:

    Engine 1  ``mesin_fib_long``      pull-back into the 0.382-0.618 golden zone
                                      of a >= 2.5 ATR impulse; needs 2 of 3
                                      triggers (micro-BOS / RSI turn / volume).
    Engine 2  ``mesin_breakout_long`` close breaks the 20-bar high on > 1.5x
                                      volume with RSI(4H) > 55 and ATR(1H) > 0.5%.

Exits (``_manage_phoenix``): SL 0.8 ATR beyond the swing (capped at 6%), take
50% at +1R and move the stop to breakeven, let the runner ride to +2R or an
EMA20 trail, and a 12-bar time-stop trims a trade that goes nowhere. Everything
is measured in R (risk multiples) so it is sizing-independent and directly
comparable with the SMC short machine.

The live twin (``evaluate_long``) scores the latest closed 1H bar; the backtest
twin (``backtest_symbol_long``) walks every bar. Both share ``_prep`` (the
indicator arrays) and ``_long_entry`` (the per-bar decision) so the live signal
and the backtest can never drift apart.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import config, indicators


# --------------------------------------------------------------------------
# Shared indicator prep
# --------------------------------------------------------------------------
def _swing_arrays(h: np.ndarray, l: np.ndarray, piv_hi: np.ndarray,
                  piv_lo: np.ndarray, pl: int):
    """Per-bar 'last confirmed swing high / low as known at bar i'. A pivot at
    bar j is only *confirmed* pl bars later, so it becomes visible at j+pl —
    exactly how ``smc_backtester`` tracks swings inside its loop."""
    n = len(h)
    swH = np.full(n, np.nan)
    swL = np.full(n, np.nan)
    cur_h = cur_l = np.nan
    for i in range(n):
        j = i - pl
        if j >= 0:
            if piv_hi[j]:
                cur_h = h[j]
            if piv_lo[j]:
                cur_l = l[j]
        swH[i] = cur_h
        swL[i] = cur_l
    return swH, swL


def _align(series: pd.Series, idx: pd.Index) -> np.ndarray:
    return series.reindex(idx, method="ffill").to_numpy()


def _prep(htf: pd.DataFrame, dtf: pd.DataFrame, ltf: pd.DataFrame) -> dict:
    """Compute every array Phoenix needs on the 1H timeframe (plus the aligned
    higher-TF trend/RSI). Returned dict is consumed by ``_long_entry``."""
    o = ltf["open"].to_numpy(); h = ltf["high"].to_numpy()
    l = ltf["low"].to_numpy(); c = ltf["close"].to_numpy()
    v = ltf["volume"].to_numpy()
    ts = ltf.index
    n = len(ltf)

    ema20 = indicators.ema(ltf["close"], 20).to_numpy()
    ema50 = indicators.ema(ltf["close"], config.EMA_FAST).to_numpy()
    ema200 = indicators.ema(ltf["close"], config.EMA_SLOW).to_numpy()
    rsi1 = indicators.rsi(ltf["close"], config.RSI_LEN).to_numpy()
    atr = indicators.atr(ltf, config.ATR_LEN).to_numpy()
    vsma = ltf["volume"].rolling(20, min_periods=5).mean().to_numpy()
    adx = indicators.adx(ltf, 14).to_numpy()
    piv_hi, piv_lo = indicators.find_pivots(ltf, config.PIVOT_LEN)
    swH, swL = _swing_arrays(h, l, piv_hi.to_numpy(), piv_lo.to_numpy(), config.PIVOT_LEN)

    # 20-bar breakout reference (prior high, current bar excluded)
    hh20 = pd.Series(h).rolling(config.PHX_BRK_LOOKBACK, min_periods=config.PHX_BRK_LOOKBACK
                                ).max().shift(1).to_numpy()

    # higher-TF trend, aligned to the 1H index
    d_bull = _align((indicators.ema(dtf["close"], config.EMA_FAST)
                     > indicators.ema(dtf["close"], config.EMA_SLOW)).astype(float), ts)
    h4_bull = _align((indicators.ema(htf["close"], config.EMA_FAST)
                      > indicators.ema(htf["close"], config.EMA_SLOW)).astype(float), ts)
    h4_rsi = _align(indicators.rsi(htf["close"], config.RSI_LEN), ts)
    h1_bull = (ema50 > ema200).astype(float)

    return {
        "o": o, "h": h, "l": l, "c": c, "v": v, "ts": ts, "n": n,
        "ema20": ema20, "ema50": ema50, "ema200": ema200, "rsi1": rsi1,
        "atr": atr, "vsma": vsma, "adx": adx, "swH": swH, "swL": swL, "hh20": hh20,
        "d_bull": d_bull, "h4_bull": h4_bull, "h4_rsi": h4_rsi, "h1_bull": h1_bull,
        "piv_hi": piv_hi.to_numpy(), "piv_lo": piv_lo.to_numpy(),
    }


# --------------------------------------------------------------------------
# The two entry engines (per-bar, stateless)
# --------------------------------------------------------------------------
def mesin_fib_long(A: dict, i: int):
    """Engine 1 — FIB retrace. Returns (fired, ratio, triggers) for bar i."""
    swH, swL = A["swH"][i], A["swL"][i]
    atr = A["atr"][i]
    c, h, ema200, rsi1, v, vsma = (A["c"], A["h"], A["ema200"][i],
                                   A["rsi1"], A["v"][i], A["vsma"][i])
    if not (np.isfinite(swH) and np.isfinite(swL) and (swH - swL) > 0
            and np.isfinite(atr) and atr > 0):
        return False, np.nan, 0
    impulse_ok = (swH - swL) >= config.PHX_FIB_IMPULSE_ATR * atr
    ratio = (swH - c[i]) / (swH - swL)
    in_zone = config.PHX_FIB_ZONE_LO <= ratio <= config.PHX_FIB_ZONE_HI
    if not (impulse_ok and in_zone and c[i] > ema200):
        return False, ratio, 0
    # 2-of-3 confirmation triggers
    t_bos = c[i] > h[i - 1]                                   # micro structure break
    t_rsi = rsi1[i] > rsi1[i - 1] and rsi1[i - 1] < 50        # RSI turning up
    t_vol = np.isfinite(vsma) and v > vsma                    # volume rising
    triggers = int(t_bos) + int(t_rsi) + int(t_vol)
    return triggers >= config.PHX_FIB_CONFIRM_MIN, ratio, triggers


def mesin_breakout_long(A: dict, i: int):
    """Engine 2 — momentum breakout. Returns fired (bool) for bar i."""
    c, hh20, v, vsma = A["c"][i], A["hh20"][i], A["v"][i], A["vsma"][i]
    atr, ema200, h4_rsi = A["atr"][i], A["ema200"][i], A["h4_rsi"][i]
    if not (np.isfinite(hh20) and np.isfinite(vsma) and np.isfinite(atr) and c > 0):
        return False
    atr_pct = atr / c * 100
    return bool(c > hh20 and v > config.PHX_BRK_VOL_MULT * vsma
                and h4_rsi > config.PHX_BRK_RSI and atr_pct > config.PHX_BRK_ATR_MIN
                and c > ema200)


def _live_engines() -> set:
    """Which long engines are enabled live (config-driven; breakout-only by
    default — the fib engine lost money over 3y)."""
    eng = set()
    if config.PHX_ENGINE_BREAKOUT:
        eng.add("breakout")
    if config.PHX_ENGINE_FIB:
        eng.add("fib")
    return eng


def _long_entry(A: dict, i: int, engines: set | None = None):
    """Run the enabled LONG engines at bar i. Returns an entry dict or None.
    Breakout takes priority when both fire (momentum leads); otherwise fib."""
    if engines is None:
        engines = _live_engines()
    c, l, atr = A["c"][i], A["l"][i], A["atr"][i]
    if not (np.isfinite(atr) and atr > 0 and c > 0):
        return None

    brk = mesin_breakout_long(A, i) if "breakout" in engines else False
    if "fib" in engines:
        fib_fired, ratio, triggers = mesin_fib_long(A, i)
    else:
        fib_fired, ratio, triggers = False, float("nan"), 0
    if not brk and not fib_fired:
        return None
    engine = "breakout" if brk else "fib"

    # SL: 0.8 ATR beyond the protective low, capped at SL_CAP_PCT
    swL = A["swL"][i]
    base_low = swL if (np.isfinite(swL) and swL < c) else l
    base_low = min(base_low, l)
    sl = max(base_low - config.PHX_SL_ATR * atr, c * (1 - config.SL_CAP_PCT))
    risk = c - sl
    if risk <= 0:
        return None
    tp1, tp2, tp3 = c + risk, c + 2 * risk, c + 3 * risk

    atr_pct = atr / c * 100
    vol_conf = np.isfinite(A["vsma"][i]) and A["v"][i] > config.PHX_BRK_VOL_MULT * A["vsma"][i]
    score = _phoenix_score(engine, A, i, ratio, triggers)
    return {
        "engine": engine, "entry": float(c), "sl": float(sl),
        "tp1": float(tp1), "tp2": float(tp2), "tp3": float(tp3), "risk": float(risk),
        "ratio": float(ratio) if np.isfinite(ratio) else float("nan"),
        "triggers": int(triggers), "atr_pct": float(atr_pct),
        "vol_conf": bool(vol_conf), "score": int(score),
    }


def _phoenix_score(engine: str, A: dict, i: int, ratio: float, triggers: int) -> int:
    """A 0-100 'Skor Setup' for the UI badge (Phoenix fires on the engine
    booleans, not on this score — it is descriptive quality, not the gate)."""
    c = A["c"][i]
    s = 60 if engine == "breakout" else 40
    if c > A["ema200"][i]:
        s += 10
    if A["h4_rsi"][i] > config.PHX_BRK_RSI:
        s += 10
    atr_pct = A["atr"][i] / c * 100 if c else 0
    if config.PHX_BRK_ATR_MIN < atr_pct < config.SMC_ATR_MAX:
        s += 10
    if np.isfinite(A["vsma"][i]) and A["v"][i] > config.PHX_BRK_VOL_MULT * A["vsma"][i]:
        s += 10
    if engine == "fib":
        s += 5 * max(0, triggers - 1)          # extra trigger beyond the required 2
    return int(min(100, s))


def _features(A: dict, i: int, e: dict) -> dict:
    """Learning signature — same KEYS as the SMC machine so lessons interoperate."""
    ratio = e["ratio"]
    if e["engine"] == "breakout":
        fib_bucket = "breakout"
    elif np.isfinite(ratio):
        fib_bucket = ("0.382-0.5" if ratio < 0.5 else "0.5-0.618"
                      if ratio <= 0.618 else "deep")
    else:
        fib_bucket = "na"
    h4_rsi = A["h4_rsi"][i]
    return {
        "machine": "long", "regime": "BULL",
        "fib_bucket": fib_bucket,
        "rsi_htf_bucket": "hi" if h4_rsi > 55 else "mid" if h4_rsi > 45 else "lo",
        "rsi_ltf_bucket": "na", "dow": int(A["ts"][i].weekday()),
        "usdtd_pos_bucket": "na",
        "score_bucket": "85+" if e["score"] >= 85 else "70-85" if e["score"] >= 70 else "lo",
        "ad_rising": bool(e["vol_conf"]),      # long coarse-signature feature
        "sar_confirm": None,
        "engine": e["engine"],                 # extra context (ignored by signatures)
    }


# --------------------------------------------------------------------------
# Exit management (long only)
# --------------------------------------------------------------------------
def _manage_phoenix(pos, bar_high, bar_low, close, ema20, bars_held):
    """Phoenix exits. Returns the final blended R when the position fully
    closes, else None. Long only (Phoenix is the long machine)."""
    entry, risk = pos["entry"], pos["risk"] or 1e-9

    # full / remaining stop-out
    if bar_low <= pos["stop"]:
        r_stop = (pos["stop"] - entry) / risk
        pos["realized"] += pos["rem"] * r_stop
        return pos["realized"]

    # TP1: bank 50% at +1R and pull the stop to breakeven
    if not pos["tp1_hit"] and bar_high >= pos["tp1"]:
        pos["realized"] += 0.5 * 1.0
        pos["rem"] -= 0.5
        pos["tp1_hit"] = True
        pos["stop"] = entry * (1 + config.BE_BUFFER_PCT)

    if pos["tp1_hit"]:
        # runner banks the rest at +2R, else trails the EMA20
        if bar_high >= pos["tp2"]:
            pos["realized"] += pos["rem"] * 2.0
            return pos["realized"]
        if close < ema20:
            pos["realized"] += pos["rem"] * ((close - entry) / risk)
            return pos["realized"]
    else:
        # time-stop: trim half of a trade that has gone nowhere after N bars
        if not pos.get("timed") and bars_held >= config.PHX_TIME_STOP_BARS:
            curr = (close - entry) / risk
            if curr < 0.5:
                pos["realized"] += 0.5 * curr
                pos["rem"] -= 0.5
                pos["timed"] = True
    return None


# --------------------------------------------------------------------------
# Backtest twin — walk every 1H bar
# --------------------------------------------------------------------------
def backtest_symbol_long(symbol, htf, dtf, ltf, usdtd_daily=None,
                         btcd_dir_daily=None, params=None) -> list[dict]:
    """Phoenix LONG trades for one symbol. Signature mirrors
    ``smc_backtester.backtest_symbol_smc`` so the runner treats both the same;
    macro/usdtd args are accepted for interface parity (Phoenix long does not
    use them). Only fires when the 1D+4H+1H trend is aligned bull (#19)."""
    params = params or {}
    if ltf is None or len(ltf) < 250 or len(htf) < config.EMA_SLOW + 30:
        return []
    engines = set(params["engines"]) if params.get("engines") else None   # None = live default

    A = _prep(htf, dtf, ltf)
    c, h, l = A["c"], A["h"], A["l"]
    ema20, ts, n = A["ema20"], A["ts"], A["n"]

    trades: list[dict] = []
    pos = None
    entry_i = -1
    cooldown_until = -1

    for i in range(210, n):
        # ---- manage an open position ----
        if pos is not None:
            done = _manage_phoenix(pos, h[i], l[i], c[i], ema20[i], i - entry_i)
            if done is not None:
                pos.update(outcome=("WIN" if done > 0.05 else "LOSS" if done < -0.05 else "BE"),
                           r=round(done, 3), exit_price=float(c[i]),
                           exit_ts=ts[i].isoformat())
                trades.append(pos)
                cooldown_until = i + config.PHX_COOLDOWN_BARS
                pos = None
            continue
        if i < cooldown_until:
            continue

        # ---- LONG only, and only in an aligned BULL regime ----
        if not (A["d_bull"][i] > 0 and A["h4_bull"][i] > 0 and A["h1_bull"][i] > 0):
            continue

        e = _long_entry(A, i, engines)
        if e is None:
            continue

        pos = {
            "symbol": symbol, "direction": "LONG", "machine": "long",
            "engine": e["engine"], "entry": e["entry"], "sl": e["sl"],
            "tp1": e["tp1"], "tp2": e["tp2"], "tp3": e["tp3"],
            "rr": round(abs(e["tp2"] - e["entry"]) / e["risk"], 2), "risk": e["risk"],
            "score": e["score"], "tp1_hit": False, "rem": 1.0, "realized": 0.0,
            "stop": e["sl"], "tp_source": "phoenix",
            "entry_ts": ts[i].isoformat(), "features": _features(A, i, e),
        }
        entry_i = i

    return trades


# --------------------------------------------------------------------------
# Live twin — score the latest closed 1H bar
# --------------------------------------------------------------------------
def evaluate_long(symbol: str, htf: pd.DataFrame, dtf: pd.DataFrame,
                  ltf: pd.DataFrame, regime: dict) -> dict | None:
    """Live Phoenix LONG signal for the latest closed 1H bar. Returns the same
    dict shape the SMC machine emits so the engine / UI consume it unchanged."""
    if len(ltf) < 250 or len(htf) < config.EMA_SLOW + 30 or len(dtf) < config.EMA_SLOW + 5:
        return None
    A = _prep(htf, dtf, ltf)
    i = A["n"] - 1
    c = A["c"]
    price = float(c[i])
    atr_val = float(A["atr"][i])

    # trend must be aligned bull (the router already picked "long", re-affirm)
    if not (A["d_bull"][i] > 0 and A["h4_bull"][i] > 0 and A["h1_bull"][i] > 0):
        return None

    engines = _live_engines()
    e = _long_entry(A, i, engines)
    fired = e is not None
    # even without a fire we can show an informative (ARMED/WATCHING) card
    fib_fired, ratio, triggers = mesin_fib_long(A, i) if "fib" in engines else (False, float("nan"), 0)
    brk = mesin_breakout_long(A, i) if "breakout" in engines else False

    if not fired:
        # build a "watching" descriptor from whichever engine is closest
        engine = "breakout" if brk else "fib"
        swH, swL = A["swH"][i], A["swL"][i]
        if np.isfinite(swH) and np.isfinite(swL) and swL < price:
            sl = max(swL - config.PHX_SL_ATR * atr_val, price * (1 - config.SL_CAP_PCT))
        else:
            sl = price * (1 - config.SL_CAP_PCT)
        risk = max(price - sl, 1e-9)
        e = {"engine": engine, "entry": price, "sl": sl,
             "tp1": price + risk, "tp2": price + 2 * risk, "tp3": price + 3 * risk,
             "risk": risk, "ratio": ratio, "triggers": triggers, "atr_pct": atr_val / price * 100,
             "vol_conf": bool(np.isfinite(A["vsma"][i]) and A["v"][i] > config.PHX_BRK_VOL_MULT * A["vsma"][i]),
             "score": _phoenix_score(engine, A, i, ratio, triggers)}

    entry, sl = e["entry"], e["sl"]
    tp1, tp2, tp3 = e["tp1"], e["tp2"], e["tp3"]
    risk = e["risk"]
    be = entry * (1 + config.BE_BUFFER_PCT)
    state = "ENTRY" if fired else ("ARMED" if (fib_fired or brk or e["score"] >= 60) else "WATCHING")

    atr_pct = e["atr_pct"]
    checklist = [
        {"rule": "Trend align 1D+4H+1H", "ok": True, "detail": "bull"},
        {"rule": "Harga > EMA200 1H", "ok": bool(price > A["ema200"][i])},
        {"rule": "RSI 4H > 55", "ok": bool(A["h4_rsi"][i] > config.PHX_BRK_RSI),
         "detail": f"{A['h4_rsi'][i]:.0f}"},
        {"rule": f"ATR 1H > {config.PHX_BRK_ATR_MIN}%", "ok": bool(atr_pct > config.PHX_BRK_ATR_MIN),
         "detail": f"{atr_pct:.2f}%"},
        {"rule": "Volume > 1.5x SMA20", "ok": bool(e["vol_conf"])},
    ]
    trigger = [
        {"rule": "Mesin FIB retrace (zona 0.382-0.618)", "ok": bool(fib_fired),
         "detail": f"ret={e['ratio']:.3f}" if np.isfinite(e["ratio"]) else "-"},
        {"rule": "  ↳ 2 dari 3 konfirmasi", "ok": bool(e["triggers"] >= config.PHX_FIB_CONFIRM_MIN),
         "detail": f"{e['triggers']}/3"},
        {"rule": "Mesin Breakout (tembus high 20-bar)", "ok": bool(brk)},
        {"rule": f"Skor Setup >= 60", "ok": bool(e["score"] >= 60), "detail": f"score={e['score']}"},
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
    features = _features(A, i, e)

    highs_arr, lows_arr = A["h"], A["l"]
    piv_hi, piv_lo = A["piv_hi"], A["piv_lo"]
    swing_highs = sorted(float(highs_arr[k]) for k in range(A["n"])
                         if piv_hi[k] and highs_arr[k] > price)
    swing_lows = sorted((float(lows_arr[k]) for k in range(A["n"])
                         if piv_lo[k] and lows_arr[k] < price), reverse=True)
    tail = htf.tail(40)
    candles = [[int(t.timestamp()), float(oo), float(hh), float(ll), float(cc)]
               for t, oo, hh, ll, cc in zip(tail.index, tail["open"], tail["high"],
                                            tail["low"], tail["close"])]

    return {
        "symbol": symbol, "machine": "long", "direction": "LONG",
        "state": state, "price": price, "atr": atr_val,
        "engine": e["engine"], "strategy": "phoenix",
        "impulse_start": float(A["swL"][i]) if np.isfinite(A["swL"][i]) else price,
        "impulse_end": float(A["swH"][i]) if np.isfinite(A["swH"][i]) else price,
        "retrace_ratio": round(e["ratio"], 3) if np.isfinite(e["ratio"]) else None,
        "score": e["score"],
        "fib": {"0.5": round((A["swH"][i] + A["swL"][i]) / 2, 8)
                if np.isfinite(A["swH"][i]) and np.isfinite(A["swL"][i]) else price,
                "ext_1.272": round(tp3, 8)},
        "swing_highs": [round(x, 8) for x in swing_highs[:6]],
        "swing_lows": [round(x, 8) for x in swing_lows[:6]],
        "checklist": checklist, "trigger": trigger,
        "htf_ok": bool(fired), "golden_zone": bool(fib_fired),
        "features": features, "plan": plan, "candles": candles,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
