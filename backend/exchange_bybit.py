"""Bybit USDT-M perpetual futures adapter (ccxt async).

Read methods (equity / positions / price / markets) + order-write methods
(leverage, market entry, reduce-only stop & take-profit, cancel, close). The
writes are only ever called by the position manager when LIVE_TRADING=1 AND
EXEC_DRY_RUN=0, and start on Bybit TESTNET so money cannot move by accident.

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

    # ---------------------------------------------------- precision helpers
    def amount(self, symbol: str, qty: float) -> float:
        try:
            return float(self.ex.amount_to_precision(symbol, qty))
        except Exception:
            return float(qty)

    def price_p(self, symbol: str, px: float) -> float:
        try:
            return float(self.ex.price_to_precision(symbol, px))
        except Exception:
            return float(px)

    # ------------------------------------------------------- FASE 3: writes
    # Every method below actually mutates the account. They are only ever called
    # when LIVE_TRADING=1 and EXEC_DRY_RUN=0 (see executor + position_manager),
    # and start on TESTNET. Each is defensive: errors are raised to the caller,
    # which logs and moves on rather than crashing the scan loop.
    async def set_leverage_isolated(self, symbol: str, leverage: float):
        """Best-effort isolated margin + leverage. Bybit rejects a no-op change
        (same leverage) with an error code we can safely ignore."""
        try:
            await self.ex.set_margin_mode(MARGIN_MODE, symbol, {"leverage": leverage})
        except Exception as exc:
            if "not modified" not in str(exc).lower() and "110026" not in str(exc):
                print(f"[bybit] set_margin_mode {symbol}: {exc}")
        try:
            await self.ex.set_leverage(leverage, symbol)
        except Exception as exc:
            if "not modified" not in str(exc).lower() and "110043" not in str(exc):
                print(f"[bybit] set_leverage {symbol}: {exc}")

    async def market_entry(self, symbol: str, side: str, qty: float) -> dict:
        """Open a position with a market order (side 'buy'/'sell')."""
        return await self.ex.create_order(symbol, "market", side, self.amount(symbol, qty))

    async def place_stop(self, symbol: str, side: str, qty: float, trigger: float) -> dict:
        """Reduce-only stop-market (protective SL). `side` closes the position."""
        return await self.ex.create_order(
            symbol, "market", side, self.amount(symbol, qty), None,
            {"reduceOnly": True, "triggerPrice": self.price_p(symbol, trigger)})

    async def place_tp(self, symbol: str, side: str, qty: float, price: float) -> dict:
        """Reduce-only limit take-profit. `side` closes the position."""
        return await self.ex.create_order(
            symbol, "limit", side, self.amount(symbol, qty),
            self.price_p(symbol, price), {"reduceOnly": True})

    async def cancel(self, symbol: str, order_id: str):
        try:
            await self.ex.cancel_order(order_id, symbol)
        except Exception as exc:
            print(f"[bybit] cancel {symbol}/{order_id}: {exc}")

    async def cancel_all(self, symbol: str):
        try:
            await self.ex.cancel_all_orders(symbol)
        except Exception as exc:
            print(f"[bybit] cancel_all {symbol}: {exc}")

    async def close_position(self, symbol: str, side: str, qty: float) -> dict:
        """Market-close a position (side is the CLOSING side), reduce-only."""
        return await self.ex.create_order(
            symbol, "market", side, self.amount(symbol, qty), None, {"reduceOnly": True})

    async def position_size(self, symbol: str) -> tuple[float, float]:
        """(signed_contracts, entry_price) for a symbol; (0,0) if flat.
        Positive = long, negative = short."""
        for p in await self.ex.fetch_positions([symbol]):
            c = float(p.get("contracts") or 0)
            if c:
                sign = -1 if (p.get("side") == "short") else 1
                return sign * c, float(p.get("entryPrice") or 0)
        return 0.0, 0.0
