"""Liveness heartbeat shared by the bot loop and the Actions watchdog.

The runner writes one at start-up; the bot then refreshes it from its
housekeeper thread while it runs. Without that refresh the watchdog
(``state_sync alive``) would see a heartbeat older than ``STALE_AFTER_SECONDS``
during a perfectly healthy multi-hour run and queue a duplicate bot.

The on-disk shape matches ``scripts/state_sync.py`` — both write
``.state/heartbeat.json`` with the same keys.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import config


def heartbeat_path() -> Path:
    return config.STATE_DIR / "heartbeat.json"


def write(status: str = "running", *, run_id: str | None = None) -> None:
    """Refresh the heartbeat. Best-effort: never raise into the caller."""
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_id": run_id if run_id is not None else os.getenv("GITHUB_RUN_ID", ""),
            "status": status,
        }
        heartbeat_path().write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def read() -> dict:
    path = heartbeat_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def age() -> float | None:
    """Seconds since the last heartbeat, or None if there is not one yet."""
    ts = read().get("ts")
    if not ts:
        return None
    try:
        return time.time() - float(ts)
    except (TypeError, ValueError):
        return None