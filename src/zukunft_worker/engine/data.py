"""Fetch public Binance market data (no API key needed) — spot + USD-M futures.

Spot uses data-api.binance.vision (works from US/cloud). Futures uses fapi
(needed for futures-only coins like LABUSDT). Futures is geo-blocked from US
cloud hosts, so those coins load only when the app runs from a non-US location.
"""
import time as _time

import requests
import pandas as pd

SPOT_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
FUT_HOSTS = ["https://fapi.binance.com"]

# Only these underlying types are REAL crypto perps. Everything else Binance lists on futures —
# EQUITY / HK_EQUITY (tokenized stocks), COMMODITY (gold/silver/etc.), INDEX — is a "TradFi perp"
# that needs a signed agreement and rejects the bot's orders (-4411), so we never trade them.
_CRYPTO_UNDERLYING = {"COIN", "PREMARKET"}
_CRYPTO_PERPS = {"syms": set(), "ts": 0.0}


def crypto_perp_symbols(ttl=3600):
    """The set of USDT-margined perps that are genuine crypto (contractType PERPETUAL, underlyingType
    COIN/PREMARKET, TRADING). Used to drop tokenized stocks, metals and indices from every scan BEFORE
    the bot ever tries to open them — so they stop flooding the log with -4411 / -1121. Cached ~1h.
    Returns an empty set if the fetch fails (callers then skip the filter rather than block everything)."""
    now = _time.time()
    if _CRYPTO_PERPS["syms"] and now - _CRYPTO_PERPS["ts"] < ttl:
        return _CRYPTO_PERPS["syms"]
    try:
        info = _get(FUT_HOSTS, "/fapi/v1/exchangeInfo", {})
        ok = {s["symbol"] for s in info.get("symbols", [])
              if s.get("contractType") == "PERPETUAL"
              and s.get("underlyingType") in _CRYPTO_UNDERLYING
              and s.get("quoteAsset") == "USDT"
              and s.get("status") == "TRADING"}
        if ok:                                  # only cache a real result; keep old set on a bad fetch
            _CRYPTO_PERPS["syms"] = ok
            _CRYPTO_PERPS["ts"] = now
    except Exception:
        pass
    return _CRYPTO_PERPS["syms"]


def _get(hosts, path, params):
    last = None
    for h in hosts:
        try:
            r = requests.get(h + path, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code} from {h}"
        except Exception as e:
            last = str(e)
    raise RuntimeError(last or "request failed")


def _to_df(data):
    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "trades", "tbav", "tqav", "ignore"]
    df = pd.DataFrame(data, columns=cols)
    df["time"] = pd.to_datetime(df["open_time"], unit="ms")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    return df[["time", "open", "high", "low", "close", "volume"]]


def get_ohlcv(symbol="BTCUSDT", interval="1d", limit=500, closed_only=False, market="auto"):
    symbol = symbol.upper()
    p = {"symbol": symbol, "interval": interval, "limit": limit}
    if market == "futures":
        df = _to_df(_get(FUT_HOSTS, "/fapi/v1/klines", p))
    elif market == "auto":
        try:
            df = _to_df(_get(SPOT_HOSTS, "/api/v3/klines", p))
        except Exception:
            df = _to_df(_get(FUT_HOSTS, "/fapi/v1/klines", p))
    else:
        raise ValueError("market must be auto or futures")
    if closed_only and len(df) > 1:         # drop the still-forming last candle
        df = df.iloc[:-1].reset_index(drop=True)
    return df


def tradeable_universe(min_quote_volume=1_000_000, limit=0):
    """ALL USDT perpetual futures with real 24h volume, ranked most-liquid first.
    Fully dynamic — no fixed list. limit=0 means every liquid coin; limit=N caps to top N."""
    try:
        data = _get(FUT_HOSTS, "/fapi/v1/ticker/24hr", {})
    except Exception:
        return []
    crypto = crypto_perp_symbols()                       # allowlist of genuine crypto perps
    rows = []
    for d in data:
        s = d.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        if crypto and s not in crypto:                   # drop tokenized stocks / metals / indices
            continue
        try:
            vol = float(d["quoteVolume"])
        except (KeyError, ValueError, TypeError):
            continue
        if vol >= min_quote_volume:
            rows.append((s, vol))
    rows.sort(key=lambda x: x[1], reverse=True)          # most liquid first
    syms = [s for s, _ in rows]
    return syms[:limit] if limit else syms


def high_volatility_coins(n=15, min_quote_volume=5_000_000, exclude=()):
    """Most volatile USDT perpetual futures by 24h price move, but liquid enough to trade.
    Adds high-vol small caps to the bot's scan. Needs non-US access (futures endpoint)."""
    try:
        data = _get(FUT_HOSTS, "/fapi/v1/ticker/24hr", {})
    except Exception:
        return []
    exclude = set(exclude)
    crypto = crypto_perp_symbols()                       # allowlist of genuine crypto perps
    rows = []
    for d in data:
        s = d.get("symbol", "")
        if not s.endswith("USDT") or s in exclude:
            continue
        if crypto and s not in crypto:                   # drop tokenized stocks / metals / indices
            continue
        try:
            change = abs(float(d["priceChangePercent"]))   # volatility proxy
            volume = float(d["quoteVolume"])                # liquidity filter (avoid dead coins)
        except (KeyError, ValueError, TypeError):
            continue
        if volume >= min_quote_volume:
            rows.append((s, change))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:n]]


def top_gainers(n=10, min_quote_volume=5_000_000):
    """The top-N USDT perps by 24h % GAIN — the 'top gainers list' the gainer strategy watches.
    Restricted to genuine crypto perps (no tokenized stocks/metals) with real liquidity, because
    the backtest showed the strategy loses money in EVERY configuration without a volume floor."""
    try:
        data = _get(FUT_HOSTS, "/fapi/v1/ticker/24hr", {})
    except Exception:
        return []
    crypto = crypto_perp_symbols()                       # allowlist of genuine crypto perps
    rows = []
    for d in data:
        s = d.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        if crypto and s not in crypto:                   # drop tokenized stocks / metals / indices
            continue
        try:
            chg = float(d["priceChangePercent"])
            vol = float(d["quoteVolume"])
        except (KeyError, ValueError, TypeError):
            continue
        if vol >= min_quote_volume:
            rows.append((s, chg))
    rows.sort(key=lambda x: x[1], reverse=True)          # biggest 24h gainer first
    return [s for s, _ in rows[:n]]


def quote_volumes():
    """24h quote (USDT) volume per USDT-perp futures symbol -> {symbol: quoteVolume}.
    Used by the special-trade LIQUIDITY FILTER to skip thin coins where a stop would slip."""
    out = {}
    try:
        for d in _get(FUT_HOSTS, "/fapi/v1/ticker/24hr", {}):
            s = d.get("symbol", "")
            if s.endswith("USDT"):
                try:
                    out[s] = float(d["quoteVolume"])
                except (KeyError, ValueError, TypeError):
                    continue
    except Exception:
        pass
    return out


def get_symbols(quote="USDT"):
    """All actively trading spot + futures perpetual pairs for a quote asset."""
    out = set()
    try:
        info = _get(SPOT_HOSTS, "/api/v3/exchangeInfo", {})
        out |= {s["symbol"] for s in info["symbols"]
                if s.get("status") == "TRADING" and s.get("quoteAsset") == quote}
    except Exception:
        pass
    try:                                    # futures (skipped silently if geo-blocked)
        finfo = _get(FUT_HOSTS, "/fapi/v1/exchangeInfo", {})
        out |= {s["symbol"] for s in finfo["symbols"]
                if s.get("status") == "TRADING" and s.get("quoteAsset") == quote
                and s.get("contractType") == "PERPETUAL"}
    except Exception:
        pass
    return sorted(out) or ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
