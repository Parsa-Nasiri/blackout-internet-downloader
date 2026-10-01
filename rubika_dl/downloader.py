"""Glue between the job queue, the yt-dlp engine and the Rubika API.

This is where a :class:`~rubika_dl.jobs.Job` actually turns into files in a chat:
probe -> download -> size-check -> (split) -> upload -> caption.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from . import config, store
from .engine import Cancelled, DownloadError, Engine, Progress
from .fmt import escape_html, human_size, human_time, progress_bar, human_speed, eta, truncate
from .jobs import Job, JobState
from .log import get_logger
from .rubika_api import RubikaClient, detect_file_type

log = get_logger("downloader")

CAPTION_LIMIT = 1000  # Rubika text length is not documented; stay conservative.


class ProgressReporter:
    """Edits a single status message in place, rate-limited."""

    def __init__(self, client: RubikaClient, chat_id: str, message_id: str | None,
                 *, interval: float | None = None) -> None:
        self.client = client
        self.chat_id = chat_id
        self.message_id = message_id
        self.interval = interval if interval is not None else config.EDIT_PROGRESS_INTERVAL
        self._last = 0.0
        self._last_text = ""

    def update(self, text: str, *, force: bool = False) -> None:
        if not self.message_id:
            return
        now = time.time()
        if not force and (now - self._last) < self.interval:
            return
        if text == self._last_text:
            return
        self._last = now
        self._last_text = text
        try:
            self.client.edit_message_text(self.chat_id, self.message_id, text)
        except Exception as exc:  # noqa: BLE001 - progress must never kill a job
            log.debug("progress edit failed: %s", exc)

    def delete(self) -> None:
        if not self.message_id:
            return
        try:
            self.client.delete_message(self.chat_id, self.message_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("progress delete failed: %s", exc)


class Downloader:
    """Owns the per-job workflow."""

    def __init__(self, client: RubikaClient) -> None:
        self.client = client

    # -- public entry point -------------------------------------------------
    def handle(self, job: Job) -> None:
        """Run one job to completion. Called from a worker thread."""
        workdir = Path(config.DOWNLOAD_DIR) / job.id
        workdir.mkdir(parents=True, exist_ok=True)
        engine = Engine()
        job.engine = engine

        reporter = ProgressReporter(self.client, job.chat_id, job.status_message_id)
        started = time.time()

        try:
            reporter.update(f"⏳ Preparing <b>{escape_html(truncate(job.url, 60))}</b>…", force=True)

            files = engine.download(
                job.url,
                workdir,
                quality=job.quality,
                container=job.container,
                audio_only=(job.kind == "audio"),
                trim=job.trim,
                subtitles=job.subtitles,
                thumbnail=job.thumbnail,
                playlist=job.playlist,
                on_progress=self._progress_cb(reporter, job),
                index=1,
                count=1,
            )

            if engine.cancelled or job.state is JobState.CANCELLED:
                raise Cancelled("cancelled")

            if not files:
                raise DownloadError("The download produced no files.")

            total_bytes = sum(f.stat().st_size for f in files)
            reporter.update("📤 Uploading…", force=True)

            sent = 0
            for media in files:
                sent += self._send_media(engine, job, media, reporter)

            elapsed = time.time() - started
            store.stats().record(job.user_id, size=total_bytes, ok=True)
            store.state().bump_runs()

            done_text = (
                f"✅ <b>Done</b> — {sent} file(s), {human_size(total_bytes)} "
                f"in {human_time(elapsed)}"
            )
            reporter.update(done_text, force=True)

        except Cancelled:
            reporter.update("🛑 Cancelled.", force=True)
            raise
        except DownloadError as exc:
            store.stats().record(job.user_id, ok=False)
            reporter.update(f"❌ <b>Failed</b>\n{escape_html(str(exc))}", force=True)
            raise
        except Exception as exc:  # noqa: BLE001
            store.stats().record(job.user_id, ok=False)
            reporter.update(f"❌ <b>Error</b>\n{escape_html(str(exc)[:300])}", force=True)
            raise
        finally:
            if config.DELETE_AFTER_UPLOAD:
                shutil.rmtree(workdir, ignore_errors=True)
            job.engine = None

    # -- progress -----------------------------------------------------------
    def _progress_cb(self, reporter: ProgressReporter, job: Job):
        def cb(progress: Progress) -> None:
            if progress.status == "downloading":
                bar = progress_bar(progress.percent)
                pct = f"{progress.percent:5.1f}%"
                size_bit = ""
                if progress.total:
                    size_bit = f" · {human_size(progress.downloaded)}/{human_size(progress.total)}"
                text = (
                    f"⬇️ <b>Downloading</b>\n"
                    f"<code>{bar}</code> {pct}\n"
                    f"{human_speed(progress.speed)}{size_bit} · ETA {eta(progress.eta)}"
                )
                reporter.update(text)
            elif progress.status == "processing":
                stage = progress.stage or "processing"
                reporter.update(f"⚙️ <b>{escape_html(stage.capitalize())}</b>…")
        return cb

    # -- uploading ----------------------------------------------------------
    def _send_media(self, engine: Engine, job: Job, media: Path,
                    reporter: ProgressReporter) -> int:
        """Upload one media file, splitting it first if it is too large."""
        size = media.stat().st_size

        if size > config.MAX_UPLOAD_SIZE:
            if not config.SPLIT_ENABLED:
                raise DownloadError(
                    f"The file is {human_size(size)} which exceeds the "
                    f"{human_size(config.MAX_UPLOAD_SIZE)} upload limit."
                )
            return self._split_and_send(engine, job, media, reporter)

        return self._upload_one(job, media, reporter, part=None)

    def _split_and_send(self, engine: Engine, job: Job, media: Path,
                        reporter: ProgressReporter) -> int:
        hard = self._limit_for(media)
        # The configured target is a preference; the Rubika ceiling is the
        # constraint. Never aim above the ceiling or split_file would return
        # the file unchanged and the upload would be rejected.
        target = min(config.SPLIT_TARGET_SIZE, hard)
        reporter.update(
            f"✂️ File is {human_size(media.stat().st_size)} — splitting to fit "
            f"{human_size(target)} parts…",
            force=True,
        )
        parts = engine.split_file(media, target, hard_limit=hard)
        total = len(parts)
        sent = 0
        for index, part in enumerate(parts, 1):
            reporter.update(f"📤 Uploading part {index}/{total}…", force=True)
            sent += self._upload_one(job, part, reporter, part=(index, total))
        return sent

    def _upload_one(self, job: Job, media: Path, reporter: ProgressReporter,
                    *, part: tuple[int, int] | None) -> int:
        size = media.stat().st_size
        file_type = self._file_type_for(media, size)

        caption = self._caption(job, media, size, part)

        file_id = self.client.upload_file(media, file_type)
        self.client.send_file(
            job.chat_id,
            file_id,
            text=caption,
            reply_to_message_id=job.status_message_id,
        )
        log.info("Sent %s (%s, %s) to %s", media.name, file_type, human_size(size), job.chat_id)
        return 1

    @staticmethod
    def _limit_for(media: Path) -> int:
        """Rubika's real ceiling for this file's type, used as the split hard stop."""
        guessed = detect_file_type(media)
        if guessed == "Image":
            return config.MAX_IMAGE_SIZE
        return config.MAX_UPLOAD_SIZE

    @staticmethod
    def _file_type_for(media: Path, size: int) -> str:
        """Pick a Rubika FileTypeEnum, respecting per-type size ceilings."""
        guessed = detect_file_type(media)
        if guessed == "Image" and size > config.MAX_IMAGE_SIZE:
            return "File"
        if guessed in {"Video", "Music"} and size > config.MAX_UPLOAD_SIZE:
            return "File"
        return guessed

    @staticmethod
    def _caption(job: Job, media: Path, size: int,
                 part: tuple[int, int] | None) -> str:
        lines: list[str] = []
        if job.caption:
            lines.append(job.caption)
        else:
            lines.append(f"🎬 {escape_html(truncate(media.stem, 120))}")
        meta = [human_size(size)]
        if part:
            meta.append(f"part {part[0]}/{part[1]}")
        lines.append(" · ".join(meta))
        lines.append(f"🔗 {escape_html(truncate(job.url, 120))}")
        return "\n".join(lines)[:CAPTION_LIMIT]
