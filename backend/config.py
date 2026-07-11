"""Central configuration for the FIB Hybrid bot.

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
IMPULSE_MIN_ATR = 3.0   # impulse range must be >= 3x ATR
IMPULSE_MAX_AGE = 60    # impulse pivot age (bars) must be <= 60
CONFIRM_MAX_BARS = 16   # bars allowed between ARM and confirmation

# --------------------------------------------------------------------------
# Active live strategy: "smc" (SMC + AI-Score confluence, backtest-validated:
# PF 1.44 over 212 trades, walk-forward OOS PF 2.09) or "fib" (original).
# --------------------------------------------------------------------------
STRATEGY = os.getenv("BOT_STRATEGY", "smc").strip().lower()
SMC_SCORE_TH = float(os.getenv("SMC_SCORE_TH", "55"))   # AI-Score gate for a live entry
SMC_ATR_MIN = 0.3       # entry only when 1H ATR is 0.3%..8% of price (#14)
SMC_ATR_MAX = 8.0

# Golden zone (fibonacci retracement of the impulse)
FIB_ZONE_LO = 0.5
FIB_ZONE_HI = 0.618
FIB_INVALID = 0.786     # deeper retrace than this invalidates the impulse
FIB_EXTENSION = 1.272   # TP2 projection

# USDT.D resistance threshold (20-day range position) for the short machine
USDTD_POS_HI = 0.7
# Short trigger requires USDT.D at resistance (canonical spec C.4: pos>0.7).
USDTD_SHORT_POS = 0.7
USDTD_LOOKBACK = 20

# RSI trigger zones on the LTF
LONG_ARM_RSI = (30.0, 50.0)
SHORT_ARM_RSI = (50.0, 70.0)

# --------------------------------------------------------------------------
# Risk
# --------------------------------------------------------------------------
RISK_PER_TRADE = 0.02       # 2% equity per trade
SL_ATR_MULT = 1.0           # SL placed 1x ATR outside swing/OB
SL_CAP_PCT = 0.06           # SL never wider than 6%
BE_BUFFER_PCT = 0.0015      # +0.15% breakeven buffer after TP1
MIN_RR = 2.0                # TP2 requires RR >= 2
COOLDOWN_BARS = 16          # per-symbol cooldown after an exit
MAX_TRADES_PER_DAY = 3
DAILY_DD_STOP = -0.08       # circuit breaker: stop after -8% day
DAILY_SL_STOP = 2           # circuit breaker: stop after 2 stop-losses

SKIP_FRIDAY_LONG = True     # weekend-gap protection for the bull machine

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
