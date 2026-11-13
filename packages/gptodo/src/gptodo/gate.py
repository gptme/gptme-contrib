"""Typed wait gates: what a waiting task waits on, who answers, and what happens at the deadline.

Free-text ``waiting_for`` cannot expire: nothing reads it as a contract, so a
task can wait on a person for months. A ``gate`` block makes the wait typed:

    gate:
      kind: decision            # closed enum, see GateKind
      actor: erik               # who must act (the "is this waiting on X" predicate)
      question: "Rotate the Stripe key now? y/n"
      default: "rotate on the deadline"   # what happens if nobody answers
      deadline: 2026-11-26T18:00:00+00:00
      on_timeout: escalate      # default | escalate | drop
      reversible: false         # false forbids on_timeout: default

Rules enforced here (pure functions, no I/O):

* ``kind`` is closed. ``pr``, ``issue``, ``task`` and ``time`` gates are
  *derived*: their resolution follows from ``ref``/``deadline``, so an authored
  ``probe:`` next to them is refused. Only ``event`` takes an authored probe.
* ``decision`` gates need an actor, a question, a default, a deadline, an
  ``on_timeout`` and an explicit ``reversible``.
* An irreversible gate never auto-executes its default on timeout.
* A deadline may be pushed once. The first push records ``original_deadline``;
  a later push is refused by :func:`check_gate_change`.

Consumers: :func:`validate_gate` runs inside ``validate_task_file`` (so
``gptodo lint``/``check`` and every ``mutate_task`` write see it) and
:func:`check_gate_change` runs on every ``mutate_task`` write and can be run at
commit time against ``HEAD`` to catch direct file edits.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any


class GateKind(str, Enum):
    """Closed set of things a task can wait on."""

    DECISION = "decision"  # a named actor must answer a question
    PR = "pr"  # resolves when the PR is merged or closed (derived)
    ISSUE = "issue"  # resolves when the issue is closed (derived)
    TASK = "task"  # resolves when another task is terminal (derived via depends)
    TIME = "time"  # resolves at the deadline (derived)
    EVENT = "event"  # an external event, checked by an authored probe


class OnTimeout(str, Enum):
    DEFAULT = "default"  # apply the recorded default
    ESCALATE = "escalate"  # surface once more, then the owner decides
    DROP = "drop"  # close the waiting task with a visible notice


DERIVED_KINDS = frozenset({GateKind.PR, GateKind.ISSUE, GateKind.TASK, GateKind.TIME})
GATE_FIELDS = frozenset(
    {
        "kind",
        "actor",
        "ref",
        "question",
        "default",
        "deadline",
        "original_deadline",
        "on_timeout",
        "reversible",
    }
)

_GITHUB_SHORT_RE = re.compile(r"^([\w.-]+)/([\w.-]+)#(\d+)$")
_GITHUB_URL_RE = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+)/(pull|issues)/(\d+)/?$")


@dataclass(frozen=True)
class Gate:
    kind: GateKind
    actor: str | None
    ref: str | None
    question: str | None
    default: str | None
    deadline: datetime | None
    original_deadline: datetime | None
    on_timeout: OnTimeout | None
    reversible: bool | None


def _as_datetime(value: Any) -> datetime | None:
    """Parse a YAML date/datetime/ISO string to an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_github_ref(ref: str) -> tuple[str, str, int] | None:
    """Return ``(owner, repo, number)`` for ``owner/repo#N`` or a GitHub URL."""
    match = _GITHUB_SHORT_RE.match(ref.strip())
    if match:
        return match.group(1), match.group(2), int(match.group(3))
    match = _GITHUB_URL_RE.match(ref.strip())
    if match:
        return match.group(1), match.group(2), int(match.group(4))
    return None


def validate_gate(metadata: dict[str, Any]) -> list[str]:
    """Return schema errors for the ``gate`` block of *metadata* (empty if absent/valid)."""
    if "gate" not in metadata:
        return []
    raw = metadata["gate"]
    if not isinstance(raw, dict):
        return ["gate must be a mapping with at least a kind"]

    errors: list[str] = []
    unknown = sorted(set(raw) - GATE_FIELDS)
    if unknown:
        errors.append(f"gate has unknown field(s): {', '.join(unknown)}")

    kind_raw = raw.get("kind")
    try:
        kind = GateKind(kind_raw)
    except ValueError:
        allowed = ", ".join(k.value for k in GateKind)
        return errors + [f"gate.kind must be one of: {allowed} (got {kind_raw!r})"]

    for field in ("deadline", "original_deadline"):
        if raw.get(field) is not None and _as_datetime(raw[field]) is None:
            errors.append(f"gate.{field} must be an ISO date or datetime")

    on_timeout = raw.get("on_timeout")
    if on_timeout is not None and (
        not isinstance(on_timeout, str) or on_timeout not in {o.value for o in OnTimeout}
    ):
        errors.append("gate.on_timeout must be one of: " + ", ".join(o.value for o in OnTimeout))
    reversible = raw.get("reversible")
    if reversible is not None and not isinstance(reversible, bool):
        errors.append("gate.reversible must be true or false")
    if reversible is False and on_timeout == OnTimeout.DEFAULT.value:
        errors.append("gate.on_timeout cannot be 'default' when reversible is false")

    ref = _text(raw.get("ref"))
    if kind in (GateKind.PR, GateKind.ISSUE):
        if ref is None or parse_github_ref(ref) is None:
            errors.append(f"gate.ref for kind {kind.value} must be owner/repo#N or a GitHub URL")
    elif kind is GateKind.TASK and ref is None:
        errors.append("gate.ref for kind task must name the task id")
    if kind in (GateKind.TIME, GateKind.DECISION, GateKind.EVENT) and raw.get("deadline") is None:
        errors.append(f"gate.deadline is required for kind {kind.value}")

    if kind is GateKind.DECISION:
        for field in ("actor", "question", "default"):
            if _text(raw.get(field)) is None:
                errors.append(f"gate.{field} is required for kind decision")
        if on_timeout is None:
            errors.append("gate.on_timeout is required for kind decision")
        if reversible is None:
            errors.append("gate.reversible is required for kind decision")
    if kind is GateKind.TASK and ref is not None:
        deps = [str(d) for key in ("depends", "requires") for d in (metadata.get(key) or [])]
        if ref not in deps:
            errors.append("gate.ref for kind task must also be listed in depends")

    if kind in DERIVED_KINDS and _text(metadata.get("probe")) is not None:
        errors.append(
            f"gate kind {kind.value} is derived from its ref/deadline; remove the authored probe"
        )
    if kind is GateKind.EVENT and _text(metadata.get("probe")) is None:
        errors.append("gate kind event needs a probe (the only kind with an authored check)")

    deadline = _as_datetime(raw.get("deadline"))
    original = _as_datetime(raw.get("original_deadline"))
    if deadline is not None and original is not None and original >= deadline:
        errors.append("gate.original_deadline must be earlier than gate.deadline")
    return errors


def parse_gate(metadata: dict[str, Any]) -> Gate | None:
    """Return the typed gate, or ``None`` when absent or invalid."""
    if "gate" not in metadata or validate_gate(metadata):
        return None
    raw = metadata["gate"]
    on_timeout = raw.get("on_timeout")
    return Gate(
        kind=GateKind(raw["kind"]),
        actor=_text(raw.get("actor")),
        ref=_text(raw.get("ref")),
        question=_text(raw.get("question")),
        default=_text(raw.get("default")),
        deadline=_as_datetime(raw.get("deadline")),
        original_deadline=_as_datetime(raw.get("original_deadline")),
        on_timeout=OnTimeout(on_timeout) if on_timeout is not None else None,
        reversible=raw.get("reversible"),
    )


def gate_actor(metadata: dict[str, Any]) -> str | None:
    """Who the task waits on. ``gate_actor(meta) == "erik"`` is the Erik predicate."""
    gate = parse_gate(metadata)
    return gate.actor if gate else None


def derived_probe(gate: Gate) -> str | None:
    """Shell check for derived kinds; exit 0 means the gate resolved.

    A failed ``gh`` call prints nothing, so ``grep`` exits 1: an unreachable
    API reads as "still waiting", never as resolved. ``task`` gates resolve
    through ``depends`` and return ``None``, as do non-derived kinds.
    """
    if gate.kind in (GateKind.PR, GateKind.ISSUE) and gate.ref:
        parsed = parse_github_ref(gate.ref)
        if parsed is None:
            return None
        owner, repo, number = parsed
        sub, done = ("pr", "MERGED|CLOSED") if gate.kind is GateKind.PR else ("issue", "CLOSED")
        return (
            f"gh {sub} view {number} --repo {shlex.quote(owner + '/' + repo)} "
            f"--json state -q .state | grep -qxE {shlex.quote(done)}"
        )
    if gate.kind is GateKind.TIME and gate.deadline is not None:
        return f'test "$(date -u +%s)" -ge {int(gate.deadline.timestamp())}'
    return None


def check_gate_change(prior: dict[str, Any], post: dict[str, Any]) -> list[str]:
    """Errors for a gate edit from *prior* to *post* metadata.

    A deadline may move later once: that first push must keep the earlier
    date in ``original_deadline``. A gate that already carries
    ``original_deadline`` cannot move later again, and the stamp cannot be
    removed or rewritten while the gate keeps its kind and ref.
    """
    before, after = prior.get("gate"), post.get("gate")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    if (before.get("kind"), before.get("ref")) != (after.get("kind"), after.get("ref")):
        return []
    old_deadline = _as_datetime(before.get("deadline"))
    new_deadline = _as_datetime(after.get("deadline"))
    old_original = before.get("original_deadline")
    if old_original is not None and _as_datetime(after.get("original_deadline")) != _as_datetime(
        old_original
    ):
        return ["gate.original_deadline is a one-time record and cannot be changed"]
    if old_deadline is None or new_deadline is None or new_deadline <= old_deadline:
        return []
    original = _as_datetime(old_original)
    if original is not None:
        return [
            f"gate.deadline was already pushed once (original {original.isoformat()}); "
            "a second push is refused. Escalate or drop instead."
        ]
    if _as_datetime(after.get("original_deadline")) != old_deadline:
        return [
            "pushing gate.deadline requires gate.original_deadline set to the previous deadline"
        ]
    return []


def stamp_deadline_push(prior: dict[str, Any], post: dict[str, Any]) -> None:
    """Record the first deadline push in *post* so writers need not hand-stamp it."""
    before, after = prior.get("gate"), post.get("gate")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return
    if (before.get("kind"), before.get("ref")) != (after.get("kind"), after.get("ref")):
        return
    if before.get("original_deadline") is not None or "original_deadline" in after:
        return
    old_deadline = _as_datetime(before.get("deadline"))
    new_deadline = _as_datetime(after.get("deadline"))
    if old_deadline is not None and new_deadline is not None and new_deadline > old_deadline:
        after["original_deadline"] = before.get("deadline")
