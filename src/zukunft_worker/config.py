from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


@dataclass(frozen=True)
class Settings:
    api_base_url: str
    worker_token: str
    worker_id: str
    private_key_path: Path
    state_dir: Path
    poll_seconds: int
    execution_enabled: bool

    @classmethod
    def from_env(cls) -> "Settings":
        poll_seconds = max(15, int(os.getenv("POLL_SECONDS", "30")))
        return cls(
            api_base_url=_required("API_BASE_URL").rstrip("/"),
            worker_token=_required("WORKER_TOKEN"),
            worker_id=os.getenv("WORKER_ID", "zukunft-customers-01").strip(),
            private_key_path=Path(os.getenv("PRIVATE_KEY_PATH", "/etc/zukunft-worker/private.pem")),
            state_dir=Path(os.getenv("STATE_DIR", "/var/lib/zukunft-worker/state")),
            poll_seconds=poll_seconds,
            execution_enabled=os.getenv("EXECUTION_ENABLED", "false").lower() == "true",
        )

