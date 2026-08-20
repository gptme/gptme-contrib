"""Account profiles for scripts/twitter/twitter.py (``--account gptmeorg``).

A named profile keeps its own OAuth 2.0 user tokens in
``$XDG_CONFIG_HOME/gptwitter/accounts/<name>.env`` and must never inherit the
default account's user-context credentials (that would post as @TimeToBuildBob
while claiming to be the profile).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Reuse the heavy module loader + fixture from the URL-guard tests (tests/ is
# not a package, so load it by path).
_spec = importlib.util.spec_from_file_location(
    "test_twitter_post_url_guard",
    Path(__file__).resolve().parent / "test_twitter_post_url_guard.py",
)
assert _spec and _spec.loader
_guard = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("test_twitter_post_url_guard", _guard)
_spec.loader.exec_module(_guard)
twitter_module = _guard.twitter_module  # fixture


def test_profile_clears_default_user_context(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # Default-account credentials that must NOT leak into the profile.
    monkeypatch.setenv("TWITTER_OAUTH2_ACCESS_TOKEN", "bob-access")
    monkeypatch.setenv("TWITTER_OAUTH2_REFRESH_TOKEN", "bob-refresh")
    monkeypatch.setenv("TWITTER_ACCESS_TOKEN", "bob-1a")
    monkeypatch.setenv("TWITTER_ACCESS_SECRET", "bob-1a-secret")
    monkeypatch.setenv("TWITTER_EXPECTED_USERNAME", "TimeToBuildBob")

    path = twitter_module._activate_account_profile("gptmeorg")

    assert path == tmp_path / "gptwitter" / "accounts" / "gptmeorg.env"
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    import os

    for var in (
        "TWITTER_OAUTH2_ACCESS_TOKEN",
        "TWITTER_OAUTH2_REFRESH_TOKEN",
        "TWITTER_ACCESS_TOKEN",
        "TWITTER_ACCESS_SECRET",
    ):
        assert os.environ.get(var) is None, var
    # Identity guard now expects the profile, not the default account.
    assert os.environ["TWITTER_EXPECTED_USERNAME"] == "gptmeorg"


@pytest.mark.parametrize(
    "name", ["../bob", "bob\nTWITTER_ACCESS_TOKEN=stolen", "", "a" * 16]
)
def test_profile_rejects_invalid_names(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path, name: str
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    with pytest.raises(ValueError, match="Invalid Twitter account profile"):
        twitter_module._activate_account_profile(name)


def test_profile_rejects_symlink(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    target = tmp_path / "target.env"
    target.write_text("do not overwrite\n")
    profile = tmp_path / "gptwitter" / "accounts" / "gptmeorg.env"
    profile.parent.mkdir(parents=True)
    profile.symlink_to(target)

    with pytest.raises(ValueError, match="must be a regular file"):
        twitter_module._activate_account_profile("gptmeorg")
    assert target.read_text() == "do not overwrite\n"


def test_profile_loads_its_own_tokens(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    prof = tmp_path / "gptwitter" / "accounts" / "gptmeorg.env"
    prof.parent.mkdir(parents=True)
    prof.write_text(
        "TWITTER_EXPECTED_USERNAME=gptmeorg\n"
        "TWITTER_OAUTH2_ACCESS_TOKEN=org-access\n"
        "TWITTER_OAUTH2_REFRESH_TOKEN=org-refresh\n"
    )
    monkeypatch.setenv("TWITTER_OAUTH2_ACCESS_TOKEN", "bob-access")

    # The module loader stubs python-dotenv; give this test a minimal real one.
    def _load(path, override=False):
        import os

        for line in Path(path).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                if override or k not in os.environ:
                    os.environ[k] = v

    monkeypatch.setattr(twitter_module, "load_dotenv", _load)

    twitter_module._activate_account_profile("gptmeorg")

    import os

    assert os.environ["TWITTER_OAUTH2_ACCESS_TOKEN"] == "org-access"
    assert os.environ["TWITTER_OAUTH2_REFRESH_TOKEN"] == "org-refresh"


def test_post_quote_passes_quote_tweet_id(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    def fake_create_tweet(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data={"id": "1"})

    monkeypatch.setattr(twitter_module, "validate_urls_in_text", lambda text: [])
    monkeypatch.setattr(
        twitter_module,
        "load_twitter_client",
        lambda require_auth=True: SimpleNamespace(create_tweet=fake_create_tweet),
    )
    monkeypatch.setattr(twitter_module, "_get_user_auth", lambda client: True)

    twitter_module.post("quoting", None, False, quote_id="999")

    assert calls and calls[0]["quote_tweet_id"] == "999"


def test_wait_for_callback_file_parses_code(twitter_module: Any, tmp_path) -> None:
    f = tmp_path / "cb.txt"
    f.write_text("http://localhost:9876/callback?state=x&code=abc123\n")
    code, url = twitter_module._wait_for_callback_file(f, timeout=5)
    assert code == "abc123"
    assert url.startswith("https://localhost:9876/callback")
    assert not f.exists()
