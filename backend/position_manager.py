"""FASE 3 — live position manager for Bybit USDT-M perps (TESTNET first).

Turns an executor order plan into real orders and manages the lifecycle exactly
like the backtest tracker:

  open : set isolated leverage -> market entry -> place SL (full) + TP1 (50%) + TP2 (rest)
  TP1  : when ~50% has filled -> cancel the SL and re-place it at BREAKEVEN on the runner
  exit : when the position is flat -> cancel leftovers, log, forget

State is persisted to data/exec_state.json and RECONCILED against the exchange
every scan (the exchange is the source of truth), so a restart never double-enters
and always re-attaches management to live positions.

SAFETY: only ever runs when LIVE_TRADING=1 AND EXEC_DRY_RUN=0. A kill-switch file
(data/EXEC_KILL) blocks all new entries. Starts on Bybit testnet (fake money).
"""
from __future__ import annotations

import json
import os
import time

from . import config
from . import exchange_bybit as xb

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "exec_state.json")
KILL_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "EXEC_KILL")
TP1_FILLED_FRAC = 0.6   # position shrank below 60% of initial -> TP1 has filled


def _load() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(state: dict):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0)


def kill_active() -> bool:
    return os.path.exists(KILL_FILE)


class PositionManager:
    def __init__(self, api: xb.BybitFutures):
        self.api = api
        self.state: dict = _load()

    async def _ensure_markets(self):
        if self.api.markets is None:
            await self.api.load()

    # ------------------------------------------------------------- open
    async def open_position(self, plan: dict) -> dict | None:
        """Place entry + protective SL/TP1/TP2 for one signal. Returns state or None."""
        if kill_active():
            print("[pm] KILL-SWITCH active (data/EXEC_KILL) — refusing new entry")
            return None
        await self._ensure_markets()
        symbol = plan["symbol"]
        if symbol in self.state:
            print(f"[pm] {symbol}: already managed — skip")
            return None
        # respect max concurrent (count live tracked + exchange positions)
        if len(self.state) >= xb.MAX_CONCURRENT:
            print(f"[pm] max concurrent {xb.MAX_CONCURRENT} reached — skip {symbol}")
            return None
        cur, _ = await self.api.position_size(symbol)
        if cur != 0:
            print(f"[pm] {symbol}: exchange already has a position — skip")
            return None

        is_long = plan["direction"] == "LONG"
        open_side, close_side = ("buy", "sell") if is_long else ("sell", "buy")
        orders = {o["tag"]: o for o in plan["orders"]}
        qty = self.api.amount(symbol, orders["ENTRY"]["qty"])
        if qty <= 0:
            print(f"[pm] {symbol}: qty 0 — skip")
            return None

        await self.api.set_leverage_isolated(symbol, plan.get("leverage", xb.LEVERAGE))
        entry_order = await self.api.market_entry(symbol, open_side, qty)
        # confirm the real filled size + avg price from the exchange
        filled, avg = await self.api.position_size(symbol)
        filled = abs(filled) or qty
        entry_px = avg or orders["ENTRY"]["price"]

        q1 = self.api.amount(symbol, filled * 0.5)
        q2 = self.api.amount(symbol, filled - q1)
        sl = await self.api.place_stop(symbol, close_side, filled, orders["SL"]["trigger"])
        tp1 = await self.api.place_tp(symbol, close_side, q1, orders["TP1"]["price"])
        tp2 = await self.api.place_tp(symbol, close_side, q2, orders["TP2"]["price"])

        buf = config.BE_BUFFER_PCT
        be = entry_px * (1 + buf) if is_long else entry_px * (1 - buf)
        st = {
            "direction": plan["direction"], "close_side": close_side,
            "entry": entry_px, "sl": orders["SL"]["trigger"],
            "tp1": orders["TP1"]["price"], "tp2": orders["TP2"]["price"],
            "be": be, "init_qty": filled,
            "sl_id": (sl or {}).get("id"), "tp1_id": (tp1 or {}).get("id"),
            "tp2_id": (tp2 or {}).get("id"),
            "moved_be": False, "risk_amt": plan.get("risk_amt"),
            "opened_ts": time.time(),
        }
        self.state[symbol] = st
        _save(self.state)
        print(f"[pm] OPENED {symbol} {plan['direction']} qty={filled} entry={entry_px} "
              f"SL={st['sl']} TP1={st['tp1']} TP2={st['tp2']}")
        return st

    # -------------------------------------------------------- reconcile
    async def reconcile(self):
        """Called every scan: advance SL->breakeven after TP1, clean up closed."""
        if not self.state:
            return
        await self._ensure_markets()
        for symbol in list(self.state.keys()):
            st = self.state[symbol]
            try:
                size, _ = await self.api.position_size(symbol)
                size = abs(size)
                if size <= 0:                      # position closed (TP2 or SL)
                    await self.api.cancel_all(symbol)
                    print(f"[pm] CLOSED {symbol} — cleaned up leftover orders")
                    del self.state[symbol]; _save(self.state)
                    continue
                # TP1 filled (runner left) and SL not yet at breakeven?
                if not st["moved_be"] and size < st["init_qty"] * TP1_FILLED_FRAC:
                    if st.get("sl_id"):
                        await self.api.cancel(symbol, st["sl_id"])
                    new_sl = await self.api.place_stop(symbol, st["close_side"], size, st["be"])
                    st["sl_id"] = (new_sl or {}).get("id")
                    st["moved_be"] = True
                    _save(self.state)
                    print(f"[pm] {symbol}: TP1 hit -> SL moved to breakeven {st['be']:.6g}")
            except Exception as exc:
                print(f"[pm] reconcile {symbol} error: {exc}")

    # ---------------------------------------------------------- kill all
    async def close_all(self):
        """Kill-switch action: cancel orders + market-close every managed position."""
        await self._ensure_markets()
        for symbol in list(self.state.keys()):
            try:
                await self.api.cancel_all(symbol)
                size, _ = await self.api.position_size(symbol)
                if size:
                    st = self.state[symbol]
                    await self.api.close_position(symbol, st["close_side"], abs(size))
                    print(f"[pm] KILL: closed {symbol}")
            except Exception as exc:
                print(f"[pm] close_all {symbol} error: {exc}")
            self.state.pop(symbol, None)
        _save(self.state)
