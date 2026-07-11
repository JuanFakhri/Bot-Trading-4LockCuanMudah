"""Central configuration for the SMC bot.

Everything is tuned to the strategy document. Values that the self-learning
engine is allowed to nudge live in the database, not here — this file only
holds the *starting* (prior) values and hard limits.
"""
from __future__ import annotations

import os

# --------------------------------------------------------------------------
# Universe & timeframes
# --------------------------------------------------------------------------
# Symbols scanned for signals (Binance spot USDT pairs). BTC/USDT is always
# fetched separately to compute the market regime.
WATCHLIST: list[str] = [
    "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "DOTUSDT", "NEARUSDT",
    "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "SUIUSDT",
]

# Higher-time-frame (bias & fibonacci) and lower-time-frame (trigger).
HTF = "4h"          # impulse / structure / fib
DTF = "1d"          # daily filters (EMA200 1D, BTC EMA50 1D)
LTF = "15m"         # entry trigger
KLIMIT = 400        # candles pulled per request

SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "60"))

# Demo mode: feed synthetic data through the real pipeline (no external API).
DEMO = os.getenv("BOT_DEMO", "0") == "1"

# --------------------------------------------------------------------------
# Indicator parameters
# --------------------------------------------------------------------------
EMA_FAST = 50
EMA_SLOW = 200
RSI_LEN = 14
ATR_LEN = 14
PIVOT_LEN = 5           # left/right bars for swing pivot

# --------------------------------------------------------------------------
# Strategy: SMC + AI-Score confluence (the only strategy; backtest-validated —
# PF 1.41 over 69 trades at score_th 60, walk-forward OOS PF 1.90).
# --------------------------------------------------------------------------
SMC_SCORE_TH = float(os.getenv("SMC_SCORE_TH", "60"))   # AI-Score gate for a live entry
# Threshold sweep (730d, real data): 55 -> PF 1.44 / OOS 2.09 / DD -12.5R (aggressive);
# 60 -> PF 1.41 / OOS 1.90 / DD -6.0R (best risk-adjusted, DEFAULT); 65/70 -> OOS
# collapses on a tiny sample (overfit). 60 keeps the edge with half the drawdown.
SMC_ATR_MIN = 0.3       # entry only when 1H ATR is 0.3%..8% of price (#14)
SMC_ATR_MAX = 8.0

# Golden zone (fibonacci retracement) — one component of the AI Score
FIB_ZONE_LO = 0.5
FIB_ZONE_HI = 0.618

# USDT.D range position (20-day) used by the market filter / macro score
USDTD_POS_HI = 0.7
USDTD_SHORT_POS = 0.7
USDTD_LOOKBACK = 20

# --------------------------------------------------------------------------
# Risk
# --------------------------------------------------------------------------
RISK_PER_TRADE = 0.02       # 2% equity per trade
SL_CAP_PCT = 0.06           # SL never wider than 6%
BE_BUFFER_PCT = 0.0015      # +0.15% breakeven buffer after TP1
COOLDOWN_BARS = 16          # per-symbol cooldown after an exit
MAX_TRADES_PER_DAY = 3
DAILY_DD_STOP = -0.08       # circuit breaker: stop after -8% day
DAILY_SL_STOP = 2           # circuit breaker: stop after 2 stop-losses

# --------------------------------------------------------------------------
# Self-learning
# --------------------------------------------------------------------------
LEARN_MIN_SAMPLES = 5           # min resolved trades before a lesson is trusted
LEARN_BLOCK_WINRATE = 0.35      # pattern win-rate below this gets blocked
LEARN_PRIOR_ALPHA = 3.0         # Bayesian prior wins (smoothing)
LEARN_PRIOR_BETA = 3.0          # Bayesian prior losses (smoothing)
CONFIDENCE_FLOOR = 0.15         # signals below this confidence are hidden

DB_PATH = os.getenv("BOT_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "bot.db"))

# Data sources (all free, no API key required)
BINANCE_BASES = [
    # data-api.binance.vision is Binance's public market-data mirror and is NOT
    # geo-restricted, so it works from US-based GitHub Actions runners where
    # api.binance.com returns HTTP 451. Tried first for that reason.
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
]
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
