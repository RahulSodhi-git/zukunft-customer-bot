from __future__ import annotations

import logging
import time

from .config import Settings
from .control_plane import ControlPlane
from .crypto import CredentialDecryptor
from .runtime import TenantRuntime

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
                if runtime is None or runtime.credential_version != version:
                    credentials = self.decryptor.decrypt(str(account["binance_ciphertext"]))
                    runtime = TenantRuntime(account, credentials, self.settings.state_dir,
                                            self.settings.execution_enabled)
                    self.runtimes[user_id] = runtime
                    LOGGER.info("Provisioned customer %s", account.get("customer_number"))
                else:
                    runtime.update(account)
                snapshots.append(runtime.snapshot())
            except Exception as exc:  # one customer must never stop the others
                LOGGER.exception("Customer %s sync failed", account.get("customer_number", user_id))
                snapshots.append({
                    "userId": user_id,
                    "actualState": "error",
                    "error": str(exc)[:500],
                    "openPositions": [],
                    "recentClosedTrades": [],
                })
        for user_id in set(self.runtimes) - active_ids:
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

