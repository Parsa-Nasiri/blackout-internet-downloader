"""Configuration validation.

The token check used to assume Telegram's ``bot_id:secret`` shape and reject
any Rubika token without a colon — which is all of them, since Rubika's docs
show a single opaque string ("SUPER_SECRET_TOKEN"). These tests pin the
permissive behaviour that replaced it.
"""

import pytest

from rubika_dl import config


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    yield
    # config values are module globals; monkeypatch undoes setattr for us.


def _problems(monkeypatch, token: str) -> list[str]:
    monkeypatch.setattr(config, "BOT_TOKEN", token)
    return config.validate()


def test_opaque_token_without_a_colon_is_accepted(monkeypatch):
    # This is the regression: a real Rubika token has no colon.
    assert _problems(monkeypatch, "SUPER_SECRET_TOKEN") == []


def test_colon_token_still_accepted(monkeypatch):
    # Some deployments do use a colon form; both must work.
    assert _problems(monkeypatch, "123456:AbCdEfGhIjKlMnOpQrSt") == []


def test_missing_token_is_reported(monkeypatch):
    assert any("not set" in p for p in _problems(monkeypatch, ""))


def test_token_with_a_space_is_rejected(monkeypatch):
    assert any("cannot appear" in p for p in _problems(monkeypatch, "abc def ghijkl"))


def test_token_with_a_slash_is_rejected(monkeypatch):
    assert any("cannot appear" in p for p in _problems(monkeypatch, "abc/def/ghijkl"))


def test_pasted_url_is_rejected(monkeypatch):
    assert any("cannot appear" in p for p in _problems(monkeypatch, "https://botapi.rubika.ir/v3/x"))


def test_short_token_is_rejected(monkeypatch):
    assert _problems(monkeypatch, "abc") != []


def test_valid_token_reports_no_problems(monkeypatch):
    monkeypatch.setattr(config, "API_BASE", "https://botapi.rubika.ir/v3")
    assert _problems(monkeypatch, "AbCdEfGhIjKlMnOp") == []


def test_bad_api_base_is_reported(monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", "AbCdEfGhIjKlMnOp")
    monkeypatch.setattr(config, "API_BASE", "ftp://nope")
    assert any("RUBIKA_API_BASE" in p for p in config.validate())


def test_mask_never_reveals_the_whole_token():
    from rubika_dl.main import _mask

    token = "SUPERSECRETTOKEN123456"
    masked = _mask(token)
    assert token not in masked
    assert "chars" in masked


def test_mask_handles_a_short_token():
    from rubika_dl.main import _mask

    assert _mask("abc") == "***"


def test_surrounding_quotes_are_stripped(monkeypatch):
    # A token pasted as "abc" (common from shell snippets and GitHub secrets)
    # must not carry the quotes into the URL.
    monkeypatch.setenv("RUBIKA_BOT_TOKEN", '"AbCdEfGhIjKlMnOp"')
    assert config._env("RUBIKA_BOT_TOKEN") == "AbCdEfGhIjKlMnOp"


def test_single_quotes_are_stripped(monkeypatch):
    monkeypatch.setenv("RUBIKA_BOT_TOKEN", "'AbCdEfGhIjKlMnOp'")
    assert config._env("RUBIKA_BOT_TOKEN") == "AbCdEfGhIjKlMnOp"


def test_surrounding_whitespace_is_stripped(monkeypatch):
    monkeypatch.setenv("RUBIKA_BOT_TOKEN", "  AbCdEfGhIjKlMnOp\n")
    assert config._env("RUBIKA_BOT_TOKEN") == "AbCdEfGhIjKlMnOp"


def test_inner_quotes_are_left_alone(monkeypatch):
    # Only a matching surrounding pair is stripped.
    monkeypatch.setenv("RUBIKA_BOT_TOKEN", 'ab"cd')
    assert config._env("RUBIKA_BOT_TOKEN") == 'ab"cd'