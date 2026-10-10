"""Hard safety limits enforced in code — the AI cannot bypass these."""
import json
import os
from runtime_control import entries_paused

_RULES_PATH = "risk_rules.json"


class RiskError(Exception):
    """Raised when an order breaks a safety rule."""


def _rules():
    with open(_RULES_PATH) as f:
        return json.load(f)


def check_order(symbol, usdt_amount, leverage=1):
    """Reject unsafe orders BEFORE they reach Binance. Returns True if allowed."""
    if entries_paused():
        raise RiskError("New entries are paused; position exits remain active.")
    r = _rules()
    if not r.get("trading_enabled", True):
        raise RiskError("Kill switch is ON (trading_enabled=false in risk_rules.json).")
    allowed = r.get("allowed_symbols") or []
    if allowed and symbol.upper() not in [s.upper() for s in allowed]:
        raise RiskError(f"{symbol} is not in allowed_symbols {allowed}.")
    cap = r.get("max_usdt_per_trade", 0)           # optional per-trade $ ceiling; 0/absent = NO cap
    if cap and usdt_amount > cap:                  # sizing is % of the allocated capital -> no fixed $ cap
        raise RiskError(f"{usdt_amount} USDT exceeds max_usdt_per_trade ({cap}).")
    if leverage > r.get("max_leverage", 1):
        raise RiskError(f"{leverage}x exceeds max_leverage ({r.get('max_leverage')}x).")
    return True
