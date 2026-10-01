"""Job queue.

Downloads run on a small thread pool so a single user cannot wedge the bot and
so the process can be shut down cleanly (each running job is cancelled first).
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from . import config
from .log import get_logger

log = get_logger("jobs")


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: str
    chat_id: str
    user_id: str
    url: str
    kind: str = "video"  # video | audio | image
    quality: str = "best"
    container: str | None = None
    status_message_id: str | None = None
    trim: tuple[float, float] | None = None
    subtitles: bool = False
    thumbnail: bool = False
    playlist: bool = False
    caption: str = ""
    state: JobState = JobState.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    engine: Any = None  # set by the worker so /cancel can reach it

    @property
    def position(self) -> int:
        return 0 if self.state is JobState.QUEUED else 1

    def describe(self) -> str:
        return f"#{self.id[:6]} {self.url}"


JobHandler = Callable[[Job], None]


class JobQueue:
    """A fixed-size worker pool over a FIFO queue of :class:`Job`."""

    def __init__(self, handler: JobHandler, workers: int | None = None) -> None:
        self.handler = handler
        self.workers_count = max(1, workers or config.MAX_CONCURRENT_DOWNLOADS)
        self._queue: "queue.Queue[Job | None]" = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._idle = threading.Event()
        self._idle.set()

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._threads:
            return
        for i in range(self.workers_count):
            thread = threading.Thread(target=self._worker, name=f"job-worker-{i}", daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("Job queue started with %d worker(s)", self.workers_count)

    def stop(self, timeout: float = 30.0) -> None:
        """Drain the queue and cancel running jobs, then join the workers."""
        self._stopping.set()
        with self._lock:
            for job in self._jobs.values():
                if job.state is JobState.RUNNING and job.engine is not None:
                    try:
                        job.engine.cancel()
                    except Exception:  # noqa: BLE001
                        pass
        for _ in self._threads:
            self._queue.put(None)
        deadline = time.time() + timeout
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.time()))
        self._threads.clear()
        log.info("Job queue stopped")

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until every queued and running job has finished."""
        return self._idle.wait(timeout)

    # -- submission ---------------------------------------------------------
    def submit(self, **kwargs: Any) -> Job:
        job = Job(id=uuid.uuid4().hex, **kwargs)
        with self._lock:
            self._jobs[job.id] = job
        self._idle.clear()
        self._queue.put(job)
        return job

    def cancel(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.state is JobState.RUNNING and job.engine is not None:
                try:
                    job.engine.cancel()
                except Exception:  # noqa: BLE001
                    pass
                return job
            if job.state is JobState.QUEUED:
                job.state = JobState.CANCELLED
                job.finished_at = time.time()
        return job

    def cancel_all(self, chat_id: str | None = None) -> list[Job]:
        cancelled: list[Job] = []
        with self._lock:
            targets = [
                j for j in self._jobs.values()
                if (chat_id is None or j.chat_id == chat_id)
                and j.state in (JobState.QUEUED, JobState.RUNNING)
            ]
        for job in targets:
            if self.cancel(job.id):
                cancelled.append(job)
        return cancelled

    # -- introspection ------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active_for_chat(self, chat_id: str) -> list[Job]:
        with self._lock:
            return [
                j for j in self._jobs.values()
                if j.chat_id == chat_id
                and j.state in (JobState.QUEUED, JobState.RUNNING)
            ]

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "id": j.id,
                    "url": j.url,
                    "state": j.state.value,
                    "created_at": j.created_at,
                }
                for j in self._jobs.values()
                if j.state in (JobState.QUEUED, JobState.RUNNING)
            ]

    # -- worker -------------------------------------------------------------
    def _worker(self) -> None:
        while not self._stopping.is_set():
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                self._check_idle()
                continue
            if job is None:
                break
            if job.state is JobState.CANCELLED:
                continue
            self._run(job)
            self._check_idle()

    def _check_idle(self) -> None:
        with self._lock:
            busy = any(
                j.state in (JobState.QUEUED, JobState.RUNNING)
                for j in self._jobs.values()
            )
        if not busy:
            self._idle.set()

    def _run(self, job: Job) -> None:
        job.state = JobState.RUNNING
        job.started_at = time.time()
        log.info("Job %s started: %s", job.id[:6], job.url)
        try:
            self.handler(job)
            if job.state is JobState.RUNNING:
                job.state = JobState.DONE
        except Exception as exc:  # noqa: BLE001
            from .engine import Cancelled, DownloadError

            if isinstance(exc, Cancelled):
                job.state = JobState.CANCELLED
            else:
                job.state = JobState.FAILED
                job.error = str(exc) if isinstance(exc, DownloadError) else f"Internal error: {exc}"
            log.exception("Job %s failed", job.id[:6])
        finally:
            job.finished_at = time.time()
            log.info("Job %s -> %s", job.id[:6], job.state.value)

    # -- housekeeping -------------------------------------------------------
    def prune(self, keep_last: int = 200) -> None:
        """Drop old terminal jobs so memory does not grow without bound."""
        with self._lock:
            finished = [
                j for j in self._jobs.values()
                if j.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)
            ]
            finished.sort(key=lambda j: j.finished_at or 0, reverse=True)
            for job in finished[keep_last:]:
                self._jobs.pop(job.id, None)

    def cleanup_workspaces(self, keep_last: int = 20) -> None:
        """Remove download directories that are no longer referenced."""
        root = Path(config.DOWNLOAD_DIR)
        if not root.exists():
            return
        with self._lock:
            active = {j.id for j in self._jobs.values() if j.state is JobState.RUNNING}
        dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
        import shutil

        for stale in dirs[keep_last:]:
            if stale.name in active:
                continue
            try:
                shutil.rmtree(stale, ignore_errors=True)
            except OSError as exc:
                log.debug("Could not remove %s: %s", stale, exc)
