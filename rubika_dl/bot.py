"""Long-polling bot loop with graceful shutdown and hand-off support."""

from __future__ import annotations

import signal
import threading
import time
from typing import Any

from . import config, store
from .commands import COMMANDS, Handlers
from .jobs import JobQueue
from .log import get_logger
from .rubika_api import RubikaClient, RubikaError
from .updates import parse_update

log = get_logger("bot")


class Bot:
    def __init__(self, client: RubikaClient | None = None) -> None:
        self.client = client or RubikaClient()
        self.queue = JobQueue(self._run_job)
        self.handlers = Handlers(self.client, self.queue)
        self._stop = threading.Event()
        self._housekeeper: threading.Thread | None = None
        self._stats: dict[str, Any] = {"updates": 0, "handled": 0, "errors": 0}

    # -- lifecycle ----------------------------------------------------------
    def _run_job(self, job) -> None:  # noqa: ANN001
        self.handlers.downloader.handle(job)

    def install_signal_handlers(self) -> None:
        def _handler(signum, _frame):  # noqa: ANN001
            log.info("Signal %s received — shutting down gracefully", signum)
            self.stop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass  # not on the main thread / unsupported platform

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def setup(self) -> dict[str, Any]:
        """Register commands and log the bot identity."""
        try:
            me = self.client.get_me()
            log.info("Connected as @%s (%s)", me.get("username"), me.get("bot_id") or me.get("user_id"))
        except RubikaError as exc:
            log.error("getMe failed: %s", exc)
            raise
        try:
            self.client.set_commands(COMMANDS)
            log.info("Registered %d commands", len(COMMANDS))
        except RubikaError as exc:
            log.warning("setCommands failed (non-fatal): %s", exc)
        return me

    def run(self, *, deadline: float | None = None) -> None:
        """Poll until stopped or until *deadline* (a monotonic timestamp)."""
        self.setup()
        self.queue.start()
        self._start_housekeeper()

        log.info("Polling for updates (offset=%s)", store.state().offset)
        if deadline:
            log.info("Runner deadline in %.0f min", (deadline - time.monotonic()) / 60)

        while not self._stop.is_set():
            if deadline and time.monotonic() >= deadline:
                log.info("Deadline reached — handing off to the next run")
                break
            try:
                self._poll_once()
            except RubikaError as exc:
                self._stats["errors"] += 1
                log.warning("getUpdates error: %s", exc)
                self._sleep(5)
            except Exception:  # noqa: BLE001
                self._stats["errors"] += 1
                log.exception("Unexpected poll error")
                self._sleep(5)

        self.shutdown()

    def _poll_once(self) -> None:
        body = self.client.get_updates(offset_id=store.state().offset, limit=50)
        updates = body.get("updates") or []
        next_offset = body.get("next_offset_id")

        for raw in updates:
            if self._stop.is_set():
                break
            self._stats["updates"] += 1
            incoming = parse_update(raw)
            if incoming is None:
                continue
            self._stats["handled"] += 1
            self.handlers.handle(incoming)

        if next_offset:
            store.state().offset = str(next_offset)
        elif updates:
            # Fall back to the newest message id if the API omitted next_offset_id.
            last = updates[-1]
            msg = last.get("new_message") or {}
            if msg.get("message_id"):
                store.state().offset = str(msg["message_id"])

        if not updates:
            self._sleep(config.POLL_INTERVAL)

    def _sleep(self, seconds: float) -> None:
        # Wake early if a stop was requested.
        self._stop.wait(timeout=seconds)

    # -- housekeeping -------------------------------------------------------
    def _start_housekeeper(self) -> None:
        def loop() -> None:
            while not self._stop.wait(timeout=60):
                try:
                    self.queue.prune()
                    self.queue.cleanup_workspaces()
                    self.handlers.expire_pending()
                except Exception:  # noqa: BLE001
                    log.exception("housekeeping error")
        self._housekeeper = threading.Thread(target=loop, name="housekeeper", daemon=True)
        self._housekeeper.start()

    # -- shutdown -----------------------------------------------------------
    def shutdown(self, *, wait_for_jobs: float = 120.0) -> None:
        log.info("Draining job queue (up to %.0fs)…", wait_for_jobs)
        self.queue.wait_idle(timeout=wait_for_jobs)
        self.queue.stop(timeout=30)
        store.state().flush()
        log.info("Shutdown complete. Stats: %s", self._stats)

    # -- introspection ------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "offset": store.state().offset,
            "queue": self.queue.queue_depth(),
            "active": self.queue.snapshot(),
            "stats": self._stats,
        }
