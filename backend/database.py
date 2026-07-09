"""SQLite persistence. Everything the bot learns is stored here so it is never
forgotten across restarts."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any

from . import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(config.DB_PATH)), exist_ok=True)
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init(_conn)
    return _conn


def _init(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, direction TEXT, machine TEXT,
            created_ts TEXT, entry REAL, sl REAL, tp1 REAL, tp2 REAL,
            rr REAL, confidence REAL,
            features TEXT, plan TEXT,
            status TEXT DEFAULT 'OPEN',       -- OPEN / TP1 / RESOLVED
            tp1_hit INTEGER DEFAULT 0,
            outcome TEXT,                     -- WIN / LOSS / BREAKEVEN
            r_multiple REAL, exit_price REAL, resolved_ts TEXT
        );

        CREATE TABLE IF NOT EXISTS pattern_stats (
            signature TEXT PRIMARY KEY,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            sum_r REAL DEFAULT 0,
            updated_ts TEXT
        );

        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, signature TEXT, kind TEXT, text TEXT,
            win_rate REAL, samples INTEGER
        );
        """
    )
    conn.commit()


def q(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        cur = _connect().execute(sql, params)
        rows = cur.fetchall()
        _connect().commit()
        return rows


def execute(sql: str, params: tuple = ()) -> int:
    with _lock:
        cur = _connect().execute(sql, params)
        _connect().commit()
        return cur.lastrowid or 0


def insert_trade(sig: dict, plan: dict, confidence: float) -> int:
    return execute(
        """INSERT INTO trades
           (symbol,direction,machine,created_ts,entry,sl,tp1,tp2,rr,confidence,features,plan)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            sig["symbol"], sig["direction"], sig["machine"], sig["ts"],
            plan["entry"], plan["sl"], plan["tp1"], plan["tp2"], plan["rr"],
            confidence, json.dumps(sig["features"]), json.dumps(plan),
        ),
    )


def open_trades() -> list[dict]:
    return [dict(r) for r in q("SELECT * FROM trades WHERE status != 'RESOLVED'")]


def recent_trades(limit: int = 50) -> list[dict]:
    return [dict(r) for r in q(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))]


def mark_tp1(trade_id: int):
    execute("UPDATE trades SET tp1_hit=1, status='TP1' WHERE id=?", (trade_id,))


def resolve_trade(trade_id: int, outcome: str, r_multiple: float, exit_price: float, ts: str):
    execute(
        "UPDATE trades SET status='RESOLVED', outcome=?, r_multiple=?, exit_price=?, resolved_ts=? WHERE id=?",
        (outcome, r_multiple, exit_price, ts, trade_id),
    )


def bump_pattern(signature: str, won: bool, r_multiple: float, ts: str):
    row = q("SELECT wins,losses,sum_r FROM pattern_stats WHERE signature=?", (signature,))
    if row:
        wins = row[0]["wins"] + (1 if won else 0)
        losses = row[0]["losses"] + (0 if won else 1)
        sum_r = row[0]["sum_r"] + r_multiple
        execute("UPDATE pattern_stats SET wins=?,losses=?,sum_r=?,updated_ts=? WHERE signature=?",
                (wins, losses, sum_r, ts, signature))
    else:
        execute("INSERT INTO pattern_stats (signature,wins,losses,sum_r,updated_ts) VALUES (?,?,?,?,?)",
                (signature, 1 if won else 0, 0 if won else 1, r_multiple, ts))


def pattern_stat(signature: str) -> dict | None:
    row = q("SELECT * FROM pattern_stats WHERE signature=?", (signature,))
    return dict(row[0]) if row else None


def add_lesson(signature: str, kind: str, text: str, win_rate: float, samples: int, ts: str):
    # avoid duplicate identical lessons
    existing = q("SELECT id FROM lessons WHERE signature=? AND kind=?", (signature, kind))
    if existing:
        execute("UPDATE lessons SET text=?,win_rate=?,samples=?,ts=? WHERE id=?",
                (text, win_rate, samples, ts, existing[0]["id"]))
    else:
        execute("INSERT INTO lessons (ts,signature,kind,text,win_rate,samples) VALUES (?,?,?,?,?,?)",
                (ts, signature, kind, text, win_rate, samples))


def lessons(limit: int = 30) -> list[dict]:
    return [dict(r) for r in q("SELECT * FROM lessons ORDER BY id DESC LIMIT ?", (limit,))]


def export_state() -> dict:
    """Dump the learning-relevant tables to a plain dict (for JSON persistence).

    Used by the GitHub-Actions runner to commit state back to the repo so the
    bot never forgets between scheduled runs, without committing a binary DB.
    """
    def rows(table: str) -> list[dict]:
        return [dict(r) for r in q(f"SELECT * FROM {table}")]

    return {
        "trades": rows("trades"),
        "pattern_stats": rows("pattern_stats"),
        "lessons": rows("lessons"),
    }


def import_state(state: dict):
    """Rebuild the DB from a dict produced by ``export_state``."""
    with _lock:
        conn = _connect()
        for table in ("trades", "pattern_stats", "lessons"):
            conn.execute(f"DELETE FROM {table}")
            for row in state.get(table, []):
                cols = list(row.keys())
                ph = ",".join("?" * len(cols))
                conn.execute(
                    f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})",
                    [row[c] for c in cols],
                )
        conn.commit()


def stats_summary() -> dict:
    rows = q("SELECT outcome, r_multiple FROM trades WHERE status='RESOLVED'")
    wins = sum(1 for r in rows if r["outcome"] == "WIN")
    losses = sum(1 for r in rows if r["outcome"] == "LOSS")
    total = wins + losses
    gross_win = sum(r["r_multiple"] for r in rows if r["r_multiple"] and r["r_multiple"] > 0)
    gross_loss = -sum(r["r_multiple"] for r in rows if r["r_multiple"] and r["r_multiple"] < 0)
    pf = (gross_win / gross_loss) if gross_loss > 0 else (gross_win if gross_win else 0.0)
    return {
        "resolved": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total * 100, 1) if total else 0.0,
        "profit_factor": round(pf, 2),
        "open": len(open_trades()),
    }
