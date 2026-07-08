"""Section A — market filter that decides which engine is active.

BULL  -> the LONG (fib-long) machine is armed.
BEAR  -> the SHORT (fib-short) machine is armed.

Inputs:
  * BTC EMA50 on the 1D chart (rising = BULL, falling = BEAR).
  * USDT.D moving toward support (falling) reinforces LONG; toward resistance
    (rising) reinforces SHORT. USDT.D at resistance (pos > 0.7) is the strongest
    short condition.
"""
from __future__ import annotations

from . import config, data_feed, indicators


async def compute_regime() -> dict:
    btc = await data_feed.get_klines("BTCUSDT", config.DTF, limit=260)
    usdtd = await data_feed.get_usdt_dominance()

    regime = "NEUTRAL"
    btc_ema50 = None
    btc_rising = None
    if len(btc) > config.EMA_FAST + 5:
        ema50 = indicators.ema(btc["close"], config.EMA_FAST)
        btc_ema50 = float(ema50.iloc[-1])
        btc_rising = indicators.slope_rising(ema50, lookback=3)
        regime = "BULL" if btc_rising else "BEAR"

    # USDT.D bias: falling toward support favours longs, rising toward
    # resistance favours shorts.
    usdtd_bias = "NEUTRAL"
    if usdtd.get("ok"):
        if usdtd["rising"] or usdtd["pos"] > config.USDTD_POS_HI:
            usdtd_bias = "SHORT"
        else:
            usdtd_bias = "LONG"

    return {
        "regime": regime,
        "btc_ema50": btc_ema50,
        "btc_ema50_rising": btc_rising,
        "usdtd_value": usdtd.get("value"),
        "usdtd_pos": usdtd.get("pos"),
        "usdtd_rising": usdtd.get("rising"),
        "usdtd_bias": usdtd_bias,
        "usdtd_at_resistance": bool(usdtd.get("ok") and usdtd["pos"] > config.USDTD_POS_HI),
        "usdtd_ok": bool(usdtd.get("ok")),
    }
