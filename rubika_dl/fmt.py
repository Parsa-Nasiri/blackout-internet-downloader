"""Human-friendly formatting helpers."""

from __future__ import annotations

import time


def human_size(num: float) -> str:
    """Format a byte count like '1.4 GiB'."""
    if num is None:
        return "0 B"
    num = float(num)
    negative = num < 0
    num = abs(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if num < 1024 or unit == "TiB":
            value = f"{num:.0f} {unit}" if unit == "B" else f"{num:.2f} {unit}"
            return ("-" + value) if negative else value
        num /= 1024
    return f"{num:.2f} PiB"


def human_time(seconds: float | int | None) -> str:
    """Format seconds like '1:02:03' or '2:03'."""
    if seconds is None:
        return "0:00"
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "0:00"
    if seconds < 0:
        seconds = 0
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def human_speed(bytes_per_sec: float | None) -> str:
    if not bytes_per_sec or bytes_per_sec <= 0:
        return "—"
    return f"{human_size(bytes_per_sec)}/s"


def eta(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "—"
    return human_time(seconds)


def progress_bar(percent: float, width: int = 14) -> str:
    """Render a text progress bar."""
    percent = max(0.0, min(100.0, float(percent)))
    filled = int(round(width * percent / 100.0))
    empty = width - filled
    return "█" * filled + "░" * empty


def truncate(text: str, limit: int = 200) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def elapsed(started_at: float) -> str:
    return human_time(time.time() - started_at)


def escape_html(text: str) -> str:
    """Escape text for Rubika's HTML markup."""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
