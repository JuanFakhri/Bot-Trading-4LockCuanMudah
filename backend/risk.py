"""Section E (risk) — per-day / per-symbol guard rails.

The trade plan (SL / TP1-3 / size) is built by the SMC strategy itself in
``strategy_smc``; this module only enforces the risk limits.
"""
from __future__ import annotations

from . import config


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
            return False, f"Maks {config.MAX_TRADES_PER_DAY} trade/hari tercapai"
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
