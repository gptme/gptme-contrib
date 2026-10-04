"""Canonical lifecycle transformation and transactional task mutation.

Mutation locks are short-lived serialization locks, not execution leases. Callers
supply patches/callbacks, never a stale replacement Post. Validation precedes
atomic replacement, so rejected edits leave the exact pre-image untouched.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import os
import tempfile
import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from gptodo.frontmatter_compat import frontmatter
from gptodo.utils import (
    advance_wait,
    find_repo_root,
    is_valid_recur_value,
    load_tasks,
    normalize_state,
    parse_recur_interval,
    parse_wait,
    validate_task_file,
)
from gptodo.utils import is_generated_recurrence_waiting_for

Intent = Literal["normal", "operator_reopen", "sync_reopen", "requeue", "alert_refire"]
CANONICAL_LIST_FIELDS = {"tags", "depends", "requires", "blocks", "related", "discovered-from"}
Change = tuple[str, str, Any]


class TransitionError(ValueError):
    """Invalid or unauthorized lifecycle transition."""


class StaleTaskError(TransitionError):
    """The candidate no longer has the expected state."""


@dataclass
class MutationResult:
    old_state: str
    requested_state: str | None
    effective_state: str
    changed_fields: dict[str, tuple[Any, Any]]
    post: Any
    written: bool


def validate_transition(
    old: str,
    new: Any,
    *,
    force: bool = False,
    intent: Intent = "normal",
    strict: bool | None = None,
) -> str:
    from gptodo.checker import VALID_TRANSITIONS

    if not isinstance(new, str) or not new:
        raise TransitionError("state cannot be null or removed")
    new = str(normalize_state(new, warn=False))
    if new not in VALID_TRANSITIONS:
        raise TransitionError(f"Invalid state: {new}")
    if old == new:
        return str(new)
    if force or intent == "operator_reopen":
        return str(new)
    if intent == "sync_reopen" and old == "done" and new == "active":
        return str(new)
    if intent == "requeue" and old == "active" and new == "todo":
        return str(new)
    if intent == "alert_refire" and old in ("done", "cancelled") and new in ("todo", "waiting"):
        return str(new)
    if old in ("done", "cancelled"):
        raise TransitionError("Refusing to reopen terminal state(s) without --force")
    if new not in VALID_TRANSITIONS.get(old, []):
        if strict is None:
            strict = os.environ.get("GPTODO_STRICT_TRANSITIONS", "").lower() in ("1", "true", "yes")
        message = f"Illegal state transition: {old} -> {new}. Legal from '{old}': {', '.join(VALID_TRANSITIONS.get(old, []))}"
        if strict:
            raise TransitionError(message)
        warnings.warn(message, UserWarning, stacklevel=2)
    return str(new)


def resolve_subtask_lines(content: str, edits: list[tuple[str, str]]) -> dict[str, int]:
    """Map each checklist selector to its unique line in ``content`` (empty if no edits).

    Resolved against the original body so a batch is order-independent: a missing,
    ambiguous or line-sharing selector rejects the whole batch.
    """
    import re

    if not edits:
        return {}
    lines = content.split("\n")
    selected: dict[str, int] = {}
    used: set[int] = set()
    for text, _state in edits:
        matches = [
            i
            for i, line in enumerate(lines)
            if text in line and re.match(r"^\s*(?:>\s*)?- \[[ x-]\]", line)
        ]
        if not matches:
            raise TransitionError(f"Subtask not found: {text}")
        if len(matches) != 1:
            raise TransitionError(f"Ambiguous subtask: {text}")
        if matches[0] in used:
            raise TransitionError(f"Duplicate subtask: {text}")
        used.add(matches[0])
        selected[text] = matches[0]
    return selected


def transform_post(
    prior: Any,
    changes: list[Change],
    *,
    now: datetime | None = None,
    force: bool = False,
    intent: Intent = "normal",
    strict: bool | None = None,
) -> Any:
    """Pure edit lifecycle, including body changes and effective recurrence state.

    Same-state terminal reassertions do not invent completion timestamps. Explicit
    completed overrides win. Cron remains terminal with scheduler fields intact.
    Recurrence uses the legacy advance_wait algorithm and a single atomic write.
    """
    now = now or datetime.now(timezone.utc)
    post = copy.deepcopy(prior)
    old = normalize_state(str(prior.metadata.get("state") or "backlog"), warn=False)
    for op, field, value in changes:
        if op not in ("set", "add", "remove", "set_subtask", "set_body"):
            raise TransitionError(f"Unknown mutation operation: {op}")
        if field == "state":
            if op != "set":
                raise TransitionError("state must be set, never added/removed")
            validate_transition(old, value, force=force, intent=intent, strict=strict)
    # Snapshot the pre-edit state: the completed-stamp logic below must key off
    # a real *transition*, not the post-edit final state (which cannot tell an
    # idempotent re-save apart from a state change).
    _prior_state = normalize_state(
        str(post.metadata.get("state", "backlog") or "backlog"), warn=False
    )

    subtask_lines = resolve_subtask_lines(
        post.content, [(f, v) for op, f, v in changes if op == "set_subtask"]
    )

    # Apply all changes
    for op, field, value in changes:
        if op == "set_body":
            post.content = value
        elif op == "set_subtask":
            # field is subtask_text, value is state ("done" or "todo"). The target
            # line was resolved against the original body above, so a batch is
            # order-independent and never lands on a line another selector hit.
            import re

            lines = post.content.split("\n")
            i = subtask_lines[field]
            target = "- [x]" if value == "done" else "- [ ]"
            # "- [-]" is the intentionally-skipped marker; recognize it so an
            # already-skipped item can still be toggled back to done/todo.
            # Setting an item *to* skipped is not exposed via the CLI: a skip
            # must carry a reason (see TASKS.md, Checkbox Semantics).
            for marker in ("- [ ]", "- [x]", "- [-]"):
                # Anchor to line start (after optional whitespace/blockquote)
                # so prose occurrences like "- [x] See - [ ] item" don't
                # steal the slot before the actual leading checkbox is found.
                if re.match(rf"^\s*(?:>\s*)?{re.escape(marker)}", lines[i]):
                    new_line = lines[i].replace(marker, target, 1)
                    # Strikethrough forms must have their ~~ markup stripped when
                    # toggling back; otherwise count_subtasks re-classifies the
                    # result as skipped and the toggle is a silent no-op.
                    new_line = re.sub(r"([ \t]*- \[[x ]\])\s*~~(.+?)~~.*$", r"\1 \2", new_line)
                    # Bare [-] form: strip the trailing reason parenthetical via
                    # regex (one nested level, e.g. "(deferred: see (issue #5))").
                    if marker == "- [-]":
                        new_line = re.sub(
                            r"\s*\([^)]*(?:\([^)]*\)[^)]*)*\)\s*$", "", new_line
                        ).rstrip()
                    lines[i] = new_line
                    break
            post.content = "\n".join(lines)
        elif field in CANONICAL_LIST_FIELDS:
            # Handle list fields (after normalization via FIELD_ALIASES)
            current = post.metadata.get(field, [])
            if op == "add":
                post.metadata[field] = list(set(current + [value]))
            else:  # remove
                post.metadata[field] = [x for x in current if x != value]
        else:  # set operation
            if value is None:  # Clear field with "none" value
                post.metadata.pop(field, None)
            else:
                # Normalize deprecated states at write time (defense in depth)
                if field == "state":
                    value = normalize_state(value, warn=False)

                post.metadata[field] = value
    # When --set wait is used on a recurrence-reset task (state=waiting,
    # wait_kind=machine, waiting_for="next recurrence gate (wait: ...)"),
    # keep waiting_for in sync with the new wait value so
    # task_has_waiting_blocker can still match the recurrence-gate pattern.
    # Without this, a manual wait adjustment permanently traps the task.
    #
    # Must run AFTER every --set in this invocation is applied. Doing it
    # inside the per-field loop misses `--set wait X --set state waiting`
    # because state still holds the pre-edit value when wait is processed
    # (P1 on gptme/gptme-contrib#1539). Combined with the existing
    # transitioning_to_waiting check just below, this covers both orderings.
    # Last --set wait wins (same as the apply loop). next() without
    # reversed() would rewrite waiting_for from the first wait while
    # metadata['wait'] holds a later one, permanently trapping the task.
    wait_change = next(
        (
            (True, value)
            for op, field, value in reversed(changes)
            if op == "set" and field == "wait"
        ),
        (False, None),
    )
    wait_was_set, wait_set = wait_change
    if wait_was_set:
        _wf = post.metadata.get("waiting_for", "")
        generated_wf = is_generated_recurrence_waiting_for(_wf)
        wait_kind = post.metadata.get("wait_kind")
        # A leftover generated waiting_for without wait_kind is still a
        # recurrence gate: `--set wait none --set state waiting` pops
        # wait_kind (state=waiting requires waiting_for) and a later
        # `--set wait NEW` must restore the machine gate. Otherwise the
        # old generated string permanently traps the task after the new
        # date expires (P1 on gptme/gptme-contrib#1539 / 7a046593).
        recurrence_gate = generated_wf and wait_kind in ("machine", None)
        if recurrence_gate and wait_set is None:
            # Clearing a generated recurrence gate means it is no longer
            # a machine time-gate. Remove the generated blocker metadata as
            # one unit so the task cannot stay permanently blocked without
            # a date. Only default state to todo when this edit did not
            # set state — `--set wait none --set state done` must not
            # overwrite the explicit state (P1 on gptme/gptme-contrib#1539).
            explicit_state = any(op == "set" and field == "state" for op, field, _value in changes)
            # Only default waiting → todo. A leftover recurrence-gate
            # string on done/cancelled/active must not reopen or demote
            # the task (P1 on gptme/gptme-contrib#1539 / ecb15242).
            if not explicit_state and post.metadata.get("state") == "waiting":
                post.metadata["state"] = "todo"
            if post.metadata.get("state") == "waiting":
                # `--set wait none --set state waiting` must not pop
                # waiting_for/waiting_since: state=waiting requires both
                # (P1 on gptme/gptme-contrib#1539 / b859fbf4). Drop
                # wait_kind so this is no longer a machine time-gate.
                post.metadata.pop("wait_kind", None)
            else:
                post.metadata.pop("waiting_for", None)
                post.metadata.pop("waiting_since", None)
                post.metadata.pop("wait_kind", None)
        elif recurrence_gate and post.metadata.get("state") == "waiting":
            post.metadata["waiting_for"] = f"next recurrence gate (wait: {wait_set})"
            post.metadata["wait_kind"] = "machine"
        elif recurrence_gate:
            # Wait changed but the task is no longer waiting. Drop the
            # generated recurrence string so it cannot trap a todo/done
            # task as a leftover human-looking blocker.
            post.metadata.pop("waiting_for", None)
            post.metadata.pop("waiting_since", None)
            post.metadata.pop("wait_kind", None)
    # Drop generated recurrence-gate waiting_for whenever the final state
    # is not waiting, even if this edit did not touch wait. A state-only
    # `--set state todo` used to skip the wait-sync (gated on wait_was_set)
    # and leave waiting_for, which task_has_waiting_blocker treats as a
    # human blocker on non-waiting states (P1 on gptme/gptme-contrib#1539
    # / 69034e96). Keep wait: — the user did not ask to clear the date.
    if post.metadata.get("state") != "waiting" and is_generated_recurrence_waiting_for(
        post.metadata.get("waiting_for", "")
    ):
        post.metadata.pop("waiting_for", None)
        post.metadata.pop("waiting_since", None)
        post.metadata.pop("wait_kind", None)
    # Auto-set waiting_since only when THIS edit explicitly sets state to waiting
    # AND waiting_for is either already present or being set in the same edit.
    # Guarding on waiting_for prevents an injected waiting_since from triggering
    # the pre-commit hook error "waiting_since requires waiting_for".
    transitioning_to_waiting = any(
        op == "set" and field == "state" and value == "waiting" for op, field, value in changes
    )
    waiting_for_present = post.metadata.get("waiting_for") or any(
        op == "set" and field == "waiting_for" and value is not None for op, field, value in changes
    )
    if transitioning_to_waiting and waiting_for_present:
        # Full ISO datetime for intra-day resolution (ErikBjare request, 2026-06-16).
        # validate_task_frontmatter.py's validate_timestamp() accepts both YYYY-MM-DD
        # and full ISO datetime via datetime.fromisoformat().
        _now_iso = now.isoformat(timespec="seconds")
        if not post.metadata.get("waiting_since"):
            post.metadata["waiting_since"] = _now_iso

        # Cumulative waiting history (TASKS.md schema), written here so it can
        # never disagree with waiting_since. Both fields were documented but
        # had no live writer until 2026-09-03 — the only writer was a one-shot
        # git-history backfill with no caller, so every value in the tree was
        # frozen at whenever someone last ran it by hand.
        #
        # The discriminator is the *prior* state, not a missing waiting_since:
        # leaving waiting does not clear waiting_since, so its absence would
        # miss exactly the re-parks these fields exist to count.
        if _prior_state != "waiting":
            # first_waiting_since is stamped once and never overwritten — it
            # is the cumulative blocker age that survives re-parks.
            _since = post.metadata.get("waiting_since")
            post.metadata.setdefault(
                "first_waiting_since", str(_since) if _since is not None else _now_iso
            )

            # waiting_spell_count counts distinct waiting spells; >= 3 is the
            # re-park signal read by task_metadata_hygiene_audit check 16.
            # Absent (or hand-corrupted) means "no spells recorded yet", so
            # the post-increment value is 1.
            try:
                _prior_spells = int(post.metadata.get("waiting_spell_count") or 0)
            except (TypeError, ValueError):
                _prior_spells = 0
            post.metadata["waiting_spell_count"] = max(_prior_spells, 0) + 1

    # Strip now-stale actionable/blocker metadata when the edit lands the task
    # in a terminal state (TASKS.md best-practice #7: terminal tasks must not
    # keep next_action/waiting_for/waiting_since/wait). Recurring tasks reset to
    # waiting further below and must keep these fields, so skip when recur is set.
    # tracking_issue / upstream_coordination_id are intentionally preserved for
    # permanent traceability.
    # cancelled is terminal regardless of recur. A done task only escapes the
    # terminal path when the recurrence reset below actually fires, and that
    # reset is gated on parse_recur_interval() — so gate the *completed stamp*
    # on the same parse rather than on the truthiness of recur:. Values the
    # parser rejects (malformed strings, and cron expressions, which are
    # documented-valid but not yet computed) leave the task sitting in done, so
    # they are terminal in practice and must be stamped like any other.
    #
    # Stale-field cleanup is deliberately gated *differently*, on
    # is_valid_recur_value() rather than parse_recur_interval(). A cron recur:
    # is a documented-valid recurrence that gptodo simply cannot compute yet —
    # the next-fire date lives in wait: and is maintained by whatever external
    # scheduler owns the cron. Stripping wait:/next_action: there destroys that
    # external state irrecoverably, so cron tasks keep their scheduling fields
    # even though they are stamped and left in done. Genuinely malformed recur:
    # values are not a recurrence at all, so they are cleaned like any terminal
    # task.
    recur = post.metadata.get("recur")
    _will_recur = (
        post.metadata.get("state") == "done"
        and recur is not None
        and parse_recur_interval(str(recur)) is not None
    )
    _recur_is_valid = recur is not None and is_valid_recur_value(str(recur))
    _is_terminal_nonrecurring = post.metadata.get("state") == "cancelled" or (
        post.metadata.get("state") == "done" and not _will_recur
    )
    _should_strip_stale_fields = post.metadata.get("state") == "cancelled" or (
        post.metadata.get("state") == "done" and not _recur_is_valid
    )
    if _should_strip_stale_fields:
        for _stale_field in (
            "next_action",
            "waiting_for",
            "waiting_since",
            "wait",
            # Cumulative waiting history is cleared on terminal states only
            # (TASKS.md schema): it exists to answer "how long was this
            # really stuck?" and is meaningless once the task is closed.
            "first_waiting_since",
            "waiting_spell_count",
        ):
            post.metadata.pop(_stale_field, None)

    # Auto-set completed timestamp when transitioning to a terminal state, and
    # clear a stale stamp when this edit reopens a task. Both halves defer to an
    # explicit `--set completed ...` in the same edit: the user's value wins over
    # the automation in either direction.
    #
    # A non-terminal task carrying completed is stale even if the reopen happened
    # outside gptodo (for example by editing the Markdown directly). Replace that
    # stale value on the next terminal transition so the latest completion gets
    # the timestamp, unless this invocation explicitly supplies or clears it.
    #
    # Both halves are gated on `_prior_state` — the state the task held *before*
    # this edit — because a post-edit-only check cannot distinguish a transition
    # from a re-assertion:
    #   - Without the prior-state gate on the stamp, re-saving an already-done
    #     legacy task (`--set state done` on a task that is already done and
    #     predates this feature, so has no `completed`) fabricates a completion
    #     of "just now", corrupting exactly the completion-duration / fast-close
    #     signals this stamp exists to measure.
    #   - Without the prior-state gate on the clear, an ordinary forward move
    #     like waiting → active or backlog → todo silently deletes a `completed`
    #     the task legitimately carries. Only a genuine reopen (terminal → open)
    #     should drop it.
    _terminal_states = ("done", "cancelled")
    _state_target = next(
        (value for op, field, value in reversed(changes) if op == "set" and field == "state"),
        None,
    )
    _completed_explicitly_cleared = any(
        op == "set" and field == "completed" and value is None for op, field, value in changes
    )
    _completed_explicitly_set = any(
        op == "set" and field == "completed" and value is not None for op, field, value in changes
    )
    _is_terminal_transition = (
        _prior_state not in _terminal_states
        and _state_target in _terminal_states
        and _is_terminal_nonrecurring
    )
    if (
        _is_terminal_transition
        and not _completed_explicitly_set
        and not _completed_explicitly_cleared
    ):
        post.metadata["completed"] = now.isoformat(timespec="seconds")
    elif (
        _prior_state in _terminal_states
        and _state_target is not None
        and _state_target not in _terminal_states
        and not _completed_explicitly_set
    ):
        post.metadata.pop("completed", None)

    if any(op == "set" and field == "state" and value == "done" for op, field, value in changes):
        recur = post.metadata.get("recur")
        if (
            post.metadata.get("state") == "done"
            and recur
            and parse_recur_interval(str(recur)) is not None
        ):
            next_wait = advance_wait(parse_wait(post.metadata.get("wait")), str(recur), now=now)
            wait_iso = next_wait.isoformat()
            post.metadata.update(
                state="waiting",
                wait=wait_iso,
                wait_kind="machine",
                waiting_for=f"next recurrence gate (wait: {wait_iso})",
                waiting_since=now.isoformat(timespec="seconds"),
            )
            post.metadata.pop("probe", None)
            if not _completed_explicitly_set:
                post.metadata.pop("completed", None)
    return post


@contextmanager
def mutation_lock(path: Path) -> Iterator[None]:
    # A stable separate inode must survive os.replace. Locking the task inode
    # would allow a waiter and a new opener to lock different generations.
    canonical = path.resolve()
    directory = Path(tempfile.gettempdir()) / f"gptodo-mutations-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    lock_path = directory / (hashlib.sha256(os.fsencode(canonical)).hexdigest() + ".lock")
    with lock_path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write_atomic(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def mutate_task(
    path: Path,
    changes: list[Change] | None = None,
    *,
    patch: dict[str, Any] | None = None,
    expected_state: str | None = None,
    prepare: Callable[[Any], list[Change]] | None = None,
    now: datetime | None = None,
    force: bool = False,
    intent: Intent = "normal",
    strict: bool | None = None,
    dry_run: bool = False,
    completion_effects: bool = True,
) -> MutationResult:
    """Fresh-read, validate and replace a task under a per-path mutation lock.

    ``prepare`` derives both metadata/body changes from the locked fresh Post.
    ``None`` patch values remove optional fields; state=None always fails.
    Hooks and propagation run after releasing the lock. Like CLI edit, an
    explicit same-state done request can rerun hooks (not timestamps).
    """
    path = Path(path).resolve()
    with mutation_lock(path):
        prior = frontmatter.load(path)
        old = normalize_state(str(prior.metadata.get("state") or "backlog"), warn=False)
        if expected_state is not None and old != normalize_state(expected_state, warn=False):
            raise StaleTaskError(f"Expected state {expected_state}, found {old}: {path}")
        operations = list(changes or []) + [
            ("set", key, value) for key, value in (patch or {}).items()
        ]
        if prepare is not None:
            operations.extend(prepare(copy.deepcopy(prior)))
        post = transform_post(prior, operations, now=now, force=force, intent=intent, strict=strict)
        # Legacy malformed recurrence can still be closed/cleaned. Do not
        # reject an unrelated pre-existing schema defect, but never introduce
        # new ones. State null/invalid is independently rejected above.
        prior_errors = validate_task_file(path, prior)
        errors = [error for error in validate_task_file(path, post) if error not in prior_errors]
        if errors:
            raise TransitionError("; ".join(errors))
        fields = {
            key: (prior.metadata.get(key), post.metadata.get(key))
            for key in prior.metadata.keys() | post.metadata.keys()
            if prior.metadata.get(key) != post.metadata.get(key)
        }
        changed = bool(fields) or prior.content != post.content
        requested = next(
            (value for op, key, value in reversed(operations) if op == "set" and key == "state"),
            None,
        )
        result = MutationResult(
            old, requested, post.metadata["state"], fields, post, changed and not dry_run
        )
        if result.written:
            _write_atomic(path, frontmatter.dumps(post))
    if (
        not dry_run
        and completion_effects
        and requested == "done"
        and result.effective_state == "done"
    ):
        _completion_effects(path)
    return result


_effect_paths: ContextVar[frozenset[Path]] = ContextVar(
    "gptodo_completion_paths", default=frozenset()
)


def _completion_effects(path: Path) -> None:
    active = _effect_paths.get()
    if path in active:
        return
    token = _effect_paths.set(active | {path})
    try:
        _run_completion_effects(path)
    finally:
        _effect_paths.reset(token)


def _run_completion_effects(path: Path) -> None:
    import subprocess
    from gptodo.unblock import auto_unblock_with_fan_in

    repo_root = find_repo_root(path.parent.parent if path.parent.name == "tasks" else path.parent)
    tasks_dir = repo_root / "tasks"
    if not tasks_dir.is_dir():
        return
    tasks = load_tasks(tasks_dir)
    completed = next((task for task in tasks if task.path.resolve() == path), None)
    if completed is None or _current_state(path) != "done":
        return
    hook = os.environ.get("HOOK_TASK_DONE")
    if hook:
        try:
            subprocess.run([hook, completed.id, completed.name, str(repo_root)], check=False)
        except Exception as exc:
            warnings.warn(f"Task completion hook error: {exc}", stacklevel=2)
    # A hook can itself reopen the task. Do not propagate stale completion.
    if _current_state(path) == "done":
        auto_unblock_with_fan_in([completed.id], tasks, tasks_dir)


def _current_state(path: Path) -> str | None:
    try:
        value = frontmatter.load(path).metadata.get("state")
        return value if isinstance(value, str) else None
    except OSError:
        return None
