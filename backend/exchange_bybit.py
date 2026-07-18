"""Bybit USDT-M perpetual futures adapter (ccxt async).

FASE 1 = READ-ONLY + scaffolding. This module can authenticate, load markets,
map watchlist symbols to Bybit linear-perp symbols, and read equity / positions /
price. It deliberately does NOT place any orders yet — order execution is Fase 2
(the executor / position manager). Every future write path will be gated behind
LIVE_TRADING and default to DRY_RUN so money can never move by accident.

Config via env (NEVER commit real keys — see deploy/.env.example):
  EXCHANGE_TESTNET=1     use Bybit testnet (default 1 = safe sandbox)
  BYBIT_API_KEY / BYBIT_API_SECRET
  LIVE_TRADING=0         master switch for real orders (default 0 = off)
  EXEC_DRY_RUN=1         log intended orders instead of sending (default 1)
  EXEC_LEVERAGE=7        isolated leverage
  EXEC_RISK_PCT=0.02     risk per trade (2% equity)
  EXEC_MAX_CONCURRENT=3  max simultaneous open positions
"""
from __future__ import annotations

import os

import ccxt.async_support as ccxt


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default) == "1"


TESTNET = _flag("EXCHANGE_TESTNET", "1")
LIVE_TRADING = _flag("LIVE_TRADING", "0")
DRY_RUN = _flag("EXEC_DRY_RUN", "1")
LEVERAGE = float(os.getenv("EXEC_LEVERAGE", "7"))
RISK_PCT = float(os.getenv("EXEC_RISK_PCT", "0.02"))
MAX_CONCURRENT = int(os.getenv("EXEC_MAX_CONCURRENT", "3"))
MARGIN_MODE = os.getenv("EXEC_MARGIN_MODE", "isolated")


class BybitFutures:
    """Thin async wrapper over ccxt.bybit for USDT-M linear perpetuals."""

    def __init__(self):
        self.testnet = TESTNET
        self.ex = ccxt.bybit({
            "apiKey": os.getenv("BYBIT_API_KEY", ""),
            "secret": os.getenv("BYBIT_API_SECRET", ""),
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},   # linear USDT perps
        })
        if self.testnet:
            self.ex.set_sandbox_mode(True)         # route to api-testnet.bybit.com
        self.markets: dict | None = None

    # ---------------------------------------------------------------- meta
    async def load(self) -> dict:
        self.markets = await self.ex.load_markets()
        return self.markets

    @staticmethod
    def to_symbol(watch: str) -> str:
        """'ETHUSDT' -> 'ETH/USDT:USDT' (Bybit linear-perp unified symbol)."""
        base = watch[:-4] if watch.upper().endswith("USDT") else watch
        return f"{base}/USDT:USDT"

    def has_market(self, watch: str) -> bool:
        return bool(self.markets) and self.to_symbol(watch) in self.markets

    # ------------------------------------------------------------ read-only
    async def equity_usdt(self) -> float:
        """Total USDT equity of the (unified) account."""
        bal = await self.ex.fetch_balance()
        usdt = bal.get("USDT") or {}
        return float(usdt.get("total") or 0.0)

    async def open_positions(self) -> list[dict]:
        """Open perp positions (non-zero size only)."""
        pos = await self.ex.fetch_positions()
        return [p for p in pos if float(p.get("contracts") or 0) != 0]

    async def price(self, watch: str) -> float:
        t = await self.ex.fetch_ticker(self.to_symbol(watch))
        return float(t["last"])

    async def close(self):
        await self.ex.close()

    # -------------------------------------------------------- fase 2 (later)
    # place_market_entry(), place_stop_loss(), place_take_profit(),
    # move_sl_to_breakeven(), cancel_all(), set_leverage() — implemented in the
    # executor once the read-only connectivity check passes on the VPS/testnet.
