"""FASE 2 — order executor (DRY_RUN).

Turns an ENTRY signal into a concrete set of exchange orders and LOGS the plan:

    ENTRY  (market)                 open the position
    SL     (stop-market, reduceOnly) protective stop at 1.0 ATR
    TP1    (limit, reduceOnly, 50%)  bank +1R -> then SL moves to breakeven
    TP2    (limit, reduceOnly, rest) bank +2R  (max RR 2)

Position size comes from the 2%-risk rule: contracts sized so that hitting SL
loses exactly EXEC_RISK_PCT of equity (leverage only affects required margin, not
size). This module does NOT send orders — real placement + lifecycle management
(SL->breakeven after TP1, reconcile) is Fase 3. Live sending is hard-guarded off.

Gated by EXEC_ENABLED (default 0) so the GitHub Actions signal scan — which does
NOT install ccxt — never imports this module.
"""
from __future__ import annotations

import os

from . import exchange_bybit as xb

EXEC_ENABLED = os.getenv("EXEC_ENABLED", "0") == "1"
# Equity used for sizing when no API key / no network is available (pure DRY-RUN).
DRY_EQUITY = float(os.getenv("EXEC_DRY_EQUITY", "1000"))
TP1_FRAC = 0.5   # bank 50% at TP1, rest rides to TP2


def size_position(equity: float, entry: float, sl: float,
                  risk_pct: float = xb.RISK_PCT) -> tuple[float, float]:
    """Contracts so a stop-out loses exactly risk_pct of equity (linear USDT perp:
    PnL = qty * price_move, so qty = risk_amount / stop_distance)."""
    stop_dist = abs(entry - sl)
    if stop_dist <= 0 or equity <= 0:
        return 0.0, 0.0
    risk_amt = equity * risk_pct
    return risk_amt / stop_dist, risk_amt


def build_plan(symbol: str, direction: str, entry: float, sl: float,
               tp1: float, tp2: float, equity: float,
               leverage: float = xb.LEVERAGE) -> dict:
    qty, risk_amt = size_position(equity, entry, sl)
    is_long = direction.upper() == "LONG"
    open_side = "buy" if is_long else "sell"
    close_side = "sell" if is_long else "buy"
    q1 = qty * TP1_FRAC
    q2 = qty - q1
    notional = qty * entry
    margin = notional / leverage if leverage else notional
    return {
        "symbol": symbol, "direction": direction.upper(),
        "leverage": leverage, "margin_mode": xb.MARGIN_MODE,
        "equity": round(equity, 2), "risk_pct": xb.RISK_PCT,
        "risk_amt": round(risk_amt, 2),
        "qty": qty, "notional": round(notional, 2), "margin": round(margin, 2),
        "orders": [
            {"tag": "ENTRY", "type": "market", "side": open_side, "qty": qty, "price": entry},
            {"tag": "SL", "type": "stop_market", "side": close_side, "trigger": sl,
             "qty": qty, "reduceOnly": True},
            {"tag": "TP1", "type": "limit", "side": close_side, "price": tp1,
             "qty": q1, "reduceOnly": True},
            {"tag": "TP2", "type": "limit", "side": close_side, "price": tp2,
             "qty": q2, "reduceOnly": True},
        ],
    }


def format_plan(p: dict) -> str:
    mode = "DRY-RUN" if xb.DRY_RUN else "LIVE"
    lines = [
        f"[exec] {mode} order plan — {p['symbol']} {p['direction']}",
        (f"[exec]   equity={p['equity']} risk={p['risk_pct']*100:.1f}%={p['risk_amt']} "
         f"lev={p['leverage']:g}x {p['margin_mode']} | qty={p['qty']:.6g} "
         f"notional={p['notional']} margin={p['margin']}"),
    ]
    for o in p["orders"]:
        px = o.get("price", o.get("trigger"))
        ro = " reduceOnly" if o.get("reduceOnly") else ""
        lines.append(f"[exec]     {o['tag']:5} {o['type']:11} {o['side']:4} "
                     f"qty={o['qty']:.6g} @ {px}{ro}")
    if p["equity"] and p["margin"] > 0.33 * p["equity"]:
        lines.append(f"[exec]   ⚠️ margin {p['margin']} > 33% equity (SL ketat -> posisi besar). "
                     f"Pertimbangkan risk lebih kecil atau batasi posisi konkuren.")
    return "\n".join(lines)


class Executor:
    """Builds + logs order plans from ENTRY signals. DRY_RUN only in Fase 2."""

    def __init__(self, api: "xb.BybitFutures | None" = None):
        self.api = api

    async def _equity(self) -> float:
        if self.api and getattr(self.api.ex, "apiKey", ""):
            try:
                eq = await self.api.equity_usdt()
                if eq > 0:
                    return eq
            except Exception as exc:
                print(f"[exec] equity fetch failed ({exc}) — using DRY equity {DRY_EQUITY}")
        return DRY_EQUITY

    async def on_entry(self, sig: dict) -> dict | None:
        plan = sig.get("plan") or {}
        if not all(k in plan and plan[k] is not None for k in ("entry", "sl", "tp1", "tp2")):
            return None
        symbol = xb.BybitFutures.to_symbol(sig["symbol"])
        # load markets once so we can round qty to the exchange's precision
        if self.api is not None and self.api.markets is None:
            try:
                await self.api.load()
            except Exception as exc:
                print(f"[exec] load markets failed ({exc}) — qty left un-rounded")
        equity = await self._equity()
        p = build_plan(symbol, sig["direction"], plan["entry"], plan["sl"],
                       plan["tp1"], plan["tp2"], equity)
        if p["qty"] <= 0:
            print(f"[exec] {symbol}: qty=0 (bad stop distance) — skip")
            return None
        if self.api is not None and self.api.markets and symbol in self.api.markets:
            try:
                p["qty"] = float(self.api.ex.amount_to_precision(symbol, p["qty"]))
                for o in p["orders"]:
                    o["qty"] = float(self.api.ex.amount_to_precision(symbol, o["qty"]))
            except Exception:
                pass
        print(format_plan(p))
        # Hard guard: real order sending is Fase 3, not Fase 2.
        if xb.LIVE_TRADING and not xb.DRY_RUN:
            raise RuntimeError(
                "Live order sending belum aktif (Fase 3). Set EXEC_DRY_RUN=1 dulu.")
        return p


_singleton: "Executor | None" = None


def get() -> Executor:
    """Lazy singleton with a shared Bybit adapter (only built when EXEC_ENABLED)."""
    global _singleton
    if _singleton is None:
        _singleton = Executor(xb.BybitFutures())
    return _singleton
