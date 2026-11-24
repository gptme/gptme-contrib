"""Credential expiry detection and re-auth helpers.

The Claude Code OAuth credential file at ``~/.claude/.credentials.json``
contains an ``expiresAt`` timestamp on the *access token*. That access
token is refreshed automatically via the long-lived refresh token. So
``expiresAt < now`` does **not** mean the slot is broken — it just means
the next API call will trigger a refresh.

We expose the file-based check (fast, no network) and a probe variant
(actually exercises the refresh) so callers can choose precision vs cost.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from gptme_subscription.state import locked_state_file

logger = logging.getLogger(__name__)

CredentialStatus = Literal[
    "valid",  # access token present and not yet expired
    "stale",  # access token expired (refresh will happen on next use)
    "missing",  # credential file not present
    "malformed",  # file present but expected keys missing or invalid
    "unreadable",  # OS-level read error
]


@dataclass
class CredentialInfo:
    sub: str
    path: Path
    status: CredentialStatus
    expires_at: int | None = None  # unix epoch milliseconds
    expires_in_seconds: int | None = None  # negative if already lapsed
    subscription_type: str | None = None
    scopes: list[str] | None = None
    error: str | None = None

    @property
    def needs_reauth_hint(self) -> bool:
        """True for states where the operator should likely re-run /login."""
        return self.status in ("missing", "malformed", "unreadable")

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "sub": self.sub,
            "path": str(self.path),
            "status": self.status,
        }
        if self.expires_at is not None:
            d["expires_at"] = self.expires_at
        if self.expires_in_seconds is not None:
            d["expires_in_seconds"] = self.expires_in_seconds
        if self.subscription_type is not None:
            d["subscription_type"] = self.subscription_type
        if self.scopes is not None:
            d["scopes"] = self.scopes
        if self.error is not None:
            d["error"] = self.error
        return d


def check_credential_file(path: Path, sub: str) -> CredentialInfo:
    """Parse a Claude Code credential file and return its status.

    Does **not** make any network calls. ``status="stale"`` means the
    access token is past its expiry but the refresh token may still be
    valid; use :func:`probe_credential` to confirm.
    """
    if not path.exists():
        return CredentialInfo(sub=sub, path=path, status="missing")

    try:
        raw = path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        return CredentialInfo(sub=sub, path=path, status="unreadable", error=str(exc))

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return CredentialInfo(sub=sub, path=path, status="malformed", error=str(exc))

    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return CredentialInfo(
            sub=sub,
            path=path,
            status="malformed",
            error="missing claudeAiOauth payload",
        )

    expires_at = oauth.get("expiresAt")
    if not isinstance(expires_at, int | float):
        return CredentialInfo(
            sub=sub,
            path=path,
            status="malformed",
            error="missing or non-numeric expiresAt",
        )
    try:
        finite = math.isfinite(expires_at)
    except OverflowError:  # an int too large for a float, e.g. 10**309
        finite = False
    if not finite:
        return CredentialInfo(
            sub=sub,
            path=path,
            status="malformed",
            error="non-finite expiresAt",
        )

    now_ms = int(time.time() * 1000)
    delta_s = int((int(expires_at) - now_ms) / 1000)
    status: CredentialStatus = "valid" if delta_s > 0 else "stale"
    scopes = oauth.get("scopes")
    if not isinstance(scopes, list):
        scopes = None
    sub_type = oauth.get("subscriptionType")
    if not isinstance(sub_type, str):
        sub_type = None

    return CredentialInfo(
        sub=sub,
        path=path,
        status=status,
        expires_at=int(expires_at),
        expires_in_seconds=delta_s,
        subscription_type=sub_type,
        scopes=scopes,
    )


def _slot_account(content: bytes) -> str | None:
    """Return the ``accountUuid`` a credential file's content names, if any."""
    try:
        oauth = json.loads(content.decode("utf-8")).get("claudeAiOauth")
    except (OSError, UnicodeDecodeError, ValueError, AttributeError):
        return None
    if not isinstance(oauth, dict):
        return None
    account = oauth.get("accountUuid")
    return account if isinstance(account, str) and account else None


def _persist_probe_refresh(
    live: Path, slot: Path, expected_account: str | None, original: bytes
) -> None:
    """Copy tokens Claude Code refreshed during a probe back into the slot.

    A refresh replaces the temporary symlink with a regular file holding a
    rotated refresh token. The old one is already consumed, so dropping the
    new file with the temp directory would leave the slot unusable.

    ``expected_account`` is the slot's ``accountUuid`` read before the probe.
    When the slot names an account, a file that names a different account (or
    none) is never written: one slot must not end up holding another
    account's tokens. ``original`` is the slot's content before the probe;
    a slot changed since then (e.g. re-authenticated by the operator, whether
    or not it names an account) is never overwritten.
    """
    if live.is_symlink() or not live.is_file():
        return
    try:
        text = live.read_text(encoding="utf-8")
        oauth = json.loads(text).get("claudeAiOauth")
    except (OSError, UnicodeDecodeError, ValueError, AttributeError):
        return
    if not isinstance(oauth, dict) or not oauth.get("refreshToken"):
        return
    if expected_account is not None and (oauth.get("accountUuid") != expected_account):
        logger.warning(
            "probe refresh for %s names a different account; slot left unchanged",
            slot.name,
        )
        return
    fd, tmp_name = tempfile.mkstemp(prefix=f".{slot.name}.", dir=slot.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp_name, 0o600)
        # Compare as late as possible: the check and the rename are separate
        # syscalls and POSIX has no compare-and-swap rename, so a write that
        # lands between them is still lost. Slot writers outside this package
        # share no lock with it; the check covers the probe's seconds-long
        # window, which is where a re-auth realistically lands.
        try:
            unchanged = slot.read_bytes() == original
        except OSError:
            unchanged = False
        if not unchanged:
            logger.warning(
                "slot %s changed while it was probed; refresh not written", slot.name
            )
            Path(tmp_name).unlink(missing_ok=True)
            return
        os.replace(tmp_name, slot)
    except OSError:
        Path(tmp_name).unlink(missing_ok=True)


def probe_lock_path(slot: Path) -> Path:
    """Path whose ``.lock`` sibling serializes probes of ``slot``."""
    return slot.with_name(f".{slot.name}.probe")


def probe_credential(
    path: Path, sub: str, *, usage_script: Path | None = None, timeout: int = 60
) -> tuple[CredentialInfo, bool, str]:
    """Run a usage-script probe against a credential to confirm it still works.

    Returns ``(info, ok, message)``. ``ok`` is True when the probe
    succeeded (refresh works, account responds). ``ok=False`` means the
    refresh token is likely revoked or the slot needs re-auth — even if
    :func:`check_credential_file` reported ``valid``.

    When ``usage_script`` is None or missing, we skip the probe and
    return ``(info, True, "probe skipped")``.
    """
    info = check_credential_file(path, sub)
    if info.status in ("missing", "malformed", "unreadable"):
        return info, False, f"cannot probe: {info.status}"

    if usage_script is None or not usage_script.exists():
        return info, True, "probe skipped (no usage_script configured)"

    env = os.environ.copy()
    try:
        target_credential = path.expanduser().resolve()
        # Serialize probes of one slot from the read of ``original`` through
        # persistence: two overlapping probes would both compare against the
        # same ``original``, and the later refresh (the only unconsumed token)
        # would be dropped. Hidden name, so slot globs never see the lock.
        probe_lock = probe_lock_path(target_credential)
        with locked_state_file(probe_lock):
            # One read for both: separate reads could describe different
            # credentials if the slot is replaced in between.
            original = target_credential.read_bytes()
            expected_account = _slot_account(original)
            with tempfile.TemporaryDirectory(
                prefix=f"gptme-subscription-probe-{sub}-"
            ) as tmp:
                claude_dir = Path(tmp) / ".claude"
                claude_dir.mkdir(parents=True, exist_ok=True)
                live_credential = claude_dir / ".credentials.json"
                # Make the requested slot appear as Claude's live credential so any
                # normal usage probe reads ``path`` instead of the operator's real
                # ~/.claude symlink.
                live_credential.symlink_to(target_credential)
                env["HOME"] = tmp
                env["CLAUDE_HOME"] = str(claude_dir)
                try:
                    result = subprocess.run(
                        [str(usage_script), "--json", "--no-cache"],
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        env=env,
                    )
                finally:
                    # Also on timeout: a refresh written before a hang would
                    # otherwise be deleted with the temporary directory.
                    _persist_probe_refresh(
                        live_credential, target_credential, expected_account, original
                    )
    except subprocess.TimeoutExpired:
        return info, False, f"probe timed out after {timeout}s"
    except OSError as exc:
        return info, False, f"probe error: {exc}"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip().splitlines()[-1:] or [""]
        return info, False, f"probe failed (rc={result.returncode}): {stderr[0]}"

    return info, True, "probe ok"


REAUTH_INSTRUCTIONS = """\
Re-authenticate a Claude Code slot
==================================

1. Launch Claude Code interactively and run /login:

       claude
       > /login

   This opens a browser, runs the OAuth flow, and writes fresh tokens to
   ~/.claude/.credentials.json. Note: /login (and routine token refresh)
   REPLACES the live symlink with a regular file, so the symlink must be
   restored at the end (step 3) — don't put `ln -sf` before /login, it just
   gets clobbered.

2. Persist the fresh tokens into the named slot:

       cp ~/.claude/.credentials.json ~/.claude/.credentials.json.{sub}

3. Restore the live symlink to the slot (do this LAST):

       ln -sf .credentials.json.{sub} ~/.claude/.credentials.json

4. (Optional, if you use identity drift detection) re-baseline the slot:

       gptme-subscription --baseline-identity {sub}

Common failure modes:
  - "Login failed" / "rate limited":   wait 5 min, retry
  - browser does not open:             use --headless or copy the URL manually
  - slot accepts login but probes 401: another slot is symlinked; restore the
                                       slot symlink (step 3) and re-probe
"""


def format_reauth_instructions(sub: str) -> str:
    """Return human-readable re-auth instructions for a specific slot."""
    return REAUTH_INSTRUCTIONS.replace("{sub}", sub)
