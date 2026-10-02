"""Rubika returns HTTP 200 with a failure status string — verify we detect it."""

import pytest
import requests

from rubika_dl.rubika_api import (
    RubikaClient,
    RubikaError,
    _describe_failure,
    _is_success,
)


class StatusSession:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code

    def post(self, url, **kwargs):
        import json as _json

        r = requests.Response()
        r.status_code = self.status_code
        r._content = _json.dumps(self.body).encode()  # noqa: SLF001
        r.url = url
        return r

    headers: dict = {}
    proxies: dict = {}


def _client_with(body, status_code=200):
    c = RubikaClient(token="1:x")
    c._local.session = StatusSession(body, status_code)  # noqa: SLF001
    return c


@pytest.mark.parametrize("status", ["INVALID_INPUT", "ACCESS_DENIED", "FILE_TOO_LARGE", "NOT_FOUND"])
def test_failure_statuses_are_detected(status):
    assert not _is_success({"status": status})


def test_invalid_access_is_a_failure():
    # Live-observed: a bogus token gets {"status": "INVALID_ACCESS",
    # "dev_message": "token is not valid."}. The old failure-allowlist did not
    # contain it, so check happily reported fake tokens as authenticated.
    assert not _is_success({"status": "INVALID_ACCESS", "dev_message": "token is not valid."})


def test_unrecognised_status_fails_closed():
    # Rubika can add new failure codes without telling us; an unknown string
    # status must never be assumed successful.
    assert not _is_success({"status": "SOME_NEW_ERROR"})


def test_non_json_error_body_is_a_failure():
    # _safe_json produces this for an HTML/text response; it must not pass.
    assert not _is_success({"status": "error", "raw": "<html>502</html>"})


def test_invalid_access_message_points_at_the_token():
    c = _client_with({"status": "INVALID_ACCESS", "dev_message": "token is not valid."})
    with pytest.raises(RubikaError) as exc:
        c.get_me()
    assert "not valid" in str(exc.value)


@pytest.mark.parametrize("body", [{"status": True}, {"message_id": "1"}, {}])
def test_success_bodies_pass(body):
    assert _is_success(body)


def test_invalid_input_raises_with_helpful_message():
    c = _client_with({"status": "INVALID_INPUT"})
    with pytest.raises(RubikaError) as exc:
        c.send_message("1", "hi")
    assert "rejected the request parameters" in str(exc.value)


def test_file_too_large_is_reported_clearly():
    c = _client_with({"status": "FILE_TOO_LARGE"})
    with pytest.raises(RubikaError) as exc:
        c.send_file("1", "fid")
    assert "too large" in str(exc.value)


def test_describe_failure_includes_detail():
    text = _describe_failure({"status": "ERROR", "message": "something broke"})
    assert "something broke" in text


def test_describe_failure_without_detail():
    assert _describe_failure({"status": "ACCESS_DENIED"}) == (
        "access denied by Rubika (check the bot's permissions)"
    )


def test_get_me_with_bot_object_and_no_status_is_ok():
    c = _client_with({"bot": {"username": "x"}})
    assert c.get_me()["username"] == "x"
