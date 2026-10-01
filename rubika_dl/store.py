"""Persistent JSON store for offset, user settings and stats.

All writes are atomic (write to a temp file then replace) so a killed runner
never leaves a half-written state file behind.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from . import config
from .log import get_logger

log = get_logger("store")


class JsonStore:
    """A tiny thread-safe, atomic JSON document store."""

    def __init__(self, path: Path, default: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._default = dict(default or {})
        self._data: dict[str, Any] = {}
        self._load()

    # -- persistence --------------------------------------------------------
    def _load(self) -> None:
        with self._lock:
            if self.path.exists():
                try:
                    self._data = json.loads(self.path.read_text(encoding="utf-8"))
                    if not isinstance(self._data, dict):
                        self._data = dict(self._default)
                except (json.JSONDecodeError, OSError) as exc:
                    log.warning("Could not read %s (%s); starting fresh", self.path, exc)
                    self._data = dict(self._default)
            else:
                self._data = dict(self._default)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            payload = json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)

    # -- dict-ish API -------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any, *, save: bool = True) -> None:
        with self._lock:
            self._data[key] = value
        if save:
            self.save()

    def update(self, values: dict[str, Any], *, save: bool = True) -> None:
        with self._lock:
            self._data.update(values)
        if save:
            self.save()

    def all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def as_dict(self) -> dict[str, Any]:
        return self.all()


# --------------------------------------------------------------------------
# Concrete stores
# --------------------------------------------------------------------------
class State:
    """Runtime state that must survive runner restarts."""

    def __init__(self) -> None:
        self._store = JsonStore(config.STATE_FILE, {"offset": 0, "runs": 0})

    @property
    def offset(self) -> int:
        try:
            return int(self._store.get("offset", 0))
        except (TypeError, ValueError):
            return 0

    @offset.setter
    def offset(self, value: int) -> None:
        self._store.set("offset", int(value))

    def bump_runs(self) -> int:
        runs = int(self._store.get("runs", 0)) + 1
        self._store.set("runs", runs)
        return runs

    def flush(self) -> None:
        self._store.save()


class Users:
    """Per-user preferences."""

    def __init__(self) -> None:
        self._store = JsonStore(config.USERS_FILE, {})

    def get(self, user_id: str) -> dict[str, Any]:
        users = self._store.get("users", {})
        if not isinstance(users, dict):
            return {}
        value = users.get(str(user_id), {})
        return dict(value) if isinstance(value, dict) else {}

    def set(self, user_id: str, values: dict[str, Any]) -> None:
        users = self._store.get("users", {})
        if not isinstance(users, dict):
            users = {}
        current = users.get(str(user_id), {})
        if not isinstance(current, dict):
            current = {}
        current.update(values)
        users[str(user_id)] = current
        self._store.set("users", users)

    def preference(self, user_id: str, key: str, default: Any = None) -> Any:
        return self.get(user_id).get(key, default)


class Stats:
    """Simple counters for the /stats command."""

    def __init__(self) -> None:
        self._store = JsonStore(
            config.STATS_FILE,
            {"downloads": 0, "failures": 0, "bytes": 0, "users": {}},
        )

    def record(self, user_id: str, *, size: int = 0, ok: bool = True) -> None:
        data = self._store.all()
        data["downloads"] = int(data.get("downloads", 0)) + (1 if ok else 0)
        if not ok:
            data["failures"] = int(data.get("failures", 0)) + 1
        data["bytes"] = int(data.get("bytes", 0)) + max(0, int(size))
        users = data.get("users", {})
        if not isinstance(users, dict):
            users = {}
        entry = users.get(str(user_id), {})
        if not isinstance(entry, dict):
            entry = {}
        entry["downloads"] = int(entry.get("downloads", 0)) + (1 if ok else 0)
        entry["bytes"] = int(entry.get("bytes", 0)) + max(0, int(size))
        users[str(user_id)] = entry
        data["users"] = users
        self._store.update(data)

    def snapshot(self) -> dict[str, Any]:
        return self._store.all()


# Lazily-created singletons (avoid touching disk at import time in tests).
_state: State | None = None
_users: Users | None = None
_stats: Stats | None = None


def state() -> State:
    global _state
    if _state is None:
        _state = State()
    return _state


def users() -> Users:
    global _users
    if _users is None:
        _users = Users()
    return _users


def stats() -> Stats:
    global _stats
    if _stats is None:
        _stats = Stats()
    return _stats
