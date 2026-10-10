from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any


class BinanceFutures:
    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://fapi.binance.com"):
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf8")
        self.base_url = base_url.rstrip("/")

    def _signed_get(self, path: str, params: dict[str, Any] | None = None, *, timestamp_offset: int = 0, timeout: int = 15) -> Any:
        values = {**(params or {}), "timestamp": int(time.time() * 1000) + timestamp_offset, "recvWindow": 5000}
        query = urllib.parse.urlencode(values)
        signature = hmac.new(self.api_secret, query.encode("utf8"), hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            f"{self.base_url}{path}?{query}&signature={signature}",
            headers={"X-MBX-APIKEY": self.api_key},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf8"))

    def open_positions(self) -> list[dict[str, Any]]:
        rows = self._signed_get("/fapi/v3/positionRisk")
        positions: list[dict[str, Any]] = []
        for row in rows:
            amount = float(row.get("positionAmt", 0))
            if amount == 0:
                continue
            entry = float(row.get("entryPrice", 0))
            mark = float(row.get("markPrice", 0))
            leverage = max(1, int(float(row.get("leverage", 1))))
            pnl = float(row.get("unRealizedProfit", 0))
            notional = abs(float(row.get("notional", amount * mark)))
            margin = abs(float(row.get("positionInitialMargin", 0))) or (notional / leverage if leverage else notional)
            direction = 1 if amount > 0 else -1
            price_pct = ((mark - entry) / entry * 100 * direction) if entry else 0
            margin_pct = (pnl / margin * 100) if margin else 0
            positions.append({
                "coin": row.get("symbol", ""),
                "side": "LONG" if amount > 0 else "SHORT",
                "entry": entry,
                "now": mark,
                "stoploss": None,
                "leverage": leverage,
                "marginUsed": round(margin, 4),
                "marginPct": round(margin_pct, 4),
                "pnlUsdt": round(pnl, 4),
                "pricePct": round(price_pct, 4),
                "quantity": abs(amount),
            })
        return positions

    def recent_realized_pnl(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._signed_get("/fapi/v1/income", {"incomeType": "REALIZED_PNL", "limit": limit})
        grouped: dict[tuple[str, int], float] = defaultdict(float)
        for row in rows:
            timestamp = int(row.get("time", 0))
            grouped[(str(row.get("symbol", "")), timestamp)] += float(row.get("income", 0))
        return [
            {
                "when": timestamp,
                "coin": symbol,
                "side": "—",
                "closeReason": "Exchange close",
                "heldFor": "—",
                "marginUsed": None,
                "marginPct": None,
                "pnlUsdt": round(pnl, 4),
            }
            for (symbol, timestamp), pnl in sorted(grouped.items(), key=lambda item: item[0][1], reverse=True)
        ][:50]

