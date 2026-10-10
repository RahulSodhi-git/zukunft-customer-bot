"""Coinalyze-first OI for Binance perpetuals, with direct Binance fallback.

This is a provider fallback, not a cross-exchange aggregate. Changes use
timestamp-aligned closed five-minute samples at the requested lookback.
"""
import math
import os
import threading
import time
from collections import deque

import requests

from . import settings  # loads the existing environment

CB = "https://api.coinalyze.net/v1"
_SESS = requests.Session()
_MAP = {"m": {}, "ts": 0.0}
_OI = {}
_LOCK = threading.RLock()
_CALLS = deque()
_BLOCKED_UNTIL = 0.0


def _request(endpoint, params, credits=1):
    global _BLOCKED_UNTIL
    key = os.getenv("COINALYZE_KEY", "")
    now = time.time()
    while _CALLS and now - _CALLS[0] >= 60:
        _CALLS.popleft()
    if not key or now < _BLOCKED_UNTIL or len(_CALLS) + credits > 40:
        return None
    _CALLS.extend([now] * credits)
    try:
        response = _SESS.get(CB + endpoint, headers={"api_key": key}, params=params, timeout=10)
        if response.status_code == 429:
            _BLOCKED_UNTIL = now + max(60, int(response.headers.get("Retry-After", 60)))
            return None
        response.raise_for_status()
        result = response.json()
        return result if isinstance(result, list) else None
    except (requests.RequestException, ValueError, TypeError):
        return None


def _symmap():
    now = time.time()
    if now - _MAP["ts"] < 3600 and _MAP["m"]:
        return _MAP["m"]
    markets = _request("/future-markets", {})
    if markets is not None:
        _MAP.update(m={x["symbol_on_exchange"]: x["symbol"] for x in markets
                       if isinstance(x, dict) and x.get("symbol_on_exchange") and x.get("symbol")
                       and x.get("exchange") == "A" and x.get("is_perpetual")
                       and x.get("quote_asset") == "USDT"}, ts=now)
    return _MAP["m"] if now - _MAP["ts"] < 3600 else {}


def _pct_change(history, minutes, end):
    """end is the opening timestamp of the most recent fully closed 5m bar."""
    start = end - minutes * 60
    try:
        samples = {int(x["t"]): float(x["c"]) for x in history}
        if any(t not in samples or not math.isfinite(samples[t]) or samples[t] <= 0
               for t in range(start, end + 1, 300)):
            return None
        return (samples[end] / samples[start] - 1) * 100
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


def prime(symbols, hours=4):
    minutes = round(float(hours) * 60)
    if minutes <= 0 or minutes % 5:
        raise ValueError("OI lookback must be a positive multiple of five minutes")
    end = int(time.time()) // 300 * 300 - 300
    with _LOCK:
        for key in list(_OI):
            if key[2] != end:
                _OI.pop(key, None)
        todo = [s.upper() for s in dict.fromkeys(symbols) if (s.upper(), minutes, end) not in _OI]
        mapping = _symmap() if todo else {}
        for symbol in todo:
            _OI[(symbol, minutes, end)] = None
        reverse = {mapping[s]: s for s in todo if s in mapping}
        names = list(reverse)
        for i in range(0, len(names), 20):
            batch = names[i:i + 20]
            result = _request("/open-interest-history", {
                "symbols": ",".join(batch), "interval": "5min",
                "from": end - minutes * 60, "to": end + 299,
            }, credits=len(batch))
            for row in result or []:
                if not isinstance(row, dict):
                    continue
                symbol = reverse.get(row.get("symbol"))
                if symbol:
                    _OI[(symbol, minutes, end)] = _pct_change(row.get("history", []), minutes, end)


def oi_change(symbol, hours=4, trader=None):
    minutes = round(float(hours) * 60)
    end = int(time.time()) // 300 * 300 - 300
    try:
        prime([symbol], hours)
    except (requests.RequestException, KeyError, TypeError, ValueError, OverflowError):
        # A malformed provider response must not bypass the fallback.
        with _LOCK:
            _OI.pop((symbol.upper(), minutes, end), None)
    with _LOCK:
        value = _OI.get((symbol.upper(), minutes, end))
    if value is not None:
        return value, "coinalyze"
    if trader is not None:
        try:
            value = trader.oi_change_pct(symbol, hours)
            if value is not None and math.isfinite(value):
                return value, "binance"
        except Exception:
            pass
    return None, "none"
