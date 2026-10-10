import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from zukunft_worker.agent import WorkerAgent


def account(n, version=1):
    user = f"00000000-0000-4000-8000-{n:012d}"
    return {"user_id": user, "customer_number": f"ZT-{n}", "credential_version": version,
            "desired_state": "running", "capital_amount": 100,
            "binance_ciphertext": f"{user}:binance:{version}",
            "coinalyze_ciphertext": f"{user}:coinalyze:{version}"}


class FakeExchange:
    fail_user = None

    def __init__(self, key, secret):
        self.user = key.split(":")[0]

    def open_positions(self):
        if self.user == self.fail_user:
            raise ValueError("SECRET_MUST_NOT_BE_LOGGED")
        return [{"coin": self.user, "marginPct": 2.3, "pnlUsdt": 9.83}]

    def recent_realized_pnl(self):
        return [{"coin": self.user, "pnlUsdt": 5}]


class AgentStressTests(unittest.TestCase):
    @patch("zukunft_worker.runtime.BinanceFutures", FakeExchange)
    @patch("zukunft_worker.agent.CredentialDecryptor")
    @patch("zukunft_worker.agent.ControlPlane")
    def test_100_accounts_1000_snapshots_isolation_failures_and_rotation(self, plane, decryptor):
        decryptor.return_value.decrypt.side_effect = lambda value: {"apiKey": value, "apiSecret": "fake"}
        with tempfile.TemporaryDirectory() as folder:
            settings = SimpleNamespace(api_base_url="https://test.invalid", worker_token="fake",
                                       private_key_path=Path(folder)/"fake.pem", state_dir=Path(folder),
                                       execution_enabled=False, worker_id="pilot")
            agent = WorkerAgent(settings)
            accounts = [account(n) for n in range(100)]
            agent.control.accounts.return_value = accounts
            for _ in range(10):
                self.assertEqual(agent.reconcile_once(), 100)
                snapshots = agent.control.heartbeat.call_args.args[1]
                for row in snapshots:
                    self.assertEqual(row["openPositions"][0]["coin"], row["userId"])
                    self.assertEqual(row["recentClosedTrades"][0]["coin"], row["userId"])
                    self.assertEqual(row["actualState"], "paused")
                    self.assertEqual(row["credentialVersion"], 1)
            self.assertEqual(len(list(Path(folder).glob("*.json"))), 100)
            FakeExchange.fail_user = accounts[0]["user_id"]
            try:
                with patch("zukunft_worker.agent.LOGGER") as logger:
                    agent.reconcile_once()
                    snapshots = agent.control.heartbeat.call_args.args[1]
                    self.assertEqual(sum(row["actualState"]=="error" for row in snapshots), 1)
                    self.assertNotIn("SECRET_MUST_NOT_BE_LOGGED", str(snapshots))
                    self.assertNotIn("SECRET_MUST_NOT_BE_LOGGED", str(logger.mock_calls))
                    self.assertEqual(snapshots[0]["openPositions"][0]["coin"], accounts[0]["user_id"])
            finally:
                FakeExchange.fail_user = None
            old_runtime = agent.runtimes[accounts[0]["user_id"]]
            accounts[0] = account(0, 2)
            agent.reconcile_once()
            self.assertIsNot(old_runtime, agent.runtimes[accounts[0]["user_id"]])
            self.assertEqual(agent.runtimes[accounts[0]["user_id"]].credential_version, 2)
            agent.control.accounts.return_value = accounts[1:]
            agent.reconcile_once()
            self.assertNotIn(accounts[0]["user_id"], agent.runtimes)

    @patch("zukunft_worker.agent.CredentialDecryptor")
    @patch("zukunft_worker.agent.ControlPlane")
    def test_flat_live_runtime_rotates_without_manual_review(self, plane, decryptor):
        decryptor.return_value.decrypt.return_value = {"apiKey":"fake", "apiSecret":"fake"}
        with tempfile.TemporaryDirectory() as folder:
            settings = SimpleNamespace(api_base_url="https://test.invalid", worker_token="fake",
                                       private_key_path=Path(folder)/"fake.pem", state_dir=Path(folder),
                                       execution_enabled=True, worker_id="pilot")
            agent = WorkerAgent(settings)
            item = account(1, 2)
            old = Mock(credential_version=1)
            old.strategy.process = Mock()
            old.snapshot.return_value = {"userId":item["user_id"], "openPositions":[]}
            agent.runtimes[item["user_id"]] = old
            agent.control.accounts.return_value = [item]
            with patch("zukunft_worker.agent.TenantRuntime") as runtime:
                runtime.return_value.snapshot.return_value = {"userId":item["user_id"],"openPositions":[]}
                agent.reconcile_once()
                old.strategy.shutdown.assert_called_once()
                runtime.assert_called_once()

    @patch("zukunft_worker.agent.CredentialDecryptor")
    @patch("zukunft_worker.agent.ControlPlane")
    def test_rotation_keeps_old_exit_monitor_when_positions_open(self, plane, decryptor):
        settings = SimpleNamespace(api_base_url="https://test.invalid", worker_token="fake",
                                   private_key_path=Path("fake.pem"), state_dir=Path("unused"),
                                   execution_enabled=True, worker_id="pilot")
        agent = WorkerAgent(settings)
        item = account(1, 2)
        old = Mock(credential_version=1)
        old.strategy.process = Mock()
        old.snapshot.return_value = {"userId":item["user_id"], "openPositions":[{"coin":"ETHUSDT"}]}
        agent.runtimes[item["user_id"]] = old
        agent.control.accounts.return_value = [item]
        agent.reconcile_once()
        old.strategy.shutdown.assert_not_called()
        decryptor.return_value.decrypt.assert_not_called()
        self.assertIn("Waiting for positions", agent.control.heartbeat.call_args.args[1][0]["error"])
