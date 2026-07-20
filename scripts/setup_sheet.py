"""Setup sheet — for EVERY coin in the live watchlist, compute the current trade
plan the bot would use (direction, entry, SL, TP1, TP2, RR, state, score) from
live structure, using the exact same machines as the live bot:

  * bearish triple-TF alignment  -> SMC short plan (evaluate_smc_machine)
  * bullish triple-TF alignment  -> Phoenix long plan (evaluate_long)
  * no alignment                 -> no setup (waiting)

This BYPASSES only the macro/regime EXECUTION gate (which decides *whether* to
trade, not the price levels) so you can see the prospective levels per coin. A
row is only actionable when its state is ENTRY *and* the live gate is open.

Writes docs/data/setup_sheet.json and prints a table. Read-only otherwise.
Usage: python -m scripts.setup_sheet
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

from backend import (config, data_feed, database as db, learning, market_filter,
                     phoenix, strategy_smc)

OUT = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "setup_sheet.json")
STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "state.json")


async def _plan_for(sym, regime):
    htf = await data_feed.get_klines(sym, config.HTF, config.KLIMIT)
    dtf = await data_feed.get_klines(sym, config.DTF, 260)
    ltf = await data_feed.get_klines(sym, "1h", 300)
    if htf.empty or dtf.empty or ltf.empty:
        return None
    d = strategy_smc._direction(htf, dtf, ltf)
    if d == "short":
        sig = strategy_smc.evaluate_smc_machine(sym, htf, dtf, ltf, regime)
    elif d == "long":
        sig = phoenix.evaluate_long(sym, htf, dtf, ltf, regime)
    else:
        return None
    if not sig or not sig.get("plan"):
        return None
    # LEARNING score (confidence) from the trained brain — same call the live
    # engine makes. This is the number the dashboard watching list gates on (>40%).
    verdict = learning.evaluate(sig.get("features", {}))
    p = sig["plan"]
    price = float(ltf["close"].iloc[-1])
    return {
        "symbol": sym, "direction": sig.get("direction"), "state": sig.get("state"),
        "score": sig.get("score"), "price": round(price, 8),
        "confidence": round(verdict.get("confidence", 0.0), 4),
        "allowed": verdict.get("allowed", True),
        "learn_reason": verdict.get("reason", ""),
        "entry": p["entry"], "sl": p["sl"], "tp1": p["tp1"], "tp2": p["tp2"],
        "rr": p["rr"],
    }


async def main():
    # restore the trained learning brain so confidence reflects real priors
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                db.import_state(json.load(f))
        except Exception as exc:
            print(f"[setup] state restore skipped: {exc}")

    regime = await market_filter.compute_regime()
    try:
        cpi = await data_feed.get_cpi_bias()
        regime["cpi_bias"] = cpi.get("bias", "NETRAL") if cpi.get("ok") else "NETRAL"
    except Exception:
        regime["cpi_bias"] = "NETRAL"

    rows = []
    for sym in config.WATCHLIST:
        try:
            r = await _plan_for(sym, regime)
            if r:
                rows.append(r)
        except Exception as exc:
            print(f"[setup] {sym} error: {exc}")

    order = {"ENTRY": 0, "ARMED": 1, "WATCHING": 2}
    rows.sort(key=lambda r: (order.get(r["state"], 3), -(r.get("confidence") or 0)))

    short_open = regime.get("cpi_bias") != "BULLISH"
    long_open = regime.get("regime") == "BULL"
    with open(OUT, "w") as f:
        json.dump({"generated_ts": datetime.now(timezone.utc).isoformat(),
                   "regime": regime.get("regime"), "cpi_bias": regime.get("cpi_bias"),
                   "short_gate_open": short_open, "long_gate_open": long_open,
                   "gate_locked": not (short_open or long_open),
                   "count": len(rows), "rows": rows}, f, ensure_ascii=False,
                  separators=(",", ":"))

    gate_note = ("SHORT gate " + ("OPEN" if regime.get("cpi_bias") != "BULLISH" else "LOCKED (CPI BULLISH)")
                 + " | LONG gate " + ("OPEN" if regime.get("regime") == "BULL" else "LOCKED (regime "
                 + str(regime.get("regime")) + ")"))
    print(f"\n================= SETUP SHEET — {len(rows)}/{len(config.WATCHLIST)} coins aligned =================")
    print(gate_note)
    print(f"{'coin':11}{'dir':6}{'state':9}{'score':>6}{'entry':>13}{'SL':>13}{'TP1':>13}{'TP2':>13}{'RR':>6}")
    print("-" * 96)
    for r in rows:
        print(f"{r['symbol']:11}{(r['direction'] or ''):6}{(r['state'] or ''):9}"
              f"{(r['score'] if r['score'] is not None else 0):>6}"
              f"{r['entry']:>13.6g}{r['sl']:>13.6g}{r['tp1']:>13.6g}{r['tp2']:>13.6g}{r['rr']:>6}")
    print("=" * 96)
    print("Actionable = state ENTRY AND its machine gate OPEN. Others are prospective "
          "(levels recomputed each 1H close).")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
