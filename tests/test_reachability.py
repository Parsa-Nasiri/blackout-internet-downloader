"""Reachability probe and API-proxy wiring.

The probe exists because GitHub-hosted runners cannot open a TCP connection to
botapi.rubika.ir at all: the requests library just hung for 3 x 60s and the log
gave no clue. These tests pin the fast, categorised diagnosis instead.
"""

import socket

import pytest

from rubika_dl import config
from rubika_dl import main as main_mod
from rubika_dl.rubika_api import RubikaClient


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    # Without this the probe short-circuits to "reachable" whenever a proxy is
    # configured, and every case below would pass for the wrong reason.
    monkeypatch.setattr(config, "API_PROXY", None)


class _FakeSocket:
    """Records create_connection() calls and raises a scripted OSError."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = []

    def create_connection(self, address, timeout=None):
        self.calls.append((address, timeout))
        raise self.exc

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _patch_socket(monkeypatch, exc):
    fake = _FakeSocket(exc)
    monkeypatch.setattr(socket, "create_connection", fake.create_connection)
    return fake


def test_probe_returns_none_when_socket_opens(monkeypatch):
    monkeypatch.setattr(
        socket, "create_connection", lambda address, timeout=None: _Null()
    )
    assert main_mod._probe_api_host(timeout=1) is None


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_timeout_is_reported_as_timeout(monkeypatch):
    fake = _patch_socket(monkeypatch, TimeoutError())
    kind, detail = main_mod._probe_api_host(timeout=3)
    assert kind == "timeout"
    assert "timed out" in detail
    assert fake.calls[0][1] == 3


def test_socket_timeout_is_also_a_timeout(monkeypatch):
    # socket.timeout is an alias of TimeoutError on 3.10+, but assert the
    # classification does not silently change if that ever diverges.
    _patch_socket(monkeypatch, socket.timeout())
    assert main_mod._probe_api_host(timeout=1)[0] == "timeout"


def test_refused_is_not_blamed_on_the_geo_block(monkeypatch):
    # A RST means the path works and the endpoint is wrong — a different fix.
    _patch_socket(monkeypatch, ConnectionRefusedError())
    kind, detail = main_mod._probe_api_host()
    assert kind == "refused"
    assert "refused" in detail


def test_other_oserror_is_unreachable(monkeypatch):
    _patch_socket(monkeypatch, OSError("network unreachable"))
    kind, detail = main_mod._probe_api_host()
    assert kind == "unreachable"
    assert "network unreachable" in detail


def test_probe_uses_host_and_port_from_api_base(monkeypatch):
    fake = _patch_socket(monkeypatch, TimeoutError())
    monkeypatch.setattr(config, "API_BASE", "https://example.test:8443/v3")
    main_mod._probe_api_host()
    assert fake.calls[0][0] == ("example.test", 8443)


def test_probe_defaults_to_port_443(monkeypatch):
    fake = _patch_socket(monkeypatch, TimeoutError())
    monkeypatch.setattr(config, "API_BASE", "https://botapi.rubika.ir/v3")
    main_mod._probe_api_host()
    assert fake.calls[0][0] == ("botapi.rubika.ir", 443)


def test_base_without_a_host_is_a_config_error(monkeypatch):
    monkeypatch.setattr(config, "API_BASE", "not-a-url")
    kind, detail = main_mod._probe_api_host()
    assert kind == "refused"
    assert "no host" in detail


def test_probe_is_skipped_when_a_proxy_is_configured(monkeypatch):
    # The direct path is irrelevant once the API goes through a proxy; the
    # getMe call is the meaningful test.
    fake = _patch_socket(monkeypatch, TimeoutError())
    monkeypatch.setattr(config, "API_PROXY", "socks5://127.0.0.1:1080")
    assert main_mod._probe_api_host() is None
    assert fake.calls == []


# -- proxy wiring -----------------------------------------------------------
def test_api_proxy_falls_back_to_proxy(monkeypatch):
    monkeypatch.setenv("PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.delenv("RUBIKA_API_PROXY", raising=False)
    assert config._api_proxy() == "socks5://127.0.0.1:1080"


def test_api_proxy_wins_over_proxy(monkeypatch):
    monkeypatch.setenv("PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("RUBIKA_API_PROXY", "http://iran:3128")
    assert config._api_proxy() == "http://iran:3128"


def test_api_proxy_is_none_when_neither_is_set(monkeypatch):
    monkeypatch.delenv("RUBIKA_API_PROXY", raising=False)
    monkeypatch.delenv("PROXY", raising=False)
    assert config._api_proxy() is None


def test_client_defaults_to_configured_api_proxy(monkeypatch):
    monkeypatch.setattr(config, "API_PROXY", "http://iran:3128")
    client = RubikaClient("x" * 20, proxy=None)  # proxy=None -> use config
    assert client._proxy == "http://iran:3128"


def test_explicit_proxy_argument_overrides_config(monkeypatch):
    monkeypatch.setattr(config, "API_PROXY", "http://configured:3128")
    client = RubikaClient("x" * 20, proxy="http://explicit:3128")
    assert client._proxy == "http://explicit:3128"


def test_session_applies_the_proxy(monkeypatch):
    monkeypatch.setattr(config, "API_PROXY", "http://iran:3128")
    client = RubikaClient("x" * 20)
    assert client.session.proxies.get("https") == "http://iran:3128"


def test_session_ignores_the_ambient_system_proxy(monkeypatch):
    monkeypatch.setattr(config, "API_PROXY", None)
    monkeypatch.setenv("HTTP_PROXY", "http://stray:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://stray:3128")
    client = RubikaClient("x" * 20)
    assert client.session.trust_env is False
    assert not client.session.proxies