"""Read-only connection verifier. No trading engine or order endpoint is loaded."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .binance import BinanceFutures
from .config import Settings
from .control_plane import ControlPlane
from .crypto import CredentialDecryptor


class Verifier:
    def __init__(self, state_file: Path):
        self.state_file = state_file
        try:
            self.retry_at = float(json.loads(state_file.read_text())["retryAt"])
        except (OSError, ValueError, KeyError):
            self.retry_at = 0

    def verify(self, credential: dict[str, str]) -> dict[str, object]:
        if time.time() < self.retry_at:
            return {"status": "failed", "message": "Binance verification is cooling down. Please wait before retrying; your keys were not rejected."}
        try:
            # Exchange time avoids timestamp failures on newly provisioned hosts.
            with urllib.request.urlopen("https://fapi.binance.com/fapi/v1/time", timeout=8) as response:
                offset = int(json.loads(response.read())["serverTime"]) - int(time.time() * 1000)
            client = BinanceFutures(credential["apiKey"], credential["apiSecret"])
            account = client._signed_get("/fapi/v2/account", timestamp_offset=offset, timeout=10)
            wallet = Decimal(str(account["totalWalletBalance"]))
            available = Decimal(str(account["availableBalance"]))
            if not wallet.is_finite() or not available.is_finite():
                raise ValueError("Invalid account response")
            # Loading account information is the connection check. Readable accounts
            # may connect even when trading is disabled; starting orders is separate.
            summary = {
                "walletBalance": format(wallet, "f"),
                "availableBalance": format(available, "f"),
                "canTrade": account.get("canTrade") is True,
                "unit": "USD" if account.get("multiAssetsMargin") is True else "USDT",
                "loadedAt": datetime.now(timezone.utc).isoformat(),
            }
            return {"status": "verified", "message": "Binance account loaded. Connected.", "account": summary}
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read(4096))
            except (ValueError, OSError):
                body = {}
            if exc.code in (418, 429):
                match = re.search(r"banned until (\d{13})", str(body.get("msg", "")))
                try:
                    delay = max(120, float(exc.headers.get("Retry-After", "120")))
                except (TypeError, ValueError):
                    delay = 120
                self.retry_at = max(time.time() + delay, int(match[1]) / 1000 if match else 0)
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                self.state_file.write_text(json.dumps({"retryAt": self.retry_at}))
                return {"status": "failed", "message": "Binance has temporarily limited the customer server. Verification is paused automatically; do not repeatedly retry."}
            code = body.get("code")
            messages = {
                -2015: "Failed to connect Binance. Check your API key, secret, Futures access and trusted IP 137.23.35.138.",
                -2014: "The Binance API key format is invalid. Paste the actual Binance API key, not your Zukunft account ID.",
                -1022: "Binance could not verify the signature. Check that the API key and secret belong to the same key.",
                -1021: "The customer server clock could not synchronize. Please try again later.",
            }
            return {"status": "failed", "message": messages.get(code, "Failed to load Binance account. Please check your Binance connection and Futures access. Your keys were not saved.")}
        except Exception:
            # Never forward exceptions/URLs/keys to logs or the browser.
            return {"status": "failed", "message": "Binance verification could not complete. Your keys were not saved. Please try again later."}


def main() -> None:
    settings = Settings.from_env()
    plane = ControlPlane(settings.api_base_url, settings.worker_token)
    decryptor = CredentialDecryptor(settings.private_key_path)
    verifier = Verifier(settings.state_dir / "verification-cooldown.json")
    while True:
        try:
            job = plane._request("GET", "/worker/verification").get("job")
            if job:
                result = verifier.verify(decryptor.decrypt(job["ciphertext"]))
                plane._request("POST", f"/worker/verification/{job['id']}", result)
        except Exception:
            print("Verification control plane unavailable; retrying safely.", flush=True)
        # One sequential consumer: at most 15 verifications/minute. No automatic
        # retry of a rejected Binance request, no UI-driven direct provider calls.
        time.sleep(4)


if __name__ == "__main__":
    main()
