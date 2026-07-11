"""Technical indicators used by the FIB Hybrid strategy.

Pure functions over pandas Series / DataFrames. Each returns a Series aligned
to the input index so callers can read the latest value with ``.iloc[-1]``.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / length, adjust=False).mean()


def adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Average Directional Index (Wilder)."""
    high, low = df["high"], df["low"]
    up = high.diff()
    down = -low.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    tr = true_range(df)
    atr_ = tr.ewm(alpha=1 / length, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr_.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr_.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / length, adjust=False).mean().fillna(0.0)


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume."""
    direction = np.sign(df["close"].diff().fillna(0.0))
    return (direction * df["volume"]).fillna(0.0).cumsum()


def ad_line(df: pd.DataFrame) -> pd.Series:
    """Accumulation / Distribution line."""
    high, low, close, vol = df["high"], df["low"], df["close"], df["volume"]
    rng = (high - low).replace(0.0, np.nan)
    mfm = ((close - low) - (high - close)) / rng
    mfm = mfm.fillna(0.0)
    return (mfm * vol).cumsum()


def parabolic_sar(df: pd.DataFrame, af_step: float = 0.02, af_max: float = 0.2) -> pd.Series:
    """Classic Wilder Parabolic SAR. Returns SAR value per bar."""
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    n = len(df)
    sar = np.zeros(n)
    if n < 2:
        return pd.Series(sar, index=df.index)

    # initialise: assume uptrend to start
    uptrend = True
    af = af_step
    ep = high[0]
    sar[0] = low[0]

    for i in range(1, n):
        prev_sar = sar[i - 1]
        if uptrend:
            cur = prev_sar + af * (ep - prev_sar)
            cur = min(cur, low[i - 1], low[max(i - 2, 0)])
            if low[i] < cur:  # flip to downtrend
                uptrend = False
                cur = ep
                ep = low[i]
                af = af_step
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af_step, af_max)
            sar[i] = cur
        else:
            cur = prev_sar + af * (ep - prev_sar)
            cur = max(cur, high[i - 1], high[max(i - 2, 0)])
            if high[i] > cur:  # flip to uptrend
                uptrend = True
                cur = ep
                ep = high[i]
                af = af_step
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af_step, af_max)
            sar[i] = cur
    return pd.Series(sar, index=df.index)


def find_pivots(df: pd.DataFrame, length: int = 5) -> tuple[pd.Series, pd.Series]:
    """Return boolean Series marking pivot highs and pivot lows.

    A pivot high at bar i has the highest high within +/- ``length`` bars.
    """
    high = df["high"]
    low = df["low"]
    n = len(df)
    piv_hi = pd.Series(False, index=df.index)
    piv_lo = pd.Series(False, index=df.index)
    for i in range(length, n - length):
        window_hi = high.iloc[i - length : i + length + 1]
        window_lo = low.iloc[i - length : i + length + 1]
        if high.iloc[i] == window_hi.max() and (window_hi.idxmax() == high.index[i]):
            piv_hi.iloc[i] = True
        if low.iloc[i] == window_lo.min() and (window_lo.idxmin() == low.index[i]):
            piv_lo.iloc[i] = True
    return piv_hi, piv_lo


def slope_rising(series: pd.Series, lookback: int = 3) -> bool:
    """True if the series is rising over the last ``lookback`` bars."""
    if len(series) <= lookback:
        return False
    return bool(series.iloc[-1] > series.iloc[-1 - lookback])
