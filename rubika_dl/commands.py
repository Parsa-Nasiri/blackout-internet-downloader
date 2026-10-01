"""Command + message routing.

Holds the handler table, the "always ask" quality flow, and the inline-keypad
callback resolution.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, store
from .downloader import Downloader, ProgressReporter
from .engine import Engine, MediaInfo, DownloadError, format_choices
from .fmt import escape_html, human_size, truncate
from .jobs import Job, JobQueue
from .log import get_logger
from .rubika_api import RubikaClient, columns, keypad, simple_button, link_button
from .updates import Incoming

log = get_logger("commands")

BOT_NAME = "BlackOut Downloader"
REPO_URL = "https://github.com/Parsa-Nasiri/blackout-internet-downloader"

HELP_TEXT = (
    f"<b>🎬 {BOT_NAME}</b>\n\n"
    "Send me a link and I'll download the media for you.\n\n"
    "<b>Commands</b>\n"
    "/vid &lt;url&gt; — download as video\n"
    "/audio &lt;url&gt; — extract audio (MP3)\n"
    "/img &lt;url&gt; — download images\n"
    "/link &lt;url&gt; — just show the direct link info\n"
    "/settings — choose default quality\n"
    "/cancel — cancel your running downloads\n"
    "/stats — usage statistics\n"
    "/id — show your chat/user ids\n"
    "/about — about this bot\n\n"
    "Supports 1500+ sites via yt-dlp (YouTube, Instagram, Twitter/X, TikTok, "
    "Reddit, and many more)."
)

COMMANDS = [
    ("start", "Start the bot"),
    ("help", "Show help"),
    ("vid", "Download video from a URL"),
    ("audio", "Extract audio (MP3)"),
    ("img", "Download images"),
    ("link", "Show media info"),
    ("settings", "Default quality & options"),
    ("cancel", "Cancel running downloads"),
    ("stats", "Usage statistics"),
    ("id", "Show ids"),
    ("about", "About"),
]


@dataclass
class Pending:
    """A URL waiting for the user to pick a quality."""

    token: str
    url: str
    chat_id: str
    user_id: str
    message_id: str | None
    kind: str
    info: MediaInfo | None
    created_at: float


class Handlers:
    def __init__(self, client: RubikaClient, queue: JobQueue) -> None:
        self.client = client
        self.queue = queue
        self.downloader = Downloader(client)
        self._pending: dict[str, Pending] = {}
        self._lock = threading.RLock()
        self._commands = {
            "start": self.cmd_start,
            "help": self.cmd_help,
            "vid": self.cmd_vid,
            "video": self.cmd_vid,
            "audio": self.cmd_audio,
            "mp3": self.cmd_audio,
            "img": self.cmd_img,
            "image": self.cmd_img,
            "photo": self.cmd_img,
            "link": self.cmd_link,
            "settings": self.cmd_settings,
            "cancel": self.cmd_cancel,
            "stats": self.cmd_stats,
            "id": self.cmd_id,
            "about": self.cmd_about,
        }

    # -- entry point --------------------------------------------------------
    def handle(self, incoming: Incoming) -> None:
        try:
            self._dispatch(incoming)
        except Exception:  # noqa: BLE001 - never let a handler kill the poller
            log.exception("Handler crashed for %s", incoming.update_type)

    def _dispatch(self, incoming: Incoming) -> None:
        # Access control first.
        if not self._is_allowed(incoming):
            log.info("Blocked %s in %s", incoming.sender_id, incoming.chat_id)
            return

        if incoming.update_type == "StartedBot":
            self.cmd_start(incoming)
            return

        if incoming.update_type == "EventData":
            log.info("Bot event %s in %s", incoming.text, incoming.chat_id)
            return

        # Inline-keypad click.
        if incoming.button_id:
            if self._handle_button(incoming):
                return

        if incoming.is_command:
            handler = self._commands.get(incoming.command)
            if handler:
                handler(incoming)
                return
            if incoming.command in {"stop"}:
                return
            self._reply(incoming, "Unknown command. Try /help")
            return

        # Plain text: look for URLs.
        urls = incoming.urls
        if urls:
            self._handle_urls(incoming, urls)
            return

        if incoming.has_media_attachment:
            self._reply(incoming, "I can download from links. Send me a URL 🙂")
            return

        # Nothing actionable — stay quiet in groups, nudge in private.
        if incoming.is_private and incoming.text.strip():
            self._reply(incoming, "Send me a link to download, or /help for commands.")

    # -- access control -----------------------------------------------------
    def _is_allowed(self, incoming: Incoming) -> bool:
        uid = incoming.sender_id or ""
        if uid and uid in set(config.BLOCKED_USERS):
            return False
        if incoming.is_group and not config.ALLOW_GROUPS:
            return False
        if uid and uid in set(config.ADMIN_IDS):
            return True
        if config.ALLOWED_USERS:
            return uid in set(config.ALLOWED_USERS)
        return True

    # -- commands -----------------------------------------------------------
    def cmd_start(self, incoming: Incoming) -> None:
        rows = columns([
            ("help", "📖 Help"),
            ("settings", "⚙️ Settings"),
            ("stats", "📊 Stats"),
        ], per_row=2)
        self._reply(incoming, HELP_TEXT, inline_keypad=keypad(rows))

    def cmd_help(self, incoming: Incoming) -> None:
        self._reply(incoming, HELP_TEXT)

    def cmd_about(self, incoming: Incoming) -> None:
        me = {}
        try:
            me = self.client.get_me()
        except Exception:  # noqa: BLE001
            pass
        text = (
            f"<b>{BOT_NAME}</b>\n"
            "A Rubika bot powered by <b>yt-dlp</b>, running on GitHub Actions.\n\n"
            f"Bot: @{escape_html(str(me.get('username') or 'unknown'))}\n"
            f"Source: {REPO_URL}\n\n"
            "Built with ❤️ — media is fetched on demand and never stored."
        )
        self._reply(incoming, text)

    def cmd_id(self, incoming: Incoming) -> None:
        text = (
            f"<b>IDs</b>\n"
            f"chat_id: <code>{escape_html(incoming.chat_id)}</code>\n"
            f"user_id: <code>{escape_html(incoming.sender_id or '—')}</code>\n"
            f"chat_type: <code>{escape_html(incoming.chat_type or '—')}</code>"
        )
        self._reply(incoming, text)

    def cmd_stats(self, incoming: Incoming) -> None:
        snap = store.stats().snapshot()
        users = snap.get("users") or {}
        mine = users.get(str(incoming.sender_id), {}) if isinstance(users, dict) else {}
        text = (
            "<b>📊 Statistics</b>\n"
            f"Total downloads: {int(snap.get('downloads', 0))}\n"
            f"Failures: {int(snap.get('failures', 0))}\n"
            f"Data transferred: {human_size(int(snap.get('bytes', 0)))}\n"
            f"Queue depth: {self.queue.queue_depth()}\n"
            f"Worker runs: {store.state()._store.get('runs', 0)}\n\n"
            f"<b>You</b>: {int(mine.get('downloads', 0))} downloads, "
            f"{human_size(int(mine.get('bytes', 0)))}"
        )
        self._reply(incoming, text)

    def cmd_settings(self, incoming: Incoming) -> None:
        pref = store.users().get(incoming.sender_id or incoming.chat_id)
        quality = pref.get("quality", "best")
        ask = pref.get("ask", True)
        rows = [
            [simple_button("set:quality:best", "🎬 Best"),
             simple_button("set:quality:1080p", "📹 1080p")],
            [simple_button("set:quality:720p", "📹 720p"),
             simple_button("set:quality:audio", "🎵 Audio")],
            [simple_button("set:ask:toggle", f"❓ Ask each time: {'ON' if ask else 'OFF'}")],
        ]
        text = (
            "<b>⚙️ Settings</b>\n"
            f"Default quality: <b>{escape_html(quality)}</b>\n"
            f"Ask each time: <b>{'ON' if ask else 'OFF'}</b>"
        )
        self._reply(incoming, text, inline_keypad=keypad(rows))

    def cmd_cancel(self, incoming: Incoming) -> None:
        cancelled = self.queue.cancel_all(incoming.chat_id)
        if cancelled:
            self._reply(incoming, f"🛑 Cancelled {len(cancelled)} job(s).")
        else:
            self._reply(incoming, "Nothing is running for this chat.")

    # -- url commands -------------------------------------------------------
    def cmd_vid(self, incoming: Incoming) -> None:
        self._command_url(incoming, kind="video")

    def cmd_audio(self, incoming: Incoming) -> None:
        self._command_url(incoming, kind="audio")

    def cmd_img(self, incoming: Incoming) -> None:
        self._command_url(incoming, kind="image")

    def cmd_link(self, incoming: Incoming) -> None:
        urls = incoming.urls or ([incoming.args] if incoming.args.startswith("http") else [])
        if not urls:
            self._reply(incoming, "Usage: /link &lt;url&gt;")
            return
        self._reply(incoming, "🔍 Fetching info…")
        try:
            info = Engine().probe(urls[0], playlist=False)
        except DownloadError as exc:
            self._reply(incoming, f"❌ {escape_html(str(exc))}")
            return
        text = (
            f"<b>{escape_html(truncate(info.title, 150))}</b>\n"
            f"{escape_html(info.summary())}\n"
            f"extractor: <code>{escape_html(info.extractor or '—')}</code>\n"
            f"🔗 {escape_html(truncate(info.webpage_url or urls[0], 200))}"
        )
        self._reply(incoming, text)

    def _command_url(self, incoming: Incoming, *, kind: str) -> None:
        urls = incoming.urls
        if not urls and incoming.args.startswith("http"):
            urls = [incoming.args.split()[0]]
        if not urls:
            self._reply(incoming, f"Usage: /{incoming.command} &lt;url&gt;")
            return
        self._start_or_ask(incoming, urls, kind=kind, force=True)

    def _handle_urls(self, incoming: Incoming, urls: list[str]) -> None:
        self._start_or_ask(incoming, urls, kind="video", force=False)

    # -- quality flow -------------------------------------------------------
    def _start_or_ask(self, incoming: Incoming, urls: list[str], *,
                      kind: str, force: bool) -> None:
        url = urls[0]
        playlist = len(urls) > 1 or self._looks_like_playlist(url)
        pref = store.users().get(incoming.sender_id or incoming.chat_id)
        ask = pref.get("ask", True) and not force
        quality = pref.get("quality", "best")

        # Direct media links (ending in .mp4/.jpg/...) skip probing.
        if not playlist and not ask and kind == "video":
            self._submit(incoming, url, kind=kind, quality=quality, playlist=False)
            return

        status = self._reply(incoming, "🔍 Reading media info…")

        info: MediaInfo | None = None
        try:
            info = Engine().probe(url, playlist=playlist)
        except DownloadError as exc:
            if self._looks_direct(url):
                # yt-dlp can't probe it, but it may still be a plain file.
                self._submit(incoming, url, kind=kind, quality="best",
                             playlist=False, status_message_id=status)
                return
            self._edit(incoming, status, f"❌ {escape_html(str(exc))}")
            return

        if ask:
            self._send_quality_menu(incoming, url, kind, info, status)
            return

        self._submit(incoming, url, kind=kind, quality=quality,
                     playlist=info.is_playlist, status_message_id=status,
                     caption=f"🎬 {truncate(info.title, 120)}")

    def _send_quality_menu(self, incoming: Incoming, url: str, kind: str,
                           info: MediaInfo, status_message_id: str | None) -> None:
        token = uuid.uuid4().hex[:8]
        with self._lock:
            self._pending[token] = Pending(
                token=token, url=url, chat_id=incoming.chat_id,
                user_id=incoming.sender_id or "", message_id=status_message_id,
                kind=kind, info=info, created_at=time.time(),
            )

        choices = format_choices(info)
        rows = columns([(f"{token}|{value}", label) for label, value in choices], per_row=2)
        rows.append([simple_button(f"canceljob|{token}", "🛑 Cancel")])

        header = (
            f"<b>{escape_html(truncate(info.title, 120))}</b>\n"
            f"{escape_html(info.summary())}\n\n"
            "Pick a quality:"
        )
        if status_message_id:
            try:
                self.client.edit_message_text(incoming.chat_id, status_message_id, header)
                self.client.edit_message_keypad(incoming.chat_id, status_message_id, keypad(rows))
                return
            except Exception as exc:  # noqa: BLE001
                log.debug("edit menu failed, sending new: %s", exc)
        self._reply(incoming, header, inline_keypad=keypad(rows))

    def _handle_button(self, incoming: Incoming) -> bool:
        button = incoming.button_id or ""
        if "|" not in button:
            return False
        action, _, payload = button.partition("|")

        if action == "canceljob":
            with self._lock:
                pending = self._pending.pop(payload, None)
            if pending:
                self._edit(incoming, pending.message_id, "🛑 Cancelled.")
            return True

        if action == "set":
            self._handle_setting(incoming, payload)
            return True

        # Quality choice: "<token>|<quality>".
        with self._lock:
            pending = self._pending.pop(action, None)
        if not pending:
            self._edit(incoming, incoming.message_id, "⌛ This menu expired. Send the link again.")
            return True

        kind = pending.kind
        if payload == "audio":
            kind = "audio"
        self._submit(
            incoming,
            pending.url,
            kind=kind,
            quality=payload,
            playlist=bool(pending.info and pending.info.is_playlist),
            status_message_id=pending.message_id,
            caption=f"🎬 {truncate(pending.info.title, 120)}" if pending.info else "",
        )
        return True

    def _handle_setting(self, incoming: Incoming, payload: str) -> None:
        key, _, value = payload.partition(":")
        uid = incoming.sender_id or incoming.chat_id
        if key == "quality":
            store.users().set(uid, {"quality": value})
            self._edit(incoming, incoming.message_id, f"✅ Default quality set to <b>{escape_html(value)}</b>.")
        elif key == "ask":
            pref = store.users().get(uid)
            new_value = not pref.get("ask", True)
            store.users().set(uid, {"ask": new_value})
            self._edit(incoming, incoming.message_id,
                       f"✅ Ask each time is now <b>{'ON' if new_value else 'OFF'}</b>.")
        self.cmd_settings(incoming)

    # -- submission ---------------------------------------------------------
    def _submit(self, incoming: Incoming, url: str, *, kind: str, quality: str,
                playlist: bool, status_message_id: str | None = None,
                caption: str = "") -> Job:
        job = self.queue.submit(
            chat_id=incoming.chat_id,
            user_id=incoming.sender_id or incoming.chat_id,
            url=url,
            kind=kind,
            quality=quality,
            playlist=playlist,
            status_message_id=status_message_id,
            caption=caption,
        )
        if status_message_id:
            self._edit(incoming, status_message_id,
                       f"📥 Queued (position {self.queue.queue_depth()})…")
        else:
            msg = self._reply(incoming, "📥 Queued…")
            job.status_message_id = msg
        return job

    # -- reply helpers ------------------------------------------------------
    def _reply(self, incoming: Incoming, text: str,
               inline_keypad: dict[str, Any] | None = None) -> str | None:
        try:
            return self.client.send_message(
                incoming.chat_id, text,
                inline_keypad=inline_keypad,
                reply_to_message_id=incoming.message_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("send_message failed: %s", exc)
            return None

    def _edit(self, incoming: Incoming, message_id: str | None, text: str) -> None:
        if not message_id:
            self._reply(incoming, text)
            return
        try:
            self.client.edit_message_text(incoming.chat_id, message_id, text)
        except Exception as exc:  # noqa: BLE001
            log.debug("edit failed: %s", exc)

    # -- misc ---------------------------------------------------------------
    @staticmethod
    def _looks_like_playlist(url: str) -> bool:
        lowered = url.lower()
        return any(marker in lowered for marker in ("list=", "/playlist", "/sets/", "/album/"))

    @staticmethod
    def _looks_direct(url: str) -> bool:
        from .urls import guess_kind
        return guess_kind(url) != "unknown"

    # -- housekeeping -------------------------------------------------------
    def expire_pending(self, max_age: float = 1800.0) -> None:
        now = time.time()
        with self._lock:
            stale = [t for t, p in self._pending.items() if now - p.created_at > max_age]
            for token in stale:
                self._pending.pop(token, None)
