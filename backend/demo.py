"""Demo data provider.

Activated with the env var ``BOT_DEMO=1``. It feeds *synthetic* but realistic
OHLCV through the exact same pipeline (regime → strategy → signals → learning),
so the whole app is fully explorable without any external API access. This is
purely for previewing the UI; live mode uses real Binance/CoinGecko data.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

_INTERVAL_SEC = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

# Per-symbol target 4H retrace ratio → drives variety of signal states.
_RETRACE = {
    "ETHUSDT": 0.55, "SOLUSDT": 0.58, "BNBUSDT": 0.30, "XRPUSDT": 0.61,
    "ADAUSDT": 0.52, "AVAXUSDT": 0.20, "LINKUSDT": 0.56, "DOGEUSDT": 0.80,
    "DOTUSDT": 0.45, "NEARUSDT": 0.60, "APTUSDT": 0.53, "ARBUSDT": 0.15,
    "OPUSDT": 0.57, "INJUSDT": 0.62, "SUIUSDT": 0.50, "BTCUSDT": 0.4,
}


def _seed(symbol: str) -> int:
    return int(hashlib.md5(symbol.encode()).hexdigest(), 16) % (2**32)


def klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    rng = np.random.default_rng(_seed(symbol) + _INTERVAL_SEC.get(interval, 60))
    n = limit
    base = 10 + (_seed(symbol) % 200)
    sec = _INTERVAL_SEC.get(interval, 3600)
    idx = pd.date_range(end=datetime.now(timezone.utc), periods=n, freq=f"{sec}s")

    if interval in ("4h", "1d"):
        # bullish structure: base -> impulse up -> partial retrace into zone
        top = base * 1.45
        r = _RETRACE.get(symbol, 0.5)
        cur = top - r * (top - base)
        imp_top = n - 12               # impulse high ~12 bars ago (age <= 60)
        imp_low = imp_top - 30
        close = np.empty(n)
        for i in range(n):
            if i < imp_low:
                close[i] = base + rng.normal(0, base * 0.004) + (base * 0.05) * (i / imp_low)
            elif i <= imp_top:
                close[i] = base * 1.05 + (top - base * 1.05) * ((i - imp_low) / (imp_top - imp_low))
            else:
                close[i] = top - (top - cur) * ((i - imp_top) / (n - 1 - imp_top))
            close[i] += rng.normal(0, base * 0.003)
        # shallow retraces (<=0.55) bounce on the last few bars: RSI recovers
        # above 50 and A/D turns up -> a valid, confirmable entry setup.
        if r <= 0.55:
            close[-8:] += np.linspace(0, base * 0.10, 8)
        close = np.abs(close)
    else:
        # LTF: mild recent dip so RSI lands in the ARM zone, then a green tick
        drift = rng.normal(0, base * 0.002, n).cumsum()
        close = base * 1.2 + drift
        close[-6:] -= np.linspace(0, base * 0.02, 6)
        close[-1] += base * 0.015      # last candle bounces green

    close = np.abs(close) + 0.001
    wig = np.abs(rng.normal(0, close * 0.004))
    high = close + wig
    low = np.clip(close - wig, 0.0001, None)
    op = np.concatenate([[close[0]], close[:-1]])
    # volume higher on up bars (helps A/D / OBV)
    up = np.concatenate([[1], np.sign(np.diff(close))])
    vol = np.abs(rng.normal(1000, 200, n)) * (1.6 * (up > 0) + 0.6)

    return pd.DataFrame(
        {"open": op, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def usdt_dominance() -> dict:
    return {"value": 4.82, "pos": 0.34, "rising": False, "ok": True}
