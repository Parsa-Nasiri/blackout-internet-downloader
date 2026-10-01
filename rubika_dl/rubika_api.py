"""A dependency-light client for the Rubika Bot API (v3).

Reference: https://rubika.ir/botapi/methods  ·  https://rubika.ir/botapi/models

Design notes
------------
* Every method is ``POST https://botapi.rubika.ir/v3/{token}/{method}``.
* JSON bodies are used throughout (the API also accepts form-data).
* Uploads are a two-step dance:
    1. ``requestSendFile(type=...)`` -> ``{upload_url, ...}``
    2. ``POST upload_url`` with multipart field ``file`` -> ``{file_id}``
    3. ``sendFile(chat_id, file_id, text=...)``
"""

from __future__ import annotations

import mimetypes
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import requests

from . import config
from .log import get_logger

log = get_logger("api")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class RubikaError(Exception):
    """Any non-success response from the Rubika API."""

    def __init__(self, method: str, message: str, *, status: str | None = None) -> None:
        self.method = method
        self.status = status
        super().__init__(f"{method}: {message}")


class RubikaAuthError(RubikaError):
    pass


class RubikaRateLimit(RubikaError):
    pass


# --------------------------------------------------------------------------
# File-type detection (Rubika FileTypeEnum)
# --------------------------------------------------------------------------
def detect_file_type(path: str | Path) -> str:
    """Map a local file to a Rubika FileTypeEnum value.

    Rubika is strict about containers:
      * ``Image`` accepts jpg / gif / png / webp
      * ``Video`` accepts mp4 only
      * ``Music``  accepts mp3
    Anything else must go as a plain ``File`` (50 MiB cap).
    """
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return "Image"
    if suffix == ".mp4":
        return "Video"
    if suffix == ".mp3":
        return "Music"
    return "File"


# --------------------------------------------------------------------------
# Keypad builders
# --------------------------------------------------------------------------
def simple_button(button_id: str, text: str) -> dict[str, Any]:
    return {"id": str(button_id), "type": "Simple", "button_text": text}


def link_button(text: str, url: str) -> dict[str, Any]:
    # A Link button opens a URL; the API still requires an id.
    return {"id": f"url:{abs(hash(url)) % 10**10}", "type": "Link",
            "button_text": text, "button_link": {"url": url}}


def keypad(rows: Iterable[Iterable[dict[str, Any]]], *, resize: bool = True,
           one_time: bool = False) -> dict[str, Any]:
    """Build a Keypad from a list of rows of buttons."""
    return {
        "rows": [{"buttons": list(row)} for row in rows],
        "resize_keyboard": resize,
        "one_time_keyboard": one_time,
    }


def columns(items: list[tuple[str, str]], per_row: int = 2) -> list[list[dict[str, Any]]]:
    """Lay out (id, text) pairs into rows of buttons."""
    rows: list[list[dict[str, Any]]] = []
    for i in range(0, len(items), per_row):
        rows.append([simple_button(i, text) for i, text in items[i:i + per_row]])
    return rows


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------
class RubikaClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        base: str | None = None,
        timeout: float = 60.0,
        proxy: str | None = None,
    ) -> None:
        self.token = token or config.BOT_TOKEN
        if not self.token:
            raise RubikaAuthError("client", "RUBIKA_BOT_TOKEN is not configured")
        self.base = (base or config.API_BASE).rstrip("/")
        self.timeout = timeout
        self._local = threading.local()
        self._proxy = proxy if proxy is not None else config.PROXY
        self._upload_timeout = 60.0 * 30  # large files take a while

    # -- http plumbing ------------------------------------------------------
    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": "BlackOutDownloader/1.0"})
            # Only use a proxy the operator explicitly configured. Ignoring the
            # ambient system proxy keeps behaviour identical on a dev box, a VPS
            # and a GitHub runner (a stray system proxy otherwise hangs requests).
            session.trust_env = False
            if self._proxy:
                session.proxies.update({"http": self._proxy, "https": self._proxy})
            self._local.session = session
        return session

    def _url(self, method: str) -> str:
        return f"{self.base}/{self.token}/{method}"

    def call(self, method: str, payload: dict[str, Any] | None = None,
             *, files: dict[str, Any] | None = None,
             timeout: float | None = None, retries: int = 3) -> dict[str, Any]:
        """POST *method* and return the parsed JSON body."""
        url = self._url(method)
        last_exc: Exception | None = None

        for attempt in range(1, retries + 1):
            try:
                if files:
                    response = self.session.post(
                        url, data=payload or {}, files=files,
                        timeout=timeout or self._upload_timeout,
                    )
                else:
                    response = self.session.post(
                        url, json=payload or {}, timeout=timeout or self.timeout,
                    )
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("%s: network error (attempt %d/%d): %s", method, attempt, retries, exc)
                time.sleep(min(2 ** attempt, 10))
                continue

            body = _safe_json(response)

            if response.status_code >= 500:
                last_exc = RubikaError(method, f"server error {response.status_code}", status=body.get("status"))
                time.sleep(min(2 ** attempt, 10))
                continue

            if response.status_code == 429:
                raise RubikaRateLimit(method, "rate limited", status=body.get("status"))

            if response.status_code in (401, 403):
                raise RubikaAuthError(method, "unauthorized — check RUBIKA_BOT_TOKEN")

            # Rubika reports failures in the body with a non-OK `status` string
            # (e.g. "INVALID_INPUT", "ACCESS_DENIED") and HTTP 200.
            if not _is_success(body):
                raise RubikaError(method, _describe_failure(body), status=str(body.get("status")))

            return body

        if isinstance(last_exc, RubikaError):
            raise last_exc
        raise RubikaError(method, f"request failed: {last_exc}")

    # -- bot info -----------------------------------------------------------
    def get_me(self) -> dict[str, Any]:
        return self.call("getMe").get("bot", {})

    def set_commands(self, commands: list[tuple[str, str]]) -> dict[str, Any]:
        payload = {
            "bot_commands": [
                {"command": cmd, "description": desc} for cmd, desc in commands
            ]
        }
        return self.call("setCommands", payload)

    # -- updates ------------------------------------------------------------
    def get_updates(self, offset_id: str | int | None = None, limit: int = 50,
                    *, timeout: float | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"limit": int(limit)}
        if offset_id:
            payload["offset_id"] = str(offset_id)
        return self.call("getUpdates", payload, timeout=timeout or config.POLL_TIMEOUT + 15)

    # -- messages -----------------------------------------------------------
    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        chat_keypad: dict[str, Any] | None = None,
        inline_keypad: dict[str, Any] | None = None,
        reply_to_message_id: str | None = None,
        chat_keypad_type: str | None = None,
        disable_notification: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        payload: dict[str, Any] = {"chat_id": str(chat_id), "text": text}
        if chat_keypad:
            payload["chat_keypad"] = chat_keypad
        if inline_keypad:
            payload["inline_keypad"] = inline_keypad
        if reply_to_message_id:
            payload["reply_to_message_id"] = str(reply_to_message_id)
        if chat_keypad_type:
            payload["chat_keypad_type"] = chat_keypad_type
        if disable_notification:
            payload["disable_notification"] = True
        if metadata:
            payload["metadata"] = metadata
        body = self.call("sendMessage", payload)
        return _message_id(body)

    def edit_message_text(self, chat_id: str, message_id: str, text: str) -> dict[str, Any]:
        return self.call("editMessageText", {
            "chat_id": str(chat_id), "message_id": str(message_id), "text": text,
        })

    def edit_message_keypad(self, chat_id: str, message_id: str,
                            inline_keypad: dict[str, Any] | None) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": str(chat_id), "message_id": str(message_id)}
        if inline_keypad:
            payload["inline_keypad"] = inline_keypad
        return self.call("editInlineKeypad", payload)

    def delete_message(self, chat_id: str, message_id: str) -> dict[str, Any]:
        return self.call("deleteMessage", {"chat_id": str(chat_id), "message_id": str(message_id)})

    def forward_message(self, from_chat_id: str, message_id: str, to_chat_id: str) -> dict[str, Any]:
        return self.call("forwardMessage", {
            "from_chat_id": str(from_chat_id),
            "message_id": str(message_id),
            "to_chat_id": str(to_chat_id),
        })

    def get_chat(self, chat_id: str) -> dict[str, Any]:
        return self.call("getChat", {"chat_id": str(chat_id)}).get("chat", {})

    # -- files --------------------------------------------------------------
    def request_send_file(self, file_type: str) -> dict[str, Any]:
        """Ask Rubika for an upload URL. Returns {upload_url, ...}."""
        return self.call("requestSendFile", {"type": file_type})

    def upload_file(self, path: str | Path, file_type: str | None = None,
                    *, on_progress: Any = None) -> str:
        """Upload a local file and return its ``file_id``."""
        path = Path(path)
        if not path.exists():
            raise RubikaError("requestSendFile", f"file not found: {path}")

        ftype = file_type or detect_file_type(path)
        req = self.request_send_file(ftype)
        upload_url = req.get("upload_url")
        if not upload_url:
            raise RubikaError("requestSendFile", f"no upload_url in response: {req}")

        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as handle:
            response = self.session.post(
                upload_url,
                files={"file": (path.name, handle, mime)},
                timeout=self._upload_timeout,
            )

        body = _safe_json(response)
        if response.status_code >= 400:
            raise RubikaError("upload", f"upload failed ({response.status_code})", status=str(response.status_code))
        if not _is_success(body):
            raise RubikaError("upload", _describe_failure(body), status=str(body.get("status")))

        file_id = body.get("file_id")
        if not file_id and isinstance(body.get("data"), dict):
            file_id = body["data"].get("file_id")
        if not file_id:
            raise RubikaError("upload", f"no file_id in upload response: {body}")
        log.info("Uploaded %s (%s) -> %s", path.name, ftype, str(file_id)[:16])
        return str(file_id)

    def send_file(
        self,
        chat_id: str,
        file_id: str,
        *,
        text: str | None = None,
        reply_to_message_id: str | None = None,
        inline_keypad: dict[str, Any] | None = None,
        chat_keypad: dict[str, Any] | None = None,
        disable_notification: bool = False,
    ) -> str | None:
        payload: dict[str, Any] = {"chat_id": str(chat_id), "file_id": str(file_id)}
        if text:
            payload["text"] = text
        if reply_to_message_id:
            payload["reply_to_message_id"] = str(reply_to_message_id)
        if inline_keypad:
            payload["inline_keypad"] = inline_keypad
        if chat_keypad:
            payload["chat_keypad"] = chat_keypad
        if disable_notification:
            payload["disable_notification"] = True
        return _message_id(self.call("sendFile", payload))

    def upload_and_send(self, chat_id: str, path: str | Path, **kwargs: Any) -> str | None:
        """Convenience: upload *path* then send it to *chat_id*."""
        file_id = self.upload_file(path, kwargs.pop("file_type", None))
        return self.send_file(chat_id, file_id, **kwargs)

    # -- misc ---------------------------------------------------------------
    def ban_chat_member(self, chat_id: str, user_id: str) -> dict[str, Any]:
        return self.call("banChatMember", {"chat_id": str(chat_id), "user_id": str(user_id)})

    def unban_chat_member(self, chat_id: str, user_id: str) -> dict[str, Any]:
        return self.call("unbanChatMember", {"chat_id": str(chat_id), "user_id": str(user_id)})

    def download_file(self, file_id: str, dest: str | Path) -> Path:
        """Fetch a file the user sent us (getFile -> download_url)."""
        info = self.call("getFile", {"file_id": str(file_id)})
        url = info.get("download_url")
        if not url:
            raise RubikaError("getFile", f"no download_url: {info}")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.session.get(url, stream=True, timeout=self._upload_timeout) as response:
            response.raise_for_status()
            with dest.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    handle.write(chunk)
        return dest


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _safe_json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"data": data}
    except ValueError:
        return {"status": "error", "raw": response.text[:500]}


# Statuses that mean "this call failed". Rubika returns HTTP 200 with one of
# these in the body; a successful call has `status: true` or no `status` at all.
_FAILURE_STATUSES = {
    "INVALID_INPUT", "ACCESS_DENIED", "USER_BANNED", "BOT_BANNED",
    "NOT_FOUND", "METHOD_NOT_FOUND", "TOO_MANY_REQUESTS", "ERROR",
    "INVALID_TOKEN", "FILE_TOO_LARGE", "FORBIDDEN", "UNAUTHORIZED",
}


def _is_success(body: dict[str, Any]) -> bool:
    status = body.get("status")
    if status is None:
        # getMe with a valid token returns the bot object and no status field.
        return True
    if status is True:
        return True
    if isinstance(status, bool):
        return status
    return str(status).strip().upper() not in _FAILURE_STATUSES


def _describe_failure(body: dict[str, Any]) -> str:
    status = str(body.get("status") or "unknown")
    if status.upper() == "INVALID_INPUT":
        return "the API rejected the request parameters (check the token and ids)"
    if status.upper() in {"ACCESS_DENIED", "USER_BANNED", "FORBIDDEN", "UNAUTHORIZED"}:
        return "access denied by Rubika (check the bot's permissions)"
    if status.upper() == "FILE_TOO_LARGE":
        return "Rubika rejected the file as too large"
    detail = body.get("message") or body.get("data") or body.get("raw")
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("error")
    return f"{status}{f': {str(detail)[:200]}' if detail else ''}"


def _message_id(body: dict[str, Any]) -> str | None:
    for key in ("message_id", "new_message_id"):
        if body.get(key):
            return str(body[key])
    # Some responses nest it.
    for key in ("message", "data", "result"):
        nested = body.get(key)
        if isinstance(nested, dict):
            found = _message_id(nested)
            if found:
                return found
    return None


def probe_token(token: str | None = None) -> dict[str, Any]:
    """Validate a token by calling getMe (used by the CLI)."""
    client = RubikaClient(token)
    return client.get_me()
