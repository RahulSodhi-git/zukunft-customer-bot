"""Load config from the .env file. Keys never live in code."""
import json
import os
from pathlib import Path

_ROOT = Path.cwd()
# No shared .env: keys supplied only to the isolated customer process.

USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"   # DEFAULT account (before any toggle)
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Live keys (one key = both spot + futures on real Binance)
_LIVE_KEY = os.getenv("BINANCE_API_KEY", "")
_LIVE_SECRET = os.getenv("BINANCE_API_SECRET", "")

# Demo / testnet keys. On the unified demo (demo.binance.com) ONE key covers BOTH markets
# (BINANCE_API_KEY_TEST / _SECRET_TEST). The classic split testnets have SEPARATE keys per market;
# the *_SPOT_TEST / *_FUT_TEST vars override the unified key for that market when present.
_TEST_KEY = os.getenv("BINANCE_API_KEY_TEST", "")
_TEST_SECRET = os.getenv("BINANCE_API_SECRET_TEST", "")
_DEMO_SPOT_KEY = os.getenv("BINANCE_API_KEY_SPOT_TEST", "") or _TEST_KEY
_DEMO_SPOT_SECRET = os.getenv("BINANCE_API_SECRET_SPOT_TEST", "") or _TEST_SECRET
_DEMO_FUT_KEY = os.getenv("BINANCE_API_KEY_FUT_TEST", "") or _TEST_KEY
_DEMO_FUT_SECRET = os.getenv("BINANCE_API_SECRET_FUT_TEST", "") or _TEST_SECRET

# Runtime account switch (dashboard Demo/Live toggle). The BOT writes the account it is ACTUALLY on
# to account_status.json after it switches; the default trader + the dashboard read it, so they always
# reflect the real account. Absent -> the .env USE_TESTNET default (so nothing changes until a switch).
ACCOUNT_STATUS_FILE = _ROOT / "account_status.json"


def active_testnet():
    """True = demo/testnet, False = live. Reads the bot's ACTIVE account (account_status.json),
    falling back to the .env USE_TESTNET default."""
    try:
        v = json.load(open(ACCOUNT_STATUS_FILE)).get("testnet")
        if v is not None:
            return bool(v)
    except Exception:
        pass
    return USE_TESTNET


def keys_for(testnet):
    """(spot_key, spot_secret, fut_key, fut_secret) for the requested account."""
    if testnet:
        return _DEMO_SPOT_KEY, _DEMO_SPOT_SECRET, _DEMO_FUT_KEY, _DEMO_FUT_SECRET
    return _LIVE_KEY, _LIVE_SECRET, _LIVE_KEY, _LIVE_SECRET


# Back-compat module-level keys = the DEFAULT account (unchanged). Anything importing SPOT_KEY/FUT_KEY
# still works; the runtime switch goes through keys_for()/active_testnet() in the client.
SPOT_KEY, SPOT_SECRET, FUT_KEY, FUT_SECRET = keys_for(USE_TESTNET)
