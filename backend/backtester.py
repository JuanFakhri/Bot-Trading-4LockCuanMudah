"""Historical backtester for the FIB Hybrid strategy.

Runs the strategy bar-by-bar over historical 4H candles (with 1D filters and a
BTC-derived regime timeline), simulates each trade forward to TP/SL, and yields
the resolved trades plus their learning feature signatures. It reuses the same
indicator and fibonacci helpers as the live engine so results reflect the real
rules — the one simplification is that the 15m entry trigger is approximated on
the 4H bar (candle direction + mini-BOS + RSI/OBV slope), keeping the backtest
fast while preserving the decision logic.

The resolved trades are fed into ``learning`` so the bot literally learns from
its historical mistakes (patterns that lose get blocked, winners get favoured).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, indicators
from .strategy import _fib_levels, _retrace_ratio, _bucket


def _align_daily(series: pd.Series, htf_index: pd.Index) -> pd.Series:
    return series.reindex(htf_index, method="ffill")


def backtest_symbol(symbol: str, htf: pd.DataFrame, dtf: pd.DataFrame,
                    regime_daily: pd.Series, usdtd_daily: pd.Series,
                    params: dict | None = None,
                    ltf: pd.DataFrame | None = None) -> list[dict]:
    """Return a list of resolved trade dicts for one symbol.

    ``params`` optionally overrides tunable settings (sl_atr, min_rr,
    confirm_bars, require_ad). When ``ltf`` (1H candles) is provided the entry
    trigger and position management run on 1H bars with 4H context — closer to
    the live strategy — instead of approximating the trigger on 4H.
    """
    params = params or {}
    p_sl_atr = float(params.get("sl_atr", config.SL_ATR_MULT))
    p_min_rr = float(params.get("min_rr", config.MIN_RR))
    p_confirm = int(params.get("confirm_bars", config.CONFIRM_MAX_BARS))
    p_require_ad = bool(params.get("require_ad", True))

    if ltf is not None and not ltf.empty:
        return _backtest_1h(symbol, htf, dtf, ltf, regime_daily, usdtd_daily,
                            p_sl_atr, p_min_rr, p_confirm, p_require_ad)

    if len(htf) < config.EMA_SLOW + 30 or len(dtf) < 30:
        return []

    close = htf["close"]
    openp = htf["open"]
    high = htf["high"]
    low = htf["low"]

    ema50 = indicators.ema(close, config.EMA_FAST).to_numpy()
    ema200 = indicators.ema(close, config.EMA_SLOW).to_numpy()
    rsi = indicators.rsi(close, config.RSI_LEN).to_numpy()
    atr = indicators.atr(htf, config.ATR_LEN).to_numpy()
    ad = indicators.ad_line(htf).to_numpy()
    obv = indicators.obv(htf).to_numpy()
    sar = indicators.parabolic_sar(htf).to_numpy()
    piv_hi, piv_lo = indicators.find_pivots(htf, config.PIVOT_LEN)
    piv_hi = piv_hi.to_numpy()
    piv_lo = piv_lo.to_numpy()

    d_ema200 = _align_daily(indicators.ema(dtf["close"], config.EMA_SLOW), htf.index).to_numpy()
    regime = _align_daily(regime_daily, htf.index)
    usdtd = _align_daily(usdtd_daily, htf.index)

    c = close.to_numpy()
    o = openp.to_numpy()
    h = high.to_numpy()
    l = low.to_numpy()
    ts = htf.index

    n = len(htf)
    pl_ = config.PIVOT_LEN
    warm = config.EMA_SLOW + 5

    # running last-confirmed swing points
    swH = swL = np.nan
    swHbar = swLbar = -1

    trades: list[dict] = []
    pos = None          # open position dict or None
    cooldown_until = -1
    armed = None        # {machine,start,end,expire} while waiting for confirmation

    for i in range(warm, n):
        # ---- confirm any pivot that becomes visible at bar i ----
        j = i - pl_
        if j >= 0:
            if piv_hi[j]:
                swH, swHbar = h[j], j
            if piv_lo[j]:
                swL, swLbar = l[j], j

        # ---- manage an open position on this bar's range ----
        if pos is not None:
            hit = _manage(pos, h[i], l[i])
            if hit is not None:
                r, exit_price = hit
                pos.update(outcome=("WIN" if r > 0.05 else "LOSS" if r < -0.05 else "BE"),
                           r=round(r, 3), exit_price=exit_price,
                           exit_ts=ts[i].isoformat())
                trades.append(pos)
                cooldown_until = i + config.COOLDOWN_BARS
                pos = None
            continue  # one position at a time

        if i < cooldown_until:
            armed = None
            continue

        reg = regime.iloc[i]
        machine = "long" if reg == "BULL" else "short" if reg == "BEAR" else None
        if machine is None or np.isnan(atr[i]) or atr[i] <= 0:
            armed = None
            continue

        dow = ts[i].weekday()
        pos_usdtd = float(usdtd.iloc[i]) if not np.isnan(usdtd.iloc[i]) else 0.5

        # ================= confirmation of an existing ARM =================
        if armed is not None:
            if armed["machine"] != machine or i > armed["expire"]:
                armed = None
            else:
                start_p, end_p = armed["start"], armed["end"]
                ratio_now = _retrace_ratio(c[i], start_p, end_p, machine)
                if ratio_now >= config.FIB_INVALID:      # retrace too deep -> invalid
                    armed = None
                else:
                    if machine == "long":
                        conf = (c[i] > o[i] and c[i] > h[i - 1]
                                and rsi[i] > rsi[i - 1] and obv[i] > obv[i - 2]
                                and rsi[i] > 50)
                    else:
                        conf = (c[i] < o[i] and c[i] < l[i - 1]
                                and rsi[i] < rsi[i - 1] and obv[i] < obv[i - 2]
                                and rsi[i] < 50)
                    if conf:
                        # opposing liquidity levels from confirmed pivots
                        conf_lim = i - pl_
                        if machine == "long":
                            liq = sorted(float(h[k]) for k in range(conf_lim + 1)
                                         if piv_hi[k] and h[k] > c[i])
                        else:
                            liq = sorted((float(l[k]) for k in range(conf_lim + 1)
                                          if piv_lo[k] and l[k] < c[i]), reverse=True)
                        pos = _open_trade(symbol, machine, i, c, start_p, end_p, atr,
                                          rsi, ad, sar, dow, pos_usdtd, ratio_now, ts,
                                          p_sl_atr, p_min_rr, liq)
                        armed = None
                        continue
            # (fall through to allow re-arming on the same bar)

        # ================= try to ARM (golden zone + structure) =================
        if machine == "long":
            if np.isnan(swH) or np.isnan(swL) or swHbar <= swLbar:
                continue
            if (swH - swL) < config.IMPULSE_MIN_ATR * atr[i]:
                continue
            start_p, end_p = swL, swH
        else:
            if np.isnan(swH) or np.isnan(swL) or swLbar <= swHbar:
                continue
            if (swH - swL) < config.IMPULSE_MIN_ATR * atr[i]:
                continue
            start_p, end_p = swH, swL

        ratio = _retrace_ratio(c[i], start_p, end_p, machine)
        if not (config.FIB_ZONE_LO <= ratio <= config.FIB_ZONE_HI and ratio < config.FIB_INVALID):
            continue

        if machine == "long":
            structure = (c[i] > ema200[i] and ema50[i] > ema200[i]
                         and c[i] > d_ema200[i]
                         and (ad[i] > ad[i - 3] or not p_require_ad)
                         and not (config.SKIP_FRIDAY_LONG and dow == 4))
        else:
            # short trigger only at EXTREME USDT.D resistance (edge from 365d bt)
            structure = (c[i] < ema200[i] and ema50[i] < ema200[i]
                         and (pos_usdtd > config.USDTD_SHORT_POS)
                         and (c[i] < sar[i]))

        if structure:
            armed = {"machine": machine, "start": start_p, "end": end_p,
                     "expire": i + p_confirm}

    return trades


def _liquidity_tp(levels, entry, risk, min_rr, machine, fib_ext):
    for lvl in levels:
        beyond = lvl > entry if machine == "long" else lvl < entry
        if beyond and (abs(lvl - entry) / risk) >= min_rr:
            return lvl, "likuiditas"
    return fib_ext, "fib-ext"


def _open_trade(symbol, machine, i, c, start_p, end_p, atr, rsi, ad, sar, dow,
                pos_usdtd, ratio, ts, sl_atr=config.SL_ATR_MULT, min_rr=config.MIN_RR,
                liq_levels=None):
    """Construct an open-position dict for an entry at bar ``i``.

    TP2 targets the nearest opposing liquidity (swing) that meets RR, falling
    back to the fib 1.272 extension.
    """
    liq_levels = liq_levels or []
    fib = _fib_levels(start_p, end_p, machine)
    entry = c[i]
    if machine == "long":
        raw_sl = min(start_p, entry) - sl_atr * atr[i]
        sl = max(raw_sl, entry * (1 - config.SL_CAP_PCT))
        risk = entry - sl
        if risk <= 0:
            return None
        tp1 = entry + risk
        tp2, tp_source = _liquidity_tp(liq_levels, entry, risk, min_rr, machine,
                                       fib.get("ext_1.272", entry + 2 * risk))
    else:
        raw_sl = max(start_p, entry) + sl_atr * atr[i]
        sl = min(raw_sl, entry * (1 + config.SL_CAP_PCT))
        risk = sl - entry
        if risk <= 0:
            return None
        tp1 = entry - risk
        tp2, tp_source = _liquidity_tp(liq_levels, entry, risk, min_rr, machine,
                                       fib.get("ext_1.272", entry - 2 * risk))

    rr = abs(tp2 - entry) / risk if risk else 0
    if rr < min_rr:
        return None

    features = {
        "machine": machine,
        "regime": "BULL" if machine == "long" else "BEAR",
        "fib_bucket": "0.5-0.55" if ratio < 0.55 else "0.55-0.618" if ratio <= 0.618 else "deep",
        "rsi_htf_bucket": _bucket(float(rsi[i]), [40, 50, 60, 70]),
        "rsi_ltf_bucket": _bucket(float(rsi[i]), [30, 40, 50, 60, 70]),
        "dow": int(dow),
        "usdtd_pos_bucket": _bucket(pos_usdtd * 100, [30, 50, 70, 85]),
        "ad_rising": bool(ad[i] > ad[i - 3]) if machine == "long" else None,
        "sar_confirm": bool(c[i] < sar[i]) if machine == "short" else None,
    }
    return {
        "symbol": symbol, "direction": "LONG" if machine == "long" else "SHORT",
        "machine": machine, "entry": float(entry), "sl": float(sl),
        "tp1": float(tp1), "tp2": float(tp2), "rr": round(rr, 2),
        "risk": float(risk), "tp1_hit": False, "tp_source": tp_source,
        "entry_ts": ts[i].isoformat(), "features": features,
    }


def _manage(pos: dict, bar_high: float, bar_low: float):
    """Update an open position against one bar. Returns (r, exit_price) if
    the trade closes on this bar, else None."""
    entry, sl, tp1, tp2 = pos["entry"], pos["sl"], pos["tp1"], pos["tp2"]
    risk = pos["risk"] or 1e-9
    if pos["direction"] == "LONG":
        be = entry * 1.0015
        stop = be if pos["tp1_hit"] else sl
        if bar_low <= stop:
            return (0.5 + 0.5 * (be - entry) / risk if pos["tp1_hit"] else -1.0, stop)
        if not pos["tp1_hit"] and bar_high >= tp1:
            pos["tp1_hit"] = True
        if bar_high >= tp2:
            return (0.5 + 0.5 * (tp2 - entry) / risk, tp2)
    else:
        be = entry * 0.9985
        stop = be if pos["tp1_hit"] else sl
        if bar_high >= stop:
            return (0.5 + 0.5 * (entry - be) / risk if pos["tp1_hit"] else -1.0, stop)
        if not pos["tp1_hit"] and bar_low <= tp1:
            pos["tp1_hit"] = True
        if bar_low <= tp2:
            return (0.5 + 0.5 * (entry - tp2) / risk, tp2)
    return None


def _backtest_1h(symbol, htf, dtf, ltf, regime_daily, usdtd_daily,
                 sl_atr, min_rr, confirm_bars, require_ad):
    """1H-triggered backtest: 4H context aligned to the 1H timeline; the entry
    trigger and trade management run on 1H bars (more faithful to live)."""
    if len(htf) < config.EMA_SLOW + 30 or len(dtf) < 30 or len(ltf) < 60:
        return []

    pl_ = config.PIVOT_LEN
    n4 = len(htf)
    h4 = htf["high"].to_numpy()
    l4 = htf["low"].to_numpy()
    c4 = htf["close"].to_numpy()

    ema50 = indicators.ema(htf["close"], config.EMA_FAST).to_numpy()
    ema200 = indicators.ema(htf["close"], config.EMA_SLOW).to_numpy()
    rsi4 = indicators.rsi(htf["close"], config.RSI_LEN).to_numpy()
    atr4 = indicators.atr(htf, config.ATR_LEN).to_numpy()
    ad4 = indicators.ad_line(htf).to_numpy()
    sar4 = indicators.parabolic_sar(htf).to_numpy()
    piv_hi, piv_lo = indicators.find_pivots(htf, pl_)
    piv_hi = piv_hi.to_numpy(); piv_lo = piv_lo.to_numpy()
    d_ema200 = indicators.ema(dtf["close"], config.EMA_SLOW).reindex(htf.index, method="ffill").to_numpy()

    # running confirmed swings + structure per 4H bar
    swH = swL = np.nan
    swHbar = swLbar = -1
    swH_s = np.full(n4, np.nan); swL_s = np.full(n4, np.nan)
    swHb_s = np.full(n4, -1); swLb_s = np.full(n4, -1)
    st_long = np.zeros(n4, bool); st_short = np.zeros(n4, bool)
    dows = htf.index.weekday.to_numpy()
    for i in range(n4):
        j = i - pl_
        if j >= 0:
            if piv_hi[j]:
                swH, swHbar = h4[j], j
            if piv_lo[j]:
                swL, swLbar = l4[j], j
        swH_s[i], swL_s[i], swHb_s[i], swLb_s[i] = swH, swL, swHbar, swLbar
        if i >= 3:
            st_long[i] = (c4[i] > ema200[i] and ema50[i] > ema200[i] and c4[i] > d_ema200[i]
                          and (ad4[i] > ad4[i - 3] or not require_ad)
                          and not (config.SKIP_FRIDAY_LONG and dows[i] == 4))
            st_short[i] = (c4[i] < ema200[i] and ema50[i] < ema200[i] and c4[i] < sar4[i])

    # align 4H context to the 1H index (use last CLOSED 4H bar -> no lookahead)
    ctx = pd.DataFrame({
        "atr": atr4, "rsi": rsi4, "ad": ad4, "sar": sar4,
        "swH": swH_s, "swL": swL_s, "swHb": swHb_s, "swLb": swLb_s,
        "stL": st_long.astype(float), "stS": st_short.astype(float),
    }, index=htf.index).reindex(ltf.index, method="ffill")

    a_atr = ctx["atr"].to_numpy(); a_rsi = ctx["rsi"].to_numpy()
    a_ad = ctx["ad"].to_numpy(); a_sar = ctx["sar"].to_numpy()
    a_swH = ctx["swH"].to_numpy(); a_swL = ctx["swL"].to_numpy()
    a_swHb = ctx["swHb"].to_numpy(); a_swLb = ctx["swLb"].to_numpy()
    a_stL = ctx["stL"].to_numpy(); a_stS = ctx["stS"].to_numpy()

    regime = regime_daily.reindex(ltf.index, method="ffill")
    usdtd = usdtd_daily.reindex(ltf.index, method="ffill")

    # 1H trigger indicators
    o1 = ltf["open"].to_numpy(); h1 = ltf["high"].to_numpy()
    l1 = ltf["low"].to_numpy(); c1 = ltf["close"].to_numpy()
    rsi1 = indicators.rsi(ltf["close"], config.RSI_LEN).to_numpy()
    obv1 = indicators.obv(ltf).to_numpy()
    ts1 = ltf.index
    n1 = len(ltf)

    # 4H pivot liquidity levels with their confirmation time
    hi_conf = [(htf.index[k + pl_], float(h4[k])) for k in range(n4) if piv_hi[k] and k + pl_ < n4]
    lo_conf = [(htf.index[k + pl_], float(l4[k])) for k in range(n4) if piv_lo[k] and k + pl_ < n4]

    trades = []
    pos = None
    cooldown_until = -1
    armed = None

    for i in range(3, n1):
        if pos is not None:
            hit = _manage(pos, h1[i], l1[i])
            if hit is not None:
                r, exit_price = hit
                pos.update(outcome=("WIN" if r > 0.05 else "LOSS" if r < -0.05 else "BE"),
                           r=round(r, 3), exit_price=exit_price, exit_ts=ts1[i].isoformat())
                trades.append(pos)
                cooldown_until = i + config.COOLDOWN_BARS
                pos = None
            continue
        if i < cooldown_until:
            armed = None
            continue

        reg = regime.iloc[i]
        machine = "long" if reg == "BULL" else "short" if reg == "BEAR" else None
        atr_i = a_atr[i]
        if machine is None or np.isnan(atr_i) or atr_i <= 0 or np.isnan(a_swH[i]) or np.isnan(a_swL[i]):
            armed = None
            continue

        # confirmation of an existing ARM on the 1H bar
        if armed is not None:
            if armed["machine"] != machine or i > armed["expire"]:
                armed = None
            else:
                ratio_now = _retrace_ratio(c1[i], armed["start"], armed["end"], machine)
                if ratio_now >= config.FIB_INVALID:
                    armed = None
                else:
                    if machine == "long":
                        conf = (c1[i] > o1[i] and c1[i] > h1[i - 1] and rsi1[i] > rsi1[i - 1]
                                and obv1[i] > obv1[i - 2] and rsi1[i] > 50)
                    else:
                        conf = (c1[i] < o1[i] and c1[i] < l1[i - 1] and rsi1[i] < rsi1[i - 1]
                                and obv1[i] < obv1[i - 2] and rsi1[i] < 50)
                    if conf:
                        t = ts1[i]
                        if machine == "long":
                            liq = sorted(p for (cts, p) in hi_conf if cts <= t and p > c1[i])
                        else:
                            liq = sorted((p for (cts, p) in lo_conf if cts <= t and p < c1[i]), reverse=True)
                        pos = _open_trade(symbol, machine, i, c1, armed["start"], armed["end"],
                                          a_atr, a_rsi, a_ad, a_sar, int(ts1[i].weekday()),
                                          float(usdtd.iloc[i]) if not np.isnan(usdtd.iloc[i]) else 0.5,
                                          ratio_now, ts1, sl_atr, min_rr, liq)
                        armed = None
                        continue

        # try to ARM using 4H context (golden zone + structure)
        if machine == "long":
            if a_swHb[i] <= a_swLb[i] or (a_swH[i] - a_swL[i]) < config.IMPULSE_MIN_ATR * atr_i:
                continue
            start_p, end_p = a_swL[i], a_swH[i]
            struct = bool(a_stL[i])
        else:
            if a_swLb[i] <= a_swHb[i] or (a_swH[i] - a_swL[i]) < config.IMPULSE_MIN_ATR * atr_i:
                continue
            start_p, end_p = a_swH[i], a_swL[i]
            ud = usdtd.iloc[i]
            struct = bool(a_stS[i]) and (not np.isnan(ud)) and (ud > config.USDTD_SHORT_POS)

        ratio = _retrace_ratio(c1[i], start_p, end_p, machine)
        if not (config.FIB_ZONE_LO <= ratio <= config.FIB_ZONE_HI and ratio < config.FIB_INVALID):
            continue
        if struct:
            armed = {"machine": machine, "start": start_p, "end": end_p, "expire": i + confirm_bars}

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
