from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class ControlPlane:
    def __init__(self, base_url: str, token: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json", "X-Worker-Token": self.token},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf8", errors="replace")[:500]
            raise RuntimeError(f"Control plane returned {exc.code}: {detail}") from exc

    def accounts(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "/worker/accounts").get("accounts", []))

    def heartbeat(self, worker_id: str, accounts: list[dict[str, Any]]) -> None:
        self._request("POST", "/worker/heartbeat", {"workerId": worker_id, "accounts": accounts})

