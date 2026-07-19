"""ABCD / ABC swing-pattern backtester (research only).

Tests the harmonic / zigzag patterns from the user's reference images:

  * Classic ABCD, AB=CD, ABCD Extension  (entry at D)
  * Bullish / bearish ABCD               (buy at D / sell at D)
  * ABC pullback-continuation            (entry at C)

Detection is pivot-based (indicators.find_pivots) — four alternating confirmed
swings A-B-C-D. A trade opens on the bar the D pivot is CONFIRMED (pl bars after
D forms — the earliest the pattern is actually knowable). Exits reuse the LIVE
2-tier engine (TP1 50% @+1R -> breakeven, TP2 @+2R, SL beyond D +/- 1 ATR cap 6%)
so results are directly comparable to the SMC / Phoenix machines.

Read-only: writes nothing. Feeds the same summarize() as smc_backtester.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, indicators
from .smc_backtester import _manage_smc, summarize  # reuse live exit + metrics


def _swings(ltf, pl):
    """Ordered list of confirmed alternating pivots: [(conf_bar, piv_bar, price,
    'H'|'L'), ...]. conf_bar = bar index at which the pivot becomes known."""
    piv_hi, piv_lo = indicators.find_pivots(ltf, pl)
    hi = piv_hi.to_numpy(); lo = piv_lo.to_numpy()
    h = ltf["high"].to_numpy(); l = ltf["low"].to_numpy()
    n = len(ltf)
    raw = []
    for j in range(n):
        if hi[j]:
            raw.append((j + pl, j, float(h[j]), "H"))
        if lo[j]:
            raw.append((j + pl, j, float(l[j]), "L"))
    raw.sort(key=lambda x: x[1])
    # enforce alternation H/L/H/L: if two same-type in a row keep the more extreme
    out = []
    for s in raw:
        if out and out[-1][3] == s[3]:
            prev = out[-1]
            better = s if ((s[3] == "H" and s[2] > prev[2]) or
                           (s[3] == "L" and s[2] < prev[2])) else prev
            out[-1] = better
        else:
            out.append(s)
    return out


def backtest_symbol_abcd(symbol, ltf, params=None) -> list[dict]:
    params = params or {}
    mode = str(params.get("mode", "strict"))     # strict | loose | abc
    allow_long = bool(params.get("allow_long", True))
    allow_short = bool(params.get("allow_short", True))
    pl = int(params.get("pivot_len", config.PIVOT_LEN))
    # BC retrace of AB, and CD size vs AB
    bc_lo = float(params.get("bc_lo", 0.382))
    bc_hi = float(params.get("bc_hi", 0.886))
    cd_lo = float(params.get("cd_lo", 0.9))
    cd_hi = float(params.get("cd_hi", 1.8))
    min_leg_atr = float(params.get("min_leg_atr", 1.0))   # AB must be > Nx ATR
    fib_tp = bool(params.get("fib_tp", False))            # TP at 38.2/61.8% of AD

    if ltf is None or len(ltf) < 260:
        return []
    h = ltf["high"].to_numpy(); l = ltf["low"].to_numpy(); c = ltf["close"].to_numpy()
    atr_1 = indicators.atr(ltf, config.ATR_LEN).to_numpy()
    ema20_1 = indicators.ema(ltf["close"], 20).to_numpy()
    ts = ltf.index
    n = len(ltf)

    sw = _swings(ltf, pl)
    # index swings by the bar at which they confirm
    by_conf: dict[int, list] = {}
    for s in sw:
        by_conf.setdefault(s[0], []).append(s)

    trades: list[dict] = []
    pos = None
    cooldown_until = -1
    seq: list = []   # running alternating swing list, appended as pivots confirm

    for i in range(pl + 5, n):
        # manage open position first (reuse live exit engine; no EMA20 trail here)
        if pos is not None:
            done = _manage_smc(pos, h[i], l[i], c[i], ema20_1[i])
            if done is not None:
                pos.update(outcome=("WIN" if done > 0.05 else "LOSS" if done < -0.05 else "BE"),
                           r=round(done, 3), exit_price=c[i], exit_ts=ts[i].isoformat())
                trades.append(pos)
                cooldown_until = i + (2 if done > 0 else 8)
                pos = None
            continue

        # grow the swing sequence with pivots that just confirmed on bar i
        for s in by_conf.get(i, []):
            if seq and seq[-1][3] == s[3]:
                prev = seq[-1]
                seq[-1] = s if ((s[3] == "H" and s[2] > prev[2]) or
                                (s[3] == "L" and s[2] < prev[2])) else prev
            else:
                seq.append(s)

        if i < cooldown_until or len(seq) < 4:
            continue
        A, B, C, D = seq[-4], seq[-3], seq[-2], seq[-1]
        # D must have just confirmed on this bar (fresh pattern)
        if D[0] != i:
            continue
        pA, pB, pC, pD = A[2], B[2], C[2], D[2]
        ab = abs(pB - pA)
        if ab <= 0 or atr_1[i] <= 0 or ab < min_leg_atr * atr_1[i]:
            continue
        bc = abs(pB - pC)
        cd = abs(pD - pC)
        r_bc = bc / ab
        r_cd = cd / ab

        # ----- classify -----
        # bearish ABCD (sell at D): A low, B high, C low, D high, D>B (higher high)
        bear = (A[3] == "L" and D[3] == "H" and pB > pA and pC > pA and pD > pB)
        # bullish ABCD (buy at D): A high, B low, C high, D low, D<B (lower low)
        bull = (A[3] == "H" and D[3] == "L" and pB < pA and pC < pA and pD < pB)

        machine = None
        if mode in ("strict", "loose"):
            ok_ratio = (mode == "loose") or (bc_lo <= r_bc <= bc_hi and cd_lo <= r_cd <= cd_hi)
            if bear and ok_ratio and allow_short:
                machine = "short"
            elif bull and ok_ratio and allow_long:
                machine = "long"
        elif mode == "abc":
            # ABC continuation: enter at C in the direction of the A->B->... trend.
            # Uptrend continuation: A high, B low, C high with C failing below B's
            # prior high? Use last 3 swings; enter at C break. Simplified: treat the
            # 3-swing zigzag as a pullback-continuation of the B->? leg.
            Ax, Bx, Cx = seq[-3], seq[-2], seq[-1]
            if Cx[0] != i:
                continue
            if Cx[3] == "L" and Bx[3] == "H" and Ax[3] == "L" and Cx[2] > Ax[2] and allow_long:
                machine = "long"; pD = Cx[2]; pA = Ax[2]
            elif Cx[3] == "H" and Bx[3] == "L" and Ax[3] == "H" and Cx[2] < Ax[2] and allow_short:
                machine = "short"; pD = Cx[2]; pA = Ax[2]
        if machine is None:
            continue

        entry = c[i]
        if machine == "short":
            sl = min(pD + atr_1[i], entry * (1 + config.SL_CAP_PCT))
            risk = sl - entry
            if risk <= 0:
                continue
            if fib_tp:
                span = pD - pA
                tp1, tp2, tp3 = entry - 0.382 * span, entry - 0.618 * span, entry - span
            else:
                tp1, tp2, tp3 = entry - risk, entry - 2 * risk, entry - 3 * risk
        else:
            sl = max(pD - atr_1[i], entry * (1 - config.SL_CAP_PCT))
            risk = entry - sl
            if risk <= 0:
                continue
            if fib_tp:
                span = pA - pD
                tp1, tp2, tp3 = entry + 0.382 * span, entry + 0.618 * span, entry + span
            else:
                tp1, tp2, tp3 = entry + risk, entry + 2 * risk, entry + 3 * risk
        if (machine == "short" and tp2 >= entry) or (machine == "long" and tp2 <= entry):
            continue

        pos = {
            "symbol": symbol, "direction": "LONG" if machine == "long" else "SHORT",
            "machine": "abcd_" + machine, "entry": float(entry), "sl": float(sl),
            "tp1": float(tp1), "tp2": float(tp2), "tp3": float(tp3),
            "rr": round(abs(tp2 - entry) / risk, 2), "risk": float(risk),
            "score": int(round(r_bc * 100)), "tp1_hit": False, "tp2_hit": False,
            "rem": 1.0, "realized": 0.0, "stop": float(sl), "tp_source": "abcd",
            "entry_ts": ts[i].isoformat(),
            "features": {"machine": machine, "r_bc": round(r_bc, 3), "r_cd": round(r_cd, 3)},
        }
    return trades
