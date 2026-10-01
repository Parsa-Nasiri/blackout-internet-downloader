"""Logging configuration."""

from __future__ import annotations

import logging
import sys

from . import config

_CONFIGURED = False


def setup_logging() -> logging.Logger:
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        return logging.getLogger("rubika_dl")

    level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if config.LOG_FILE:
        file_handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    # Silence chatty libraries.
    for noisy in ("urllib3", "requests", "yt_dlp"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    _CONFIGURED = True
    return logging.getLogger("rubika_dl")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"rubika_dl.{name}")
