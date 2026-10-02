"""Access control.

The bot ships open: anyone who starts it can use it. The allow-list is opt-in,
so these tests pin down both the default and the restriction.
"""

import pytest

from rubika_dl import config
from rubika_dl.commands import Handlers
from rubika_dl.updates import Incoming


def _incoming(user_id: str, *, chat_id: str = "1", chat_type: str = "User") -> Incoming:
    return Incoming(
        update_type="NewMessage",
        chat_id=chat_id,
        chat_type=chat_type,
        message_id="1",
        sender_id=user_id,
        text="https://example.com",
    )


@pytest.fixture
def handlers() -> Handlers:
    return Handlers(client=object(), queue=object())  # only access control is exercised


@pytest.fixture(autouse=True)
def _clean_lists(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USERS", [])
    monkeypatch.setattr(config, "BLOCKED_USERS", [])
    monkeypatch.setattr(config, "ADMIN_IDS", [])
    monkeypatch.setattr(config, "ALLOW_GROUPS", True)


def test_anyone_is_allowed_by_default(handlers):
    # The whole point: no configuration means the bot is open.
    assert handlers._is_allowed(_incoming("999999")) is True
    assert handlers._is_allowed(_incoming("1")) is True


def test_allow_list_restricts_when_set(handlers, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USERS", ["111", "222"])
    assert handlers._is_allowed(_incoming("111")) is True
    assert handlers._is_allowed(_incoming("333")) is False


def test_admin_always_passes_the_allow_list(handlers, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USERS", ["111"])
    monkeypatch.setattr(config, "ADMIN_IDS", ["777"])
    assert handlers._is_allowed(_incoming("777")) is True


def test_block_list_beats_everything(handlers, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USERS", ["111"])
    monkeypatch.setattr(config, "ADMIN_IDS", ["111"])
    monkeypatch.setattr(config, "BLOCKED_USERS", ["111"])
    assert handlers._is_allowed(_incoming("111")) is False


def test_groups_can_be_disabled(handlers, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_GROUPS", False)
    assert handlers._is_allowed(_incoming("1", chat_id="-100", chat_type="Group")) is False
    # Private chats are unaffected by the group switch.
    assert handlers._is_allowed(_incoming("1", chat_type="User")) is True


def test_missing_sender_id_is_not_crashy(handlers):
    # A malformed update without a sender must not raise in access control.
    assert handlers._is_allowed(_incoming(None)) is True


def test_a_stranger_starting_the_bot_gets_a_reply():
    """End-to-end: the default configuration must actually serve anyone."""
    sent: list[str] = []

    class CapturingClient:
        def send_message(self, chat_id, text, **kwargs):
            sent.append(text)
            return "1"

    handlers = Handlers(client=CapturingClient(), queue=object())
    handlers.handle(Incoming(
        update_type="StartedBot",
        chat_id="999999",
        chat_type="User",
        message_id="1",
        sender_id="999999",
        text="/start",
    ))
    assert sent, "a stranger's /start was silently dropped"
    assert "BlackOut Downloader" in sent[0]


def test_a_stranger_gets_help_for_an_unknown_command():
    sent: list[str] = []

    class CapturingClient:
        def send_message(self, chat_id, text, **kwargs):
            sent.append(text)
            return "1"

    handlers = Handlers(client=CapturingClient(), queue=object())
    handlers.handle(Incoming(
        update_type="NewMessage",
        chat_id="999999",
        chat_type="User",
        message_id="2",
        sender_id="999999",
        text="/frobnicate",
    ))
    assert sent and "Unknown command" in sent[0]