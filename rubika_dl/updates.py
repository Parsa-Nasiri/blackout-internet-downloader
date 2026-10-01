"""Normalise raw Rubika ``Update`` payloads into a small dataclass.

Rubika's ``getUpdates`` returns ``{updates: [...], next_offset_id: ...}``. Each
update is a tagged union keyed by ``type``:

    NewMessage      -> update["new_message"]      (a Message)
    UpdatedMessage  -> update["updated_message"]
    RemovedMessage  -> update["removed_message_id"]
    StartedBot      -> user pressed Start
    StoppedBot
    EventData       -> update["event_data"]       (added/removed from a group)
    UpdatedPayment

See https://rubika.ir/botapi/models#update
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .urls import find_urls


@dataclass
class Attachment:
    file_id: str | None = None
    file_name: str | None = None
    size: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_media(self) -> bool:
        return bool(self.file_id)


@dataclass
class Incoming:
    """A normalised inbound event."""

    update_type: str
    chat_id: str
    message_id: str | None = None
    text: str = ""
    sender_id: str | None = None
    sender_type: str | None = None
    chat_type: str | None = None
    button_id: str | None = None
    start_id: str | None = None
    reply_to_message_id: str | None = None
    attachment: Attachment | None = None
    sticker: dict[str, Any] | None = None
    location: dict[str, Any] | None = None
    contact: dict[str, Any] | None = None
    is_inline: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    # -- derived ------------------------------------------------------------
    @property
    def is_command(self) -> bool:
        return self.text.lstrip().startswith("/")

    @property
    def command(self) -> str:
        """The bare command name, lowercased and without a /command@bot suffix."""
        if not self.is_command:
            return ""
        first = self.text.lstrip().split()[0][1:]
        return first.split("@", 1)[0].lower()

    @property
    def args(self) -> str:
        parts = self.text.lstrip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @property
    def urls(self) -> list[str]:
        return find_urls(self.text)

    @property
    def is_group(self) -> bool:
        return (self.chat_type or "").lower() in {"group", "channel"}

    @property
    def is_private(self) -> bool:
        return not self.is_group

    @property
    def has_media_attachment(self) -> bool:
        return bool(self.attachment and self.attachment.is_media)


def parse_update(update: dict[str, Any]) -> Incoming | None:
    """Convert one raw update into an :class:`Incoming`, or None if unhandled."""
    utype = str(update.get("type") or "")
    chat_id = str(update.get("chat_id") or "")

    if utype == "NewMessage":
        return _from_message(utype, chat_id, update.get("new_message") or {}, update)

    if utype == "UpdatedMessage":
        return _from_message(utype, chat_id, update.get("updated_message") or {}, update)

    if utype == "RemovedMessage":
        return Incoming(
            update_type=utype,
            chat_id=chat_id,
            message_id=_s(update.get("removed_message_id")),
            raw=update,
        )

    if utype in {"StartedBot", "StoppedBot"}:
        aux = update.get("aux_data") or {}
        return Incoming(
            update_type=utype,
            chat_id=chat_id,
            sender_id=_s(aux.get("sender_id") or update.get("sender_id")),
            start_id=_s(aux.get("start_id")),
            text="/start" if utype == "StartedBot" else "/stop",
            raw=update,
        )

    if utype == "EventData":
        event = update.get("event_data") or {}
        return Incoming(
            update_type=utype,
            chat_id=chat_id,
            sender_id=_s(event.get("user_id")),
            text=str(event.get("type") or ""),
            raw=update,
        )

    # Inline-keypad click: not wrapped in the standard Update envelope.
    if "inline_message" in update:
        return _from_inline(update.get("inline_message") or {}, update)

    return None


def parse_inline_message(payload: dict[str, Any]) -> Incoming:
    """Public entry point for a webhook ``receiveInlineMessage`` payload."""
    return _from_inline(payload.get("inline_message") or payload, payload)


def _from_message(utype: str, chat_id: str, message: dict[str, Any],
                  raw: dict[str, Any]) -> Incoming:
    aux = message.get("aux_data") or {}
    file_obj = message.get("file") or {}
    attachment = None
    if file_obj:
        attachment = Attachment(
            file_id=_s(file_obj.get("file_id")),
            file_name=_s(file_obj.get("file_name")),
            size=_int(file_obj.get("size")),
            raw=file_obj,
        )

    return Incoming(
        update_type=utype,
        chat_id=chat_id or _s(raw.get("chat_id")),
        message_id=_s(message.get("message_id")),
        text=message.get("text") or "",
        sender_id=_s(message.get("sender_id")),
        sender_type=_s(message.get("sender_type")),
        chat_type=_s(message.get("chat_type")),
        button_id=_s(aux.get("button_id")),
        start_id=_s(aux.get("start_id")),
        reply_to_message_id=_s(message.get("reply_to_message_id")),
        attachment=attachment,
        sticker=message.get("sticker"),
        location=message.get("location"),
        contact=message.get("contact_message"),
        raw=raw,
    )


def _from_inline(message: dict[str, Any], raw: dict[str, Any]) -> Incoming:
    aux = message.get("aux_data") or {}
    file_obj = message.get("file") or {}
    attachment = None
    if file_obj:
        attachment = Attachment(
            file_id=_s(file_obj.get("file_id")),
            file_name=_s(file_obj.get("file_name")),
            size=_int(file_obj.get("size")),
            raw=file_obj,
        )
    return Incoming(
        update_type="InlineMessage",
        chat_id=_s(message.get("chat_id")),
        message_id=_s(message.get("message_id")),
        text=message.get("text") or "",
        sender_id=_s(message.get("sender_id")),
        sender_type="User",
        button_id=_s(aux.get("button_id")),
        start_id=_s(aux.get("start_id")),
        attachment=attachment,
        location=message.get("location"),
        is_inline=True,
        raw=raw,
    )


def _s(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
