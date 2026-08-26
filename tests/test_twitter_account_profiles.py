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


def test_profile_expected_username_cannot_override_profile_name(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    profile = tmp_path / "gptwitter" / "accounts" / "gptmeorg.env"
    profile.parent.mkdir(parents=True)
    profile.write_text("TWITTER_EXPECTED_USERNAME=TimeToBuildBob\n")

    twitter_module._activate_account_profile("gptmeorg")

    import os

    assert os.environ["TWITTER_EXPECTED_USERNAME"] == "gptmeorg"


def test_existing_profile_permissions_are_tightened(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    profile = tmp_path / "gptwitter" / "accounts" / "gptmeorg.env"
    profile.parent.mkdir(parents=True)
    profile.write_text("TWITTER_EXPECTED_USERNAME=gptmeorg\n")
    profile.chmod(0o644)

    twitter_module._activate_account_profile("gptmeorg")

    assert (profile.stat().st_mode & 0o777) == 0o600


def test_profile_creation_permission_error_raises_clear_error(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A PermissionError from os.open (e.g. unwritable directory) raises ValueError."""
    import os as _os

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    real_os_open = _os.open

    def patched_open(path, flags, mode=0o666, *args, **kwargs):
        if "gptmeorg.env" in str(path) and (_os.O_EXCL & flags):
            raise PermissionError(13, "Permission denied", str(path))
        return real_os_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(twitter_module.os, "open", patched_open)

    with pytest.raises(ValueError, match="not writable"):
        twitter_module._activate_account_profile("gptmeorg")


def test_cli_account_survives_workspace_dotenv_override(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWITTER_ACCOUNT", "gptmeorg")

    def load_workspace_dotenv(*args, **kwargs) -> None:
        monkeypatch.setenv("TWITTER_ACCOUNT", "TimeToBuildBob")

    class ProfileActivated(Exception):
        pass

    def activate_profile(name: str) -> None:
        assert name == "gptmeorg"
        raise ProfileActivated

    monkeypatch.setattr(twitter_module, "load_dotenv", load_workspace_dotenv)
    monkeypatch.setattr(twitter_module, "_activate_account_profile", activate_profile)

    with pytest.raises(ProfileActivated):
        twitter_module.load_twitter_client(require_auth=True)


def test_explicit_default_account_survives_workspace_dotenv_override(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWITTER_ACCOUNT", "")

    def load_workspace_dotenv(*args, **kwargs) -> None:
        monkeypatch.setenv("TWITTER_ACCOUNT", "gptmeorg")

    monkeypatch.setattr(twitter_module, "load_dotenv", load_workspace_dotenv)
    monkeypatch.setattr(
        twitter_module,
        "_activate_account_profile",
        lambda name: pytest.fail(f"unexpected profile activation: {name}"),
    )
    monkeypatch.setattr(
        twitter_module, "console", SimpleNamespace(print=lambda *a, **k: None)
    )

    with pytest.raises(SystemExit):
        twitter_module.load_twitter_client(require_auth=True)

    import os

    assert os.environ["TWITTER_ACCOUNT"] == ""


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
        lambda require_auth=True, headless=False: SimpleNamespace(
            create_tweet=fake_create_tweet
        ),
    )
    monkeypatch.setattr(twitter_module, "_get_user_auth", lambda client: True)

    twitter_module.post("quoting", None, False, quote_id="999")

    assert calls and calls[0]["quote_tweet_id"] == "999"


@pytest.mark.parametrize(
    ("reply_to", "thread"),
    [("123", False), (None, True)],
)
def test_post_rejects_invalid_quote_combinations_before_auth(
    twitter_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    reply_to: str | None,
    thread: bool,
) -> None:
    def fail_if_called(*args, **kwargs):
        raise AssertionError("authentication must not run for invalid arguments")

    monkeypatch.setattr(twitter_module, "load_twitter_client", fail_if_called)

    with pytest.raises(SystemExit):
        twitter_module.post("invalid", reply_to, thread, quote_id="999")


@pytest.mark.parametrize("value", ["30m", "", "0", "-1"])
def test_oauth_callback_timeout_falls_back_on_invalid_config(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("TWITTER_OAUTH_CALLBACK_TIMEOUT", value)
    assert twitter_module._oauth_callback_timeout() == 1800


def test_wait_for_callback_file_parses_code(twitter_module: Any, tmp_path) -> None:
    f = tmp_path / "cb.txt"
    f.write_text("http://localhost:9876/callback?state=x&code=abc123\n")
    code, url = twitter_module._wait_for_callback_file(f, timeout=5)
    assert code == "abc123"
    assert url.startswith("https://localhost:9876/callback")
    # Unlink is now deferred to the caller (after CSRF state validation),
    # so the file should still exist after _wait_for_callback_file returns.
    assert f.exists()


def test_wait_for_callback_file_preserves_file_for_caller_cleanup(
    twitter_module: Any, tmp_path
) -> None:
    """Callback file must survive _wait_for_callback_file so the caller can
    unlink it after CSRF state validation rather than before."""
    f = tmp_path / "cb.txt"
    f.write_text("http://localhost:9876/callback?state=good&code=xyz\n")
    code, url = twitter_module._wait_for_callback_file(f, timeout=5)
    assert code == "xyz"
    # File still present — caller's responsibility to clean up after CSRF check.
    assert f.exists(), (
        "callback file must not be deleted inside _wait_for_callback_file"
    )


def test_wait_for_callback_file_handles_file_disappearing_before_read(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    f = tmp_path / "cb.txt"
    f.write_text("http://localhost:9876/callback?state=x&code=abc123")
    original_read_text = Path.read_text
    reads = 0

    def disappearing_read_text(path: Path, *args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            path.unlink()
            raise FileNotFoundError(path)
        return original_read_text(path, *args, **kwargs)

    monotonic_values = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(Path, "read_text", disappearing_read_text)
    monkeypatch.setattr("time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    assert twitter_module._wait_for_callback_file(f, timeout=1) == (None, None)


def test_wait_for_callback_file_keeps_partial_url(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    f = tmp_path / "cb.txt"
    f.write_text("http://localhost:9876/callback?state=x")
    monotonic_values = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr("time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    assert twitter_module._wait_for_callback_file(f, timeout=1) == (None, None)
    assert f.exists()


def test_post_passes_headless_to_auth(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    class Client:
        def create_tweet(self, **kwargs):
            return type("Response", (), {"data": {"id": "123"}})()

    def fake_load_twitter_client(**kwargs):
        seen.update(kwargs)
        return Client()

    monkeypatch.setattr(twitter_module, "load_twitter_client", fake_load_twitter_client)
    twitter_module.post(
        "hello", reply_to=None, thread=False, quote_id=None, headless=True
    )

    assert seen == {"require_auth": True, "headless": True}


def test_wait_for_callback_file_rejects_non_callback_host(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    f = tmp_path / "cb.txt"
    f.write_text("https://attacker.example/callback?state=x&code=abc123")
    monotonic_values = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr("time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    assert twitter_module._wait_for_callback_file(f, timeout=1) == (None, None)
    assert f.exists()


def test_wait_for_callback_file_rejects_wrong_port(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A localhost URL on the wrong port must not be accepted."""
    f = tmp_path / "cb.txt"
    f.write_text("http://localhost:9999/callback?state=x&code=abc123")
    monotonic_values = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr("time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    assert twitter_module._wait_for_callback_file(f, timeout=1) == (None, None)
    assert f.exists()


def test_wait_for_callback_file_rejects_wrong_path(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A localhost:9876 URL on a path other than /callback must not be accepted."""
    f = tmp_path / "cb.txt"
    f.write_text("http://localhost:9876/evil?state=x&code=abc123")
    monotonic_values = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr("time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    assert twitter_module._wait_for_callback_file(f, timeout=1) == (None, None)
    assert f.exists()
