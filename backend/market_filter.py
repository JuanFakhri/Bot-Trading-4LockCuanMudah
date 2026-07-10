"""Section A — market filter that decides which engine is active.

The setup is read in the order the trader asked for:

  1. USDT.D first — falling toward support = risk-on (money leaving stablecoins
     into alts); rising toward resistance = risk-off (alts bleed).
  2. BTC direction + BTC dominance (BTC.D) via the dominance matrix:

        BTC.D  +  BTC   =  ALT
        NAIK      NAIK      TURUN
        NAIK      TURUN     TURUN
        NAIK      STABIL    STABIL
        TURUN     NAIK      NAIK      <- best for alts
        TURUN     TURUN     STABIL
        TURUN     STABIL    NAIK

  3. The resulting ALT bias picks the machine (LONG/SHORT). TP/SL are then taken
     from liquidity (swing highs/lows ± ATR) in risk.py.

BTC.D direction uses the ETH/BTC ratio as a free proxy: ETH/BTC rising means
alts outperform BTC, i.e. dominance falling (and vice-versa).
"""
from __future__ import annotations

from . import config, data_feed, indicators

_ALT_MATRIX = {
    ("NAIK", "NAIK"): "TURUN",
    ("NAIK", "TURUN"): "TURUN",
    ("NAIK", "STABIL"): "STABIL",
    ("TURUN", "NAIK"): "NAIK",
    ("TURUN", "TURUN"): "STABIL",
    ("TURUN", "STABIL"): "NAIK",
}


def _direction(ema, lookback: int = 3, deadband: float = 0.005) -> str:
    """NAIK / TURUN / STABIL from an EMA series slope with a flat deadband."""
    if ema is None or len(ema) <= lookback:
        return "STABIL"
    now = float(ema.iloc[-1])
    prev = float(ema.iloc[-1 - lookback])
    if prev == 0:
        return "STABIL"
    pct = (now - prev) / abs(prev)
    if pct > deadband:
        return "NAIK"
    if pct < -deadband:
        return "TURUN"
    return "STABIL"


async def compute_regime() -> dict:
    btc = await data_feed.get_klines("BTCUSDT", config.DTF, limit=260)
    ethbtc = await data_feed.get_klines("ETHBTC", config.DTF, limit=120)
    usdtd = await data_feed.get_usdt_dominance()
    btcd = await data_feed.get_btc_dominance()

    # ---- 2a. BTC direction (1D EMA50) ----
    btc_ema50 = None
    btc_dir = "STABIL"
    if len(btc) > config.EMA_FAST + 5:
        ema50 = indicators.ema(btc["close"], config.EMA_FAST)
        btc_ema50 = float(ema50.iloc[-1])
        btc_dir = _direction(ema50)

    # ---- 2b. BTC.D direction via ETH/BTC proxy (inverse) ----
    btcd_dir = "STABIL"
    if len(ethbtc) > config.EMA_FAST + 5:
        eth_ema = indicators.ema(ethbtc["close"], config.EMA_FAST)
        ethbtc_dir = _direction(eth_ema)
        btcd_dir = {"NAIK": "TURUN", "TURUN": "NAIK", "STABIL": "STABIL"}[ethbtc_dir]

    alt_pred = _ALT_MATRIX.get((btcd_dir, btc_dir), "STABIL")  # info only

    # ---- DECISION (canonical spec) ----
    # Section A: BTC EMA50 1D is the MAIN market direction — rising = BULL (long
    # machine), falling = BEAR (short machine). USDT.D is confirmation/leading
    # signal (support = risk-on for longs, resistance = risk-off for shorts);
    # the short machine additionally requires USDT.D at resistance (pos>0.7, C.4).
    btc_rising = None
    regime = "NEUTRAL"
    if btc_ema50 is not None and len(btc) > config.EMA_FAST + 5:
        ema50 = indicators.ema(btc["close"], config.EMA_FAST)
        btc_rising = bool(ema50.iloc[-1] > ema50.iloc[-4])
        regime = "BULL" if btc_rising else "BEAR"

    alt_bias = "LONG" if regime == "BULL" else "SHORT" if regime == "BEAR" else "NEUTRAL"
    decider = "BTC EMA50 1D"

    at_support = at_resistance = False
    usdtd_bias = "NEUTRAL"
    if usdtd.get("ok"):
        at_support = usdtd["pos"] < (1 - config.USDTD_POS_HI)
        at_resistance = usdtd["pos"] > config.USDTD_POS_HI
        if at_resistance or usdtd["rising"]:
            usdtd_bias = "SHORT"
        else:
            usdtd_bias = "LONG"
    usdtd_target = ("di resistance" if at_resistance else "di support" if at_support
                    else "menuju resistance" if usdtd.get("rising") else "menuju support")

    return {
        "regime": regime,
        "alt_bias": alt_bias,
        "alt_prediction": alt_pred,
        "usdtd_target": usdtd_target,
        "usdtd_consolidating": False,
        "decider": decider,
        "btc_ema50": btc_ema50,
        "btc_ema50_rising": btc_rising,
        "btc_dir": btc_dir,
        "btcd_value": btcd.get("value"),
        "btcd_dir": btcd_dir,
        "usdtd_value": usdtd.get("value"),
        "usdtd_pos": usdtd.get("pos"),
        "usdtd_rising": usdtd.get("rising"),
        "usdtd_bias": usdtd_bias,
        "usdtd_at_support": bool(usdtd.get("ok") and usdtd["pos"] < (1 - config.USDTD_POS_HI)),
        "usdtd_at_resistance": bool(usdtd.get("ok") and usdtd["pos"] > config.USDTD_POS_HI),
        "usdtd_at_extreme": bool(usdtd.get("ok") and usdtd["pos"] > config.USDTD_SHORT_POS),
        "usdtd_ok": bool(usdtd.get("ok")),
    }
