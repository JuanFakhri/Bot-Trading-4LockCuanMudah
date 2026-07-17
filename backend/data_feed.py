"""Free market-data feed.

OHLCV comes from Binance's public REST endpoints (no API key needed).
USDT dominance (USDT.D) is derived from CoinGecko's free ``/global`` endpoint
plus Tether market-cap history as a 20-day range proxy.

Every call degrades gracefully: on failure it returns cached/empty data so the
rest of the bot keeps running instead of crashing.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
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

    result = {"value": None, "pos": 0.5, "rising": False, "consolidating": False, "ok": False}
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
                # consolidation: little movement over the last ~7 days
                recent = caps[-7:]
                recent_pos = [(x - lo) / rng for x in recent]
                consolidating = (max(recent_pos) - min(recent_pos)) < 0.2 if len(recent) >= 4 else False
                result = {
                    "value": round(current, 3),
                    "pos": round(max(0.0, min(1.0, pos)), 3),
                    "rising": bool(rising),
                    "consolidating": bool(consolidating),
                    "ok": True,
                }
    except Exception as exc:
        print(f"[data_feed] usdt.d failed: {exc}")

    _usdtd_cache["_ts"] = now
    _usdtd_cache["value"] = result
    return result


_btcd_cache: dict[str, object] = {}


async def get_btc_dominance() -> dict:
    """Return {'value': pct, 'ok': bool} — current BTC dominance from CoinGecko.

    Direction is derived elsewhere (via the ETH/BTC ratio proxy) since free
    historical dominance is not readily available.
    """
    if config.DEMO:
        return {"value": 54.0, "ok": True}

    now = time.time()
    if _btcd_cache and (now - float(_btcd_cache.get("_ts", 0))) < 300:
        return _btcd_cache["value"]  # type: ignore[return-value]

    result = {"value": None, "ok": False}
    try:
        g = await _client.get(config.COINGECKO_BASE + "/global")
        if g.status_code == 200:
            btc = g.json().get("data", {}).get("market_cap_percentage", {}).get("btc")
            if btc is not None:
                result = {"value": round(btc, 2), "ok": True}
    except Exception as exc:
        print(f"[data_feed] btc.d failed: {exc}")

    _btcd_cache["_ts"] = now
    _btcd_cache["value"] = result
    return result


def _screen_event(ev: dict) -> dict:
    """Attach the crypto-impact screen (bias / verdict / reason) to one event.

    For upcoming events the actual print isn't out yet, so the screen reads the
    *expectation* (forecast vs previous): "kalau rilis sesuai perkiraan, dampak
    ke crypto bagus/buruk". This is analysis only — it does not touch trading.
    """
    from . import macro_news
    a = macro_news.assess_event(ev.get("title", ""), actual=ev.get("actual"),
                                forecast=ev.get("forecast"), previous=ev.get("previous"))
    ev.update({
        "type": a.type_label,
        "bias": a.bias,
        "verdict": a.verdict,
        "score": round(a.score, 3),
        "reason": a.reason,
        "basis": a.basis,
    })
    return ev


async def get_economic_calendar() -> list[dict]:
    """High-impact economic news for this week (ForexFactory JSON mirror).

    Each event is ``{"title","country","impact","ts","forecast","previous",
    "bias","verdict","score","reason","basis"}``. ``ts`` is ISO-8601 UTC. Only
    High-impact, timed events are kept. The crypto-impact screen (bias/verdict)
    is added by ``macro_news`` and is analysis-only — the live trade engine is
    untouched.
    """
    if config.DEMO:
        # synthetic upcoming events (with forecast/previous) so the screen shows
        base = datetime.now(timezone.utc)
        demo = [
            {"title": "CPI y/y", "country": "USD", "impact": "High",
             "ts": (base + timedelta(hours=3)).isoformat(),
             "forecast": "3.0%", "previous": "3.3%"},        # inflasi turun → bagus
            {"title": "Federal Funds Rate", "country": "USD", "impact": "High",
             "ts": (base + timedelta(hours=26)).isoformat(),
             "forecast": "5.00%", "previous": "5.25%"},       # rate cut → bagus
            {"title": "Non-Farm Employment Change", "country": "USD", "impact": "High",
             "ts": (base + timedelta(hours=50)).isoformat(),
             "forecast": "230K", "previous": "180K"},         # jobs panas → buruk
        ]
        return [_screen_event(e) for e in demo]
    out: list[dict] = []
    try:
        r = await _client.get(config.NEWS_URL)
        if r.status_code != 200:
            print(f"[data_feed] news HTTP {r.status_code}")
            return out
        for ev in r.json():
            if str(ev.get("impact", "")).lower() != "high":
                continue
            raw = ev.get("date")
            if not raw:
                continue
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out.append(_screen_event({
                "title": ev.get("title", "News"),
                "country": ev.get("country", ""),
                "impact": "High",
                "ts": dt.astimezone(timezone.utc).isoformat(),
                "forecast": ev.get("forecast", ""),
                "previous": ev.get("previous", ""),
            }))
    except Exception as exc:
        print(f"[data_feed] news failed: {exc}")
    out.sort(key=lambda e: e["ts"])
    return out


async def get_cpi_bias() -> dict:
    """Macro backdrop from US CPI trend (FRED, free CSV, no key).

    Backtest (3y) showed: inflation FALLING -> crypto +5.5% (BTC, 14d), inflation
    RISING -> -2.5%. So we surface a standing bias: disinflation = bullish
    backdrop, rising CPI = bearish. Informational screen only — does not gate
    trades. Returns {yoy, prev_yoy, direction, bias, asof, ok}.
    """
    if config.DEMO:
        return {"yoy": 2.9, "prev_yoy": 3.1, "direction": "TURUN",
                "bias": "BULLISH", "asof": "2026-06", "ok": True}
    try:
        import io
        r = await _client.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL",
                              timeout=30.0)
        if r.status_code != 200:
            return {"ok": False}
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = ["date", "value"]
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.dropna().reset_index(drop=True)
        if len(df) < 14:
            return {"ok": False}
        # year-over-year inflation for the last two available months
        v = df["value"]
        yoy_now = (v.iloc[-1] / v.iloc[-13] - 1) * 100
        yoy_prev = (v.iloc[-2] / v.iloc[-14] - 1) * 100
        delta = yoy_now - yoy_prev
        if delta < -0.05:
            direction, bias = "TURUN", "BULLISH"
        elif delta > 0.05:
            direction, bias = "NAIK", "BEARISH"
        else:
            direction, bias = "STABIL", "NETRAL"
        return {"yoy": round(float(yoy_now), 2), "prev_yoy": round(float(yoy_prev), 2),
                "direction": direction, "bias": bias,
                "asof": str(df["date"].iloc[-1])[:7], "ok": True}
    except Exception as exc:
        print(f"[data_feed] cpi bias failed: {exc}")
        return {"ok": False}


async def close():
    await _client.aclose()
