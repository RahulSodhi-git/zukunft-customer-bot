from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf8")
    temporary.replace(path)


class CustomerStrategy:
    """Separate process, private working directory, expiring entry permission.

    Pause blocks NEW orders only. Existing positions keep their exit watcher.
    Stop also pauses entries and only terminates after the exchange reports flat.
    """

    def __init__(self, user_id, state_dir, binance, coinalyze):
        self.directory = state_dir / "customers" / str(UUID(user_id))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.binance = binance
        self.coinalyze = coinalyze
        self.process = None
        self.log_handle = None

    def reconcile(self, account, positions):
        desired = account.get("desired_state", "paused")
        capital = float(account.get("capital_amount") or 0)
        running = self.process is not None and self.process.poll() is None
        atomic_json(self.directory / "entry_control.json", {
            "entries_paused": True, "expires_at": time.time() + 90,
        })
        if desired == "stopped":
            if positions:
                return "paused", "Stop deferred: exits remain active while positions are open."
            self.shutdown()
            return "stopped", None
        if capital < 100:
            atomic_json(self.directory / "entry_control.json", {"entries_paused": True})
            return "paused", "Minimum trading capital is 100 USDT."
        if not self.coinalyze or not self.coinalyze.get("apiKey"):
            return "paused", "Verified Coinalyze credentials are required."
        atomic_json(self.directory / "ui_settings.json", {
            "live_mode": True, "trading_capital": capital,
        })
        if not running:
            # Never silently restart a crashed trading engine. Operator review is required.
            if self.process is not None:
                return "error", "Strategy process exited; review its logs before restarting."
            if desired != "running":
                return "paused", None
            # No adoption of pre-existing manual trades: the legacy orphan sweep closes them.
            try:
                tracked = json.loads((self.directory / "bot_state.json").read_text())
            except (OSError, ValueError):
                tracked = {}
            if any(p["coin"] not in tracked for p in positions):
                return "error", "Account has untracked positions; automatic start is blocked."
            source = Path(__file__).parent / "engine/risk_rules.json"
            (self.directory / "risk_rules.json").write_bytes(source.read_bytes())
            env = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "TMP", "TEMP"}}
            env["EXECUTION_ENABLED"] = "true"
            env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
            self.log_handle = (self.directory / "engine.log").open("ab")
            self.process = subprocess.Popen(
                [sys.executable, "-u", "-m", "zukunft_worker.engine_runner"],
                cwd=self.directory, env=env, stdin=subprocess.PIPE,
                stdout=self.log_handle, stderr=subprocess.STDOUT,
            )
            # Secret transport is a pipe, not command-line arguments or disk files.
            payload = {"binance": self.binance, "coinalyze": self.coinalyze, "capital": capital}
            self.process.stdin.write((json.dumps(payload) + "\n").encode())
            self.process.stdin.close()
        monitor = self.directory / "exit_monitor_status.json"
        try:
            status = json.loads(monitor.read_text())
            completed = float(status["completed_at"])
            fresh = status.get("pid") == self.process.pid and 0 <= time.time() - completed <= 45
        except (OSError, ValueError, KeyError, TypeError):
            fresh = False
        if not fresh:
            return "paused", "Strategy starting or exit monitor stale; not confirmed healthy."
        atomic_json(self.directory / "entry_control.json", {
            "entries_paused": desired != "running", "expires_at": time.time() + 90,
        })
        return desired if desired in {"running", "paused"} else "paused", None

    def shutdown(self):
        atomic_json(self.directory / "entry_control.json", {"entries_paused": True})
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self.log_handle:
            self.log_handle.close()
