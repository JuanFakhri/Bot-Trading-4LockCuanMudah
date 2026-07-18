"""Section E (risk) — per-day / per-symbol guard rails.

The trade plan (SL / TP1-3 / size) is built by the SMC strategy itself in
``strategy_smc``; this module only enforces the risk limits.
"""
from __future__ import annotations

from . import config


class RiskGuard:
    """Per-symbol cooldown only. No daily trade cap and no win/loss circuit
    breaker — the bot trades freely; the only things tracked are open positions
    (via the DB) and cumulative real Total R."""

    def __init__(self):
        self.total_r = 0.0                    # cumulative realized R (real)
        self.cooldown: dict[str, float] = {}  # symbol -> unix ts when free again

    def can_enter(self, symbol: str, today: str, now_ts: float) -> tuple[bool, str]:
        free_at = self.cooldown.get(symbol, 0.0)
        if now_ts < free_at:
            return False, f"Cooldown (sisa {int((free_at - now_ts) / 60)}m)"
        return True, "OK"

    def register_entry(self, today: str):
        pass  # no daily cap to track

    def register_exit(self, symbol: str, r_multiple: float, bar_sec: float, now_ts: float):
        self.total_r += r_multiple
        self.cooldown[symbol] = now_ts + config.COOLDOWN_BARS * bar_sec

    def snapshot(self) -> dict:
        # only open positions + Total R matter; nothing is ever "halted"
        return {"total_r": round(self.total_r, 2), "halted": False}
