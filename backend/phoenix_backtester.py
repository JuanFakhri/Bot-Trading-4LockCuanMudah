"""Phoenix Hybrid — multi-engine RESEARCH backtester.

Three engines share the same regime filter and risk framework, so there is a
setup for every market condition:

  * 🔁 **FIB retrace**   — pullback into the 0.382–0.618 zone of a ≥2.5×ATR
                           impulse, with a 2-of-3 trigger (BOS / RSI turn /
                           volume). Runs in BULL (long) or BEAR (short).
  * 🚀 **Momentum breakout** — close through the 20-bar high/low on >1.5× volume
                           with trend alignment (EMA200, RSI, A/D). Runs in a
                           trending regime with live volatility.
  * 📦 **Range mean-reversion** — fade a well-defined support/resistance range
                           (≥2×ATR wide, held ≥12h) with an RSI extreme. Runs in
                           NEUTRAL or low-volatility regimes.

Exits are shared: SL 0.8×ATR beyond the swing, TP1 50% at +1R then an EMA20 /
Parabolic-SAR trailing stop, TP2 on the runner at +2R or the 1.272 fib
extension, and a 12-bar time-stop that trims a stalled trade.

This module ONLY produces per-trade results (tagged with their engine & regime).
The portfolio layer (dynamic sizing, recovery mode, daily/weekly stops, max
concurrency) lives in :func:`simulate_portfolio`. Nothing here touches the live
trade engine — it is evaluated by ``scripts/backtest_phoenix.py``.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, indicators

ENGINES = ("fib", "breakout", "range")


def _align(series: pd.Series, idx: pd.Index, fill="ffill") -> np.ndarray:
    return series.reindex(idx, method=fill).to_numpy()


# --------------------------------------------------------------------------
# Regime (market-wide, driven by BTC) — BULL / BEAR / NEUTRAL
# --------------------------------------------------------------------------
def btc_regime_daily(btc_daily: pd.DataFrame) -> pd.Series:
    """Per-day regime from BTC EMA50 (1D). Flat within ±band over N days ->
    NEUTRAL; otherwise BULL (rising) / BEAR (falling)."""
    ema50 = indicators.ema(btc_daily["close"], config.EMA_FAST)
    band, days = config.PHX_NEUTRAL_BAND, config.PHX_NEUTRAL_DAYS
    prev = ema50.shift(days)
    chg = (ema50 - prev) / prev.abs()
    regime = pd.Series("NEUTRAL", index=ema50.index)
    regime[chg > band] = "BULL"
    regime[chg < -band] = "BEAR"
    return regime


# --------------------------------------------------------------------------
# Shared exit management (returns final blended R when fully closed)
# --------------------------------------------------------------------------
def _manage_phoenix(pos, high, low, close, ema20, sar, bars_held):
    entry, risk = pos["entry"], pos["risk"] or 1e-9
    long = pos["direction"] == "LONG"

    def hit(level, up):
        return high >= level if up else low <= level

    # ---- hard stop on the remaining size ----
    stopped = low <= pos["stop"] if long else high >= pos["stop"]
    if stopped:
        r_stop = (pos["stop"] - entry) / risk if long else (entry - pos["stop"]) / risk
        pos["realized"] += pos["rem"] * r_stop
        return pos["realized"]

    cur_r = (close - entry) / risk if long else (entry - close) / risk

    # ---- TP1: bank a portion at +tp1_r R, move to breakeven, arm the trail ----
    if not pos["tp1_hit"] and hit(pos["tp1"], long):
        frac = pos.get("tp1_frac", 0.50)
        tp1_r = (pos["tp1"] - entry) / risk if long else (entry - pos["tp1"]) / risk
        pos["realized"] += frac * tp1_r
        pos["rem"] -= frac
        pos["tp1_hit"] = True
        pos["stop"] = entry * (1 + config.BE_BUFFER_PCT) if long else entry * (1 - config.BE_BUFFER_PCT)

    # ---- time-stop: stalled trade before TP1 -> trim half once ----
    tstop = pos.get("time_stop", config.PHX_TIME_STOP_BARS)
    if (tstop and not pos["tp1_hit"] and not pos["time_trim"]
            and bars_held >= tstop and cur_r < 0.5):
        trim = min(0.50, pos["rem"])
        pos["realized"] += trim * cur_r
        pos["rem"] -= trim
        pos["time_trim"] = True

    # ---- runner: TP2 at +2R / fib1.272, else trail on EMA20 or PSAR ----
    if pos["tp1_hit"]:
        if hit(pos["tp2"], long):
            r2 = (pos["tp2"] - entry) / risk if long else (entry - pos["tp2"]) / risk
            pos["realized"] += pos["rem"] * r2
            return pos["realized"]
        trail_ref = ema20 if np.isnan(sar) else (max(ema20, sar) if long else min(ema20, sar))
        trail_break = close < trail_ref if long else close > trail_ref
        if trail_break:
            pos["realized"] += pos["rem"] * cur_r
            return pos["realized"]
    return None


# --------------------------------------------------------------------------
# Per-symbol engine loop
# --------------------------------------------------------------------------
def backtest_symbol_phoenix(symbol, htf, dtf, ltf, regime_daily, usdtd_daily,
                            params=None) -> list[dict]:
    params = params or {}
    engines_on = set(params.get("engines", ENGINES))
    sides_on = set(params.get("sides", ("LONG", "SHORT")))   # research knob
    # Exit tuning (all default to the current live behavior, so an empty params
    # dict reproduces the validated engine exactly). Used by the exit sweep.
    ex = params.get("exit", {})
    sl_atr = float(ex.get("sl_atr", config.PHX_SL_ATR))
    tp1_r = float(ex.get("tp1_r", 1.0))          # TP1 level in R
    tp1_frac = float(ex.get("tp1_frac", 0.50))   # portion banked at TP1
    tp2_r = float(ex.get("tp2_r", 2.0))          # runner target in R (<=0 => trail-only)
    time_stop = int(ex.get("time_stop", config.PHX_TIME_STOP_BARS))  # 0 => off
    # Range-engine tuning (defaults reproduce current behavior; ADX/hold filters off)
    rg = params.get("range", {})
    r_min_atr = float(rg.get("min_atr", config.PHX_RANGE_MIN_ATR))
    r_rsi_lo = float(rg.get("rsi_lo", config.PHX_RANGE_RSI_LO))
    r_near = float(rg.get("near_frac", 0.15))
    r_rr = float(rg.get("rr_min", config.PHX_RANGE_RR))
    r_adx_max = float(rg.get("adx_max", 999.0))      # only trade when ADX below this (999 = off)
    r_cooldown = int(rg.get("cooldown", config.PHX_COOLDOWN_BARS))
    if ltf is None or len(ltf) < 260 or len(htf) < config.EMA_SLOW + 30:
        return []

    o = ltf["open"].to_numpy(); h = ltf["high"].to_numpy()
    l = ltf["low"].to_numpy(); c = ltf["close"].to_numpy(); v = ltf["volume"].to_numpy()
    ema50_1 = indicators.ema(ltf["close"], config.EMA_FAST).to_numpy()
    ema200_1 = indicators.ema(ltf["close"], config.EMA_SLOW).to_numpy()
    ema20_1 = indicators.ema(ltf["close"], 20).to_numpy()
    rsi_1 = indicators.rsi(ltf["close"], config.RSI_LEN).to_numpy()
    atr_1 = indicators.atr(ltf, config.ATR_LEN).to_numpy()
    adx_1 = indicators.adx(ltf, 14).to_numpy() if r_adx_max < 900 else None  # only when filtering
    sar_1 = indicators.parabolic_sar(ltf).to_numpy()
    ad_1 = indicators.ad_line(ltf)
    ad_rising = (ad_1 > ad_1.shift(3)).to_numpy()
    vsma = ltf["volume"].rolling(20, min_periods=5).mean().to_numpy()
    piv_hi, piv_lo = indicators.find_pivots(ltf, config.PIVOT_LEN)
    piv_hi = piv_hi.to_numpy(); piv_lo = piv_lo.to_numpy()
    ts = ltf.index
    n = len(ltf)
    pl_ = config.PIVOT_LEN

    # higher-TF context aligned to the 1H index
    h4_rsi = _align(indicators.rsi(htf["close"], config.RSI_LEN), ts)
    h4_atr = indicators.atr(htf, config.ATR_LEN)
    h4_atr_pct = _align(h4_atr / htf["close"] * 100, ts)
    d_ema200 = _align(indicators.ema(dtf["close"], config.EMA_SLOW), ts)
    regime = pd.Series(regime_daily).reindex(ts, method="ffill").fillna("NEUTRAL").to_numpy()

    # rolling break levels & range bounds
    hi20 = pd.Series(h).rolling(config.PHX_BRK_LOOKBACK, min_periods=5).max().shift(1).to_numpy()
    lo20 = pd.Series(l).rolling(config.PHX_BRK_LOOKBACK, min_periods=5).min().shift(1).to_numpy()
    rw = config.PHX_RANGE_WINDOW
    res_r = pd.Series(h).rolling(rw, min_periods=rw // 2).max().shift(1).to_numpy()
    sup_r = pd.Series(l).rolling(rw, min_periods=rw // 2).min().shift(1).to_numpy()

    swH = swL = np.nan
    swHb = swLb = -1
    # FIB "arm then confirm": price wicks into the golden zone (arm), then a
    # trigger candle fires the entry within ARM_EXPIRY bars. Without this, the
    # zone is only touched on down-bars while the confirmation appears on the
    # reversal bar that has already left the zone (discrete-bar timing miss).
    ARM_EXPIRY = 8
    fib_arm = None   # ("LONG"/"SHORT", arm_bar, swH, swL)
    trades: list[dict] = []
    pos = None
    entry_bar = -1
    cooldown_until = -1

    for i in range(255, n):
        j = i - pl_
        if j >= 0:
            if piv_hi[j]:
                swH, swHb = h[j], j
            if piv_lo[j]:
                swL, swLb = l[j], j

        # ---- manage open position ----
        if pos is not None:
            done = _manage_phoenix(pos, h[i], l[i], c[i], ema20_1[i], sar_1[i], i - entry_bar)
            if done is not None:
                pos.update(outcome=("WIN" if done > 0.05 else "LOSS" if done < -0.05 else "BE"),
                           r=round(done, 3), exit_price=float(c[i]), exit_ts=ts[i].isoformat())
                trades.append(pos)
                cooldown_until = i + (r_cooldown if pos.get("engine") == "range"
                                      else config.PHX_COOLDOWN_BARS)
                pos = None
            continue
        if i < cooldown_until or np.isnan(atr_1[i]) or atr_1[i] <= 0:
            continue

        reg = regime[i]
        atrp_1 = atr_1[i] / c[i] * 100 if c[i] else 0
        vol_live = h4_atr_pct[i] >= config.PHX_VOL_MIN_PCT
        sig = None  # (engine, direction, entry, sl, tp2)

        # ==================== Engine 3: Range (NEUTRAL / low vol) ============
        # ADX filter (optional): only mean-revert when the market is genuinely
        # NOT trending — the key missing filter (a rolling max/min always exists,
        # even mid-trend, which is why the naive range engine catches knives).
        adx_ok = (adx_1 is None) or (not np.isnan(adx_1[i]) and adx_1[i] < r_adx_max)
        if "range" in engines_on and adx_ok and (reg == "NEUTRAL" or not vol_live):
            res, sup = res_r[i], sup_r[i]
            if not (np.isnan(res) or np.isnan(sup)) and (res - sup) >= r_min_atr * atr_1[i]:
                near = r_near * (res - sup)
                if c[i] <= sup + near and rsi_1[i] < r_rsi_lo:      # buy support
                    entry = c[i]; sl = sup - atr_1[i]; tp = (res + sup) / 2
                    if entry - sl > 0 and (tp - entry) / (entry - sl) >= r_rr:
                        sig = ("range", "LONG", entry, sl, tp)
                elif c[i] >= res - near and rsi_1[i] > 100 - r_rsi_lo:  # sell resistance
                    entry = c[i]; sl = res + atr_1[i]; tp = (res + sup) / 2
                    if sl - entry > 0 and (entry - tp) / (sl - entry) >= r_rr:
                        sig = ("range", "SHORT", entry, sl, tp)

        # ==================== Trend engines (BULL / BEAR + live vol) =========
        if sig is None and reg in ("BULL", "BEAR") and vol_live:
            long = reg == "BULL"
            # ---------- Engine 2: Momentum breakout ----------
            if "breakout" in engines_on and atrp_1 > config.PHX_BRK_ATR_MIN:
                vol_ok = (not np.isnan(vsma[i])) and v[i] > config.PHX_BRK_VOL_MULT * vsma[i]
                if long and not np.isnan(hi20[i]) and c[i] > hi20[i] and vol_ok \
                        and c[i] > d_ema200[i] and h4_rsi[i] > config.PHX_BRK_RSI and ad_rising[i]:
                    entry = c[i]; sl = min(l[i], swL if not np.isnan(swL) else l[i]) - sl_atr * atr_1[i]
                    if entry - sl > 0:
                        sig = ("breakout", "LONG", entry, sl, entry + 2 * (entry - sl))
                elif (not long) and not np.isnan(lo20[i]) and c[i] < lo20[i] and vol_ok \
                        and c[i] < d_ema200[i] and h4_rsi[i] < 100 - config.PHX_BRK_RSI and not ad_rising[i]:
                    entry = c[i]; sl = max(h[i], swH if not np.isnan(swH) else h[i]) + sl_atr * atr_1[i]
                    if sl - entry > 0:
                        sig = ("breakout", "SHORT", entry, sl, entry - 2 * (sl - entry))

            # ---------- Engine 1: FIB retrace (arm in zone, confirm to enter) --
            if "fib" in engines_on and not (np.isnan(swH) or np.isnan(swL)):
                impulse = swH - swL
                deep_enough = impulse >= config.PHX_FIB_IMPULSE_ATR * atr_1[i]
                # ARM: this bar's wick reaches the golden zone of the impulse
                if deep_enough:
                    if long:
                        zlo = swH - config.PHX_FIB_ZONE_HI * impulse   # deep (0.618)
                        zhi = swH - config.PHX_FIB_ZONE_LO * impulse   # shallow (0.382)
                        if l[i] <= zhi and l[i] >= zlo - 0.25 * impulse and c[i] > swL:
                            fib_arm = ("LONG", i, swH, swL)
                    else:
                        zlo = swL + config.PHX_FIB_ZONE_LO * impulse
                        zhi = swL + config.PHX_FIB_ZONE_HI * impulse
                        if h[i] >= zlo and h[i] <= zhi + 0.25 * impulse and c[i] < swH:
                            fib_arm = ("SHORT", i, swH, swL)
                # invalidate a stale / broken arm
                if fib_arm is not None:
                    adir, abar, aH, aL = fib_arm
                    if i - abar > ARM_EXPIRY or (adir == "LONG" and c[i] < aL) \
                            or (adir == "SHORT" and c[i] > aH) or adir != ("LONG" if long else "SHORT"):
                        fib_arm = None
                # CONFIRM: trigger candle within the arm window -> enter
                if sig is None and fib_arm is not None:
                    adir, abar, aH, aL = fib_arm
                    vol_up = int((not np.isnan(vsma[i])) and v[i] > vsma[i])
                    if adir == "LONG":
                        bos = int(c[i] > h[i - 1])
                        rsi_turn = int(rsi_1[i] > rsi_1[i - 1] and rsi_1[i] > 45)
                        if bos + rsi_turn + vol_up >= config.PHX_FIB_CONFIRM_MIN:
                            entry = c[i]; sl = aL - sl_atr * atr_1[i]
                            if entry - sl > 0:
                                sig = ("fib", "LONG", entry, sl, entry + 2 * (entry - sl))
                                fib_arm = None
                    else:
                        bos = int(c[i] < l[i - 1])
                        rsi_turn = int(rsi_1[i] < rsi_1[i - 1] and rsi_1[i] < 55)
                        if bos + rsi_turn + vol_up >= config.PHX_FIB_CONFIRM_MIN:
                            entry = c[i]; sl = aH + sl_atr * atr_1[i]
                            if sl - entry > 0:
                                sig = ("fib", "SHORT", entry, sl, entry - 2 * (sl - entry))
                                fib_arm = None

        if sig is None:
            continue
        if sig[1] not in sides_on:      # direction filter (LONG/SHORT research knob)
            continue

        engine, direction, entry, sl, _tp2_sig = sig
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        # exit levels from the (tunable) exit params; defaults = +1R / +2R
        if direction == "LONG":
            tp1 = entry + tp1_r * risk
            tp2 = entry + (tp2_r * risk if tp2_r > 0 else 1e9 * risk)   # <=0 => trail-only
        else:
            tp1 = entry - tp1_r * risk
            tp2 = entry - (tp2_r * risk if tp2_r > 0 else 1e9 * risk)
        pos = {
            "symbol": symbol, "engine": engine, "direction": direction,
            "regime": reg, "entry": float(entry), "sl": float(sl), "stop": float(sl),
            "tp1": float(tp1), "tp2": float(tp2), "risk": float(risk),
            "rr": round(abs(tp2 - entry) / risk, 2),
            "tp1_hit": False, "time_trim": False, "rem": 1.0, "realized": 0.0,
            "tp1_frac": tp1_frac, "time_stop": time_stop,
            "entry_ts": ts[i].isoformat(),
        }
        entry_bar = i

    return trades


# --------------------------------------------------------------------------
# Portfolio simulation — dynamic sizing, recovery mode, daily/weekly stops
# --------------------------------------------------------------------------
def simulate_portfolio(trades: list[dict], params=None) -> dict:
    """Turn per-trade R results into an account equity curve under the Phoenix
    risk rules. Returns the accepted trades (with account P&L), the equity
    curve (in %), and the recovery episodes."""
    params = params or {}
    start_equity = 1.0
    equity = start_equity
    peak = start_equity
    recovering = False
    recovery_episodes = 0

    # event-driven over entries; realise P&L at each trade's own exit time
    srt = sorted(trades, key=lambda t: t["entry_ts"])
    open_positions: list[dict] = []      # accepted, not yet realised
    accepted: list[dict] = []
    curve = [{"ts": srt[0]["entry_ts"], "eq": 0.0}] if srt else []

    day_pnl: dict[str, float] = {}
    week_pnl: dict[str, float] = {}
    day_trades: dict[str, int] = {}

    def _flush_exits(before_ts: str):
        nonlocal equity, peak, recovering, recovery_episodes
        still_open = []
        for p in sorted(open_positions, key=lambda x: x["exit_ts"]):
            if p["exit_ts"] <= before_ts:
                pnl = p["risk_pct"] * p["r"]
                equity += pnl
                peak = max(peak, equity)
                d = p["exit_ts"][:10]
                wk = pd.Timestamp(p["exit_ts"]).strftime("%G-W%V")
                day_pnl[d] = day_pnl.get(d, 0.0) + pnl
                week_pnl[wk] = week_pnl.get(wk, 0.0) + pnl
                p["equity_after"] = round(equity, 5)
                curve.append({"ts": p["exit_ts"], "eq": round((equity / start_equity - 1) * 100, 3)})
                # recovery-mode transitions
                if not recovering and equity <= peak * (1 - config.PHX_RECOVERY_DD):
                    recovering = True
                    recovery_episodes += 1
                elif recovering and equity >= peak * config.PHX_RECOVERY_EXIT:
                    recovering = False
            else:
                still_open.append(p)
        open_positions[:] = still_open

    for t in srt:
        _flush_exits(t["entry_ts"])
        d = t["entry_ts"][:10]
        wk = pd.Timestamp(t["entry_ts"]).strftime("%G-W%V")

        # ---- portfolio gates ----
        if len(open_positions) >= config.PHX_MAX_CONCURRENT:
            continue
        if day_pnl.get(d, 0.0) <= -config.PHX_DAILY_MAX_LOSS:
            continue
        if week_pnl.get(wk, 0.0) <= -config.PHX_WEEKLY_MAX_LOSS:
            continue
        if recovering:
            if t["engine"] != "fib":                      # recovery: FIB only
                continue
            if day_trades.get(d, 0) >= config.PHX_RECOVERY_MAX_TRADES_DAY:
                continue
            risk_pct = config.PHX_RISK_RECOVERY
        else:
            risk_pct = config.PHX_RISK_RANGE if t["engine"] == "range" else config.PHX_RISK_TREND

        t = dict(t)
        t["risk_pct"] = risk_pct
        t["recovering"] = recovering
        day_trades[d] = day_trades.get(d, 0) + 1
        open_positions.append(t)
        accepted.append(t)

    # realise everything left open
    _flush_exits("9999-12-31T23:59:59")

    # equity-curve drawdown (%)
    eqs = [p["eq"] for p in curve]
    peak_c = -1e9
    max_dd = 0.0
    for e in eqs:
        peak_c = max(peak_c, e)
        max_dd = min(max_dd, e - peak_c)

    # keep the curve light for JSON
    step = max(1, len(curve) // 200)
    thin = curve[::step]
    if curve and thin[-1] is not curve[-1]:
        thin.append(curve[-1])

    return {
        "final_return_pct": round((equity / start_equity - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "recovery_episodes": recovery_episodes,
        "accepted": accepted,
        "n_accepted": len(accepted),
        "n_signals": len(trades),
        "equity_curve": thin,
    }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def _stats(trades: list[dict]) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "expectancy_r": 0.0, "total_r": 0.0}
    wins = [t for t in trades if t["r"] > 0.05]
    gross_w = sum(t["r"] for t in wins)
    gross_l = -sum(t["r"] for t in trades if t["r"] < -0.05)
    return {
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 1),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else round(gross_w, 2),
        "expectancy_r": round(sum(t["r"] for t in trades) / n, 3),
        "total_r": round(sum(t["r"] for t in trades), 2),
    }


def summarize_phoenix(all_trades: list[dict], portfolio: dict) -> dict:
    by_engine = {e: _stats([t for t in all_trades if t["engine"] == e]) for e in ENGINES}
    by_regime = {r: _stats([t for t in all_trades if t["regime"] == r])
                 for r in ("BULL", "BEAR", "NEUTRAL")}
    return {
        "overall": _stats(all_trades),
        "by_engine": by_engine,
        "by_regime": by_regime,
        "portfolio": {k: v for k, v in portfolio.items() if k != "accepted"},
    }
