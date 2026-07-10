"""Section D (exit) & E (risk) — position sizing, SL/TP and guard rails."""
from __future__ import annotations

from . import config, tuning


def _liquidity_tp(levels, entry, risk, min_rr, direction, fib_ext):
    """Pick the nearest opposing liquidity (swing) that still yields RR >= min_rr.

    ``levels`` are ordered from nearest to farthest in the target direction
    (swing highs ascending for longs, swing lows descending for shorts).
    Falls back to the fib 1.272 extension when no swing qualifies.
    """
    for lvl in levels:
        beyond = lvl > entry if direction == "LONG" else lvl < entry
        if beyond and (abs(lvl - entry) / risk) >= min_rr:
            return lvl, "likuiditas"
    return fib_ext, "fib-ext"


def build_trade_plan(signal: dict, equity: float = 1000.0) -> dict | None:
    """Compute SL / TP1 / TP2 / size for an ENTRY signal.

    SL sits (tuned) x ATR beyond the impulse swing (capped at 6%).
    TP1 = +1R (close 50%, move SL to breakeven +0.15%).
    TP2 = fib 1.272 extension, required only if RR >= tuned min (default 2).

    ``sl_atr`` and ``min_rr`` are taken from the optimizer's tuning if present,
    otherwise the config defaults.
    """
    direction = signal["direction"]
    entry = signal["price"]
    atr = signal["atr"]
    fib = signal["fib"]
    sl_atr = float(tuning.get("sl_atr", config.SL_ATR_MULT))
    min_rr = float(tuning.get("min_rr", config.MIN_RR))

    if direction == "LONG":
        swing = signal["impulse_start"]  # swing low of the impulse
        raw_sl = min(swing, entry) - sl_atr * atr
        sl = max(raw_sl, entry * (1 - config.SL_CAP_PCT))
        risk = entry - sl
        if risk <= 0:
            return None
        tp1 = entry + risk
        fib_ext = fib.get("ext_1.272", entry + 2 * risk)
        tp2, tp_source = _liquidity_tp(signal.get("swing_highs", []), entry, risk,
                                       min_rr, "LONG", fib_ext)
    else:
        swing = signal["impulse_start"]  # swing high of the impulse
        raw_sl = max(swing, entry) + sl_atr * atr
        sl = min(raw_sl, entry * (1 + config.SL_CAP_PCT))
        risk = sl - entry
        if risk <= 0:
            return None
        tp1 = entry - risk
        fib_ext = fib.get("ext_1.272", entry - 2 * risk)
        tp2, tp_source = _liquidity_tp(signal.get("swing_lows", []), entry, risk,
                                       min_rr, "SHORT", fib_ext)

    reward = abs(tp2 - entry)
    rr = reward / risk if risk else 0.0

    qty = (equity * config.RISK_PER_TRADE) / risk
    be = entry * (1 + config.BE_BUFFER_PCT) if direction == "LONG" \
        else entry * (1 - config.BE_BUFFER_PCT)

    return {
        "entry": round(entry, 8),
        "sl": round(sl, 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
        "breakeven": round(be, 8),
        "risk_per_unit": round(risk, 8),
        "rr": round(rr, 2),
        "rr_ok": rr >= min_rr,
        "tp_source": tp_source,
        "position_size": round(qty, 6),
        "risk_pct": config.RISK_PER_TRADE,
        "sl_pct": round(abs(entry - sl) / entry, 4),
    }


class RiskGuard:
    """Tracks per-day / per-symbol limits (Section E)."""

    def __init__(self):
        self.day = None
        self.trades_today = 0
        self.stops_today = 0
        self.pnl_today = 0.0        # in R units (approx equity fraction)
        self.cooldown: dict[str, float] = {}  # symbol -> unix ts when free again

    def _roll_day(self, today: str):
        if self.day != today:
            self.day = today
            self.trades_today = 0
            self.stops_today = 0
            self.pnl_today = 0.0

    def can_enter(self, symbol: str, today: str, now_ts: float) -> tuple[bool, str]:
        self._roll_day(today)
        if self.trades_today >= config.MAX_TRADES_PER_DAY:
            return False, "Maks 3 trade/hari tercapai"
        if self.stops_today >= config.DAILY_SL_STOP:
            return False, "Circuit breaker: 2 SL hari ini"
        if self.pnl_today <= config.DAILY_DD_STOP:
            return False, "Circuit breaker: -8% hari ini"
        free_at = self.cooldown.get(symbol, 0.0)
        if now_ts < free_at:
            return False, f"Cooldown 16 bar (sisa {int((free_at-now_ts)/60)}m)"
        return True, "OK"

    def register_entry(self, today: str):
        self._roll_day(today)
        self.trades_today += 1

    def register_exit(self, symbol: str, r_multiple: float, bar_sec: float, now_ts: float):
        self.pnl_today += r_multiple * config.RISK_PER_TRADE
        if r_multiple < 0:
            self.stops_today += 1
        self.cooldown[symbol] = now_ts + config.COOLDOWN_BARS * bar_sec

    def snapshot(self) -> dict:
        return {
            "trades_today": self.trades_today,
            "stops_today": self.stops_today,
            "pnl_today_pct": round(self.pnl_today * 100, 2),
            "max_trades": config.MAX_TRADES_PER_DAY,
            "halted": self.stops_today >= config.DAILY_SL_STOP
            or self.pnl_today <= config.DAILY_DD_STOP,
        }
