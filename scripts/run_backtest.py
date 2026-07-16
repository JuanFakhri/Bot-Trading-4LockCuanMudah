"""Run a historical backtest of the SMC + AI-Score strategy and let the bot LEARN.

Steps:
  1. Build a USDT.D position timeline (20-day range) + BTC.D direction (ETH/BTC
     proxy) for the macro components of the AI Score.
  2. Backtest every watchlist symbol on the 1H entry timeframe with 1D/4H bias.
  3. REBUILD the learning brain from the backtest: every resolved trade is fed
     into ``learning`` so losing patterns get blocked and winners get favoured.
  4. Write the report to ``docs/data/backtest.json`` (shown in the web UI) and
     persist the updated learning to ``data/state.json``.

Usage: ``BOT_DEMO=1 python -m scripts.run_backtest``  (or without BOT_DEMO on a
host that can reach Binance/CoinGecko, e.g. GitHub Actions).
"""
from __future__ import annotations

import asyncio
import json
import os

import numpy as np
import pandas as pd

from backend import (config, data_feed, database as db, indicators,
                     learning, market_filter, smc_backtester)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "data", "state.json")
OUT_PATH = os.path.join(ROOT, "docs", "data", "backtest.json")

LOOKBACK_DAYS = int(os.getenv("BACKTEST_DAYS", "180"))

# Optional symbol override (comma-separated, e.g. "ETHUSDT,SOLUSDT"). Empty = all.
_env_syms = os.getenv("BACKTEST_SYMBOLS", "").strip()
SYMBOLS = [s.strip().upper() for s in _env_syms.split(",") if s.strip()] or config.WATCHLIST

# SMC AI-Score gate (defaults to the live value).
SCORE_TH = float(os.getenv("BACKTEST_SCORE_TH", str(config.SMC_SCORE_TH)))

# Macro-calendar policy gate (#13). When on, the SMC engine only takes LONGs when
# economic policy is NOT risk-off and SHORTs when NOT risk-on (a counter-policy
# setup becomes NEUTRAL/no-trade). MACRO_REQUIRE_ON additionally demands RISK_ON
# for a long (fixes the "weak long" the trader flagged). Validated via OOS PF.
MACRO_GATE = os.getenv("BACKTEST_MACRO_GATE", "0") == "1"
MACRO_REQUIRE_ON = os.getenv("BACKTEST_MACRO_REQUIRE_ON", "0") == "1"
# "Strengthen long": TP the long runner at the real resistance instead of flat 3R.
LONG_STRUCT_TP = os.getenv("BACKTEST_LONG_STRUCT_TP", "0") == "1"
# "Strengthen long" via conviction: higher Setup Score required for LONGs only.
_lst = os.getenv("BACKTEST_LONG_SCORE_TH", "").strip()
LONG_SCORE_TH = float(_lst) if _lst else SCORE_TH
# "Strengthen long" via entry quality: LONG must be a real sweep-reclaim + BOS.
LONG_REVERSAL_HARD = os.getenv("BACKTEST_LONG_REVERSAL_HARD", "0") == "1"
# When set, do NOT rewrite the live learning brain (data/state.json). Used for the
# macro-gate A/B so an unvalidated change never leaks into the live bot.
NO_PERSIST = os.getenv("BACKTEST_NO_PERSIST", "0") == "1"


async def _macro_bias_timeline(days: int):
    """Daily crypto-policy bias (net macro_news score, trailing 7-day sum) over
    the backtest window. Real: FRED releases; DEMO: synthetic. Empty on failure."""
    from scripts.backtest_news import _build_events
    from backend import macro_news
    end = pd.Timestamp.utcnow().normalize()
    start = end - pd.Timedelta(days=days + 90)
    rng = np.random.default_rng(20260716)
    try:
        events = await _build_events(start, end, rng)
    except Exception as exc:
        print(f"[backtest] macro events failed: {exc}")
        return pd.Series(dtype=float)
    day: dict = {}
    for ev in events:
        a = macro_news.assess_event(ev["title"], actual=ev.get("actual"),
                                    previous=ev.get("previous"))
        if not a.matched or a.score == 0:
            continue
        d = pd.Timestamp(ev["ts"]).normalize()
        day[d] = day.get(d, 0.0) + a.score
    if not day:
        return pd.Series(dtype=float)
    s = pd.Series(day).sort_index()
    s.index = s.index.tz_convert("UTC") if s.index.tz else s.index.tz_localize("UTC")
    idx = pd.date_range(start=s.index.min(), end=end, freq="D", tz="UTC")
    # a release's policy tone persists ~a week, then fades back to NEUTRAL
    daily = s.reindex(idx, fill_value=0.0).rolling(7, min_periods=1).sum()
    print(f"[backtest] macro timeline: {len(day)} release-days, "
          f"RISK_ON {int((daily > 0.15).sum())}d / RISK_OFF {int((daily < -0.15).sum())}d")
    return daily


def _dir_series(df: pd.Series | None, idx: pd.Index, invert: bool = False) -> pd.Series:
    """Per-day NAIK/TURUN/STABIL from an EMA50 slope (deadband 0.5%), aligned to idx."""
    if df is None or df.empty:
        return pd.Series("STABIL", index=idx)
    ema = indicators.ema(df["close"], config.EMA_FAST)
    pct = (ema - ema.shift(3)) / ema.shift(3).abs()
    d = pd.Series("STABIL", index=ema.index)
    d[pct > 0.005] = "NAIK"
    d[pct < -0.005] = "TURUN"
    if invert:
        d = d.map({"NAIK": "TURUN", "TURUN": "NAIK", "STABIL": "STABIL"})
    return d.reindex(idx, method="ffill").fillna("STABIL")


async def _usdtd_timeline() -> pd.Series:
    idx = pd.date_range(end=pd.Timestamp.utcnow().normalize(),
                        periods=LOOKBACK_DAYS + 60, freq="D", tz="UTC")
    if config.DEMO:
        # synthetic oscillation so some short regimes appear in demo
        vals = 0.5 + 0.35 * np.sin(np.linspace(0, 6.28 * 3, len(idx)))
        return pd.Series(vals, index=idx)
    try:
        h = await data_feed._client.get(
            config.COINGECKO_BASE + "/coins/tether/market_chart",
            params={"vs_currency": "usd", "days": "365", "interval": "daily"},
        )
        if h.status_code == 200:
            caps = h.json().get("market_caps", [])
            if len(caps) > 25:
                s = pd.Series([c[1] for c in caps],
                              index=pd.to_datetime([c[0] for c in caps], unit="ms", utc=True))
                lo = s.rolling(config.USDTD_LOOKBACK, min_periods=5).min()
                hi = s.rolling(config.USDTD_LOOKBACK, min_periods=5).max()
                pos = ((s - lo) / (hi - lo).replace(0, np.nan)).clip(0, 1).fillna(0.5)
                # CoinGecko free only covers ~365d; span the full backtest index
                # (NaN before that -> the older period falls back to the BTC.D matrix)
                pos.index = pos.index.normalize()
                pos = pos[~pos.index.duplicated(keep="last")]
                return pos.reindex(idx)
    except Exception as exc:
        print(f"[backtest] usdtd history failed: {exc}")
    return pd.Series(np.nan, index=idx)


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)

    print(f"[backtest] lookback={LOOKBACK_DAYS}d, symbols={len(SYMBOLS)}, score_th={SCORE_TH}")
    usdtd_daily = await _usdtd_timeline()
    if usdtd_daily.empty:
        print("[backtest] no USDT.D data — aborting")
        return

    # BTC.D direction (ETH/BTC proxy) for the SMC macro score component
    ethbtc = await data_feed.get_klines_history("ETHBTC", "1d", LOOKBACK_DAYS + 80)
    btcd_dir_daily = _dir_series(ethbtc, usdtd_daily.index, invert=True)

    # macro policy timeline (only when the gate is on)
    macro_bias_daily = await _macro_bias_timeline(LOOKBACK_DAYS) if MACRO_GATE else None
    if MACRO_GATE and (macro_bias_daily is None or macro_bias_daily.empty):
        print("[backtest] macro gate requested but no macro data — running WITHOUT gate")

    all_trades: list[dict] = []
    for sym in SYMBOLS:
        try:
            htf = await data_feed.get_klines_history(sym, config.HTF, LOOKBACK_DAYS)
            dtf = await data_feed.get_klines_history(sym, config.DTF, LOOKBACK_DAYS + 60)
            ltf = await data_feed.get_klines_history(sym, "1h", LOOKBACK_DAYS)  # 1H trigger
            if htf.empty or dtf.empty:
                print(f"[backtest] {sym}: no data")
                continue
            trades = smc_backtester.backtest_symbol_smc(
                sym, htf, dtf, ltf, usdtd_daily, btcd_dir_daily,
                {"score_th": SCORE_TH, "macro_gate": MACRO_GATE,
                 "macro_require_on": MACRO_REQUIRE_ON, "macro_bias_daily": macro_bias_daily,
                 "long_struct_tp": LONG_STRUCT_TP, "long_score_th": LONG_SCORE_TH,
                 "long_reversal_hard": LONG_REVERSAL_HARD})
            all_trades.extend(trades)
            print(f"[backtest] {sym}: {len(trades)} trades")
        except Exception as exc:
            print(f"[backtest] {sym} error: {exc}")

    summary = smc_backtester.summarize(all_trades)

    # ---- WALK-FORWARD: learn on train, apply the self-learning filter to the
    # unseen test split. This measures what the LIVE bot would actually trade
    # (it refuses blocked / low-confidence patterns), out-of-sample. ----
    srt = sorted(all_trades, key=lambda x: x["exit_ts"])
    cut = int(len(srt) * 0.7)
    train, test = srt[:cut], srt[cut:]
    db.execute("DELETE FROM pattern_stats")
    db.execute("DELETE FROM lessons")
    for t in train:
        learning.record_outcome(t["features"], t["r"] > 0.05, t["r"])
    kept = [t for t in test if learning.evaluate(t["features"])["allowed"]
            and learning.evaluate(t["features"])["confidence"] >= config.CONFIDENCE_FLOOR]
    walkforward = {
        "cutoff_ts": test[0]["exit_ts"] if test else None,
        "test_all": smc_backtester.summarize(test),
        "test_filtered": smc_backtester.summarize(kept),
        "kept": len(kept), "test_n": len(test),
    }

    # ---- REBUILD the learning brain from ALL trades (for the live bot) ----
    db.execute("DELETE FROM pattern_stats")
    db.execute("DELETE FROM lessons")
    for t in srt:
        learning.record_outcome(t["features"], t["r"] > 0.05, t["r"])

    blocked = [l for l in db.lessons(200) if l["kind"] == "BLOCK"]
    favored = [l for l in db.lessons(200) if l["kind"] == "FAVOR"]

    report = {
        "generated_ts": pd.Timestamp.utcnow().isoformat(),
        "params": {"lookback_days": LOOKBACK_DAYS, "htf": config.HTF, "ltf": "1h",
                   "symbols": len(SYMBOLS), "demo": config.DEMO, "strategy": "smc",
                   "score_th": SCORE_TH, "macro_gate": MACRO_GATE,
                   "macro_require_on": MACRO_REQUIRE_ON, "long_struct_tp": LONG_STRUCT_TP,
                   "long_score_th": LONG_SCORE_TH, "long_reversal_hard": LONG_REVERSAL_HARD},
        "summary": summary,
        "recent_trades": [
            {k: t[k] for k in ("symbol", "direction", "entry", "exit_price",
                               "outcome", "r", "rr", "entry_ts", "exit_ts")}
            for t in sorted(all_trades, key=lambda x: x["exit_ts"], reverse=True)[:40]
        ],
        "learned": {
            "blocked_count": len(blocked),
            "favored_count": len(favored),
            "lessons": db.lessons(40),
        },
        "walkforward": walkforward,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))
    # NO_PERSIST: research A/B (e.g. macro-gate trial) must NOT overwrite the live
    # learning brain until it is validated ("jangan ditambahkan kalau kurang bagus").
    if not NO_PERSIST:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(db.export_state(), f, ensure_ascii=False, indent=0)
    else:
        print("[backtest] NO_PERSIST — live learning state left untouched")

    wf = walkforward.get("test_filtered", {})
    s = summary
    print(f"[backtest] DONE: {s['trades']} trades, win {s['win_rate']}%, "
          f"PF {s['profit_factor']}, expectancy {s['expectancy_r']}R, "
          f"maxDD {s['max_drawdown_r']}R | learned {len(blocked)} blocks, {len(favored)} favors")
    print(f"[backtest] OOS(walk-forward): PF {wf.get('profit_factor')} "
          f"win {wf.get('win_rate')}% trades {wf.get('trades')} "
          f"| macro_gate={MACRO_GATE} require_on={MACRO_REQUIRE_ON}")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
