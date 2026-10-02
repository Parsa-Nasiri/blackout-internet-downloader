"""The workflow must run on the automatic GITHUB_TOKEN alone.

No personal access token exists in the workflows any more, so the token
helper has to work from what Actions provides — and fail loudly otherwise,
because a silent failure would lose the hand-off chain.
"""

import pytest

import scripts.state_sync as state_sync


def _clear_tokens(monkeypatch):
    for name in ("GH_PAT", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_uses_the_automatic_github_token(monkeypatch):
    _clear_tokens(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_auto")
    assert state_sync._token() == "ghs_auto"


def test_gh_token_alias_from_the_workflow(monkeypatch):
    _clear_tokens(monkeypatch)
    # bot.yml/watchdog.yml pass the Actions token under both names.
    monkeypatch.setenv("GH_TOKEN", "ghs_alias")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_auto")
    assert state_sync._token() in {"ghs_alias", "ghs_auto"}


def test_legacy_pat_is_not_required(monkeypatch):
    _clear_tokens(monkeypatch)
    # Setting a PAT must not be necessary — and must not take precedence
    # over the token the workflow provides.
    monkeypatch.setenv("GH_PAT", "ghp_legacy")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_auto")
    assert state_sync._token() == "ghs_auto"


def test_missing_token_fails_loudly(monkeypatch):
    _clear_tokens(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        state_sync._token()
    assert "GITHUB_TOKEN" in str(exc.value)