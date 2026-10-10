import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zukunft_worker.runtime import TenantRuntime


class FakeBinance:
    def __init__(self, *_args, **_kwargs):
        pass

    def open_positions(self):
        return [{"coin": "BTCUSDT", "marginPct": 2.3, "pnlUsdt": 9.83}]

    def recent_realized_pnl(self):
        return [{"coin": "ETHUSDT", "pnlUsdt": 3.5}]


class TenantRuntimeTests(unittest.TestCase):
    @patch("zukunft_worker.runtime.BinanceFutures", FakeBinance)
    def test_execution_is_locked_by_default(self):
        account = {
            "user_id": "00000000-0000-0000-0000-000000000001",
            "customer_number": "ZT-TEST-001",
            "credential_version": 1,
            "desired_state": "running",
        }
        with tempfile.TemporaryDirectory() as folder:
            runtime = TenantRuntime(account, {"apiKey": "key", "apiSecret": "secret"}, Path(folder), False)
            snapshot = runtime.snapshot()
        self.assertEqual(snapshot["actualState"], "paused")
        self.assertIn("safety-locked", snapshot["error"])
        self.assertEqual(snapshot["openPositions"][0]["coin"], "BTCUSDT")
        self.assertIn("openedAt", snapshot["openPositions"][0])


if __name__ == "__main__":
    unittest.main()

