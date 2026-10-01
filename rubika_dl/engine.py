"""yt-dlp engine: probe, download, post-process.

Everything that touches yt-dlp/ffmpeg lives here. The Telegram/Rubika layer
never imports yt_dlp directly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import config, urls
from .fmt import human_size as human_bytes, human_time
from .log import get_logger

log = get_logger("engine")

try:
    import yt_dlp  # type: ignore

    YTDLP_AVAILABLE = True
except Exception:  # pragma: no cover
    yt_dlp = None  # type: ignore
    YTDLP_AVAILABLE = False


class Cancelled(Exception):
    """Raised from a hook when the user cancels a job."""


class DownloadError(Exception):
    """A user-facing download failure."""


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------
@dataclass
class MediaInfo:
    """A compact, platform-agnostic description of a probed URL."""

    url: str
    title: str = "Untitled"
    uploader: str | None = None
    duration: int | None = None
    thumbnail: str | None = None
    webpage_url: str | None = None
    extractor: str | None = None
    is_playlist: bool = False
    playlist_count: int = 0
    entries: list[dict[str, Any]] = field(default_factory=list)
    formats: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def best_height(self) -> int | None:
        heights = [f.get("height") for f in self.formats if f.get("height")]
        return max(heights) if heights else None

    def summary(self) -> str:
        bits: list[str] = []
        if self.uploader:
            bits.append(self.uploader)
        if self.duration:
            bits.append(human_time(self.duration))
        if self.is_playlist:
            bits.append(f"playlist · {self.playlist_count} items")
        if self.best_height:
            bits.append(f"up to {self.best_height}p")
        return " · ".join(bits)


@dataclass
class Progress:
    """Progress snapshot handed to the UI callback."""

    status: str = "starting"  # starting | downloading | processing | finished | error
    percent: float = 0.0
    downloaded: int = 0
    total: int | None = None
    speed: float | None = None
    eta: float | None = None
    filename: str | None = None
    stage: str = ""
    index: int = 0
    count: int = 1


ProgressCallback = Callable[[Progress], None]


# --------------------------------------------------------------------------
# Format selection
# --------------------------------------------------------------------------
QUALITY_PRESETS: dict[str, str] = {
    # H.264/AAC ladder — the combination every client (including Rubika's
    # mp4-only player) handles. AV1/VP9 are deliberately deprioritised.
    "best": "bv*[vcodec*=avc1]+ba[acodec*=mp4a]/bv*[vcodec*=avc1]+ba/bv*+ba/b",
    # capped heights, same codec preference
    "2160p": "bv*[vcodec*=avc1][height<=2160]+ba[acodec*=mp4a]/bv*[height<=2160]+ba/bv*+ba/b",
    "1440p": "bv*[vcodec*=avc1][height<=1440]+ba[acodec*=mp4a]/bv*[height<=1440]+ba/bv*+ba/b",
    "1080p": "bv*[vcodec*=avc1][height<=1080]+ba[acodec*=mp4a]/bv*[height<=1080]+ba/bv*+ba/b",
    "720p": "bv*[vcodec*=avc1][height<=720]+ba[acodec*=mp4a]/bv*[height<=720]+ba/bv*+ba/b",
    "480p": "bv*[vcodec*=avc1][height<=480]+ba[acodec*=mp4a]/bv*[height<=480]+ba/bv*+ba/b",
    "360p": "bv*[vcodec*=avc1][height<=360]+ba[acodec*=mp4a]/bv*[height<=360]+ba/bv*+ba/b",
    "240p": "bv*[vcodec*=avc1][height<=240]+ba[acodec*=mp4a]/bv*[height<=240]+ba/bv*+ba/b",
    # smallest video
    "worst": "wv*+wa/w",
    # audio only
    "audio": "ba/b",
}

# Tried in order; the first that produces a file wins. Progressively looser so a
# site that only offers AV1, or only offers a muxed stream, still works.
FORMAT_LADDER: list[dict[str, Any]] = [
    {
        "format": "bv*[vcodec*=avc1][ext=mp4]+ba[acodec*=mp4a]/bv*[vcodec*=avc1]+ba[acodec*=mp4a]",
        "merge_output_format": "mp4",
    },
    {
        "format": "bv*[vcodec*=avc1]+ba[acodec*=mp4a]/bv*[vcodec*=avc1]+ba/bv*+ba",
        "merge_output_format": "mp4",
    },
    {"format": "bv*+ba/b", "merge_output_format": "mp4"},
    {"format": "best", "merge_output_format": "mp4"},
]

CONTAINER_PRESETS = {"mp4": "mp4", "mkv": "mkv", "webm": "webm"}


def build_format(quality: str | None, container: str | None = None) -> str:
    fmt = QUALITY_PRESETS.get((quality or "best").lower(), QUALITY_PRESETS["best"])
    if container in ("mp4", "webm") and not fmt.startswith(("ba", "wv")):
        # Prefer the requested container when both streams offer it.
        fmt = f"{fmt}[ext={container}]/{fmt}"
    return fmt


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
class Engine:
    """Wraps yt-dlp with progress, cancellation and ffmpeg helpers."""

    def __init__(self, cookie_file: str | None = None) -> None:
        self.cookie_file = cookie_file or config.COOKIES_FILE
        self._cancel = threading.Event()
        self._current_outtmpl_dir: Path | None = None

    # -- lifecycle ----------------------------------------------------------
    def cancel(self) -> None:
        self._cancel.set()

    def reset(self) -> None:
        self._cancel.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # -- base options -------------------------------------------------------
    def _base_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": False,
            "ignoreerrors": False,
            "retries": 5,
            "fragment_retries": 10,
            "socket_timeout": 60,
            "consoletitle": False,
            "nocheckcertificate": True,
            "http_headers": {"User-Agent": config.USER_AGENT},
            "extractor_retries": 3,
            "file_access_retries": 3,
            "overwrites": True,
            "windowsfilenames": True,
            "source_address": "0.0.0.0",
        }
        if self.cookie_file and Path(self.cookie_file).exists():
            opts["cookiefile"] = self.cookie_file
        if config.YTDLP_PROXY:
            opts["proxy"] = config.YTDLP_PROXY
        if config.USE_NODE:
            # YouTube's EJS challenge solver needs a JS runtime (Node >= 20).
            opts["js_runtimes"] = {"node": {}}
        if config.IMPERSONATE:
            opts["extractor_args"] = {"generic": {"impersonate": [config.IMPERSONATE]}}
        if config.MAX_DURATION:
            opts["match_filter"] = _duration_filter(config.MAX_DURATION)
        return opts

    def _outtmpl(self, workdir: Path) -> str:
        # yt-dlp appends the correct extension automatically.
        return str(workdir / "%(title).150B [%(id)s].%(ext)s")

    # -- probing ------------------------------------------------------------
    def probe(self, url: str, *, playlist: bool = False) -> MediaInfo:
        """Fetch metadata without downloading."""
        if not YTDLP_AVAILABLE:
            raise DownloadError("yt-dlp is not installed on this runner")

        opts = self._base_opts()
        opts.update({
            "skip_download": True,
            "noplaylist": not playlist,
            "extract_flat": "in_playlist" if playlist else False,
            "playlistend": config.MAX_PLAYLIST_ITEMS if playlist else None,
        })
        opts = {k: v for k, v in opts.items() if v is not None}

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[union-attr]
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            raise DownloadError(_friendly_error(exc)) from exc

        if info is None:
            raise DownloadError("Could not read any media from that link.")

        return self._to_media_info(url, info)

    @staticmethod
    def _to_media_info(url: str, info: dict[str, Any]) -> MediaInfo:
        is_playlist = info.get("_type") == "playlist" or "entries" in info
        entries: list[dict[str, Any]] = []
        if is_playlist:
            for entry in (info.get("entries") or []):
                if entry:
                    entries.append(entry)
        return MediaInfo(
            url=url,
            title=info.get("title") or "Untitled",
            uploader=info.get("uploader") or info.get("channel") or info.get("uploader_id"),
            duration=info.get("duration"),
            thumbnail=info.get("thumbnail"),
            webpage_url=info.get("webpage_url") or url,
            extractor=info.get("extractor_key") or info.get("extractor"),
            is_playlist=is_playlist,
            playlist_count=info.get("playlist_count") or len(entries),
            entries=entries,
            formats=info.get("formats") or [],
            raw=info,
        )

    # -- downloading --------------------------------------------------------
    def download(
        self,
        url: str,
        workdir: Path,
        *,
        quality: str = "best",
        container: str | None = None,
        audio_only: bool = False,
        audio_format: str = "mp3",
        trim: tuple[float, float] | None = None,
        subtitles: bool = False,
        thumbnail: bool = False,
        playlist: bool = False,
        on_progress: ProgressCallback | None = None,
        index: int = 0,
        count: int = 1,
    ) -> list[Path]:
        """Download *url* into *workdir* and return the produced media files."""
        if not YTDLP_AVAILABLE:
            raise DownloadError("yt-dlp is not installed on this runner")

        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        self._current_outtmpl_dir = workdir
        self.reset()

        before = set(workdir.iterdir())

        # Build the list of (format, merge) attempts, loosest last.
        if audio_only:
            attempts: list[dict[str, Any]] = [{"format": QUALITY_PRESETS["audio"]}]
        else:
            attempts = [dict(a) for a in FORMAT_LADDER]
            if quality in QUALITY_PRESETS and quality not in {"audio", "worst"}:
                preferred = build_format(quality, container)
                attempts.insert(0, {"format": preferred, "merge_output_format": container or "mp4"})
            elif quality == "worst":
                attempts.insert(0, {"format": QUALITY_PRESETS["worst"],
                                    "merge_output_format": container or "mp4"})

        last_error: Exception | None = None

        for attempt in attempts:
            opts = self._base_opts()
            opts.update({
                "outtmpl": self._outtmpl(workdir),
                "paths": {"home": str(workdir), "temp": str(workdir / ".tmp")},
                "noplaylist": not playlist,
                "playlistend": config.MAX_PLAYLIST_ITEMS if playlist else None,
                "format": attempt["format"],
                "merge_output_format": attempt.get("merge_output_format", "mp4"),
                "progress_hooks": [self._make_progress_hook(on_progress, index, count)],
                "postprocessor_hooks": [self._make_pp_hook(on_progress, index, count)],
            })

            if audio_only:
                opts.setdefault("postprocessors", []).append({
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": audio_format,
                    "preferredquality": "0",
                })

            if subtitles:
                opts.update({
                    "writesubtitles": True,
                    "writeautomaticsub": True,
                    "subtitleslangs": ["en", "fa", ".*"],
                    "subtitlesformat": "srt/best",
                })
                opts.setdefault("postprocessors", []).append(
                    {"key": "FFmpegSubtitlesConvertor", "format": "srt"})

            if thumbnail:
                opts["writethumbnail"] = True
                opts.setdefault("postprocessors", []).append({"key": "EmbedThumbnail"})

            opts.setdefault("postprocessors", []).append({"key": "FFmpegMetadata"})

            if trim and trim[1] > trim[0]:
                start, end = trim
                opts["download_ranges"] = _range_factory(start, end)
                opts["force_keyframes_at_cuts"] = True

            opts = {k: v for k, v in opts.items() if v is not None}

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[union-attr]
                    ydl.download([url])
                last_error = None
                break
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.debug("format attempt failed (%s), trying a looser selector",
                          attempt["format"][:60])
                continue

        if last_error is not None:
            raise DownloadError(_friendly_error(last_error)) from last_error

        if on_progress:
            on_progress(Progress(status="processing", stage="finalising", percent=100.0, index=index, count=count))

        after = set(workdir.iterdir())
        produced = [
            p for p in sorted(after - before)
            if p.is_file() and not p.name.endswith((".part", ".ytdl", ".tmp"))
            and p.suffix.lower() not in {".json", ".description"}
        ]
        # Also catch files that pre-existed and were overwritten (name reuse).
        if not produced:
            produced = [
                p for p in sorted(workdir.iterdir())
                if p.is_file() and p.suffix.lower() not in {".part", ".ytdl", ".json", ".tmp"}
            ]
        # Drop subtitle/thumbnail sidecars from the primary media list.
        produced = [
            p for p in produced
            if p.suffix.lower() not in {".srt", ".vtt", ".ass", ".webp", ".jpg", ".jpeg", ".png"}
        ]
        return produced

    # -- hooks --------------------------------------------------------------
    def _make_progress_hook(self, cb: ProgressCallback | None, index: int, count: int):
        def hook(data: dict[str, Any]) -> None:
            if self._cancel.is_set():
                raise Cancelled("cancelled by user")
            if not cb:
                return
            status = data.get("status")
            if status == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                done = data.get("downloaded_bytes") or 0
                percent = (done / total * 100.0) if total else 0.0
                cb(Progress(
                    status="downloading",
                    percent=percent,
                    downloaded=done,
                    total=total,
                    speed=data.get("speed"),
                    eta=data.get("eta"),
                    filename=os.path.basename(data.get("filename") or ""),
                    stage="downloading",
                    index=index,
                    count=count,
                ))
            elif status == "finished":
                cb(Progress(
                    status="processing",
                    percent=100.0,
                    downloaded=data.get("downloaded_bytes") or 0,
                    total=data.get("total_bytes") or data.get("total_bytes_estimate"),
                    filename=os.path.basename(data.get("filename") or ""),
                    stage="merging",
                    index=index,
                    count=count,
                ))
        return hook

    def _make_pp_hook(self, cb: ProgressCallback | None, index: int, count: int):
        def hook(data: dict[str, Any]) -> None:
            if self._cancel.is_set():
                raise Cancelled("cancelled by user")
            if cb and data.get("status") == "started":
                cb(Progress(
                    status="processing",
                    percent=100.0,
                    stage=str(data.get("postprocessor") or "post-processing").replace("FFmpeg", "").strip() or "processing",
                    index=index,
                    count=count,
                ))
        return hook

    # -- ffmpeg helpers -----------------------------------------------------
    @staticmethod
    def ffmpeg_available() -> bool:
        return shutil.which("ffmpeg") is not None

    def make_thumbnail(self, video: Path, out: Path, at: float = 3.0) -> Path | None:
        """Grab a frame for use as the upload thumbnail."""
        if not self.ffmpeg_available():
            return None
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", str(max(0.0, at)),
            "-i", str(video),
            "-frames:v", "1",
            "-vf", "scale='min(320,iw)':-2",
            "-q:v", "5",
            str(out),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
            return out if out.exists() else None
        except (subprocess.SubprocessError, OSError) as exc:
            log.debug("thumbnail extraction failed: %s", exc)
            return None

    # A part may run slightly over the target: muxers can only cut on a
    # keyframe, and rate-controlled encoding overshoots a little.
    _SPLIT_TOLERANCE = 1.05

    # Shortest segment worth cutting; below this the per-segment container
    # overhead outweighs the size saving.
    _MIN_SEGMENT = 0.1

    # Floor for the re-encode video bitrate. Below this the picture is
    # unwatchable, and a part that fits but is unwatchable helps nobody.
    _MIN_VIDEO_BPS = 80_000

    def split_file(
        self,
        media: Path,
        target_size: int,
        on_part: Callable[[Path, int, int], None] | None = None,
        hard_limit: int | None = None,
    ) -> list[Path]:
        """Split a large media file into parts small enough to upload.

        A lossless stream-copy is attempted first; if the muxer cannot cut
        small enough (long GOPs, sparse keyframes) the file is re-encoded with
        a bitrate capped to the per-segment budget, so parts shrink instead of
        a CRF re-encode inflating them past the original.

        ``target_size`` is what each part should be; ``hard_limit`` (Rubika's
        real ceiling) is what it must be. Parts over the target but under the
        hard limit are returned with a warning, because sending a slightly big
        part beats failing the whole download.
        """
        if not self.ffmpeg_available():
            raise DownloadError("ffmpeg is not available, cannot split the file")

        size = media.stat().st_size
        if size <= target_size:
            return [media]

        hard = hard_limit or target_size
        duration = self._probe_duration(media)
        if not duration:
            raise DownloadError("Could not determine duration to split the file.")

        segment_time = self._segment_length(size, duration, target_size)
        outdir = media.parent / f"{media.stem}_parts"
        outdir.mkdir(parents=True, exist_ok=True)
        fmt = media.suffix.lstrip(".") or "mp4"

        # 1. Fast and lossless — but the cut lands on the next keyframe, so a
        #    long-GOP source can come back as a single oversized part.
        pieces = self._run_segment(self._copy_cmd(media, outdir, fmt, segment_time), outdir)
        if not pieces or self._oversized(pieces, target_size):
            if pieces:
                log.info(
                    "stream-copy left %d part(s) over %s; re-encoding",
                    sum(1 for p in pieces if p.stat().st_size > target_size),
                    human_bytes(target_size),
                )
            pieces = self._reencode_segments(
                media, outdir, fmt, duration, segment_time, target_size,
            )

        if not pieces:
            raise DownloadError("Splitting produced no output.")

        if self._oversized(pieces, target_size):
            biggest = max(p.stat().st_size for p in pieces)
            if biggest > hard * self._SPLIT_TOLERANCE:
                raise DownloadError(
                    f"Could not split the file below {human_bytes(hard)} "
                    f"(best part was {human_bytes(biggest)}). "
                    f"Set SPLIT_TARGET_SIZE higher."
                )
            log.warning(
                "split parts are up to %s — over the %s target but under Rubika's limit",
                human_bytes(biggest), human_bytes(target_size),
            )

        for i, piece in enumerate(pieces, 1):
            if on_part:
                on_part(piece, i, len(pieces))
        return pieces

    def _reencode_segments(
        self,
        media: Path,
        outdir: Path,
        fmt: str,
        duration: float,
        segment_time: float,
        target_size: int,
        max_attempts: int = 4,
    ) -> list[Path]:
        """Re-encode into segments capped to *target_size*, iterating on the cap.

        A CRF re-encode inflates the bitrate of high-motion sources (tiny GOPs
        make it worse), so parts come out larger than the input and shrinking
        the segment length alone makes it worse. Capping the bitrate to the
        per-segment budget is what actually bounds the size; each retry scales
        the cap by how far the biggest part overshot.
        """
        has_video = self._has_video(media)
        audio_bps = self._audio_bitrate(target_size, segment_time)
        budget_bps = int(target_size * 0.9 * 8 / max(segment_time, 0.01))
        video_bps = max(self._MIN_VIDEO_BPS, budget_bps - audio_bps)

        pieces: list[Path] = []
        for attempt in range(max_attempts):
            self._clear_parts(outdir)
            pieces = self._run_segment(
                self._reencode_cmd(
                    media, outdir, fmt, segment_time,
                    video_bps=video_bps if has_video else None,
                    audio_bps=audio_bps,
                ),
                outdir,
            )
            if not pieces:
                return []
            if not self._oversized(pieces, target_size):
                return pieces
            if attempt == max_attempts - 1:
                break

            worst = max(p.stat().st_size for p in pieces)
            scale = (target_size * 0.95) / worst
            if has_video:
                if video_bps <= self._MIN_VIDEO_BPS:
                    break  # cannot encode any lower
                new_bps = max(self._MIN_VIDEO_BPS, int(video_bps * scale))
                if new_bps >= video_bps:
                    break  # already at the floor
                log.info(
                    "re-encode parts up to %s; retrying with %d bps video",
                    human_bytes(worst), new_bps,
                )
                video_bps = new_bps
            else:
                new_seg = max(self._MIN_SEGMENT, segment_time * scale)
                if new_seg >= segment_time - 0.01:
                    break
                log.info(
                    "re-encode parts up to %s; retrying with %.2fs segments",
                    human_bytes(worst), new_seg,
                )
                segment_time = new_seg

        return pieces

    @staticmethod
    def _segment_length(size: int, duration: float, target_size: int) -> float:
        """Seconds per segment so one part lands under ``target_size``.

        Derived from the file's measured bitrate, because stream-copy cuts
        snap to keyframes and can overshoot the naive division.
        """
        bytes_per_second = max(size / max(duration, 0.001), 1.0)
        return max(Engine._MIN_SEGMENT, (target_size * 0.9) / bytes_per_second)

    @staticmethod
    def _audio_bitrate(target_size: int, segment_time: float) -> int:
        """Audio bitrate that keeps audio under ~30% of a segment's budget."""
        budget_bps = int(target_size * 0.9 * 8 / max(segment_time, 0.01))
        return min(128_000, max(32_000, budget_bps * 3 // 10))

    @staticmethod
    def _oversized(pieces: list[Path], target_size: int) -> bool:
        limit = target_size * Engine._SPLIT_TOLERANCE
        return any(p.stat().st_size > limit for p in pieces)

    @staticmethod
    def _clear_parts(outdir: Path) -> None:
        # glob() would treat '[', ']' in a filename as a character class, so
        # filter the directory listing instead.
        for old in outdir.iterdir():
            if old.is_file() and old.name.startswith("part"):
                old.unlink()

    def _copy_cmd(self, media: Path, outdir: Path, fmt: str, segment_time: float) -> list[str]:
        return [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(media),
            "-c", "copy", "-map", "0",
            "-f", "segment",
            "-segment_time", f"{segment_time:.3f}",
            "-reset_timestamps", "1",
            "-segment_format", fmt,
            str(outdir / f"part%03d.{fmt}"),
        ]

    def _reencode_cmd(
        self, media: Path, outdir: Path, fmt: str, segment_time: float,
        *, video_bps: int | None, audio_bps: int,
    ) -> list[str]:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(media)]
        if video_bps is not None:
            # ABR with a hard ceiling: CRF would happily inflate the bitrate —
            # and with it the part size — on high-motion sources.
            cmd += [
                "-c:v", "libx264", "-preset", "veryfast",
                "-b:v", str(video_bps),
                "-maxrate", str(video_bps),
                "-bufsize", str(video_bps * 2),
                "-pix_fmt", "yuv420p",
                # Lets the segment muxer cut exactly where we ask instead of
                # snapping to the source's (possibly very long) GOP.
                "-force_key_frames", f"expr:gte(t,n_forced*{segment_time:.3f})",
            ]
        cmd += ["-c:a", self._audio_codec(fmt), "-b:a", str(audio_bps)]
        cmd += [
            "-f", "segment",
            "-segment_time", f"{segment_time:.3f}",
            "-reset_timestamps", "1",
            "-segment_format", fmt,
            str(outdir / f"part%03d.{fmt}"),
        ]
        return cmd

    @staticmethod
    def _audio_codec(fmt: str) -> str:
        """Audio encoder that the output container actually accepts."""
        if fmt == "mp3":
            return "libmp3lame"
        if fmt in {"webm", "ogg", "opus"}:
            return "libopus"
        return "aac"

    @staticmethod
    def _has_video(media: Path) -> bool:
        if not shutil.which("ffprobe"):
            return True  # video options are unused when there is no stream
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            str(media),
        ]
        try:
            out = subprocess.run(cmd, check=True, capture_output=True, timeout=60).stdout
            return bool(out.decode().strip())
        except (subprocess.SubprocessError, OSError):
            return True

    @staticmethod
    def _run_segment(cmd: list[str], outdir: Path) -> list[Path]:
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=3600)
        except subprocess.CalledProcessError as exc:
            log.debug("ffmpeg segment failed: %s", (exc.stderr or b"")[:400])
            return []
        except (subprocess.SubprocessError, OSError) as exc:
            log.debug("ffmpeg segment error: %s", exc)
            return []
        return sorted(
            p for p in outdir.iterdir()
            if p.is_file() and p.name.startswith("part")
        )

    @staticmethod
    def _probe_duration(media: Path) -> float | None:
        if not shutil.which("ffprobe"):
            return None
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(media),
        ]
        try:
            out = subprocess.run(cmd, check=True, capture_output=True, timeout=60).stdout
            return float(out.decode().strip())
        except (subprocess.SubprocessError, ValueError, OSError):
            return None

    def probe_local(self, media: Path) -> dict[str, Any]:
        """Return duration/size metadata for a local file."""
        return {
            "size": media.stat().st_size if media.exists() else 0,
            "duration": self._probe_duration(media),
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _range_factory(start: float, end: float) -> Callable[..., Iterable[dict[str, float]]]:
    def _ranges(info_dict: dict[str, Any], ydl: Any = None):  # noqa: ANN001
        return [{"start_time": float(start), "end_time": float(end)}]
    return _ranges


def _duration_filter(max_duration: int) -> Callable[[dict[str, Any]], str | None]:
    """yt-dlp match_filter: skip anything longer than *max_duration* seconds."""
    def _filter(info_dict: dict[str, Any]) -> str | None:
        duration = info_dict.get("duration")
        if duration and duration > max_duration:
            return f"longer than the {max_duration}s limit"
        return None
    return _filter


_FRIENDLY = [
    ("Unsupported URL", "That link isn't supported by the downloader."),
    ("Video unavailable", "That media is unavailable."),
    ("Private video", "That media is private (cookies may be required)."),
    ("This video is available to this channel's members", "Members-only media — cookies required."),
    ("Sign in to confirm your age", "Age-restricted — cookies required."),
    ("Login required", "Login required — cookies needed."),
    ("HTTP Error 403", "Access denied by the site (403). Try again or use cookies."),
    ("HTTP Error 404", "Not found (404)."),
    ("HTTP Error 429", "Rate-limited by the site (429). Try again later."),
    ("Unable to download", "The site refused the download."),
    ("ffmpeg", "ffmpeg failed while processing the media."),
    ("timed out", "The connection timed out."),
    ("No video formats found", "No downloadable format was found."),
]


def _friendly_error(exc: Exception) -> str:
    text = str(exc)
    for needle, friendly in _FRIENDLY:
        if needle.lower() in text.lower():
            return friendly
    # Trim noisy extractor prefixes.
    text = text.replace("ERROR: ", "").strip()
    return text[:400] or "Unknown download error."


def format_choices(info: MediaInfo) -> list[tuple[str, str]]:
    """Return (label, format_selector) pairs for the quality menu."""
    choices: list[tuple[str, str]] = [("🎬 Best quality", "best")]
    heights = sorted(
        {int(f["height"]) for f in info.formats if f.get("height")},
        reverse=True,
    )
    for h in heights:
        if h >= 144:
            label = f"📹 {h}p"
            choices.append((label, f"{h}p"))
    choices.append(("🎵 Audio only (MP3)", "audio"))
    choices.append(("📦 Smallest file", "worst"))
    return choices


def looks_direct_media(url: str) -> bool:
    return urls.guess_kind(url) != "unknown"
