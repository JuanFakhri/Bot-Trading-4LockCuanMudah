"""Backtest the Phoenix Hybrid multi-engine strategy and report the results.

Runs the three engines (FIB retrace, momentum breakout, range mean-reversion)
over the watchlist on the 1H entry timeframe with 4H/1D context and a BTC-driven
BULL/BEAR/NEUTRAL regime, then simulates the portfolio risk rules (dynamic
sizing, recovery mode, daily/weekly stops, max concurrency).

This is a RESEARCH backtest — it writes ``docs/data/phoenix_backtest.json`` for
the "Phoenix" tab and does NOT touch the live bot's signals or learning state.

Usage:
  BOT_DEMO=1 python -m scripts.backtest_phoenix        # offline synthetic
  python -m scripts.backtest_phoenix                   # real Binance data
"""
from __future__ import annotations

import asyncio
import json
import os

import pandas as pd

from backend import config, data_feed, phoenix_backtester as phx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "data", "phoenix_backtest.json")

LOOKBACK_DAYS = int(os.getenv("PHOENIX_DAYS", "365"))
_env_syms = os.getenv("PHOENIX_SYMBOLS", "").strip()
SYMBOLS = [s.strip().upper() for s in _env_syms.split(",") if s.strip()] or config.WATCHLIST


async def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    print(f"[phoenix] lookback={LOOKBACK_DAYS}d, symbols={len(SYMBOLS)}, demo={config.DEMO}")

    # BTC drives the market-wide regime
    btc_daily = await data_feed.get_klines_history("BTCUSDT", "1d", LOOKBACK_DAYS + 90)
    if btc_daily is None or btc_daily.empty:
        print("[phoenix] no BTC data — aborting")
        await data_feed.close()
        return
    regime_daily = phx.btc_regime_daily(btc_daily)
    reg_counts = regime_daily.value_counts().to_dict()
    print(f"[phoenix] regime days: {reg_counts}")

    all_trades: list[dict] = []
    for sym in SYMBOLS:
        try:
            htf = await data_feed.get_klines_history(sym, config.HTF, LOOKBACK_DAYS)
            dtf = await data_feed.get_klines_history(sym, config.DTF, LOOKBACK_DAYS + 60)
            ltf = await data_feed.get_klines_history(sym, "1h", LOOKBACK_DAYS)
            if htf.empty or dtf.empty or ltf.empty:
                print(f"[phoenix] {sym}: no data")
                continue
            trades = phx.backtest_symbol_phoenix(sym, htf, dtf, ltf, regime_daily, None)
            all_trades.extend(trades)
            eng = {e: sum(1 for t in trades if t["engine"] == e) for e in phx.ENGINES}
            print(f"[phoenix] {sym}: {len(trades)} trades {eng}")
        except Exception as exc:
            print(f"[phoenix] {sym} error: {exc}")

    if not all_trades:
        print("[phoenix] no trades generated")
        # still emit an empty report so the tab renders a clean empty-state
        report = {"generated_ts": pd.Timestamp.utcnow().isoformat(),
                  "params": {"lookback_days": LOOKBACK_DAYS, "symbols": len(SYMBOLS),
                             "demo": config.DEMO}, "summary": phx.summarize_phoenix([], {}),
                  "recent_trades": []}
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, separators=(",", ":"))
        await data_feed.close()
        return

    portfolio = phx.simulate_portfolio(all_trades)
    summary = phx.summarize_phoenix(all_trades, portfolio)

    report = {
        "generated_ts": pd.Timestamp.utcnow().isoformat(),
        "params": {"lookback_days": LOOKBACK_DAYS, "htf": config.HTF, "ltf": "1h",
                   "symbols": len(SYMBOLS), "demo": config.DEMO,
                   "regime_days": {str(k): int(v) for k, v in reg_counts.items()},
                   "risk_trend": config.PHX_RISK_TREND, "risk_range": config.PHX_RISK_RANGE},
        "summary": summary,
        "equity_curve": portfolio["equity_curve"],
        "recent_trades": [
            {k: t.get(k) for k in ("symbol", "engine", "direction", "regime", "entry",
                                   "exit_price", "outcome", "r", "rr", "recovering",
                                   "entry_ts", "exit_ts")}
            for t in sorted(portfolio["accepted"], key=lambda x: x["exit_ts"], reverse=True)[:50]
        ],
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))

    ov = summary["overall"]
    pf = summary["portfolio"]
    print(f"[phoenix] DONE: {ov['trades']} signals, win {ov['win_rate']}%, "
          f"PF {ov['profit_factor']}, exp {ov['expectancy_r']}R | "
          f"portfolio {pf['final_return_pct']}% (maxDD {pf['max_drawdown_pct']}%, "
          f"recovery x{pf['recovery_episodes']}, took {pf['n_accepted']}/{pf['n_signals']})")
    for e in phx.ENGINES:
        s = summary["by_engine"][e]
        print(f"[phoenix]   {e:9s}: {s['trades']} trades, win {s['win_rate']}%, PF {s['profit_factor']}")
    await data_feed.close()


if __name__ == "__main__":
    asyncio.run(main())
