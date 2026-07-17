"""One-shot scan for the GitHub-Actions runner (GitHub-only deployment).

Flow each run:
  1. restore the learning state from the committed ``data/state.json`` (so the
     bot remembers past lessons even though the runner is ephemeral),
  2. run exactly one strategy scan (regime → signals → track open trades →
     learning),
  3. write the dashboard snapshot to ``docs/data/snapshot.json`` (served by
     GitHub Pages), and
  4. export the updated learning state back to ``data/state.json``.

The workflow then commits the two JSON files, so the site updates and the bot
never forgets. No server, no paid host — everything lives in the repo.
"""
from __future__ import annotations

import asyncio
import json
import os

from backend import config, data_feed, database as db
from backend.engine import engine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "data", "state.json")
SNAP_PATH = os.path.join(ROOT, "docs", "data", "snapshot.json")
NEWS_PATH = os.path.join(ROOT, "docs", "data", "news.json")


async def main():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(SNAP_PATH), exist_ok=True)

    # 1. restore learning from committed JSON if the DB is fresh
    if not os.path.exists(config.DB_PATH) and os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            db.import_state(json.load(f))
        print(f"[scan] restored state: {db.stats_summary()}")

    # 2. one scan cycle
    await engine.scan()
    snap = engine.snapshot()

    # 3. write dashboard snapshot for GitHub Pages
    with open(SNAP_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))

    # 4. persist learning back to JSON
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(db.export_state(), f, ensure_ascii=False, indent=0)

    # 5. high-impact calendar (news alert, WIB) + macro screens (events + CPI)
    try:
        from backend import macro_news
        events = await data_feed.get_economic_calendar()
        # aggregate the next ~48h of events into one crypto bias for the screen
        assessments = [
            macro_news.assess_event(e.get("title", ""), forecast=e.get("forecast"),
                                    previous=e.get("previous"))
            for e in events
        ]
        screen = macro_news.aggregate_day(assessments)
        cpi = await data_feed.get_cpi_bias()   # inflasi turun = latar bullish crypto
        with open(NEWS_PATH, "w", encoding="utf-8") as f:
            json.dump({"generated_ts": snap.get("last_scan"),
                       "alert_hours": config.NEWS_ALERT_HOURS,
                       "screen": screen, "cpi": cpi, "events": events},
                      f, ensure_ascii=False, separators=(",", ":"))
        print(f"[scan] news: {len(events)} events | screen={screen['bias']} | "
              f"CPI bias={cpi.get('bias')} (YoY {cpi.get('prev_yoy')}->{cpi.get('yoy')})")
    except Exception as exc:
        print(f"[scan] news fetch failed: {exc}")

    reg = snap["regime"].get("regime")
    print(f"[scan] done: regime={reg} signals={len(snap['signals'])} "
          f"lessons={len(snap['lessons'])} stats={snap['stats']} err={snap.get('error')}")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
