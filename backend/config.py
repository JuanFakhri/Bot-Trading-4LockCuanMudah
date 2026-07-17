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
# v1.1 ablation-validated entry filters. ATR expansion (atr > atr-SMA20, enforced
# in strategy_smc) is always on. The volume multiple dials signal frequency:
#   1.0 -> ATR-only: 41 trades/730d, PF 1.79, OOS 3.66  (more signals, DEFAULT)
#   1.5 -> ATR+Vol : 18 trades/730d, PF 2.60, OOS 2.72  (fewer, higher quality)
SMC_VOL_MULT = float(os.getenv("SMC_VOL_MULT", "1.0"))   # #5 volume > Nx SMA20

# Two-machine architecture (see strategy_smc.evaluate router):
#   LONG  -> Phoenix Hybrid (backend/phoenix.py) — BUILT & wired, but OFF by
#            default. A 3y walk-forward showed the long side does not hold out of
#            sample: full Phoenix PF 0.97 (-10R); breakout-only PF 1.03 in-sample
#            but OOS PF 0.95 (<1 = loses on unseen data). Enable for research /
#            live with SMC_ALLOW_LONG=1 (it will run the breakout engine).
#   SHORT -> classic SMC — the validated edge (64% win, PF 1.62, OOS 2.85 over 3y).
# Live therefore runs SHORT-ONLY until the long side proves an out-of-sample edge.
SMC_ALLOW_LONG = os.getenv("SMC_ALLOW_LONG", "0") == "1"
SMC_ALLOW_SHORT = os.getenv("SMC_ALLOW_SHORT", "1") == "1"

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
MAX_TRADES_PER_DAY = 5
DAILY_DD_STOP = -0.08       # circuit breaker: stop after -8% day
DAILY_SL_STOP = 2           # circuit breaker: stop after 2 stop-losses

# --------------------------------------------------------------------------
# Self-learning
# --------------------------------------------------------------------------
LEARN_MIN_SAMPLES = 5           # min resolved trades before a lesson is trusted
LEARN_BLOCK_WINRATE = 0.35      # pattern win-rate below this gets blocked
# Prior 1.0 lets the learning confidence swing wide — up to ~95% for strongly-
# winning patterns. Floor kept low (0.15) so signals stay frequent.
LEARN_PRIOR_ALPHA = 1.0         # Bayesian prior wins
LEARN_PRIOR_BETA = 1.0          # Bayesian prior losses
CONFIDENCE_FLOOR = 0.15         # signals below this confidence are hidden

# --------------------------------------------------------------------------
# Phoenix Hybrid — the LIVE LONG machine (backend/phoenix.py). Two entry engines
# fire in a BULL regime: FIB retrace + momentum breakout. Backtested via
# scripts/backtest_phoenix.py (Phoenix-long + SMC-short combined). Tuning here
# changes live long entries.
# --------------------------------------------------------------------------
PHX_NEUTRAL_BAND = 0.02          # BTC EMA50 flat within ±2% over N days -> NEUTRAL
PHX_NEUTRAL_DAYS = 5
PHX_VOL_MIN_PCT = 1.5            # ATR(14) 4H must exceed this % of price for trend engines
# Engine 1 — FIB retrace
PHX_FIB_IMPULSE_ATR = 2.5        # min impulse leg size (ATR multiples), down from 3
PHX_FIB_ZONE_LO = 0.382         # wider golden zone
PHX_FIB_ZONE_HI = 0.618
PHX_FIB_CONFIRM_MIN = 2          # need 2 of 3 (BOS / RSI turn / volume) triggers
# Engine 2 — Momentum breakout
PHX_BRK_LOOKBACK = 20            # break the high/low of the last N bars
PHX_BRK_VOL_MULT = 1.5          # volume > 1.5x SMA20
PHX_BRK_ATR_MIN = 0.5           # ATR(14) 1H > 0.5% of price
PHX_BRK_RSI = 55                # RSI 4H > 55 (long) / < 45 (short)
# Which long engines are LIVE. A 3y backtest showed the FIB-retrace engine
# over-trades and loses (-25R over 620 trades), while momentum breakout is net
# positive (+15R). So live runs BREAKOUT-ONLY; enable fib via env for research.
PHX_ENGINE_BREAKOUT = os.getenv("PHX_ENGINE_BREAKOUT", "1") == "1"
PHX_ENGINE_FIB = os.getenv("PHX_ENGINE_FIB", "0") == "1"
# Engine 3 — Range mean-reversion
PHX_RANGE_MIN_ATR = 2.0          # range width must be >= 2x ATR
PHX_RANGE_WINDOW = 40            # bars used to define the range
PHX_RANGE_HOLD_BARS = 12         # range must have held >= 12h (12x 1H bars)
PHX_RANGE_RR = 1.5               # min reward:risk for a range trade
PHX_RANGE_RSI_LO = 35            # oversold (long) / 100-this overbought (short)
# Exits
PHX_SL_ATR = 0.8                 # SL beyond swing/OB by 0.8 ATR (tighter -> better RR)
PHX_TIME_STOP_BARS = 12          # close half if < 0.5R after N bars
PHX_COOLDOWN_BARS = 10           # per-pair cooldown after an exit
# Risk / recovery (portfolio level)
PHX_RISK_TREND = 0.015           # 1.5% equity per FIB/breakout trade
PHX_RISK_RANGE = 0.005           # 0.5% equity per range trade
PHX_RISK_RECOVERY = 0.01         # 1% while in recovery mode
PHX_RECOVERY_DD = 0.10           # enter recovery after -10% from equity peak
PHX_RECOVERY_EXIT = 0.95         # leave recovery once back to 95% of peak
PHX_DAILY_MAX_LOSS = 0.04        # stop for the day after -4%
PHX_WEEKLY_MAX_LOSS = 0.08       # stop for the week after -8%
PHX_MAX_CONCURRENT = 3           # max simultaneous open positions
PHX_RECOVERY_MAX_TRADES_DAY = 2  # cap trades/day while recovering

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

# Economic calendar (ForexFactory this-week feed, free JSON mirror). Used to warn
# on the dashboard before high-impact news — the bot avoids trading around them.
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_ALERT_HOURS = 4          # show the "news ahead" warning this many hours before
WIB_OFFSET_HOURS = 7          # WIB = UTC+7 (display only; stored times stay UTC)
