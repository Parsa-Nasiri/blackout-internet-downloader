"""The liveness heartbeat the Actions watchdog depends on.

If the bot stops refreshing it mid-run the watchdog decides the run is dead
and queues a duplicate, so the read/write shape matters as much as the values.
"""

import json
import time

from rubika_dl import config, heartbeat


def test_write_then_read_roundtrips():
    heartbeat.write("running")
    data = heartbeat.read()
    assert data["status"] == "running"
    assert data["ts"] <= time.time()
    assert data["iso"].endswith("Z")


def test_age_is_small_right_after_a_write():
    heartbeat.write("running")
    age = heartbeat.age()
    assert age is not None and age < 5


def test_age_is_none_without_a_heartbeat():
    heartbeat.heartbeat_path().unlink(missing_ok=True)
    assert heartbeat.age() is None
    assert heartbeat.read() == {}


def test_read_tolerates_a_corrupt_file():
    heartbeat.heartbeat_path().write_text("{not json", encoding="utf-8")
    assert heartbeat.read() == {}
    assert heartbeat.age() is None


def test_age_ignores_a_nonsense_timestamp():
    heartbeat.heartbeat_path().write_text(json.dumps({"ts": "soon"}), encoding="utf-8")
    assert heartbeat.age() is None


def test_write_is_best_effort_when_the_directory_is_missing():
    # A read-only or missing state dir must not take the bot down.
    original = config.STATE_DIR
    try:
        config.STATE_DIR = original / "does" / "not" / "exist" / "x" / "y" / "z"
        heartbeat.write("running")  # must not raise
    finally:
        config.STATE_DIR = original


def test_script_and_bot_agree_on_the_path():
    import scripts.state_sync as state_sync

    assert state_sync.heartbeat_path() == heartbeat.heartbeat_path()