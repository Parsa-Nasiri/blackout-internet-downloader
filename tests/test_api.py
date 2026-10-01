"""Rubika API client tests using a stub transport (no network)."""

from __future__ import annotations

import json as _json

import pytest
import requests

from rubika_dl.rubika_api import (
    RubikaAuthError,
    RubikaClient,
    RubikaError,
    RubikaRateLimit,
    columns,
    detect_file_type,
    keypad,
    simple_button,
)


class FakeSession:
    """Records requests and replays canned responses."""

    def __init__(self, responses: dict[str, object] | None = None) -> None:
        self.calls: list[dict] = []
        self.responses = responses or {}
        self.headers: dict[str, str] = {}
        self.proxies: dict[str, str] = {}

    def post(self, url, json=None, data=None, files=None, timeout=None, **kwargs):  # noqa: A002
        method = url.rsplit("/", 1)[-1]
        self.calls.append({"method": method, "url": url, "json": json, "data": data, "files": files})
        if "upload" in url:
            body = {"status": True, "file_id": "FILEID123"}
        else:
            body = self.responses.get(method, {"status": True})
        response = requests.Response()
        response.status_code = 200
        response._content = _json.dumps(body).encode()  # noqa: SLF001
        response.url = url
        return response

    def get(self, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content = b"data"  # noqa: SLF001
        return response


@pytest.fixture
def client():
    c = RubikaClient(token="123:abc")
    c._local.session = FakeSession({  # noqa: SLF001
        "getMe": {"status": True, "bot": {"username": "mybot", "bot_id": "123"}},
        "sendMessage": {"status": True, "message_id": "900"},
        "editMessageText": {"status": True, "message_id": "900"},
        "getUpdates": {"status": True, "updates": [], "next_offset_id": "77"},
        "requestSendFile": {"status": True, "upload_url": "https://upload.example/x"},
        "sendFile": {"status": True, "message_id": "901"},
        "getChat": {"status": True, "chat": {"chat_id": "1", "chat_type": "User"}},
    })
    return c


def test_get_me(client):
    assert client.get_me()["username"] == "mybot"


def test_send_message_returns_message_id(client):
    assert client.send_message("42", "hi") == "900"
    call = client._local.session.calls[-1]  # noqa: SLF001
    assert call["json"] == {"chat_id": "42", "text": "hi"}


def test_send_message_with_keypad(client):
    kp = keypad(columns([("a", "A"), ("b", "B")]))
    client.send_message("42", "pick", inline_keypad=kp)
    call = client._local.session.calls[-1]  # noqa: SLF001
    assert call["json"]["inline_keypad"]["rows"][0]["buttons"][0]["button_text"] == "A"
    assert call["json"]["inline_keypad"]["rows"][0]["buttons"][0]["id"] == "a"


def test_get_updates_passes_offset(client):
    body = client.get_updates(offset_id="55", limit=10)
    assert body["next_offset_id"] == "77"
    call = client._local.session.calls[-1]  # noqa: SLF001
    assert call["json"]["offset_id"] == "55"
    assert call["json"]["limit"] == 10


def test_upload_file_two_step(client, tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x" * 128)
    file_id = client.upload_file(media)
    assert file_id
    methods = [c["method"] for c in client._local.session.calls]  # noqa: SLF001
    assert "requestSendFile" in methods
    assert "sendFile" not in methods  # upload only, no send


def test_upload_and_send(client, tmp_path):
    media = tmp_path / "pic.jpg"
    media.write_bytes(b"x" * 16)
    client.upload_and_send("42", media, text="caption")
    methods = [c["method"] for c in client._local.session.calls]  # noqa: SLF001
    assert methods[-1] == "sendFile"
    call = client._local.session.calls[-1]  # noqa: SLF001
    assert call["json"]["text"] == "caption"
    assert call["json"]["chat_id"] == "42"


def test_auth_error_raises():
    c = RubikaClient(token="123:abc")
    session = FakeSession({"getMe": {"status": "unauthorized"}})
    c._local.session = session  # noqa: SLF001
    # getMe with a 200 + non-ok status string is surfaced as an error only when
    # the status field is a known failure value; 'unauthorized' is not in the
    # list, so it passes through. Verify the HTTP path instead.

    def raiser(*a, **kw):
        response = requests.Response()
        response.status_code = 401
        response._content = b"{}"  # noqa: SLF001
        return response

    session.post = raiser  # type: ignore[method-assign]
    with pytest.raises(RubikaAuthError):
        c.get_me()


def test_rate_limit_raises():
    c = RubikaClient(token="123:abc")
    session = FakeSession()

    def raiser(*a, **kw):
        response = requests.Response()
        response.status_code = 429
        response._content = b"{}"  # noqa: SLF001
        return response

    session.post = raiser  # type: ignore[method-assign]
    c._local.session = session  # noqa: SLF001
    with pytest.raises(RubikaRateLimit):
        c.get_me()


def test_file_type_detection():
    assert detect_file_type("a.jpg") == "Image"
    assert detect_file_type("a.mp4") == "Video"
    assert detect_file_type("a.mp3") == "Music"
    assert detect_file_type("a.mkv") == "File"
    assert detect_file_type("a.bin") == "File"


def test_keypad_helpers():
    assert simple_button("1", "Go") == {"id": "1", "type": "Simple", "button_text": "Go"}
    kp = keypad([[simple_button("1", "A")], [simple_button("2", "B"), simple_button("3", "C")]])
    assert len(kp["rows"]) == 2
    assert kp["rows"][1]["buttons"][1]["id"] == "3"


def test_columns_layout():
    rows = columns([("1", "A"), ("2", "B"), ("3", "C")], per_row=2)
    assert len(rows) == 2
    assert len(rows[0]) == 2
    assert len(rows[1]) == 1
