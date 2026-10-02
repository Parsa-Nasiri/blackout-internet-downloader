"""Configuration loaded from environment variables / .env file."""

from __future__ import annotations

import os
from pathlib import Path

try:  # python-dotenv is optional at runtime
    from dotenv import load_dotenv

    load_dotenv(override=False)
except Exception:  # pragma: no cover - dotenv is a soft dependency
    pass


BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    # GitHub secret values keep literal quotes if they were typed in, and a
    # token copied from a shell snippet often arrives as "abc". Strip one
    # matching surrounding pair.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value if value else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = _env(name)
    if not raw:
        return list(default or [])
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


# --------------------------------------------------------------------------
# Bot identity
# --------------------------------------------------------------------------
BOT_TOKEN: str = _env("RUBIKA_BOT_TOKEN", "") or ""
API_BASE: str = (_env("RUBIKA_API_BASE", "https://botapi.rubika.ir/v3") or "").rstrip("/")

# --------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------
ADMIN_IDS: list[str] = _env_list("ADMIN_IDS")
ALLOWED_USERS: list[str] = _env_list("ALLOWED_USERS")  # empty => everyone allowed
BLOCKED_USERS: list[str] = _env_list("BLOCKED_USERS")
ALLOW_GROUPS: bool = _env_bool("ALLOW_GROUPS", True)

# --------------------------------------------------------------------------
# Networking / engine
# --------------------------------------------------------------------------
PROXY: str | None = _env("PROXY")  # e.g. socks5://127.0.0.1:1080
COOKIES_FILE: str | None = _env("COOKIES_FILE")  # path inside the runner
COOKIES_URL: str | None = _env("COOKIES_URL")  # remote cookie file to fetch
USER_AGENT: str = _env(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
) or ""
YTDLP_PROXY: str | None = PROXY
MAX_CONCURRENT_DOWNLOADS: int = _env_int("MAX_CONCURRENT_DOWNLOADS", 2)

# YouTube's EJS challenge solver needs a JS runtime (Node >= 20). The GitHub
# runner and the Docker image both provide it; harmless elsewhere.
USE_NODE: bool = _env_bool("USE_NODE", True)
# Browser fingerprint yt-dlp should impersonate for generic/HLS extractors.
IMPERSONATE: str | None = _env("IMPERSONATE", "chrome")

# --------------------------------------------------------------------------
# Limits (bytes)
#
# Rubika's documented ceilings (FileTypeEnum):
#   File  -> 50 MiB
#   Video -> 50 MiB (mp4)
#   Image -> 10 MiB (jpg/gif/png/webp)
#   Music/Voice -> mp3
# Anything larger must be split. Raise MAX_UPLOAD_SIZE if your bot account
# is allowed bigger uploads.
# --------------------------------------------------------------------------
MIB = 1024 * 1024
MAX_FILESIZE: int = _env_int("MAX_FILESIZE", 4 * 1024 * MIB)  # what we allow downloading
MAX_UPLOAD_SIZE: int = _env_int("MAX_UPLOAD_SIZE", 50 * MIB)  # per-file Rubika ceiling
MAX_IMAGE_SIZE: int = _env_int("MAX_IMAGE_SIZE", 10 * MIB)
MAX_DURATION: int = _env_int("MAX_DURATION", 0)  # seconds, 0 = unlimited
MAX_PLAYLIST_ITEMS: int = _env_int("MAX_PLAYLIST_ITEMS", 25)

# --------------------------------------------------------------------------
# Behaviour
# --------------------------------------------------------------------------
DELETE_AFTER_UPLOAD: bool = _env_bool("DELETE_AFTER_UPLOAD", True)
SEND_AS_DOCUMENT_OVER: int = _env_int("SEND_AS_DOCUMENT_OVER", 0)  # 0 = auto
SPLIT_ENABLED: bool = _env_bool("SPLIT_ENABLED", True)
SPLIT_TARGET_SIZE: int = _env_int("SPLIT_TARGET_SIZE", 1_800 * 1024 * 1024)
EDIT_PROGRESS_INTERVAL: float = float(_env("EDIT_PROGRESS_INTERVAL", "6") or 6)
POLL_TIMEOUT: int = _env_int("POLL_TIMEOUT", 30)
POLL_INTERVAL: float = float(_env("POLL_INTERVAL", "0.5") or 0.5)

# --------------------------------------------------------------------------
# Paths / state
# --------------------------------------------------------------------------
DOWNLOAD_DIR: Path = Path(_env("DOWNLOAD_DIR", str(BASE_DIR / "downloads")))  # type: ignore[arg-type]
STATE_DIR: Path = Path(_env("STATE_DIR", str(BASE_DIR / ".state")))  # type: ignore[arg-type]
STATE_FILE: Path = STATE_DIR / "state.json"
STATS_FILE: Path = STATE_DIR / "stats.json"
USERS_FILE: Path = STATE_DIR / "users.json"

# Set when running on GitHub Actions to persist state back into the repo.
GITHUB_ACTIONS: bool = _env_bool("GITHUB_ACTIONS", False)
STATE_GIT_PUSH: bool = _env_bool("STATE_GIT_PUSH", GITHUB_ACTIONS)

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
LOG_LEVEL: str = (_env("LOG_LEVEL", "INFO") or "INFO").upper()
LOG_FILE: str | None = _env("LOG_FILE")


def ensure_dirs() -> None:
    """Create runtime directories if they do not exist."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def validate() -> list[str]:
    """Return a list of fatal configuration problems (empty == ok).

    Deliberately permissive about the token's shape: Rubika's docs only say
    "the token BotFather gives you" and show an opaque string, so there is no
    documented format to check against. Guessing one (Telegram's
    ``bot_id:secret``) rejected perfectly good tokens. Only reject values that
    cannot work in a URL path; ``check`` confirms the token with a live call.
    """
    problems: list[str] = []
    if not BOT_TOKEN:
        problems.append("RUBIKA_BOT_TOKEN is not set")
    elif not _token_looks_usable(BOT_TOKEN):
        problems.append(
            "RUBIKA_BOT_TOKEN contains characters that cannot appear in the "
            "API URL (copy it again from BotFather — it should be one token "
            "with no spaces, slashes or question marks)"
        )
    if not API_BASE.startswith("http"):
        problems.append("RUBIKA_API_BASE must be an http(s) URL")
    return problems


def _token_looks_usable(token: str) -> bool:
    """True unless the token clearly cannot be placed in a URL path segment."""
    if len(token) < 8:
        return False
    # The token goes into /v3/{token}/{method}, so these break the request.
    if any(ch.isspace() for ch in token):
        return False
    if any(ch in token for ch in "/?#&"):
        return False
    # A full URL pasted in by mistake.
    return "://" not in token
