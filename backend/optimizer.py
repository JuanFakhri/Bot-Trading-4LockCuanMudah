"""Adaptive parameter optimizer — "turn losses into wins".

Blocking losing patterns avoids repeating mistakes; this goes one step further:
it searches a small grid of tunable settings (SL distance, minimum RR,
confirmation window, whether the A/D filter is required) for the combination
that most improves expectancy — then VALIDATES it out-of-sample before adopting.

Anti-overfit guardrails:
  * The grid is selected on the older TRAIN split only.
  * The winner must also beat the baseline on the unseen TEST split.
  * Minimum sample sizes are required, otherwise the baseline is kept.

The chosen parameters are written to ``data/tuning.json`` and applied to the
LIVE engine (via ``backend.tuning``), so the bot literally retunes itself from
what it learned in the backtest.
"""
from __future__ import annotations

import pandas as pd

from . import backtester, config

# search grid (baseline defaults are included)
GRID_SL = [0.5, 1.0, 1.5]
GRID_RR = [1.5, 2.0, 2.5]
GRID_AD = [True, False]

MIN_TRAIN = 8
MIN_TEST = 4


def _expectancy(trades: list[dict]) -> float:
    return round(sum(t["r"] for t in trades) / len(trades), 3) if trades else 0.0


def _win_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    return round(sum(1 for t in trades if t["r"] > 0.05) / len(trades) * 100, 1)


def _run_all(symbol_data: dict, regime, usdtd, params: dict) -> list[dict]:
    out = []
    for sym, (htf, dtf) in symbol_data.items():
        out.extend(backtester.backtest_symbol(sym, htf, dtf, regime, usdtd, params))
    return out


def _split(trades: list[dict], cutoff: pd.Timestamp):
    train = [t for t in trades if pd.Timestamp(t["entry_ts"]) < cutoff]
    test = [t for t in trades if pd.Timestamp(t["entry_ts"]) >= cutoff]
    return train, test


def optimize(symbol_data: dict, regime, usdtd, cutoff_frac: float = 0.7) -> dict:
    baseline_params = {"sl_atr": config.SL_ATR_MULT, "min_rr": config.MIN_RR,
                       "confirm_bars": config.CONFIRM_MAX_BARS, "require_ad": True}
    base_trades = _run_all(symbol_data, regime, usdtd, baseline_params)
    if len(base_trades) < (MIN_TRAIN + MIN_TEST):
        return {"accepted": False, "reason": "Sampel terlalu sedikit untuk optimasi aman.",
                "params": baseline_params, "baseline": _metrics(base_trades, base_trades[:0]),
                "tuned": None, "grid": len(GRID_SL) * len(GRID_RR) * len(GRID_AD)}

    entries = sorted(pd.Timestamp(t["entry_ts"]) for t in base_trades)
    cutoff = entries[int(len(entries) * cutoff_frac)]
    b_train, b_test = _split(base_trades, cutoff)
    base_train_exp = _expectancy(b_train)
    base_test_exp = _expectancy(b_test)

    best = None
    for sl in GRID_SL:
        for rr in GRID_RR:
            for ad in GRID_AD:
                params = {"sl_atr": sl, "min_rr": rr,
                          "confirm_bars": config.CONFIRM_MAX_BARS, "require_ad": ad}
                trades = base_trades if params == baseline_params else _run_all(symbol_data, regime, usdtd, params)
                train, test = _split(trades, cutoff)
                if len(train) < MIN_TRAIN:
                    continue
                exp = _expectancy(train)
                if best is None or exp > best["train_exp"]:
                    best = {"params": params, "train": train, "test": test, "train_exp": exp}

    if best is None:
        return {"accepted": False, "reason": "Tidak ada kombinasi dengan cukup trade di data latih.",
                "params": baseline_params, "baseline": _metrics(b_train, b_test), "tuned": None,
                "grid": len(GRID_SL) * len(GRID_RR) * len(GRID_AD)}

    tuned_test_exp = _expectancy(best["test"])
    accepted = (best["params"] != baseline_params
                and len(best["test"]) >= MIN_TEST
                and tuned_test_exp > base_test_exp)

    return {
        "accepted": accepted,
        "reason": ("Parameter baru menang di data uji (out-of-sample)." if accepted
                   else "Optimasi tidak lebih baik out-of-sample — tetap pakai default."),
        "params": best["params"] if accepted else baseline_params,
        "baseline": _metrics(b_train, b_test),
        "tuned": _metrics(best["train"], best["test"]),
        "cutoff_ts": cutoff.isoformat(),
        "grid": len(GRID_SL) * len(GRID_RR) * len(GRID_AD),
    }


def _metrics(train: list[dict], test: list[dict]) -> dict:
    return {
        "train": {"n": len(train), "win_rate": _win_rate(train), "expectancy_r": _expectancy(train)},
        "test": {"n": len(test), "win_rate": _win_rate(test), "expectancy_r": _expectancy(test)},
    }
