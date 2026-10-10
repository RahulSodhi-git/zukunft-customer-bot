"""Append a full record of every completed trade to trade_history.csv (for later analysis)."""
import csv
import os

HIST_FILE = "trade_history.csv"

COLUMNS = ["open_time", "close_time", "coin", "side", "type", "strategy", "timeframe",
           "leverage", "confirmations", "expected_return_%", "entry", "exit",
           "margin_usdt", "price_move_%", "pnl_%", "pnl_usdt", "exit_reason"]


def _ensure_schema():
    """If an older CSV (e.g. missing pnl_usdt) exists, upgrade it in place so appended
    rows line up with the header. Old rows get blank values for any new column."""
    if not os.path.exists(HIST_FILE):
        return
    with open(HIST_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == COLUMNS:
            return                                    # already current — nothing to do
        rows = list(reader)                           # keyed by the OLD header
    with open(HIST_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in COLUMNS})


def record(row):
    _ensure_schema()
    exists = os.path.exists(HIST_FILE)
    with open(HIST_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in COLUMNS})
