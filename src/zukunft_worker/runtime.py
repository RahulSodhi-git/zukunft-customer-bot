from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .binance import BinanceFutures
from .strategy import CustomerStrategy


class TenantRuntime:
    """Isolated in-memory runtime for one customer. Secrets are never written to disk."""

    def __init__(self, account: dict[str, Any], binance_credentials: dict[str, str], state_dir: Path,
                 execution_enabled: bool, coinalyze_credentials=None):
        self.user_id = str(account["user_id"])
        self.customer_number = str(account["customer_number"])
        self.credential_version = int(account.get("credential_version", 0))
        self.desired_state = str(account.get("desired_state", "paused"))
        self.execution_enabled = execution_enabled
        self.client = BinanceFutures(binance_credentials["apiKey"], binance_credentials["apiSecret"])
        self.state_path = state_dir / f"{self.user_id}.json"
        self.state = self._load_state()
        self.account = account
        self.strategy = (CustomerStrategy(self.user_id, state_dir, binance_credentials,
                                         coinalyze_credentials) if execution_enabled else None)

    def _load_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, separators=(",", ":")), encoding="utf8")
        temporary.replace(self.state_path)

    def update(self, account: dict[str, Any]) -> None:
        self.account = account
        self.desired_state = str(account.get("desired_state", "paused"))

    def snapshot(self) -> dict[str, Any]:
        positions = self.client.open_positions()
        now = int(time.time() * 1000)
        first_seen = self.state.setdefault("firstSeen", {})
        peaks = self.state.setdefault("peakMarginPct", {})
        active_symbols = set()
        for position in positions:
            symbol = position["coin"]
            active_symbols.add(symbol)
            first_seen.setdefault(symbol, now)
            peaks[symbol] = max(float(peaks.get(symbol, position["marginPct"])), float(position["marginPct"]))
            position["openedAt"] = first_seen[symbol]
            position["peakMarginPct"] = round(peaks[symbol], 4)
        for symbol in list(first_seen):
            if symbol not in active_symbols:
                first_seen.pop(symbol, None)
                peaks.pop(symbol, None)
        self._save_state()

        if self.desired_state == "running" and not self.execution_enabled:
            actual_state = "paused"
            error = "Execution is safety-locked while the migrated strategy is being verified."
        elif self.strategy:
            actual_state, error = self.strategy.reconcile(self.account, positions)
        else:
            actual_state = "stopped" if self.desired_state == "stopped" else "paused"
            error = None

        return {
            "userId": self.user_id,
            "credentialVersion": self.credential_version,
            "actualState": actual_state,
            "error": error,
            "openPositions": positions,
            "recentClosedTrades": self.client.recent_realized_pnl(),
        }

