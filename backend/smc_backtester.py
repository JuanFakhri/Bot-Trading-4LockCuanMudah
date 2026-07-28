"""SMC + confluence-score backtester (the "20-point" strategy).

Entry is no longer a bare fib tap. It requires a confluence of Smart-Money
concepts scored with the #15 "Setup Score" (0-100); a trade fires only when the
score clears a threshold AND a few hard filters pass. This trades far less than
the fib engine but aims for higher-quality setups.

Implemented (16 of 20): 4-stage entry (liquidity sweep -> CHOCH -> BOS -> FVG ->
OB retest), premium/discount, ADX>25, volume spike, ATR band, multi-TF trend
alignment (D/4H/1H), BTC.D & refined USDT.D, London/NY session, cooldown 8/2,
risk 1%, exit TP 30/30/40 + EMA20 trail, and the weighted Setup-Score gate.

Deferred (need data not freely available): DXY (#8), TOTAL3 (#10), news (#13).

All detection is a pragmatic heuristic — good for relative comparison, not a
perfect discretionary SMC read. Trades feed the same learning / walk-forward.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, indicators

# Setup-Score weights (#15). DXY is deferred so its 5 pts are unreachable (max 95).
W = {"ema": 10, "rsi": 5, "adx": 10, "fib": 15, "sweep": 15, "choch": 15,
     "bos": 10, "fvg": 10, "ob": 5, "btcd": 5, "usdtd": 5}


def _align(series: pd.Series, idx: pd.Index) -> np.ndarray:
    return series.reindex(idx, method="ffill").to_numpy()


def backtest_symbol_smc(symbol, htf, dtf, ltf, usdtd_daily, btcd_dir_daily,
                        params=None) -> list[dict]:
    params = params or {}
    score_th = float(params.get("score_th", 70))     # default 70 (85 trades ~0)
    atr_min = float(params.get("atr_min_pct", 0.3))
    atr_max = float(params.get("atr_max_pct", 8.0))
    use_session = bool(params.get("use_session", True))
    # allow_long/allow_short default to the LIVE machine config (short-only): a
    # 3y backtest showed the LONG machine loses money, so live runs SHORT-only.
    # Pass allow_long=True to study the long machine in isolation.
    allow_long = bool(params.get("allow_long", config.SMC_ALLOW_LONG))    # default = live
    allow_short = bool(params.get("allow_short", config.SMC_ALLOW_SHORT))
    # Macro-calendar gate (#13, previously deferred): a daily crypto-policy bias
    # from macro_news (RISK_ON = easing = bullish, RISK_OFF = tightening). When on,
    # LONGs are only taken when policy is NOT risk-off and SHORTs only when NOT
    # risk-on — a counter-policy setup becomes a NEUTRAL (no-trade). Default off so
    # the live engine is unchanged until the A/B backtest proves it helps.
    macro_gate = bool(params.get("macro_gate", False))
    macro_require_on = bool(params.get("macro_require_on", False))  # longs need RISK_ON
    macro_on_th = float(params.get("macro_on_th", 0.15))
    macro_off_th = float(params.get("macro_off_th", -0.15))
    # "Strengthen long": target the runner at the nearest real RESISTANCE (recent
    # swing-high) instead of a flat 3R, so a long banks at the right level. Longs
    # only; shorts keep the tested 3R exit. Default off (validated via OOS PF).
    long_struct_tp = bool(params.get("long_struct_tp", False))
    res_lookback = int(params.get("res_lookback", 40))
    # "Strengthen long" via conviction: demand a higher Setup Score for LONGs only
    # (weak longs were ~50% coin-flips). Defaults to score_th = no change.
    long_score_th = float(params.get("long_score_th", score_th))
    # "Strengthen long" via ENTRY quality: a LONG must be a real bullish reversal —
    # a liquidity sweep below support that reclaims AND a change-of-character / BOS —
    # not a bare EMA-aligned dip. Longs only; default off.
    long_reversal_hard = bool(params.get("long_reversal_hard", False))
    # SHORT relaxation knobs (research only): loosen the short machine to test if
    # MORE trades can come WITHOUT losing win-rate. Defaults reproduce the LIVE
    # short exactly (triple-TF bearish alignment, vol>=SMC_VOL_MULT, ADX>25).
    #   short_align: "triple" (1D+4H+1H bearish, live) | "dual_dh4" (1D+4H only,
    #                drop the 1H requirement) | "dual_h4h1" (4H+1H only, drop 1D)
    short_align = str(params.get("short_align", "triple"))
    short_vol_mult = float(params.get("short_vol_mult", config.SMC_VOL_MULT))
    short_adx_min = float(params.get("short_adx_min", 25))
    # ICT "Kill Zones" time filter (research only). Default None = keep the live
    # 07-22 UTC session window. When a list of UTC hours is passed, ONLY those
    # hours are tradeable — used to A/B test whether restricting entries to the
    # London/NY kill zones (high-liquidity windows) beats the broad session.
    kz_hours = params.get("kz_hours", None)
    # PURE ICT model (research only): ignore our composite Setup Score and the
    # non-ICT confluences (EMA/RSI/ADX/fib/USDT.D/vol/ATR). Take the trade ONLY
    # on the canonical ICT sequence — liquidity SWEEP -> Market-Structure-Shift
    # (choch) -> FVG retrace — inside the premium/discount array and kill zone.
    # This measures ICT's OWN win-rate as a standalone strategy.
    ict_pure = bool(params.get("ict_pure", False))
    # ICT "SMT divergence" filter (research only). Compare this coin to a
    # correlated reference (BTC, or ETH for BTC itself): when the two DISAGREE on
    # making a fresh higher-high (bearish SMT) / lower-low (bullish SMT), smart
    # money is likely distributing/accumulating. Confirms SHORTs on bearish SMT
    # and LONGs on bullish SMT. Needs smt_ref_high/low series; default OFF.
    smt_filter = bool(params.get("smt_filter", False))

    if ltf is None or len(ltf) < 250 or len(htf) < config.EMA_SLOW + 30:
        return []

    # ---- 1H (entry TF) indicators ----
    o = ltf["open"].to_numpy(); h = ltf["high"].to_numpy()
    l = ltf["low"].to_numpy(); c = ltf["close"].to_numpy()
    v = ltf["volume"].to_numpy()
    ema50_1 = indicators.ema(ltf["close"], config.EMA_FAST).to_numpy()
    ema200_1 = indicators.ema(ltf["close"], config.EMA_SLOW).to_numpy()
    ema20_1 = indicators.ema(ltf["close"], 20).to_numpy()
    rsi_1 = indicators.rsi(ltf["close"], config.RSI_LEN).to_numpy()
    atr_1 = indicators.atr(ltf, config.ATR_LEN).to_numpy()
    atr_sma = pd.Series(atr_1).rolling(20, min_periods=5).mean().to_numpy()   # v1.1 #4
    adx_1 = indicators.adx(ltf, 14).to_numpy()
    vsma = ltf["volume"].rolling(20, min_periods=5).mean().to_numpy()
    piv_hi, piv_lo = indicators.find_pivots(ltf, config.PIVOT_LEN)
    piv_hi = piv_hi.to_numpy(); piv_lo = piv_lo.to_numpy()
    ts = ltf.index
    n = len(ltf)
    pl_ = config.PIVOT_LEN

    # ---- higher-TF trend (aligned to 1H, last closed bar) ----
    d_bull = _align((indicators.ema(dtf["close"], config.EMA_FAST)
                     > indicators.ema(dtf["close"], config.EMA_SLOW)).astype(float), ts)
    h4_e50 = indicators.ema(htf["close"], config.EMA_FAST)
    h4_e200 = indicators.ema(htf["close"], config.EMA_SLOW)
    h4_bull = _align((h4_e50 > h4_e200).astype(float), ts)
    h4_rsi = _align(indicators.rsi(htf["close"], config.RSI_LEN), ts)
    h1_bull = (ema50_1 > ema200_1).astype(float)

    # macro aligned to 1H
    usdtd = _align(usdtd_daily, ts)
    usdtd_prev = _align(usdtd_daily.shift(5), ts)
    btcd_dir = pd.Series(btcd_dir_daily).reindex(ts, method="ffill").fillna("STABIL").to_numpy() \
        if btcd_dir_daily is not None else np.array(["STABIL"] * n)

    # macro policy bias (daily net score) aligned to 1H; 0.0 where unknown
    macro_bias_daily = params.get("macro_bias_daily")
    if macro_gate and macro_bias_daily is not None and len(macro_bias_daily):
        macro_bias = pd.Series(macro_bias_daily).reindex(ts, method="ffill").fillna(0.0).to_numpy()
    else:
        macro_bias = None
        macro_gate = False

    # session (UTC hours): London ~07-16, New York ~13-22
    hours = ts.hour.to_numpy()
    if kz_hours is not None:
        in_session = np.isin(hours, np.asarray(list(kz_hours), dtype=int))
    else:
        in_session = ((hours >= 7) & (hours < 22))

    # ---- SMT divergence vs a correlated reference (research filter) ----
    smt_bear = smt_bull = None
    if smt_filter and params.get("smt_ref_high") is not None:
        _W = 10                                     # swing window for HH/LL check
        rh = _align(params["smt_ref_high"], ts)
        rl = _align(params["smt_ref_low"], ts)

        def _hh(arr):                               # making a fresh higher-high?
            s = pd.Series(arr).rolling(_W, min_periods=_W).max()
            return (s > s.shift(_W)).to_numpy()

        def _ll(arr):                               # making a fresh lower-low?
            s = pd.Series(arr).rolling(_W, min_periods=_W).min()
            return (s < s.shift(_W)).to_numpy()

        # bearish SMT = coin & reference DISAGREE on the higher-high (a top tell);
        # bullish SMT = they disagree on the lower-low (a bottom tell).
        smt_bear = _hh(h) != _hh(rh)
        smt_bull = _ll(l) != _ll(rl)
    else:
        smt_filter = False

    # ---- running swings for SMC ----
    swH = swL = np.nan
    swHb = swLb = -1
    # last FVG bounds (bullish gap: low[i] > high[i-2]; bearish: high[i] < low[i-2])
    fvg_bull_lo = fvg_bull_hi = np.nan
    fvg_bear_lo = fvg_bear_hi = np.nan
    # ICT sequential-setup state (pure-ICT research mode only). ICT is a SEQUENCE
    # across bars — liquidity sweep, THEN a market-structure-shift, THEN a retrace
    # into the resulting FVG — so we arm each stage and fire on the retrace.
    ICT_WIN = 10                       # bars allowed between each ICT stage
    s_sweep_bar = s_mss_bar = -1       # short seq: last sweep / MSS bar index
    s_fvg_lo = s_fvg_hi = np.nan       # short seq: bear-FVG entry zone
    l_sweep_bar = l_mss_bar = -1       # long seq
    l_fvg_lo = l_fvg_hi = np.nan

    trades: list[dict] = []
    pos = None
    cooldown_until = -1
    lowest = pd.Series(l).rolling(10, min_periods=3).min().shift(1).to_numpy()
    highest = pd.Series(h).rolling(10, min_periods=3).max().shift(1).to_numpy()
    # nearest resistance (rolling swing-high) for the structure-based long TP
    res_hi = pd.Series(h).rolling(res_lookback, min_periods=res_lookback // 2).max().shift(1).to_numpy()

    for i in range(210, n):
        # confirm pivots (known pl_ bars later)
        j = i - pl_
        if j >= 0:
            if piv_hi[j]:
                swH, swHb = h[j], j
            if piv_lo[j]:
                swL, swLb = l[j], j
        # track latest FVG
        if i >= 2:
            if l[i] > h[i - 2]:
                fvg_bull_lo, fvg_bull_hi = h[i - 2], l[i]
            if h[i] < l[i - 2]:
                fvg_bear_lo, fvg_bear_hi = h[i], l[i - 2]

        # ---- ICT sequential setup tracker (every bar; pure-ICT mode only) ----
        if ict_pure:
            # SHORT sequence: sweep of a swing-high -> MSS down -> bear FVG.
            sw_s = (not np.isnan(highest[i])) and h[i] > highest[i] and c[i] < highest[i]
            mss_s = (not np.isnan(swL)) and c[i] < swL and c[i - 1] >= swL
            if sw_s:
                s_sweep_bar = i
            if mss_s and 0 <= i - s_sweep_bar <= ICT_WIN:
                s_mss_bar = i
                s_fvg_lo, s_fvg_hi = fvg_bear_lo, fvg_bear_hi
            elif s_mss_bar >= 0 and np.isnan(s_fvg_hi) and not np.isnan(fvg_bear_lo):
                s_fvg_lo, s_fvg_hi = fvg_bear_lo, fvg_bear_hi   # FVG formed just after MSS
            # LONG sequence: sweep of a swing-low -> MSS up -> bull FVG.
            sw_l = (not np.isnan(lowest[i])) and l[i] < lowest[i] and c[i] > lowest[i]
            mss_l = (not np.isnan(swH)) and c[i] > swH and c[i - 1] <= swH
            if sw_l:
                l_sweep_bar = i
            if mss_l and 0 <= i - l_sweep_bar <= ICT_WIN:
                l_mss_bar = i
                l_fvg_lo, l_fvg_hi = fvg_bull_lo, fvg_bull_hi
            elif l_mss_bar >= 0 and np.isnan(l_fvg_hi) and not np.isnan(fvg_bull_lo):
                l_fvg_lo, l_fvg_hi = fvg_bull_lo, fvg_bull_hi

        # ---- manage open position ----
        if pos is not None:
            done = _manage_smc(pos, h[i], l[i], c[i], ema20_1[i])
            if done is not None:
                pos.update(outcome=("WIN" if done > 0.05 else "LOSS" if done < -0.05 else "BE"),
                           r=round(done, 3), exit_price=c[i], exit_ts=ts[i].isoformat())
                trades.append(pos)
                cooldown_until = i + (2 if done > 0 else 8)   # #18
                pos = None
            continue
        if i < cooldown_until:
            continue

        # ---- direction from multi-TF alignment (#19) ----
        long_ok = d_bull[i] > 0 and h4_bull[i] > 0 and h1_bull[i] > 0
        if short_align == "dual_dh4":       # drop the 1H bearish requirement
            short_ok = d_bull[i] == 0 and h4_bull[i] == 0
        elif short_align == "dual_h4h1":    # drop the 1D bearish requirement
            short_ok = h4_bull[i] == 0 and h1_bull[i] == 0
        else:                                # "triple" — live behavior
            short_ok = d_bull[i] == 0 and h4_bull[i] == 0 and h1_bull[i] == 0
        if ict_pure:
            # ICT direction comes from the setup, not our trend-alignment regime:
            # evaluate whichever side this run allows (run short-only & long-only
            # separately) so the ICT sweep->MSS->FVG sequence decides the trade.
            if allow_short and not allow_long:
                machine = "short"
            elif allow_long and not allow_short:
                machine = "long"
            else:
                machine = "long" if long_ok else "short" if short_ok else None
        else:
            machine = "long" if long_ok else "short" if short_ok else None
        if machine is None:
            continue
        if (machine == "long" and not allow_long) or (machine == "short" and not allow_short):
            continue

        # ---- macro policy gate (#13): don't fight the central-bank tone ----
        if macro_gate:
            mb = macro_bias[i]
            if machine == "long" and mb <= macro_off_th:      # policy risk-off -> no long
                continue
            if machine == "short" and mb >= macro_on_th:       # policy risk-on -> no short
                continue
            if machine == "long" and macro_require_on and mb < macro_on_th:  # weak-long fix
                continue

        # ---- hard filters ----
        atr_pct = atr_1[i] / c[i] * 100 if c[i] else 0
        if not (atr_min <= atr_pct <= atr_max):          # #14
            continue
        if use_session and not in_session[i]:            # #12
            continue
        if np.isnan(swH) or np.isnan(swL) or (swH - swL) <= 0:
            continue
        mid = (swH + swL) / 2
        discount = c[i] < mid
        premium = c[i] > mid
        if machine == "long" and not discount:           # #2
            continue
        if machine == "short" and not premium:
            continue

        # ---- fib golden zone (of the last swing) ----
        if machine == "long":
            ratio = (swH - c[i]) / (swH - swL)
        else:
            ratio = (c[i] - swL) / (swH - swL)
        in_fib = config.FIB_ZONE_LO <= ratio <= config.FIB_ZONE_HI

        # ---- SMC signals (heuristic) ----
        if machine == "long":
            sweep = (not np.isnan(lowest[i])) and l[i] < lowest[i] and c[i] > lowest[i]
            choch = (not np.isnan(swH)) and c[i] > swH and c[i - 1] <= swH
            bos = (not np.isnan(swH)) and c[i] > swH
            fvg = (not np.isnan(fvg_bull_lo)) and l[i] <= fvg_bull_hi and c[i] >= fvg_bull_lo
            ob = c[i - 1] < o[i - 1] and bos
            ema_ok = c[i] > ema200_1[i] and ema50_1[i] > ema200_1[i]
            rsi_ok = h4_rsi[i] > 50
            btcd_ok = btcd_dir[i] == "TURUN"                     # #9 long alt: BTC.D down
            usdtd_ok = usdtd[i] < usdtd_prev[i]                  # #11 USDT.D lower (falling)
        else:
            sweep = (not np.isnan(highest[i])) and h[i] > highest[i] and c[i] < highest[i]
            choch = (not np.isnan(swL)) and c[i] < swL and c[i - 1] >= swL
            bos = (not np.isnan(swL)) and c[i] < swL
            fvg = (not np.isnan(fvg_bear_lo)) and h[i] >= fvg_bear_lo and c[i] <= fvg_bear_hi
            ob = c[i - 1] > o[i - 1] and bos
            ema_ok = c[i] < ema200_1[i] and ema50_1[i] < ema200_1[i]
            rsi_ok = h4_rsi[i] < 50
            btcd_ok = btcd_dir[i] == "NAIK"                      # short alt: BTC.D up
            usdtd_ok = usdtd[i] > usdtd_prev[i]                  # USDT.D higher (rising)

        # v1.1 ablation-validated filters (PF 1.41->2.60, DD -6->-3.4R over 730d)
        _vmult = short_vol_mult if machine == "short" else config.SMC_VOL_MULT
        vol_ok = (not np.isnan(vsma[i])) and v[i] > _vmult * vsma[i]                # #5
        atr_exp = (not np.isnan(atr_sma[i])) and atr_1[i] > atr_sma[i]              # #4
        _adx_min = short_adx_min if machine == "short" else 25
        adx_ok = adx_1[i] > _adx_min                            # #3

        # ---- Setup Score (#15) ----
        score = (W["ema"] * ema_ok + W["rsi"] * rsi_ok + W["adx"] * adx_ok
                 + W["fib"] * in_fib + W["sweep"] * sweep + W["choch"] * choch
                 + W["bos"] * bos + W["fvg"] * fvg + W["ob"] * ob
                 + W["usdtd"] * usdtd_ok)   # BTC.D dibuang (ablation-validated)
        if ict_pure:
            # Canonical ICT fires on the RETRACE into the post-MSS FVG, within the
            # window after the sweep->MSS sequence. No score / EMA / RSI / ADX / vol
            # confluence — ICT's own edge only. Consume the setup once entered.
            if machine == "short":
                trig = (s_mss_bar >= 0 and 0 <= i - s_mss_bar <= ICT_WIN
                        and not np.isnan(s_fvg_hi)
                        and h[i] >= s_fvg_lo and c[i] <= s_fvg_hi)
            else:
                trig = (l_mss_bar >= 0 and 0 <= i - l_mss_bar <= ICT_WIN
                        and not np.isnan(l_fvg_hi)
                        and l[i] <= l_fvg_hi and c[i] >= l_fvg_lo)
            if not trig:
                continue
            if machine == "short":
                s_mss_bar = s_sweep_bar = -1
                s_fvg_lo = s_fvg_hi = np.nan
            else:
                l_mss_bar = l_sweep_bar = -1
                l_fvg_lo = l_fvg_hi = np.nan
        else:
            if not vol_ok or not atr_exp:   # volume spike + volatility expansion (hard)
                continue
            # strengthen long: require a genuine sweep-reclaim + structure break
            if machine == "long" and long_reversal_hard and not (sweep and (choch or bos)):
                continue
            th = long_score_th if machine == "long" else score_th   # asymmetric long gate
            if score < th:
                continue

        # ---- SMT divergence confirmation (research filter) ----
        if smt_filter:
            if machine == "short" and not smt_bear[i]:
                continue
            if machine == "long" and not smt_bull[i]:
                continue

        # ---- build trade: SL beyond swing +/-1 ATR (cap 6%), risk 1% ----
        entry = c[i]
        if machine == "long":
            sl = max(min(swL, entry) - atr_1[i], entry * (1 - config.SL_CAP_PCT))
            risk = entry - sl
            if risk <= 0:
                continue
            tp1, tp2 = entry + risk, entry + 2 * risk       # max target +2R (no TP3)
        else:
            sl = min(max(swH, entry) + atr_1[i], entry * (1 + config.SL_CAP_PCT))
            risk = sl - entry
            if risk <= 0:
                continue
            tp1, tp2 = entry - risk, entry - 2 * risk

        features = {
            "machine": machine, "regime": "BULL" if machine == "long" else "BEAR",
            "fib_bucket": "0.5-0.55" if ratio < 0.55 else "0.55-0.618" if ratio <= 0.618 else "deep",
            "rsi_htf_bucket": "hi" if (h4_rsi[i] > 55) else "mid" if h4_rsi[i] > 45 else "lo",
            "rsi_ltf_bucket": "na", "dow": int(ts[i].weekday()),
            "usdtd_pos_bucket": "na",
            "score_bucket": "85+" if score >= 85 else "70-85" if score >= 70 else "lo",
            "ad_rising": bool(sweep and choch) if machine == "long" else None,
            "sar_confirm": bool(sweep and choch) if machine == "short" else None,
        }
        pos = {
            "symbol": symbol, "direction": "LONG" if machine == "long" else "SHORT",
            "machine": machine, "entry": float(entry), "sl": float(sl),
            "tp1": float(tp1), "tp2": float(tp2),
            "rr": round(abs(tp2 - entry) / risk, 2), "risk": float(risk),
            "score": int(score), "tp1_hit": False, "tp2_hit": False,
            "rem": 1.0, "realized": 0.0, "stop": float(sl), "tp_source": "smc",
            "entry_ts": ts[i].isoformat(), "features": features,
        }

    return trades


def summarize(trades: list[dict]) -> dict:
    """Aggregate win rate, profit factor, expectancy, equity curve, drawdown."""
    trades = sorted(trades, key=lambda t: t["exit_ts"])
    wins = [t for t in trades if t["r"] > 0.05]
    losses = [t for t in trades if t["r"] < -0.05]
    gross_win = sum(t["r"] for t in wins)
    gross_loss = -sum(t["r"] for t in losses)
    total = len(trades)

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    curve = []
    for t in trades:
        equity += t["r"]
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        curve.append({"ts": t["exit_ts"], "r": round(equity, 2)})

    per_symbol: dict[str, dict] = {}
    for t in trades:
        s = per_symbol.setdefault(t["symbol"], {"n": 0, "w": 0, "r": 0.0})
        s["n"] += 1
        s["w"] += 1 if t["r"] > 0.05 else 0
        s["r"] += t["r"]

    # per-direction win rate
    def _dir(d):
        sub = [t for t in trades if t["direction"] == d]
        w = sum(1 for t in sub if t["r"] > 0.05)
        return {"n": len(sub), "win_rate": round(w / len(sub) * 100, 1) if sub else 0.0,
                "total_r": round(sum(t["r"] for t in sub), 2)}

    # average trade duration in 4H bars
    durs = []
    for t in trades:
        try:
            dt = (pd.Timestamp(t["exit_ts"]) - pd.Timestamp(t["entry_ts"])).total_seconds()
            durs.append(dt / 14400.0)
        except Exception:
            pass
    avg_dur = round(sum(durs) / len(durs), 1) if durs else 0.0

    # R distribution histogram
    edges = [(-99, -1), (-1, 0), (0, 1), (1, 2), (2, 3), (3, 99)]
    labels = ["≤-1", "-1..0", "0..1", "1..2", "2..3", ">3"]
    hist = []
    for (lo, hi), lab in zip(edges, labels):
        hist.append({"label": lab, "count": sum(1 for t in trades if lo < t["r"] <= hi)})

    return {
        "trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / total * 100, 1) if total else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else round(gross_win, 2),
        "expectancy_r": round(sum(t["r"] for t in trades) / total, 3) if total else 0.0,
        "total_r": round(sum(t["r"] for t in trades), 2),
        "max_drawdown_r": round(max_dd, 2),
        "avg_duration_bars": avg_dur,
        "long": _dir("LONG"),
        "short": _dir("SHORT"),
        "r_histogram": hist,
        "equity_curve": curve,
        "per_symbol": {
            k: {"n": v["n"], "win_rate": round(v["w"] / v["n"] * 100, 1),
                "total_r": round(v["r"], 2)}
            for k, v in sorted(per_symbol.items(), key=lambda kv: -kv[1]["r"])
        },
    }


def _manage_smc(pos, bar_high, bar_low, close, ema20):
    """2-tier exit capped at +2R (max RR 2), identical to the LIVE tracker:
    take 50% at +1R and move to breakeven, close the rest at +2R. No TP3, no
    trail beyond 2R. Returns final blended R when fully closed, else None."""
    entry, risk = pos["entry"], pos["risk"] or 1e-9
    long = pos["direction"] == "LONG"

    def hit(level, up):  # did price reach `level` this bar
        return bar_high >= level if up else bar_low <= level

    # stop-out (or breakeven after TP1) of the remaining size
    stopped = bar_low <= pos["stop"] if long else bar_high >= pos["stop"]
    if stopped:
        r_stop = (pos["stop"] - entry) / risk if long else (entry - pos["stop"]) / risk
        pos["realized"] += pos["rem"] * r_stop
        return pos["realized"]

    if not pos["tp1_hit"] and hit(pos["tp1"], long):        # +1R, bank 50%, -> BE
        pos["realized"] += 0.50 * 1.0
        pos["rem"] -= 0.50
        pos["tp1_hit"] = True
        pos["stop"] = entry * (1 + config.BE_BUFFER_PCT) if long else entry * (1 - config.BE_BUFFER_PCT)
    if pos["tp1_hit"] and hit(pos["tp2"], long):            # +2R, close the rest (MAX target)
        pos["realized"] += pos["rem"] * 2.0
        return pos["realized"]
    return None
