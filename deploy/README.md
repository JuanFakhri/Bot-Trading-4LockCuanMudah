# Live execution (VPS) — Bybit USDT-M futures

The signal brain (Phoenix long + SMC short) is unchanged. This adds the "hands"
that turn ENTRY signals into real orders. Rolled out in phases; nothing moves
money until you explicitly flip the switches.

## Status
- **Fase 1 — DONE (read-only):** Bybit adapter + connectivity check.
- **Fase 2 — DONE (DRY_RUN):** executor sizes each ENTRY (2% risk, 7x) and LOGS the
  full order plan (entry + SL + TP1 50% + TP2). No orders sent; live sending guarded.
- Fase 3 — position manager (place orders, SL→breakeven after TP1, reconcile), tiny real money.
- Fase 4 — kill-switch + alerts, scale up.

### Try Fase 2 now (offline, no keys)
```bash
python -m scripts.exec_demo        # prints example LONG + SHORT order plans
```
On the VPS/uDroid, set `EXEC_ENABLED=1` in `.env` and run the bot
(`uvicorn backend.main:app ...`): every ENTRY signal now logs its order plan in
the journal (`journalctl -u nestsmc -f` or the tmux window). Still DRY_RUN —
nothing is sent until Fase 3.

## Why Bybit (not MEXC)
MEXC blocks **futures order placement via API** for retail accounts (the contract
order endpoint is "under maintenance"; only whitelisted market-makers can trade).
Reading data works, placing orders does not. Bybit's retail futures API is fully
open and has a testnet — so we start there.

## Fase 1 — run the read-only check
On the VPS (Ubuntu). Start on **testnet** (fake money):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-exec.txt
cp deploy/.env.example .env      # then edit .env

# public check (no keys needed) — verifies symbols map to Bybit perps
EXCHANGE_TESTNET=1 python -m scripts.bybit_check

# with testnet keys (testnet.bybit.com) — also reads equity + positions
set -a; source .env; set +a
python -m scripts.bybit_check
```

Expect: all 15 watchlist symbols print `OK`, and (with keys) your testnet USDT
equity + open positions. **This script never places an order.**

## Safety model
| Switch | Default | Meaning |
|---|---|---|
| `EXCHANGE_TESTNET` | `1` | fake-money sandbox |
| `LIVE_TRADING` | `0` | `0` = paper only, no exchange orders at all |
| `EXEC_DRY_RUN` | `1` | executor logs intended orders instead of sending |

Real orders require `LIVE_TRADING=1` **and** `EXEC_DRY_RUN=0`. API key permission
must be **trade only — withdraw disabled**. Keep keys in `.env` on the server,
never in the repo.
