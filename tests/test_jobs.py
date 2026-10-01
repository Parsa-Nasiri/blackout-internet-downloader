"""Job queue tests."""

from __future__ import annotations

import threading
import time

import pytest

from rubika_dl.jobs import JobQueue, JobState


def test_submit_runs_handler():
    done = threading.Event()
    seen = []

    def handler(job):
        seen.append(job.url)
        done.set()

    q = JobQueue(handler, workers=1)
    q.start()
    job = q.submit(chat_id="1", user_id="2", url="https://example.com/v")
    assert done.wait(timeout=5)
    assert seen == ["https://example.com/v"]
    assert q.get(job.id).state is JobState.DONE
    q.stop()


def test_failure_marks_job_failed():
    def handler(job):
        raise ValueError("nope")

    q = JobQueue(handler, workers=1)
    q.start()
    job = q.submit(chat_id="1", user_id="2", url="https://x")
    deadline = time.time() + 5
    while time.time() < deadline and q.get(job.id).state in (JobState.QUEUED, JobState.RUNNING):
        time.sleep(0.05)
    assert q.get(job.id).state is JobState.FAILED
    assert "nope" in (q.get(job.id).error or "")
    q.stop()


def test_cancel_queued_job():
    gate = threading.Event()

    def handler(job):
        gate.wait(timeout=5)

    q = JobQueue(handler, workers=1)
    q.start()
    first = q.submit(chat_id="1", user_id="1", url="https://a")
    second = q.submit(chat_id="1", user_id="1", url="https://b")
    time.sleep(0.2)
    q.cancel(second.id)
    gate.set()
    time.sleep(0.4)
    assert q.get(second.id).state is JobState.CANCELLED
    q.stop()


def test_cancel_running_job_calls_engine():
    started = threading.Event()
    release = threading.Event()

    class FakeEngine:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    def handler(job):
        job.engine = engine
        started.set()
        release.wait(timeout=5)
        if engine.cancelled:
            from rubika_dl.engine import Cancelled
            raise Cancelled("stopped")

    engine = FakeEngine()
    q = JobQueue(handler, workers=1)
    q.start()
    job = q.submit(chat_id="1", user_id="1", url="https://a")
    assert started.wait(timeout=5)
    q.cancel(job.id)
    assert engine.cancelled
    release.set()
    time.sleep(0.3)
    assert q.get(job.id).state is JobState.CANCELLED
    q.stop()


def test_cancel_all_for_chat():
    def handler(job):
        time.sleep(0.4)

    q = JobQueue(handler, workers=1)
    q.start()
    for _ in range(3):
        q.submit(chat_id="mine", user_id="1", url="https://a")
    q.submit(chat_id="other", user_id="1", url="https://b")
    cancelled = q.cancel_all(chat_id="mine")
    assert len(cancelled) == 3
    q.stop()


def test_wait_idle():
    counter = []

    def handler(job):
        counter.append(1)

    q = JobQueue(handler, workers=2)
    q.start()
    for _ in range(5):
        q.submit(chat_id="1", user_id="1", url="https://a")
    assert q.wait_idle(timeout=10)
    assert len(counter) == 5
    q.stop()


def test_prune_drops_old_terminal_jobs():
    def handler(job):
        pass

    q = JobQueue(handler, workers=1)
    q.start()
    for _ in range(5):
        q.submit(chat_id="1", user_id="1", url="https://a")
    q.wait_idle(timeout=10)
    q.prune(keep_last=2)
    assert len(q.snapshot()) == 0
    q.stop()
