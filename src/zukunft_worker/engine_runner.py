"""Dedicated process: legacy globals and state cannot leak between customers."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
    if os.environ.get("EXECUTION_ENABLED") != "true":
        raise RuntimeError("Strategy execution is safety-locked")
    payload = json.loads(sys.stdin.readline())
    credentials = payload["binance"]
    capital = float(payload["capital"])
    if capital < 100:
        raise ValueError("Original engine requires at least 100 USDT trading capital")
    os.environ["BINANCE_API_KEY"] = credentials["apiKey"]
    os.environ["BINANCE_API_SECRET"] = credentials["apiSecret"]
    os.environ["COINALYZE_KEY"] = payload["coinalyze"]["apiKey"]
    os.environ["USE_TESTNET"] = "false"
    os.environ["DRY_RUN"] = "false"
    del credentials, payload
    sys.path.insert(0, str(Path(__file__).parent / "engine"))
    from bot import run_auto
    run_auto(capital)


if __name__ == "__main__":
    main()
