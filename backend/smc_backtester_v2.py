"""SMC v2 — stricter confluence backtester (EXPERIMENTAL, backtest only).

Implements the user's v2 proposal on top of v1 so we can measure whether the
extra strictness actually improves the edge BEFORE touching the live bot:

  1  Double-BOS confirmation (sweep -> CHOCH -> BOS1 -> BOS2 -> retest entry)
  2  EMA50 slope gate (flat trend = no trade)
  3  ADX > 25 AND rising
  4  ATR expansion (ATR > ATR-SMA20)
  5  Volume expansion (Vol > 1.5x SMA20)
  6/15  New AI-Score weights, entry gate >= 75
  7  RSI momentum zone (LONG 55-70, SHORT 30-45; no exhaustion)
  8  BTC.D + USDT.D + BTC + ETH direction (4 macro confirmations, scored)
  9  ETH trend correlation (ETH EMA50>EMA200 for longs)
  10 Order-block retest + candlestick confirmation (engulf / pin / strong close)
  11 Session: London-open & NY-open first 2h only
  12 Exit: TP1 1R/20%, TP2 2R/30%, runner 50% trailed by Chandelier (ATR) exit
  14 Market-regime filter (ADX-based RANGE => trend setups off)
  16 Anti-fake retest (skip retest candle that closes hard against the trend)
  17 USDT.D correlation is tested separately (see usdtd_correlation()).

Deferred: #13 news filter (no free historical economic calendar).

This module NEVER feeds the live engine; run it via BACKTEST_VARIANT=v2.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, indicators
from .smc_backtester import summarize  # reuse the shared metrics aggregator

# ---- AI Score v2 weights (max 115); entry gate default 75 (#15) ----
W = {"mtf": 15, "sweep": 10, "choch": 10, "bos1": 10, "bos2": 10, "golden": 10,
     "ob": 10, "fvg": 5, "ema_slope": 5, "adx_rising": 5, "atr_exp": 5,
     "vol_exp": 5, "btcd": 5, "usdtd": 5, "eth": 5}


def _align(series, idx, fill="ffill"):
    if series is None:
        return None
    return series.reindex(idx, method="ffill")


def _bull_candle(o, h, l, c):
    """Bullish confirmation: engulfing OR hammer/pin OR strong close."""
    rng = (h - l) or 1e-9
    body = abs(c - o)
    strong_close = (c - l) / rng >= 0.7 and c > o
    lower_wick = min(o, c) - l
    hammer = lower_wick >= 2 * body and (c - l) / rng >= 0.6
    return bool(strong_close or hammer)


def _bear_candle(o, h, l, c):
    rng = (h - l) or 1e-9
    body = abs(c - o)
    strong_close = (h - c) / rng >= 0.7 and c < o
    upper_wick = h - max(o, c)
    shooting = upper_wick >= 2 * body and (h - c) / rng >= 0.6
    return bool(strong_close or shooting)


def backtest_symbol_v2(symbol, htf, dtf, ltf, macro, params=None) -> list[dict]:
    params = params or {}
    score_th = float(params.get("score_th", 75))
    vol_mult = float(params.get("vol_mult", 1.5))
    slope_min = float(params.get("ema_slope_min", 0.002))   # 0.2% over 5 bars ~ "angle"
    setup_timeout = int(params.get("setup_timeout", 30))    # bars from sweep to entry
    adx_range = float(params.get("adx_range", 20))          # ADX below this = RANGE

    diag = params.get("diag")   # optional dict for instrumentation
    if ltf is None or len(ltf) < 260 or len(htf) < config.EMA_SLOW + 30:
        return []

    o = ltf["open"].to_numpy(); h = ltf["high"].to_numpy()
    l = ltf["low"].to_numpy(); c = ltf["close"].to_numpy()
    v = ltf["volume"].to_numpy()
    ts = ltf.index
    n = len(ltf)
    pl = config.PIVOT_LEN

    ema50 = indicators.ema(ltf["close"], config.EMA_FAST).to_numpy()
    ema200 = indicators.ema(ltf["close"], config.EMA_SLOW).to_numpy()
    rsi = indicators.rsi(ltf["close"], config.RSI_LEN).to_numpy()
    atr = indicators.atr(ltf, config.ATR_LEN).to_numpy()
    atr_sma = pd.Series(atr).rolling(20, min_periods=5).mean().to_numpy()
    adx = indicators.adx(ltf, 14).to_numpy()
    vsma = ltf["volume"].rolling(20, min_periods=5).mean().to_numpy()
    piv_hi, piv_lo = indicators.find_pivots(ltf, pl)
    piv_hi = piv_hi.to_numpy(); piv_lo = piv_lo.to_numpy()
    roll_lo10 = pd.Series(l).rolling(10, min_periods=3).min().shift(1).to_numpy()
    roll_hi10 = pd.Series(h).rolling(10, min_periods=3).max().shift(1).to_numpy()
    # Chandelier exit levels (#12)
    ch_long = (pd.Series(h).rolling(22, min_periods=5).max() - 3 * pd.Series(atr)).to_numpy()
    ch_short = (pd.Series(l).rolling(22, min_periods=5).min() + 3 * pd.Series(atr)).to_numpy()

    # higher-TF bias aligned to 1H
    d_bull = _align((indicators.ema(dtf["close"], config.EMA_FAST)
                     > indicators.ema(dtf["close"], config.EMA_SLOW)).astype(float), ts).to_numpy()
    h4_bull = _align((indicators.ema(htf["close"], config.EMA_FAST)
                      > indicators.ema(htf["close"], config.EMA_SLOW)).astype(float), ts).to_numpy()

    # macro daily series aligned to 1H
    def macro_arr(key, default="STABIL"):
        s = macro.get(key)
        if s is None:
            return np.array([default] * n, dtype=object)
        return pd.Series(s).reindex(ts, method="ffill").fillna(default).to_numpy()

    usdtd = _align(macro.get("usdtd"), ts).to_numpy() if macro.get("usdtd") is not None else np.full(n, np.nan)
    usdtd_prev = _align(macro.get("usdtd"), ts).shift(120).to_numpy() if macro.get("usdtd") is not None else np.full(n, np.nan)
    btcd_dir = macro_arr("btcd_dir")
    btc_dir = macro_arr("btc_dir")
    eth_dir = macro_arr("eth_dir")
    eth_bull = _align(macro.get("eth_bull"), ts).fillna(0).to_numpy() if macro.get("eth_bull") is not None else np.ones(n)

    hours = ts.hour.to_numpy()
    # London open (07-09) + NY open (13-15) first ~2h — highest liquidity (#11)
    session = np.isin(hours, [7, 8, 13, 14])

    trades: list[dict] = []
    pos = None
    cooldown_until = -1
    # FSM per symbol
    phase = 0            # 0 need sweep,1 need choch,2 need bos1,3 need bos2,4 retest
    phase_dir = None
    phase_start = -1
    bos1_level = np.nan
    swH = swL = np.nan
    ob_lo = ob_hi = np.nan
    fvg_lo = fvg_hi = np.nan

    for i in range(210, n):
        # confirm pivots
        j = i - pl
        if j >= 0:
            if piv_hi[j]:
                swH = h[j]
            if piv_lo[j]:
                swL = l[j]
        # track latest FVG + bullish/bearish OB candle zone
        if i >= 2:
            if l[i] > h[i - 2]:
                fvg_lo, fvg_hi = h[i - 2], l[i]
            if h[i] < l[i - 2]:
                fvg_lo, fvg_hi = h[i], l[i - 2]

        # manage open position
        if pos is not None:
            done = _manage_v2(pos, h[i], l[i], c[i], ch_long[i], ch_short[i])
            if done is not None:
                pos.update(outcome=("WIN" if done > 0.05 else "LOSS" if done < -0.05 else "BE"),
                           r=round(done, 3), exit_price=c[i], exit_ts=ts[i].isoformat())
                trades.append(pos)
                cooldown_until = i + (2 if done > 0 else 8)
                pos = None
            continue
        if i < cooldown_until:
            continue
        if np.isnan(swH) or np.isnan(swL) or (swH - swL) <= 0:
            continue

        # ---- direction from multi-TF alignment + ETH trend (#9) ----
        long_ok = d_bull[i] > 0 and h4_bull[i] > 0 and ema50[i] > ema200[i] and eth_bull[i] > 0
        short_ok = d_bull[i] == 0 and h4_bull[i] == 0 and ema50[i] < ema200[i] and eth_bull[i] == 0
        machine = "long" if long_ok else "short" if short_ok else None

        # ---- regime filter (#14): ADX too low => RANGE => trend setups off ----
        if machine is None or np.isnan(adx[i]) or adx[i] < adx_range:
            phase = 0; phase_dir = None
            continue

        # reset stale setup
        if phase > 0 and (phase_dir != machine or i - phase_start > setup_timeout):
            phase = 0; phase_dir = None

        # ================= Double-BOS state machine (#1) =================
        if machine == "long":
            sweep = (not np.isnan(roll_lo10[i])) and l[i] < roll_lo10[i] and c[i] > roll_lo10[i]
            broke = c[i] > swH and c[i - 1] <= swH        # fresh close above swing high
        else:
            sweep = (not np.isnan(roll_hi10[i])) and h[i] > roll_hi10[i] and c[i] < roll_hi10[i]
            broke = c[i] < swL and c[i - 1] >= swL

        if phase == 0:
            if sweep:
                phase, phase_dir, phase_start = 1, machine, i
                if diag is not None: diag["sweep"] = diag.get("sweep", 0) + 1
        elif phase == 1:                                   # need CHOCH (first break)
            if broke:
                phase, phase_start = 2, i               # reset staleness timer per leg
                if diag is not None: diag["choch"] = diag.get("choch", 0) + 1
                # record the order-block = last opposite candle before the break
                if machine == "long" and c[i - 1] < o[i - 1]:
                    ob_lo, ob_hi = l[i - 1], h[i - 1]
                elif machine == "short" and c[i - 1] > o[i - 1]:
                    ob_lo, ob_hi = l[i - 1], h[i - 1]
        elif phase == 2:                                   # need BOS1
            if broke:
                phase, phase_start, bos1_level = 3, i, (swH if machine == "long" else swL)
                if diag is not None: diag["bos1"] = diag.get("bos1", 0) + 1
        elif phase == 3:                                   # need BOS2 (double confirm)
            second = (c[i] > swH and swH > bos1_level) if machine == "long" \
                else (c[i] < swL and swL < bos1_level)
            if second:
                phase, phase_start = 4, i
                if diag is not None: diag["bos2"] = diag.get("bos2", 0) + 1
        elif phase == 4:                                   # retest + entry
            in_ob = (not np.isnan(ob_lo)) and l[i] <= ob_hi and h[i] >= ob_lo
            in_fvg = (not np.isnan(fvg_lo)) and l[i] <= fvg_hi and h[i] >= fvg_lo
            retest = in_ob or in_fvg
            if retest:
                if diag is not None: diag["retest"] = diag.get("retest", 0) + 1
                sig = _try_entry(symbol, machine, i, o, h, l, c, v, ts, ema50, ema200,
                                 rsi, atr, atr_sma, adx, vsma, swH, swL, ob_lo, ob_hi,
                                 fvg_lo, fvg_hi, in_ob, in_fvg, usdtd, usdtd_prev,
                                 btcd_dir, btc_dir, eth_dir, session, roll_lo10, roll_hi10,
                                 vol_mult, slope_min, score_th, diag)
                if sig is not None:
                    pos = sig
                    phase = 0; phase_dir = None

    return trades


def _try_entry(symbol, machine, i, o, h, l, c, v, ts, ema50, ema200, rsi, atr,
               atr_sma, adx, vsma, swH, swL, ob_lo, ob_hi, fvg_lo, fvg_hi, in_ob,
               in_fvg, usdtd, usdtd_prev, btcd_dir, btc_dir, eth_dir, session,
               roll_lo10, roll_hi10, vol_mult, slope_min, score_th, diag=None):
    def rej(reason):
        if diag is not None:
            r = diag.setdefault("rej", {})
            r[reason] = r.get(reason, 0) + 1
        return None
    price = c[i]
    # ---- hard filters ----
    if not session[i]:                                           # #11
        return rej("session")
    # premium/discount vs swing mid
    mid = (swH + swL) / 2
    if machine == "long" and price >= mid:
        return rej("premdisc")
    if machine == "short" and price <= mid:
        return rej("premdisc")
    # EMA50 slope (#2) — "angle" proxy
    slope = (ema50[i] - ema50[i - 5]) / (abs(ema50[i - 5]) or 1e-9)
    ema_slope_ok = slope >= slope_min if machine == "long" else slope <= -slope_min
    if not ema_slope_ok:
        return rej("ema_slope")
    # ADX rising (#3)
    adx_ok = adx[i] > 25 and adx[i] > adx[i - 3]
    if not adx_ok:
        return rej("adx")
    # ATR expansion (#4)
    atr_exp = (not np.isnan(atr_sma[i])) and atr[i] > atr_sma[i]
    if not atr_exp:
        return rej("atr_exp")
    # Volume expansion (#5)
    vol_exp = (not np.isnan(vsma[i])) and v[i] > vol_mult * vsma[i]
    if not vol_exp:
        return rej("vol_exp")
    # RSI momentum zone (#7)
    if machine == "long" and not (55 <= rsi[i] <= 70):
        return rej("rsi")
    if machine == "short" and not (30 <= rsi[i] <= 45):
        return rej("rsi")
    # candlestick confirmation (#10) + anti-fake retest (#16)
    body = abs(c[i] - o[i]); rng = (h[i] - l[i]) or 1e-9
    if machine == "long":
        if not _bull_candle(o[i], h[i], l[i], c[i]):
            return rej("candle")
        if c[i] < o[i] and body / rng > 0.6:                    # #16 hard bearish retest
            return rej("antifake")
    else:
        if not _bear_candle(o[i], h[i], l[i], c[i]):
            return rej("candle")
        if c[i] > o[i] and body / rng > 0.6:
            return rej("antifake")

    # golden zone
    ratio = (swH - price) / (swH - swL) if machine == "long" else (price - swL) / (swH - swL)
    in_fib = config.FIB_ZONE_LO <= ratio <= config.FIB_ZONE_HI

    # macro confirmations (#8) — scored, not hard (USDT.D only ~365d free)
    if machine == "long":
        btcd_ok = btcd_dir[i] == "TURUN"
        usdtd_ok = (not np.isnan(usdtd[i])) and (not np.isnan(usdtd_prev[i])) and usdtd[i] < usdtd_prev[i]
        btc_ok = btc_dir[i] == "NAIK"
        eth_ok = eth_dir[i] == "NAIK"
    else:
        btcd_ok = btcd_dir[i] == "NAIK"
        usdtd_ok = (not np.isnan(usdtd[i])) and (not np.isnan(usdtd_prev[i])) and usdtd[i] > usdtd_prev[i]
        btc_ok = btc_dir[i] == "TURUN"
        eth_ok = eth_dir[i] == "TURUN"

    # ---- AI Score v2 (#15) ----
    score = (W["mtf"] + W["sweep"] + W["choch"] + W["bos1"] + W["bos2"]   # sequence reached phase 4
             + W["golden"] * in_fib + W["ob"] * in_ob + W["fvg"] * in_fvg
             + W["ema_slope"] + W["adx_rising"] + W["atr_exp"] + W["vol_exp"]
             + W["btcd"] * btcd_ok + W["usdtd"] * usdtd_ok + W["eth"] * eth_ok)
    score = int(score)
    if score < score_th:
        return rej(f"score<{int(score_th)}({score})")

    # ---- build trade: SL beyond swing +/-1 ATR (cap 6%) ----
    entry = price
    if machine == "long":
        sl = max(min(swL, entry) - atr[i], entry * (1 - config.SL_CAP_PCT))
        risk = entry - sl
        tp1, tp2 = entry + risk, entry + 2 * risk
    else:
        sl = min(max(swH, entry) + atr[i], entry * (1 + config.SL_CAP_PCT))
        risk = sl - entry
        tp1, tp2 = entry - risk, entry - 2 * risk
    if risk <= 0:
        return None

    features = {
        "machine": machine, "regime": "BULL" if machine == "long" else "BEAR",
        "fib_bucket": "0.5-0.55" if ratio < 0.55 else "0.55-0.618" if ratio <= 0.618 else "deep",
        "rsi_htf_bucket": "na", "rsi_ltf_bucket": "na", "dow": int(ts[i].weekday()),
        "usdtd_pos_bucket": "na",
        "score_bucket": "90+" if score >= 90 else "75-90" if score >= 75 else "lo",
        "ad_rising": bool(btc_ok and eth_ok) if machine == "long" else None,
        "sar_confirm": bool(btc_ok and eth_ok) if machine == "short" else None,
    }
    return {
        "symbol": symbol, "direction": "LONG" if machine == "long" else "SHORT",
        "machine": machine, "entry": float(entry), "sl": float(sl),
        "tp1": float(tp1), "tp2": float(tp2), "risk": float(risk),
        "rr": round(abs(tp2 - entry) / risk, 2), "score": score,
        "tp1_hit": False, "tp2_hit": False, "rem": 1.0, "realized": 0.0,
        "stop": float(sl), "tp_source": "smc-v2",
        "entry_ts": ts[i].isoformat(), "features": features,
    }


def _manage_v2(pos, bar_high, bar_low, close, ch_long, ch_short):
    """Exit (#12): TP1 1R close 20%, TP2 2R close 30%, runner 50% trailed by the
    Chandelier ATR stop. Returns blended R when fully closed, else None."""
    entry, risk = pos["entry"], pos["risk"] or 1e-9
    long = pos["direction"] == "LONG"

    def hit(level):
        return bar_high >= level if long else bar_low <= level

    stopped = bar_low <= pos["stop"] if long else bar_high >= pos["stop"]
    if stopped:
        r_stop = (pos["stop"] - entry) / risk if long else (entry - pos["stop"]) / risk
        pos["realized"] += pos["rem"] * r_stop
        return pos["realized"]

    if not pos["tp1_hit"] and hit(pos["tp1"]):          # +1R, close 20%
        pos["realized"] += 0.20 * 1.0
        pos["rem"] -= 0.20
        pos["tp1_hit"] = True
        pos["stop"] = entry * (1 + config.BE_BUFFER_PCT) if long else entry * (1 - config.BE_BUFFER_PCT)
    if pos["tp1_hit"] and not pos["tp2_hit"] and hit(pos["tp2"]):   # +2R, close 30%
        pos["realized"] += 0.30 * 2.0
        pos["rem"] -= 0.30
        pos["tp2_hit"] = True
    if pos["tp2_hit"]:                                   # trail runner 50% on Chandelier
        trail = ch_long if long else ch_short
        if not np.isnan(trail):
            pos["stop"] = max(pos["stop"], trail) if long else min(pos["stop"], trail)
    return None


# ---------------------------------------------------------------------------
# #17 — USDT.D correlation test (does USDT.D direction lead ALT/BTC/ETH?)
# ---------------------------------------------------------------------------
def usdtd_correlation(usdtd_daily: pd.Series, closes: dict[str, pd.Series],
                      horizon: int = 5) -> dict:
    """Empirically test the hypothesis: USDT.D falling (toward support) -> alts/
    BTC/ETH rise; USDT.D rising (toward resistance) -> they fall.

    Returns per-asset: pearson corr of daily USDT.D change vs asset change, and
    mean forward `horizon`-day return conditioned on USDT.D falling vs rising.
    """
    out = {"horizon_days": horizon, "assets": {}}
    u = usdtd_daily.dropna()
    if len(u) < 40:
        out["note"] = "USDT.D history too short (free tier ~365d)."
        return out
    u_chg = u.diff()
    for name, px in closes.items():
        p = px.copy()
        p.index = pd.to_datetime(p.index).normalize()
        p = p[~p.index.duplicated(keep="last")]
        joined = pd.concat([u.rename("u"), p.rename("p")], axis=1).dropna()
        if len(joined) < 40:
            continue
        d_u = joined["u"].diff()
        d_p = joined["p"].pct_change()
        corr = float(d_p.corr(d_u))
        fwd = joined["p"].shift(-horizon) / joined["p"] - 1.0
        falling = d_u < 0
        rising = d_u > 0
        out["assets"][name] = {
            "corr_daily": round(corr, 3),
            "fwd_ret_when_usdtd_falling_pct": round(float(fwd[falling].mean()) * 100, 2),
            "fwd_ret_when_usdtd_rising_pct": round(float(fwd[rising].mean()) * 100, 2),
            "n": int(len(joined)),
        }
    # verdict: hypothesis holds if corr negative AND falling>rising on average
    xs = out["assets"].values()
    if xs:
        neg = sum(1 for a in xs if a["corr_daily"] < -0.05)
        spread = sum(1 for a in xs if a["fwd_ret_when_usdtd_falling_pct"]
                     > a["fwd_ret_when_usdtd_rising_pct"])
        out["holds"] = bool(neg >= len(out["assets"]) / 2 and spread >= len(out["assets"]) / 2)
    return out
