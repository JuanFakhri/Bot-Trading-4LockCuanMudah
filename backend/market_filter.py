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

    alt_pred = _ALT_MATRIX.get((btcd_dir, btc_dir), "STABIL")  # info only now

    # ---- DECISION ----
    # Primary: USDT.D. Heading to support (falling) -> LONG; to resistance
    # (rising) -> SHORT. BUT if USDT.D is CONSOLIDATING (sideways, no clear
    # target), fall back to the BTC + BTC.D dominance matrix.
    at_support = at_resistance = consolidating = False
    usdtd_target = "–"
    decider = "USDT.D"
    alt_bias, regime = "NEUTRAL", "NEUTRAL"
    if usdtd.get("ok"):
        at_support = usdtd["pos"] < (1 - config.USDTD_POS_HI)   # pos < 0.3
        at_resistance = usdtd["pos"] > config.USDTD_POS_HI       # pos > 0.7
        consolidating = bool(usdtd.get("consolidating")) and not at_support and not at_resistance

        if at_resistance:
            alt_bias, regime, usdtd_target = "SHORT", "BEAR", "di resistance"
        elif at_support:
            alt_bias, regime, usdtd_target = "LONG", "BULL", "di support"
        elif consolidating:
            # USDT.D ranging -> use BTC.D matrix (image rules)
            decider = "Matriks BTC.D"
            usdtd_target = "konsolidasi"
            alt_bias = {"NAIK": "LONG", "TURUN": "SHORT", "STABIL": "NEUTRAL"}[alt_pred]
            regime = {"LONG": "BULL", "SHORT": "BEAR", "NEUTRAL": "NEUTRAL"}[alt_bias]
        elif usdtd["rising"]:
            alt_bias, regime, usdtd_target = "SHORT", "BEAR", "menuju resistance"
        else:
            alt_bias, regime, usdtd_target = "LONG", "BULL", "menuju support"

    usdtd_bias = alt_bias

    return {
        "regime": regime,
        "alt_bias": alt_bias,
        "alt_prediction": alt_pred,
        "usdtd_target": usdtd_target,
        "usdtd_consolidating": consolidating,
        "decider": decider,
        "btc_ema50": btc_ema50,
        "btc_ema50_rising": (btc_dir == "NAIK") if btc_dir != "STABIL" else None,
        "btc_dir": btc_dir,
        "btcd_value": btcd.get("value"),
        "btcd_dir": btcd_dir,
        "usdtd_value": usdtd.get("value"),
        "usdtd_pos": usdtd.get("pos"),
        "usdtd_rising": usdtd.get("rising"),
        "usdtd_bias": usdtd_bias,
        "usdtd_at_support": bool(usdtd.get("ok") and usdtd["pos"] < (1 - config.USDTD_POS_HI)),
        "usdtd_at_resistance": bool(usdtd.get("ok") and usdtd["pos"] > config.USDTD_POS_HI),
        "usdtd_ok": bool(usdtd.get("ok")),
    }
