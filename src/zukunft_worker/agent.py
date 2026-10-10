from __future__ import annotations

import logging
import time

from .config import Settings
from .control_plane import ControlPlane
from .crypto import CredentialDecryptor
from .runtime import TenantRuntime
from .strategy import atomic_json

LOGGER = logging.getLogger("zukunft-worker")


class WorkerAgent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.control = ControlPlane(settings.api_base_url, settings.worker_token)
        self.decryptor = CredentialDecryptor(settings.private_key_path)
        self.runtimes: dict[str, TenantRuntime] = {}

    def reconcile_once(self) -> int:
        accounts = self.control.accounts()
        active_ids: set[str] = set()
        snapshots = []
        for account in accounts:
            user_id = str(account["user_id"])
            active_ids.add(user_id)
            try:
                runtime = self.runtimes.get(user_id)
                version = int(account.get("credential_version", 0))
                if runtime and runtime.strategy and runtime.strategy.process:
                    if runtime.credential_version != version:
                        paused_account = {**account, "desired_state": "paused"}
                        runtime.update(paused_account)
                        snapshot = runtime.snapshot()
                        if snapshot["openPositions"]:
                            snapshot["error"] = "Waiting for positions to close before loading replacement credentials; exits remain active."
                            snapshots.append(snapshot)
                            continue
                        runtime.strategy.shutdown()
                if runtime is None or runtime.credential_version != version:
                    credentials = self.decryptor.decrypt(str(account["binance_ciphertext"]))
                    coinalyze = self.decryptor.decrypt(str(account["coinalyze_ciphertext"]))
                    # 1 GB host: deliberately limit the first live pilot to one customer.
                    admitted = self.settings.execution_enabled and not any(
                        item.strategy for key, item in self.runtimes.items() if key != user_id)
                    runtime = TenantRuntime(account, credentials, self.settings.state_dir,
                                            admitted, coinalyze)
                    self.runtimes[user_id] = runtime
                    LOGGER.info("Provisioned customer %s", account.get("customer_number"))
                else:
                    runtime.update(account)
                snapshot = runtime.snapshot()
                runtime.last_snapshot = snapshot
                snapshots.append(snapshot)
            except Exception as exc:  # one customer must never stop the others
                LOGGER.error("Customer %s sync failed (%s)", account.get("customer_number", user_id), type(exc).__name__)
                runtime = self.runtimes.get(user_id)
                if runtime and runtime.strategy:
                    atomic_json(runtime.strategy.directory / "entry_control.json", {"entries_paused": True})
                previous = getattr(self.runtimes.get(user_id), "last_snapshot", {})
                snapshots.append({
                    "userId": user_id,
                    "actualState": "error",
                    "error": "Exchange sync failed. Displayed positions may be stale; trading health is not confirmed.",
                    "openPositions": previous.get("openPositions", []),
                    "recentClosedTrades": previous.get("recentClosedTrades", []),
                    "credentialVersion": version,
                })
        for user_id in set(self.runtimes) - active_ids:
            runtime = self.runtimes[user_id]
            if runtime.strategy and runtime.strategy.process:
                # Subscription/connection loss must not abandon open-position exits.
                runtime.update({**runtime.account, "desired_state": "paused"})
                try:
                    runtime.snapshot()
                except Exception:
                    LOGGER.error("Removed customer exit monitoring needs operator review")
            else:
                self.runtimes.pop(user_id, None)
        self.control.heartbeat(self.settings.worker_id, snapshots)
        return len(snapshots)

    def run_forever(self) -> None:
        LOGGER.info("Worker %s started; execution_enabled=%s", self.settings.worker_id,
                    self.settings.execution_enabled)
        while True:
            started = time.monotonic()
            try:
                count = self.reconcile_once()
                LOGGER.info("Reconciled %d customer account(s)", count)
            except Exception:
                LOGGER.exception("Worker reconciliation failed")
            elapsed = time.monotonic() - started
            time.sleep(max(1, self.settings.poll_seconds - elapsed))

