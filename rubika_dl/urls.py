"""URL detection and normalisation.

The bot supports "1500+ platforms" because yt-dlp does the heavy lifting; we
only need to (a) pull URLs out of free-form text and (b) decide which
extractor class to ask for.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

# Matches bare http(s) URLs and common protocol-less "www." links.
_URL_RE = re.compile(
    r"""(?xi)
    (?:
        https?://[^\s<>"'()\[\]{}]+
      | www\.[^\s<>"'()\[\]{}]+
    )
    """,
)

# A conservative set of TLD-ish endings so we do not treat "file.txt" as a URL.
_LIKELY_HOST_RE = re.compile(r"(?i)\.[a-z]{2,24}(?:[/:?#]|$)")

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".avif", ".heic",
}
AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".m4v", ".3gp",
}

# Domains we never try to download from (loopback / internal / unsafe).
_BLOCKED_HOSTS = {
    "localhost", "127.0.0.1", "0.0.0.0", "::1", "metadata.google.internal",
    "169.254.169.254",
}

# Sites that are better served by gallery-dl.
GALLERY_DOMAINS = {
    "instagram.com", "twitter.com", "x.com", "pixiv.net", "deviantart.com",
    "flickr.com", "tumblr.com", "reddit.com", "pinterest.com", "artstation.com",
    "danbooru.donmai.us", "gelbooru.com", "safebooru.org", "weibo.com",
}

# Domains that only ever serve images/audio/video (skip the "what is this?" step).
_DIRECT_MEDIA_HINT = IMAGE_EXTENSIONS | AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


def _clean(url: str) -> str:
    url = url.strip().strip("<>\"'")
    # Strip trailing punctuation that often clings to pasted links.
    url = url.rstrip(".,;:!?)")
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    return url


def find_urls(text: str) -> list[str]:
    """Return every plausible URL in a blob of text, de-duplicated, in order."""
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(text):
        candidate = _clean(match.group(0))
        if not _LIKELY_HOST_RE.search(candidate):
            continue
        if candidate not in seen:
            seen.add(candidate)
            found.append(candidate)
    return found


def normalize(url: str) -> str:
    """Normalise a URL (drop fragments, lowercase host)."""
    try:
        parts = urlparse(_clean(url))
    except ValueError:
        return url
    netloc = parts.netloc.lower()
    return urlunparse((parts.scheme or "https", netloc, parts.path, parts.params, parts.query, ""))


def host_of(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().lstrip(".")


def registrable_domain(host: str) -> str:
    """Best-effort eTLD+1 (handles the common two-label ccTLD cases)."""
    host = (host or "").lower()
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    # e.g. foo.co.uk / bar.com.br
    two_level = {"co", "com", "net", "org", "gov", "edu", "ac"}
    if labels[-2] in two_level and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_blocked(url: str) -> bool:
    host = host_of(url)
    if not host:
        return True
    if host in _BLOCKED_HOSTS:
        return True
    # Private network ranges.
    if host.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.",
                        "172.19.", "172.2", "172.30.", "172.31.")):
        return True
    return False


def is_gallery_url(url: str) -> bool:
    return registrable_domain(host_of(url)) in GALLERY_DOMAINS


def guess_kind(url: str) -> str:
    """Return 'image' | 'audio' | 'video' | 'unknown' from the path suffix."""
    path = urlparse(url).path.lower()
    for ext in _DIRECT_MEDIA_HINT:
        if path.endswith(ext):
            if ext in IMAGE_EXTENSIONS:
                return "image"
            if ext in AUDIO_EXTENSIONS:
                return "audio"
            return "video"
    return "unknown"


def is_supported(text: str) -> bool:
    """True if the text contains at least one downloadable-looking URL."""
    for url in find_urls(text):
        if not is_blocked(url):
            return True
    return False
