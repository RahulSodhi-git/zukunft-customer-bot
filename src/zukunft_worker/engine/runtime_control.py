"""Entry controls are separate from exit monitoring and strategy settings."""
import json
import os
import time
from pathlib import Path

CONTROL_FILE = "entry_control.json"
MONITOR_FILE = "exit_monitor_status.json"


def _write(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def entries_paused(path=CONTROL_FILE):
    try:
        control = json.loads(Path(path).read_text(encoding="utf-8"))
        value = control["entries_paused"]
        if "expires_at" in control and time.time() >= float(control["expires_at"]):
            return True  # Lost control-plane heartbeat pauses entries, never exits.
        return value is not False
    except FileNotFoundError:
        return os.environ.get("EXECUTION_ENABLED") == "true"
    except (OSError, ValueError, KeyError, TypeError):
        return True  # An unreadable control must not enable new exposure.


def set_entries_paused(paused, *, authorized, path=CONTROL_FILE):
    if not authorized:
        raise PermissionError("Trading PIN required")
    if not isinstance(paused, bool):
        raise ValueError("entries_paused must be boolean")
    _write(path, {"entries_paused": paused, "updated_at": time.time()})


def record_monitor_pass(started_at, positions, path=MONITOR_FILE):
    _write(path, {"pid": os.getpid(), "started_at": started_at,
                  "completed_at": time.time(), "positions": positions})


def monitor_is_fresh(path=MONITOR_FILE, max_age=30):
    try:
        completed = float(json.loads(Path(path).read_text(encoding="utf-8"))["completed_at"])
        return 0 <= time.time() - completed <= max_age
    except (OSError, ValueError, KeyError, TypeError):
        return False
