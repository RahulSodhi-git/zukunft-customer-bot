import io
import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock

from zukunft_worker.verifier import Verifier


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "cooldown.json"
        self.verifier = Verifier(self.path)
        self.credentials = {"apiKey": "fake-key", "apiSecret": "fake-secret"}

    @patch("zukunft_worker.verifier.BinanceFutures._signed_get")
    @patch("zukunft_worker.verifier.urllib.request.urlopen")
    def test_success_and_permissions(self, urlopen, get):
        urlopen.return_value.__enter__.return_value.read.return_value = json.dumps({"serverTime": int(time.time()*1000)}).encode()
        get.return_value = {"canTrade": True}
        self.assertEqual(self.verifier.verify(self.credentials)["status"], "verified")
        self.assertEqual(get.call_args.args, ("/fapi/v2/account",))
        get.return_value = {"canTrade": False}
        self.assertEqual(self.verifier.verify(self.credentials)["status"], "failed")

    @patch("zukunft_worker.verifier.urllib.request.urlopen")
    def test_ban_persists_and_suppresses_requests(self, urlopen):
        until = int((time.time()+3600)*1000)
        urlopen.side_effect = urllib.error.HTTPError("safe", 418, "ban", {}, io.BytesIO(json.dumps({"msg": f"IP banned until {until}"}).encode()))
        self.assertEqual(self.verifier.verify(self.credentials)["status"], "failed")
        other = Verifier(self.path)
        self.assertGreater(other.retry_at, time.time())
        other.verify(self.credentials)
        self.assertEqual(urlopen.call_count, 1)

    @patch("zukunft_worker.verifier.urllib.request.urlopen")
    def test_errors_never_expose_secrets(self, urlopen):
        urlopen.side_effect = RuntimeError("fake-secret")
        result = self.verifier.verify(self.credentials)
        self.assertNotIn("fake-secret", json.dumps(result))
        self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
