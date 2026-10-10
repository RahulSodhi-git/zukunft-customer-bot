import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from zukunft_worker.strategy import CustomerStrategy


class StrategyTests(unittest.TestCase):
    user = "00000000-0000-0000-0000-000000000001"

    def test_invalid_customer_id_cannot_escape_state_directory(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(ValueError):
            CustomerStrategy("../../other", Path(folder), {}, {})

    @patch("zukunft_worker.strategy.subprocess.Popen")
    def test_pause_and_low_capital_never_start_orders(self, spawn):
        with tempfile.TemporaryDirectory() as folder:
            strategy = CustomerStrategy(self.user, Path(folder), {}, {"apiKey": "fake"})
            state, _ = strategy.reconcile({"desired_state": "paused", "capital_amount": 100}, [])
            self.assertEqual(state, "paused")
            state, error = strategy.reconcile({"desired_state": "running", "capital_amount": 50}, [])
            self.assertIn("100 USDT", error)
            control = json.loads((strategy.directory / "entry_control.json").read_text())
            self.assertTrue(control["entries_paused"])
        spawn.assert_not_called()

    @patch("zukunft_worker.strategy.subprocess.Popen")
    def test_unknown_exchange_positions_block_initial_start(self, spawn):
        with tempfile.TemporaryDirectory() as folder:
            strategy = CustomerStrategy(self.user, Path(folder), {}, {"apiKey": "fake"})
            state, error = strategy.reconcile({"desired_state": "running", "capital_amount": 100},
                                              [{"coin": "BTCUSDT"}])
            self.assertEqual(state, "error")
            self.assertIn("untracked", error)
        spawn.assert_not_called()

    def test_stop_does_not_terminate_open_position_monitor(self):
        with tempfile.TemporaryDirectory() as folder:
            strategy = CustomerStrategy(self.user, Path(folder), {}, {})
            with patch.object(strategy, "shutdown") as stop:
                state, error = strategy.reconcile({"desired_state": "stopped"}, [{"coin": "BTCUSDT"}])
                self.assertEqual(state, "paused")
                stop.assert_not_called()

    def test_control_permission_expires_without_disabling_exits(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/zukunft_worker/engine"))
        import runtime_control
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "entry_control.json"
            path.write_text(json.dumps({"entries_paused": False, "expires_at": time.time() - 1}))
            self.assertTrue(runtime_control.entries_paused(path))
            with patch.dict(os.environ, {"EXECUTION_ENABLED": "true"}):
                self.assertTrue(runtime_control.entries_paused(Path(folder) / "missing.json"))

    def test_engine_runner_is_locked_before_loading_keys(self):
        from zukunft_worker.engine_runner import main
        with patch.dict(os.environ, {"EXECUTION_ENABLED": "false"}), self.assertRaises(RuntimeError):
            main()

    @patch("zukunft_worker.strategy.subprocess.Popen")
    def test_old_monitor_cannot_enable_entries_and_keys_are_not_command_arguments(self, spawn):
        process = Mock(pid=42)
        process.poll.return_value = None
        spawn.return_value = process
        with tempfile.TemporaryDirectory() as folder:
            strategy = CustomerStrategy(self.user, Path(folder),
                                        {"apiKey": "SECRET_KEY", "apiSecret": "SECRET_VALUE"},
                                        {"apiKey": "OI_SECRET"})
            (strategy.directory / "exit_monitor_status.json").write_text(json.dumps({
                "completed_at": time.time(), "pid": 41,
            }))
            state, _ = strategy.reconcile({"desired_state": "running", "capital_amount": 100}, [])
            self.assertEqual(state, "paused")
            self.assertTrue(json.loads((strategy.directory / "entry_control.json").read_text())["entries_paused"])
            self.assertNotIn("SECRET_KEY", str(spawn.call_args))
            self.assertNotIn("SECRET_VALUE", str(spawn.call_args))
            self.assertNotIn("OI_SECRET", str(spawn.call_args))
            self.assertIn(b"SECRET_KEY", process.stdin.write.call_args.args[0])
            strategy.shutdown()
