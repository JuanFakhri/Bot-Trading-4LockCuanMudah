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


def _tf_trend(df, bars: int, band: float) -> str:
    """BULL/BEAR/NEUTRAL from EMA50 change over `bars`, flat within +/-band.
    Same rule as the trading regime, applied per timeframe (DISPLAY ONLY — the
    trade decision still uses BTC 1D; a regime sweep proved 4H/1H gating loses)."""
    if df is None or len(df) < config.EMA_FAST + bars + 2:
        return "NEUTRAL"
    ema = indicators.ema(df["close"], config.EMA_FAST)
    prev = float(ema.iloc[-1 - bars])
    if prev == 0:
        return "NEUTRAL"
    chg = (float(ema.iloc[-1]) - prev) / abs(prev)
    if chg > band:
        return "BULL"
    if chg < -band:
        return "BEAR"
    return "NEUTRAL"


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
    btc4h = await data_feed.get_klines("BTCUSDT", "4h", limit=260)
    btc1h = await data_feed.get_klines("BTCUSDT", "1h", limit=400)
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
    if btc_ema50 is not None and len(btc) > config.EMA_FAST + config.PHX_NEUTRAL_DAYS + 2:
        ema50 = indicators.ema(btc["close"], config.EMA_FAST)
        prev = float(ema50.iloc[-1 - config.PHX_NEUTRAL_DAYS])
        chg = (float(ema50.iloc[-1]) - prev) / abs(prev) if prev else 0.0
        btc_rising = chg > 0
        # SAME definition as the backtest (phoenix_backtester.btc_regime_daily):
        # a real trend (> +/-band over N days) -> BULL/BEAR, otherwise NEUTRAL.
        # This makes live trade exactly the regimes the strategy was validated on
        # (strong trend only); flat/sideways -> NEUTRAL, bot idles (no trade),
        # instead of the old loose 3-bar slope that traded every single day.
        if chg > config.PHX_NEUTRAL_BAND:
            regime = "BULL"
        elif chg < -config.PHX_NEUTRAL_BAND:
            regime = "BEAR"
        else:
            regime = "NEUTRAL"

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

    _band, _days = config.PHX_NEUTRAL_BAND, config.PHX_NEUTRAL_DAYS
    btc_tf = {
        "1d": _tf_trend(btc, _days, _band),          # 5 daily bars
        "4h": _tf_trend(btc4h, _days * 6, _band),    # 5 days = 30x 4H bars
        "1h": _tf_trend(btc1h, _days * 24, _band),   # 5 days = 120x 1H bars
    }

    return {
        "regime": regime,
        "btc_tf": btc_tf,
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
