"""Worktree occupancy marker and push-time branch-claim guard (warn mode).

Ported from Bob's brain repo (ErikBjare/bob) so concurrent-agent worktree
protection travels with the gptme agent template. Context: ErikBjare/alice#83
— two concurrent Claude sessions shared one working tree and uncommitted work
was swept into another session's commit.

Two guards:

- ``run_guard`` (post-commit hook): worktree occupancy marker. Self-scoped to
  ``/tmp/worktrees/**``. Phase 1 warns, never blocks; the deny path emits
  ``would_deny`` to the ledger.
- ``run_push_guard`` (pre-push hook): auto-claims pushed branches as
  ``pr-branch:org/repo#branch`` coordination keys and warns (or, with
  ``BOB_WORKTREE_PUSH_GUARD_DENY=1``, blocks) when a live sibling holds the
  claim — preventing force-pushes over convergent sibling work.

All outcomes are logged to ``state/coordination/worktree-guard.jsonl`` in the
workspace root. The guard fails open on every infrastructure error:
availability beats enforcement.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MARKER_NAME = "bob-session-lock"
LEDGER_REL = Path("state/coordination/worktree-guard.jsonl")
_WORKTREE_PREFIX = "/tmp/worktrees/"
_ZERO_SHA = "0" * 40
UTC = timezone.utc  # datetime.UTC was added in Python 3.11.


# ---------------------------------------------------------------------------
# Environment / identity helpers
# ---------------------------------------------------------------------------


def _get_session_id() -> str | None:
    """Resolve session ID from the standard env chain."""
    for var in ("GIT_COMMITTER_SESSION_ID", "BOB_SESSION_ID", "CC_SESSION_ID"):
        if val := os.environ.get(var):
            return val
    return None


def _get_session_pid() -> int:
    """Best-effort session PID for recording in the marker.

    ``gptme-runloops`` exports the long-lived dispatcher PID as
    ``BOB_SESSION_PID`` for every backend. Otherwise we fall back to the parent
    PID (the git process), which is only a fail-open hook-window signal for
    callers outside that launcher contract.
    """
    raw = os.environ.get("BOB_SESSION_PID", "")
    if raw.isdigit():
        return int(raw)
    return os.getppid()


def _get_agent_id() -> str:
    """Autonomous agent ID for sentinel-based liveness fallback."""
    if agent_id := os.environ.get("BOB_AUTONOMOUS_AGENT_ID"):
        return agent_id
    if session_id := _get_session_id():
        harness = os.environ.get("BOB_AMBIENT_HARNESS", "agent")
        return f"bob-autonomous-{harness}-{session_id}"
    return ""


def _get_marker_agent_id() -> str:
    """Authoritative agent id for the occupancy marker (env-provided only).

    ``_get_agent_id`` synthesizes an id from any session id so push-claim
    identity always exists, but a synthesized id cannot be liveness-probed.
    Recording it in the occupancy marker would make a dead holder look
    permanently alive, so takeover could never happen. The marker therefore
    stores only an env-provided id; an empty value means the PID is the sole
    liveness signal, and a dead PID is treated as a dead holder.
    """
    return os.environ.get("BOB_AUTONOMOUS_AGENT_ID", "")


_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str) -> bool:
    """Parse a documented ``=1`` boolean env flag.

    Plain truthiness treats ``"0"`` (and ``"false"``) as enabled, so a
    conventional ``=0`` configuration would switch the guard *on*. Only an
    explicit affirmative value enables the flag.
    """
    return os.environ.get(name, "").strip().lower() in _TRUE_ENV_VALUES


def _get_git_dir() -> Path | None:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return Path(result.stdout.strip()).resolve()
    except Exception:
        return None


def _get_worktree_root() -> Path | None:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return Path(result.stdout.strip()).resolve()
    except Exception:
        return None


def _get_brain_root() -> Path:
    """Resolve the agent workspace root for ledger writes.

    Honor the standard workspace variables before the legacy brain-root alias,
    then fall back to the home-directory convention (``/home/<user>/<user>``).
    This matches ``resolve_coordination_db_path`` so the ledger and coordination
    claims live under the same root.
    """
    for var in ("BOB_WORKSPACE", "AGENT_WORKSPACE", "BOB_BRAIN_ROOT"):
        if workspace := os.environ.get(var):
            path = Path(workspace)
            if path.is_absolute():
                return path
    home = Path.home()
    candidate = home / home.name  # /home/bob → /home/bob/bob
    if candidate.is_dir():
        return candidate
    return home


# ---------------------------------------------------------------------------
# Marker I/O
# ---------------------------------------------------------------------------


def read_marker(git_dir: Path) -> dict[str, Any] | None:
    """Read the occupancy marker, returning None on absence or parse error."""
    marker = git_dir / MARKER_NAME
    try:
        return json.loads(marker.read_text())  # type: ignore[no-any-return]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def write_marker(
    git_dir: Path,
    session_id: str,
    pid: int,
    agent_id: str = "",
    *,
    takeovers: list[dict[str, str]] | None = None,
    started_at: str | None = None,
) -> None:
    """Overwrite the occupancy marker atomically (tmpfile + rename)."""
    data: dict[str, Any] = {
        "session_id": session_id,
        "pid": pid,
        "agent_id": agent_id,
        "started_at": started_at or datetime.now(UTC).isoformat(),
        "takeovers": takeovers or [],
    }
    marker = git_dir / MARKER_NAME
    # PID-scoped tmp avoids concurrent write_marker() calls racing on the same file.
    tmp = marker.parent / f"{MARKER_NAME}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data))
    tmp.rename(marker)  # POSIX rename() is atomic


def _update_marker_with_history(
    git_dir: Path,
    session_id: str,
    pid: int,
    agent_id: str,
    new_takeover: dict[str, str],
    *,
    expected_holder: dict[str, Any] | None = None,
    require_dead: bool = True,
) -> bool:
    """Conditionally replace a marker and preserve takeover history.

    The holder observed before acquiring the lock is passed as
    ``expected_holder``. Under the lock, a changed holder wins and is not
    overwritten. Dead-holder takeover also re-probes liveness under the lock;
    force takeover sets ``require_dead=False`` but still uses compare-and-swap.
    Returns whether this session wrote the marker. If the lock itself is
    unavailable, fail open without replacing an unvalidated holder.
    """
    import fcntl

    marker = git_dir / MARKER_NAME
    lock_path = git_dir / f"{MARKER_NAME}.flock"
    try:
        with lock_path.open("a") as _lf:
            fcntl.flock(_lf.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    existing = json.loads(marker.read_text())
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    existing = None

                if expected_holder is not None:
                    if existing is None:
                        return False
                    holder_fields = ("session_id", "pid", "agent_id", "started_at")
                    if any(
                        existing.get(field) != expected_holder.get(field)
                        for field in holder_fields
                    ):
                        return False
                    if require_dead and _is_holder_alive(existing):
                        return False

                takeovers: list[dict[str, str]] = (
                    list(existing.get("takeovers", [])) if existing else []
                )
                takeovers.append(new_takeover)
                write_marker(git_dir, session_id, pid, agent_id, takeovers=takeovers)
                return True
            finally:
                fcntl.flock(_lf.fileno(), fcntl.LOCK_UN)
    except OSError:
        return False


def write_marker_atomic_new(
    git_dir: Path,
    session_id: str,
    pid: int,
    agent_id: str = "",
) -> bool:
    """Create the marker only if it does not exist (O_EXCL adopt race).

    Returns True if we successfully adopted the worktree, False if another
    session beat us to it (caller should re-read the marker and proceed to
    the live-holder check).
    """
    data: dict[str, Any] = {
        "session_id": session_id,
        "pid": pid,
        "agent_id": agent_id,
        "started_at": datetime.now(UTC).isoformat(),
        "takeovers": [],
    }
    marker = git_dir / MARKER_NAME
    try:
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        return True
    except FileExistsError:
        return False


# ---------------------------------------------------------------------------
# Liveness probe
# ---------------------------------------------------------------------------


def is_pid_alive(pid: int) -> bool:
    """Check whether a process is alive via ``/proc/<pid>`` existence."""
    if pid <= 0:
        return False
    return Path(f"/proc/{pid}").exists()


def _is_holder_alive(marker: dict[str, Any]) -> bool:
    """Best-effort liveness of the marker's recorded holder.

    Probe order:
    1. Direct ``/proc/<pid>`` check — fast and conclusive when the pid is fresh.
    2. No authoritative ``agent_id`` recorded → the dead PID is conclusive:
       treat the holder as dead so dead-holder takeover can proceed.
    3. Authoritative ``agent_id`` present → fail-safe-alive. The brain repo
       additionally probes claim-shape-aware liveness via ``coordination.health``
       (sentinel scans for autonomous session ids); until that lands upstream,
       an unverifiable holder is treated as alive rather than stolen.

    The marker only records an env-provided ``agent_id`` (see
    ``_get_marker_agent_id``), so a synthesized id can never mask a dead PID.
    """
    holder_pid = marker.get("pid", 0)
    if is_pid_alive(holder_pid):
        return True

    # PID is dead. Only an authoritative agent id can keep the holder "alive"
    # for further probing; otherwise the dead PID is conclusive.
    return bool(marker.get("agent_id", ""))


def _claim_expired(claim: Any) -> bool:
    """Whether a coordination claim is past its ``expires_at``.

    Expired claims intentionally keep ``status == 'claimed'``, so a status
    check alone treats an abandoned claim as a live sibling holder.
    """
    expires_at = getattr(claim, "expires_at", None)
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return bool(expires_at < datetime.now(UTC))


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def append_ledger(brain_root: Path, record: dict[str, Any]) -> None:
    """Append a JSON record to the worktree guard ledger."""
    ledger = brain_root / LEDGER_REL
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Push-time branch guard
# ---------------------------------------------------------------------------


def origin_slug(remote_url: str) -> str | None:
    """Return ``org/repo`` from a GitHub remote URL, if present."""
    match = re.fullmatch(
        r"(?:https?://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([^/ :]+/[^/ :]+?)(?:\.git)?",
        remote_url,
    )
    return match.group(1) if match else None


def pushed_branches(refspec_lines: list[str]) -> list[str]:
    """Extract remote branch targets from pre-push stdin refspec lines."""
    branches: list[str] = []
    for line in refspec_lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, remote_ref, _remote_sha = parts
        if local_sha == _ZERO_SHA or not remote_ref.startswith("refs/heads/"):
            continue
        branches.append(remote_ref.removeprefix("refs/heads/"))
    return branches


def _push_deny_message(key: str, holder: str) -> str:
    return (
        f"[worktree-push-guard] branch claim {key} is held by {holder}.\n"
        "Accept the sibling's canonical CI-green tip: fetch the branch, verify it "
        "covers your scope, and add only targeted gap commits. Never force-push "
        "over a sibling's convergent work."
    )


def legacy_pr_branch_key(task_id: str) -> str | None:
    """Return the pre-repo-qualified free-text alias for a branch key.

    ``pr-branch:<branch>`` claims predate repo-qualified branch claims
    (``pr-branch:org/repo#branch``). Push-time migration must treat a live old
    claim as an alias collision rather than silently creating a parallel
    qualified claim.
    """
    prefix = "pr-branch:"
    if not task_id.startswith(prefix):
        return None
    referent = task_id.removeprefix(prefix)
    if "#" not in referent:
        return None
    # Keys are built as ``pr-branch:{org}/{repo}#{branch}``. Repo names never
    # contain '#', but branch names may, so the branch is everything after the
    # FIRST '#' (rsplit would truncate a branch like ``feat#x``).
    return f"{prefix}{referent.split('#', 1)[1]}"


def run_push_guard(
    refspec_lines: list[str],
    *,
    worktree_root: Path | None = None,
    brain_root: Path | None = None,
    remote_url: str | None = None,
    session_id: str | None = None,
    agent_id: str | None = None,
    deny: bool | None = None,
    db_path: Path | None = None,
) -> int:
    """Auto-claim pushed branches and optionally block a live sibling holder.

    The guard intentionally fails open on database or origin-resolution errors:
    blocking a finished external-repo push during an outage is worse than the
    status quo.  ``BOB_WORKTREE_PUSH_GUARD_DENY=1`` enables the Phase-3 deny
    path after the warn-mode ledger has enough soak data.
    """
    root = worktree_root if worktree_root is not None else _get_worktree_root()
    if root is None:
        return 0

    # Scope: every checkout EXCEPT the brain repo. A branch claim is keyed on
    # org/repo#branch, not on a filesystem path, so scoping enforcement to
    # /tmp/worktrees/ made the guard blind to pushes from any other path. The
    # brain repo stays exempt because it pushes master directly (via
    # git-safe-push-master) and must not auto-claim pr-branch:...#master.
    _brain_root = brain_root if brain_root is not None else _get_brain_root()
    if root == _brain_root:
        return 0

    sid = session_id if session_id is not None else _get_session_id()
    if not sid:
        return 0
    aid = agent_id if agent_id is not None else _get_agent_id()
    if not aid:
        return 0
    # Git supplies the actual push destination as pre-push argv[2]. Falling
    # back to ``origin`` is only for direct/test callers that do not provide
    # the hook arguments.
    url = remote_url
    if url is None:
        import subprocess

        try:
            url = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()
        except Exception:
            url = ""
    slug = origin_slug(url)
    if not slug:
        append_ledger(
            _brain_root,
            {
                "type": "push_skip",
                "reason": "unparseable_origin",
                "at": datetime.now(UTC).isoformat(),
            },
        )
        return 0

    should_deny = (
        deny if deny is not None else _env_flag("BOB_WORKTREE_PUSH_GUARD_DENY")
    )
    try:
        from gptme_coordination.db import CoordinationDB, resolve_coordination_db_path
        from gptme_coordination.work import WorkClaimManager
        from gptme_coordination.worktree_guard import (
            legacy_pr_branch_key,  # local, below
        )

        resolved_db = db_path or resolve_coordination_db_path()
        with CoordinationDB(resolved_db) as db:
            work = WorkClaimManager(db)
            for branch in pushed_branches(refspec_lines):
                key = f"pr-branch:{slug}#{branch}"
                legacy_key = legacy_pr_branch_key(key)
                legacy = work.get(legacy_key) if legacy_key else None
                if (
                    legacy
                    and legacy.status == "claimed"
                    and legacy.claimer != aid
                    and not _claim_expired(legacy)
                ):
                    # Legacy aliases are repo-ambiguous by construction (they
                    # predate the org/repo-qualified key), so a holder on the
                    # same branch name in a *different* repo is a plausible
                    # false collision. Warn only — never deny on an alias — but
                    # still fall through and claim the repo-qualified key:
                    # skipping the claim here would leave later sibling pushes
                    # without the intended collision warning or denial.
                    now = datetime.now(UTC).isoformat()
                    append_ledger(
                        _brain_root,
                        {
                            "type": "push_legacy_alias",
                            "session_id": sid,
                            "agent_id": aid,
                            "key": key,
                            "legacy_key": legacy_key,
                            "holder": legacy.claimer or "unknown",
                            "at": now,
                        },
                    )
                    print(
                        f"[worktree-push-guard] legacy alias {legacy_key} held by "
                        f"{legacy.claimer or 'unknown'} (repo-ambiguous — "
                        f"informational only); claiming {key}",
                        file=sys.stderr,
                    )
                existing_before = work.get(key)
                claim = work.claim(aid, key, ttl_minutes=60)
                if not claim:
                    # Dead-holder recovery (when available) before treating a
                    # live-TTL row as a sibling collision.
                    reap = getattr(work, "reap_dead_holders", None)
                    if callable(reap):
                        reap()
                    claim = work.claim(aid, key, ttl_minutes=60)
                now = datetime.now(UTC).isoformat()
                if claim:
                    event = (
                        "push_renew"
                        if existing_before and existing_before.claimer == aid
                        else "push_auto_claim"
                    )
                    append_ledger(
                        _brain_root,
                        {
                            "type": event,
                            "session_id": sid,
                            "agent_id": aid,
                            "key": key,
                            "at": now,
                        },
                    )
                    continue

                existing = work.get(key)
                holder = (
                    existing.claimer if existing and existing.claimer else "unknown"
                )
                event = "push_deny" if should_deny else "push_would_deny"
                append_ledger(
                    _brain_root,
                    {
                        "type": event,
                        "session_id": sid,
                        "agent_id": aid,
                        "key": key,
                        "holder": holder,
                        "at": now,
                    },
                )
                print(_push_deny_message(key, holder), file=sys.stderr)
                if should_deny:
                    return 1
    except Exception as exc:  # noqa: BLE001 - availability beats push enforcement
        try:
            append_ledger(
                _brain_root,
                {
                    "type": "push_warning",
                    "session_id": sid,
                    "reason": f"db_unreachable: {exc}",
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        except Exception:  # noqa: BLE001 - ledger must never block a push
            pass
        print(
            f"[worktree-push-guard] skipping (DB unavailable: {exc})", file=sys.stderr
        )
    return 0


# ---------------------------------------------------------------------------
# Main guard
# ---------------------------------------------------------------------------


def run_guard(
    *,
    session_id: str | None = None,
    worktree_root: Path | None = None,
    git_dir: Path | None = None,
    brain_root: Path | None = None,
    pid: int | None = None,
    agent_id: str | None = None,
    force: bool | None = None,
) -> int:
    """Run the occupancy guard. Always returns 0 in Phase 1 (warn mode).

    Keyword args are production entry points. Call with no arguments from a
    hook script; pass explicit values in tests to avoid subprocess calls.

    Ordered steps mirror §4.2 of the design doc:
    1. Scope — only /tmp/worktrees/** is guarded.
    2. Identity — no session id → fail-open (humans at a shell are not the
       incident class).
    3. No marker → adopt (O_EXCL; loser re-reads and falls through to step 5).
    4. Marker matches own session → proceed silently.
    5. Marker from a different session → liveness probe:
       - alive → Phase 1: log ``would_deny``, allow.
       - dead  → takeover: rewrite marker, log ``takeover``, allow.
    6. Force override (``BOB_WORKTREE_GUARD_FORCE=1``) skips liveness and
       rewrites the marker regardless, logging ``force``.
    """
    # 1. Scope
    root = worktree_root if worktree_root is not None else _get_worktree_root()
    if root is None or not str(root).startswith(_WORKTREE_PREFIX):
        return 0

    _brain_root = brain_root if brain_root is not None else _get_brain_root()

    # 2. Identity
    sid = session_id if session_id is not None else _get_session_id()
    if not sid:
        return 0

    gd = git_dir if git_dir is not None else _get_git_dir()
    if gd is None:
        return 0

    spid = pid if pid is not None else _get_session_pid()
    # Marker agent id: only an env-provided id is recorded, so a synthesized id
    # cannot make a dead holder look permanently alive (see _get_marker_agent_id).
    aid = agent_id if agent_id is not None else _get_marker_agent_id()
    do_force = force if force is not None else _env_flag("BOB_WORKTREE_GUARD_FORCE")
    now_iso = datetime.now(UTC).isoformat()

    # 3. No marker → try to adopt
    marker = read_marker(gd)
    if marker is None:
        won = write_marker_atomic_new(gd, sid, spid, aid)
        if won:
            append_ledger(
                _brain_root,
                {
                    "type": "adopt",
                    "session_id": sid,
                    "worktree": str(root),
                    "at": now_iso,
                },
            )
            return 0
        # Lost the O_EXCL race — re-read and fall through
        marker = read_marker(gd)
        if marker is None:
            return 0  # marker disappeared between create-fail and re-read → proceed

    # 4. Same session → proceed
    if marker.get("session_id") == sid:
        return 0

    # 5. Different session
    holder_session = marker.get("session_id", "unknown")
    holder_pid = marker.get("pid", 0)

    # Force override: still compare-and-swap against the holder we observed, so
    # a stale force decision cannot overwrite a session that acquired the
    # marker while this process was waiting for the lock.
    if do_force:
        replaced = _update_marker_with_history(
            gd,
            sid,
            spid,
            aid,
            {"from": holder_session, "to": sid, "at": now_iso, "reason": "force"},
            expected_holder=marker,
            require_dead=False,
        )
        if not replaced:
            return 0
        append_ledger(
            _brain_root,
            {
                "type": "force",
                "session_id": sid,
                "holder_session": holder_session,
                "holder_pid": holder_pid,
                "worktree": str(root),
                "at": now_iso,
            },
        )
        return 0

    alive = _is_holder_alive(marker)
    if alive:
        # Phase 1: would deny but allow
        append_ledger(
            _brain_root,
            {
                "type": "would_deny",
                "session_id": sid,
                "holder_session": holder_session,
                "holder_pid": holder_pid,
                "worktree": str(root),
                "at": now_iso,
            },
        )
        print(
            f"[worktree-guard] WARN: worktree owned by live session"
            f" {holder_session} (pid={holder_pid});"
            f" would deny in Phase 2."
            f" Set BOB_WORKTREE_GUARD_FORCE=1 to override.",
            file=sys.stderr,
        )
        return 0  # Phase 1: warn mode, never block
    else:
        # Dead holder → takeover
        took_over = _update_marker_with_history(
            gd,
            sid,
            spid,
            aid,
            {"from": holder_session, "to": sid, "at": now_iso, "reason": "dead"},
            expected_holder=marker,
        )
        if not took_over:
            return 0
        append_ledger(
            _brain_root,
            {
                "type": "takeover",
                "session_id": sid,
                "from_session": holder_session,
                "holder_pid": holder_pid,
                "worktree": str(root),
                "at": now_iso,
            },
        )
        return 0
