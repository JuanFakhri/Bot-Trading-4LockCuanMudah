"""Free market-data feed.

OHLCV comes from Binance's public REST endpoints (no API key needed).
USDT dominance (USDT.D) is derived from CoinGecko's free ``/global`` endpoint
plus Tether market-cap history as a 20-day range proxy.

Every call degrades gracefully: on failure it returns cached/empty data so the
rest of the bot keeps running instead of crashing.
"""
from __future__ import annotations

import time
from typing import Optional

import httpx
import pandas as pd

from . import config

_client = httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "fib-hybrid-bot/1.0"})

# simple in-memory cache: key -> (timestamp, dataframe)
_kline_cache: dict[str, tuple[float, pd.DataFrame]] = {}
_usdtd_cache: dict[str, object] = {}


async def _binance_get(path: str, params: dict) -> Optional[list]:
    last_err = None
    for base in config.BINANCE_BASES:
        try:
            r = await _client.get(base + path, params=params)
            if r.status_code == 200:
                return r.json()
            last_err = f"{base} -> HTTP {r.status_code}"
        except Exception as exc:  # network / TLS / timeout
            last_err = f"{base} -> {exc}"
    print(f"[data_feed] binance {path} failed: {last_err}")
    return None


async def get_klines(symbol: str, interval: str, limit: int = config.KLIMIT,
                     max_age: float = 20.0) -> pd.DataFrame:
    """Return an OHLCV DataFrame indexed by close time (UTC)."""
    if config.DEMO:
        from . import demo
        return demo.klines(symbol, interval, limit)

    key = f"{symbol}:{interval}:{limit}"
    now = time.time()
    cached = _kline_cache.get(key)
    if cached and (now - cached[0]) < max_age:
        return cached[1]

    raw = await _binance_get(
        "/api/v3/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )
    if not raw:
        return cached[1] if cached else pd.DataFrame()

    df = pd.DataFrame(
        raw,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "trades", "tbav", "tbqv", "ignore",
        ],
    )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df = df[["time", "open", "high", "low", "close", "volume"]].set_index("time")
    df = df.dropna()
    _kline_cache[key] = (now, df)
    return df


async def get_klines_history(symbol: str, interval: str, days: int) -> pd.DataFrame:
    """Paginated historical OHLCV covering roughly the last ``days`` days.

    Binance caps klines at 1000 per request, so we walk forward from the start
    time. Used by the backtester. In demo mode a synthetic long series is
    generated instead.
    """
    if config.DEMO:
        from . import demo
        bars = {"15m": 96, "1h": 24, "4h": 6, "1d": 1}.get(interval, 6) * days
        return demo.klines(symbol, interval, min(bars, 4000))

    ms_per = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[interval]
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    frames = []
    cursor = start
    guard = 0
    while cursor < end and guard < 60:
        guard += 1
        raw = await _binance_get(
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval, "startTime": cursor,
             "endTime": end, "limit": 1000},
        )
        if not raw:
            break
        frames.append(raw)
        last_open = raw[-1][0]
        nxt = last_open + ms_per
        if nxt <= cursor or len(raw) < 1000:
            cursor = nxt
            if len(raw) < 1000:
                break
        else:
            cursor = nxt

    if not frames:
        return pd.DataFrame()
    rows = [r for chunk in frames for r in chunk]
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "tbav", "tbqv", "ignore"])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df = df[["time", "open", "high", "low", "close", "volume"]].set_index("time")
    df = df[~df.index.duplicated(keep="first")].dropna().sort_index()
    return df


async def get_price(symbol: str) -> Optional[float]:
    raw = await _binance_get("/api/v3/ticker/price", {"symbol": symbol})
    if raw and "price" in raw:
        try:
            return float(raw["price"])
        except (TypeError, ValueError):
            return None
    return None


async def get_usdt_dominance() -> dict:
    """Return {'value': pct, 'pos': 0..1, 'rising': bool, 'ok': bool}.

    ``pos`` is USDT.D position inside its 20-day range (0 = support, 1 = resistance).
    ``rising`` compares against ~1 day ago. Falls back to neutral values.
    """
    if config.DEMO:
        from . import demo
        return demo.usdt_dominance()

    now = time.time()
    if _usdtd_cache and (now - float(_usdtd_cache.get("_ts", 0))) < 300:
        return _usdtd_cache["value"]  # type: ignore[return-value]

    result = {"value": None, "pos": 0.5, "rising": False, "ok": False}
    try:
        g = await _client.get(config.COINGECKO_BASE + "/global")
        current = None
        if g.status_code == 200:
            mcp = g.json().get("data", {}).get("market_cap_percentage", {})
            current = mcp.get("usdt")
        # 20-day tether market-cap history as a range proxy for USDT.D position
        h = await _client.get(
            config.COINGECKO_BASE + "/coins/tether/market_chart",
            params={"vs_currency": "usd", "days": str(config.USDTD_LOOKBACK), "interval": "daily"},
        )
        if h.status_code == 200 and current is not None:
            caps = [p[1] for p in h.json().get("market_caps", []) if p and p[1]]
            if len(caps) >= 3:
                lo, hi = min(caps), max(caps)
                cur_cap = caps[-1]
                rng = (hi - lo) or 1.0
                pos = (cur_cap - lo) / rng
                rising = caps[-1] > caps[max(0, len(caps) - 2)]
                result = {
                    "value": round(current, 3),
                    "pos": round(max(0.0, min(1.0, pos)), 3),
                    "rising": bool(rising),
                    "ok": True,
                }
    except Exception as exc:
        print(f"[data_feed] usdt.d failed: {exc}")

    _usdtd_cache["_ts"] = now
    _usdtd_cache["value"] = result
    return result


async def close():
    await _client.aclose()
