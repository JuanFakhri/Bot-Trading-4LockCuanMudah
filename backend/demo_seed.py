"""Seed a realistic self-learning history into the DB (demo only).

Simulates a batch of resolved trades across several pattern signatures so the
learning engine derives real BLOCK / FAVOR lessons, and the journal + KPIs are
populated. Run once: ``BOT_DEMO=1 python -m backend.demo_seed``.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

from . import database as db, learning

random.seed(7)

# (features, true win-probability) — a couple of good patterns, a couple bad.
PATTERNS = [
    (dict(machine="long", regime="BULL", fib_bucket="0.55-0.618", rsi_htf_bucket="50-60",
          rsi_ltf_bucket="40-50", dow=1, usdtd_pos_bucket="30-50", ad_rising=True, sar_confirm=None), 0.72, "SOLUSDT"),
    (dict(machine="long", regime="BULL", fib_bucket="0.5-0.55", rsi_htf_bucket="60-70",
          rsi_ltf_bucket="40-50", dow=2, usdtd_pos_bucket="30-50", ad_rising=True, sar_confirm=None), 0.66, "ETHUSDT"),
    (dict(machine="long", regime="BULL", fib_bucket="0.5-0.55", rsi_htf_bucket="50-60",
          rsi_ltf_bucket="30-40", dow=4, usdtd_pos_bucket="50-70", ad_rising=False, sar_confirm=None), 0.22, "DOGEUSDT"),
    (dict(machine="short", regime="BEAR", fib_bucket="0.55-0.618", rsi_htf_bucket="-inf-40",
          rsi_ltf_bucket="50-60", dow=3, usdtd_pos_bucket="70-85", ad_rising=None, sar_confirm=True), 0.68, "XRPUSDT"),
    (dict(machine="short", regime="BEAR", fib_bucket="0.5-0.55", rsi_htf_bucket="40-50",
          rsi_ltf_bucket="60-70", dow=0, usdtd_pos_bucket="30-50", ad_rising=None, sar_confirm=False), 0.25, "ADAUSDT"),
]


def run(n_per: int = 8):
    now = datetime.now(timezone.utc)
    t = now - timedelta(days=20)
    for feat, p, sym in PATTERNS:
        for i in range(n_per):
            won = random.random() < p
            r = round(random.uniform(1.8, 3.0), 2) if won else -1.0
            direction = "LONG" if feat["machine"] == "long" else "SHORT"
            entry = round(random.uniform(1, 200), 4)
            risk = entry * 0.04
            sl = entry - risk if direction == "LONG" else entry + risk
            exit_price = entry + risk * r if direction == "LONG" else entry - risk * r
            conf = round(p + random.uniform(-0.08, 0.08), 3)
            tid = db.insert_trade(
                {"symbol": sym, "direction": direction, "machine": feat["machine"],
                 "ts": t.isoformat(), "features": feat},
                {"entry": round(entry, 4), "sl": round(sl, 4),
                 "tp1": round(entry + risk if direction == "LONG" else entry - risk, 4),
                 "tp2": round(exit_price, 4), "rr": 2.5},
                conf,
            )
            outcome = "WIN" if won else "LOSS"
            db.resolve_trade(tid, outcome, r, round(exit_price, 4), t.isoformat())
            learning.record_outcome(feat, won, r)
            t += timedelta(hours=random.uniform(4, 14))
    print("seeded:", db.stats_summary())
    print("lessons:", len(db.lessons()))


if __name__ == "__main__":
    run()
