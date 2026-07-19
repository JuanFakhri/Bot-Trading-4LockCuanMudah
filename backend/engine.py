"""Bot orchestrator.

Runs a scan every SCAN_INTERVAL_SEC:
  1. compute market regime,
  2. evaluate every watchlist symbol with the active machine,
  3. score each candidate with the learning engine (confidence + block),
  4. open paper trades for confirmed entries that pass risk + learning gates,
  5. track open trades to TP/SL and feed the outcome back into learning.

State is kept in memory for the API and mirrored to SQLite for persistence.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from . import (config, data_feed, database as db, learning, market_filter, risk,
               strategy_smc, telegram)

_BAR_SEC = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


class Engine:
    def __init__(self):
        self.guard = risk.RiskGuard()
        self.regime: dict = {}
        self.signals: list[dict] = []
        self.last_scan: str | None = None
        self.scanning = False
        self.error: str | None = None
        self.prices: dict[str, float] = {}   # last price per symbol (for user-trade tracking)

    # ------------------------------------------------------------------ scan
    async def scan(self):
        if self.scanning:
            return
        self.scanning = True
        try:
            self.regime = await market_filter.compute_regime()
            # macro backdrop for the CPI gate (block trades that fight inflation trend)
            try:
                cpi = await data_feed.get_cpi_bias()
                self.regime["cpi_bias"] = cpi.get("bias", "NETRAL") if cpi.get("ok") else "NETRAL"
            except Exception:
                self.regime["cpi_bias"] = "NETRAL"
            await self._update_open_trades()
            await self._maybe_reconcile()

            # Scan every regime: the router fires Phoenix LONG only in BTC BULL,
            # but the SMC SHORT is a per-symbol bearish setup that is valid in any
            # BTC regime (including NEUTRAL) — so we must scan there too.
            results: list[dict] = []
            for sym in config.WATCHLIST:
                try:
                    sig = await self._eval_symbol(sym)
                    if sig:
                        results.append(sig)
                except Exception as exc:  # never let one symbol kill the scan
                    print(f"[engine] {sym} eval error: {exc}")
            # rank: ENTRY first, then by confidence
            order = {"ENTRY": 0, "ARMED": 1, "WATCHING": 2}
            results.sort(key=lambda s: (order.get(s["state"], 3), -s["confidence"]))
            self.signals = results
            self.last_scan = datetime.now(timezone.utc).isoformat()
            self.error = None
        except Exception as exc:
            self.error = str(exc)
            print(f"[engine] scan error: {exc}")
        finally:
            self.scanning = False

    async def _eval_symbol(self, symbol: str) -> dict | None:
        htf = await data_feed.get_klines(symbol, config.HTF, config.KLIMIT)
        dtf = await data_feed.get_klines(symbol, config.DTF, 260)
        ltf = await data_feed.get_klines(symbol, "1h", 300)   # SMC entry TF is 1H
        if htf.empty or dtf.empty or ltf.empty:
            return None

        # record latest price for every scanned symbol (used to track the
        # trades a user manually marks as taken, even if no signal fires)
        self.prices[symbol] = float(ltf["close"].iloc[-1])

        sig = strategy_smc.evaluate(symbol, htf, dtf, ltf, self.regime)
        if sig is None:
            return None

        verdict = learning.evaluate(sig["features"])
        sig["confidence"] = verdict["confidence"]
        sig["allowed"] = verdict["allowed"]
        sig["learn_reason"] = verdict["reason"]

        # SMC ships its own plan (swing-based SL, 1/2/3R).
        plan = sig.get("plan")
        sig["plan"] = plan

        # Decide whether to open a paper trade.
        sig["actionable"] = False
        sig["gate"] = ""
        if sig["state"] == "ENTRY" and plan and plan["rr_ok"] and verdict["allowed"] \
                and sig["confidence"] >= config.CONFIDENCE_FLOOR:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            ok, why = self.guard.can_enter(symbol, today, datetime.now(timezone.utc).timestamp())
            if not self._has_open(symbol):
                if ok:
                    self._open_trade(sig, plan)
                    sig["actionable"] = True
                    sig["gate"] = "ENTRY dibuka"
                    await self._maybe_execute(sig)
                    if telegram.enabled():
                        await telegram.send(telegram.entry_msg(sig, plan))
                else:
                    sig["gate"] = why
            else:
                sig["gate"] = "Posisi masih terbuka"
        elif sig["state"] == "ENTRY" and not verdict["allowed"]:
            sig["gate"] = f"Dihindari oleh pembelajaran: {verdict['reason']}"

        return sig

    async def _maybe_reconcile(self):
        """When live trading, advance/clean up real positions each scan (SL->BE
        after TP1, close-out cleanup). Lazy + EXEC_ENABLED-gated like _maybe_execute."""
        import os
        if os.getenv("EXEC_ENABLED", "0") != "1":
            return
        try:
            from . import executor
            executor.get()          # ensure the manager exists to re-attach on restart
            await executor.reconcile()
        except Exception as exc:
            print(f"[engine] reconcile error: {exc}")

    async def _maybe_execute(self, sig: dict):
        """When EXEC_ENABLED=1 (VPS with ccxt installed), hand the ENTRY to the
        executor which sizes + logs the exchange order plan (DRY_RUN by default).
        Imported lazily so the GitHub Actions scan — which does NOT install ccxt —
        never touches this path."""
        import os
        if os.getenv("EXEC_ENABLED", "0") != "1":
            return
        try:
            from . import executor
            await executor.get().on_entry(sig)
        except Exception as exc:
            print(f"[engine] executor error: {exc}")

    # ------------------------------------------------------------- trade mgmt
    def _has_open(self, symbol: str) -> bool:
        return any(t["symbol"] == symbol for t in db.open_trades())

    def _open_trade(self, sig: dict, plan: dict):
        db.insert_trade(sig, plan, sig["confidence"])
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.guard.register_entry(today)

    async def _update_open_trades(self):
        now = datetime.now(timezone.utc)
        for t in db.open_trades():
            df = await data_feed.get_klines(t["symbol"], config.LTF, 3)
            if df.empty:
                continue
            last = df.iloc[-1]
            hi, lo = float(last["high"]), float(last["low"])
            direction = t["direction"]
            entry, sl, tp1, tp2 = t["entry"], t["sl"], t["tp1"], t["tp2"]
            risk_unit = abs(entry - sl) or 1e-9
            plan = json.loads(t["plan"]) if t["plan"] else {}
            be = plan.get("breakeven", entry)
            features = json.loads(t["features"]) if t["features"] else {}

            if direction == "LONG":
                # stop-loss (or breakeven after TP1)
                stop = be if t["tp1_hit"] else sl
                if lo <= stop:
                    r = (0.5 * 1 + 0.5 * (be - entry) / risk_unit) if t["tp1_hit"] else -1.0
                    self._resolve(t, r, stop, features, now)
                    continue
                if not t["tp1_hit"] and hi >= tp1:
                    db.mark_tp1(t["id"])
                    t["tp1_hit"] = 1
                if hi >= tp2:
                    r = 0.5 * 1 + 0.5 * (tp2 - entry) / risk_unit
                    self._resolve(t, r, tp2, features, now)
            else:
                stop = be if t["tp1_hit"] else sl
                if hi >= stop:
                    r = (0.5 * 1 + 0.5 * (entry - be) / risk_unit) if t["tp1_hit"] else -1.0
                    self._resolve(t, r, stop, features, now)
                    continue
                if not t["tp1_hit"] and lo <= tp1:
                    db.mark_tp1(t["id"])
                    t["tp1_hit"] = 1
                if lo <= tp2:
                    r = 0.5 * 1 + 0.5 * (entry - tp2) / risk_unit
                    self._resolve(t, r, tp2, features, now)

    def _resolve(self, t: dict, r_multiple: float, exit_price: float, features: dict, now):
        outcome = "WIN" if r_multiple > 0.05 else "LOSS" if r_multiple < -0.05 else "BREAKEVEN"
        db.resolve_trade(t["id"], outcome, round(r_multiple, 3), exit_price, now.isoformat())
        learning.record_outcome(features, outcome == "WIN", r_multiple)
        self.guard.register_exit(
            t["symbol"], r_multiple, _BAR_SEC[config.LTF], now.timestamp()
        )
        print(f"[engine] resolved {t['symbol']} {outcome} r={r_multiple:.2f}")
        if telegram.enabled():
            asyncio.create_task(telegram.send(
                telegram.exit_msg(t["symbol"], t["direction"], outcome, r_multiple, exit_price)))

    # --------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        return {
            "regime": self.regime,
            "signals": self.signals,
            "risk": self.guard.snapshot(),
            "stats": db.stats_summary(),
            "lessons": db.lessons(20),
            "blocked": learning.blocked_patterns(),
            "recent_trades": db.recent_trades(30),
            "prices": self.prices,
            "last_scan": self.last_scan,
            "error": self.error,
            "config": {
                "watchlist": config.WATCHLIST,
                "scan_interval": config.SCAN_INTERVAL_SEC,
                "htf": config.HTF, "ltf": config.LTF,
            },
        }


engine = Engine()


async def run_loop():
    while True:
        await engine.scan()
        await asyncio.sleep(config.SCAN_INTERVAL_SEC)
