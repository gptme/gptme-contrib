"""Typed wait gate: schema, derived probes, one deadline push."""

from pathlib import Path

import pytest
import yaml

from gptodo.frontmatter_compat import frontmatter
from gptodo.gate import (
    GateKind,
    check_gate_change,
    derived_probe,
    gate_actor,
    parse_gate,
    validate_gate,
)
from gptodo.lifecycle import TransitionError, mutate_task

DECISION = {
    "kind": "decision",
    "actor": "erik",
    "question": "Rotate the key now? y/n",
    "default": "rotate on the deadline",
    "deadline": "2026-11-26T18:00:00+00:00",
    "on_timeout": "escalate",
    "reversible": False,
}


def test_no_gate_is_valid() -> None:
    assert validate_gate({"state": "waiting"}) == []
    assert parse_gate({"state": "waiting"}) is None


def test_decision_gate_parses_and_names_actor() -> None:
    meta = {"gate": dict(DECISION)}
    assert validate_gate(meta) == []
    gate = parse_gate(meta)
    assert gate is not None and gate.kind is GateKind.DECISION
    assert gate.deadline is not None and gate.deadline.tzinfo is not None
    assert gate_actor(meta) == "erik"


def test_kind_is_closed() -> None:
    errors = validate_gate({"gate": {"kind": "vibes"}})
    assert errors and "gate.kind must be one of" in errors[0]


@pytest.mark.parametrize("field", ["actor", "question", "default", "on_timeout", "reversible"])
def test_decision_requires_every_contract_field(field: str) -> None:
    gate = {k: v for k, v in DECISION.items() if k != field}
    assert any(field in e for e in validate_gate({"gate": gate}))


def test_irreversible_gate_never_defaults_on_timeout() -> None:
    gate = dict(DECISION, on_timeout="default")
    assert any("reversible is false" in e for e in validate_gate({"gate": gate}))
    assert validate_gate({"gate": dict(gate, reversible=True)}) == []


def test_unknown_gate_field_is_refused() -> None:
    errors = validate_gate({"gate": dict(DECISION, ask_again=True)})
    assert any("unknown field" in e for e in errors)


def test_derived_kinds_refuse_authored_probe() -> None:
    meta = {"gate": {"kind": "pr", "ref": "gptme/gptme#1"}, "probe": "true"}
    assert any("remove the authored probe" in e for e in validate_gate(meta))
    del meta["probe"]
    assert validate_gate(meta) == []


def test_event_gate_needs_probe_and_deadline() -> None:
    errors = validate_gate({"gate": {"kind": "event"}})
    assert any("needs a probe" in e for e in errors)
    assert any("deadline is required" in e for e in errors)


def test_pr_ref_must_be_github() -> None:
    assert validate_gate({"gate": {"kind": "issue", "ref": "somewhere"}})
    url = "https://github.com/ErikBjare/bob/issues/1480"
    assert validate_gate({"gate": {"kind": "issue", "ref": url}}) == []


def test_task_gate_must_be_in_depends() -> None:
    meta: dict = {"gate": {"kind": "task", "ref": "other-task"}}
    assert any("depends" in e for e in validate_gate(meta))
    meta["depends"] = ["other-task"]
    assert validate_gate(meta) == []


def test_derived_probe_shapes() -> None:
    pr = parse_gate({"gate": {"kind": "pr", "ref": "gptme/gptme#42"}})
    assert pr is not None
    probe = derived_probe(pr)
    assert probe is not None and "gh pr view 42 --repo gptme/gptme" in probe
    assert "MERGED|CLOSED" in probe
    t = parse_gate({"gate": {"kind": "time", "deadline": "2026-01-01T00:00:00Z"}})
    assert t is not None and derived_probe(t) == 'test "$(date -u +%s)" -ge 1767225600'
    task = parse_gate({"gate": {"kind": "task", "ref": "x"}, "depends": ["x"]})
    assert task is not None and derived_probe(task) is None


def test_yaml_date_deadline_is_accepted() -> None:
    meta = yaml.safe_load("gate:\n  kind: time\n  deadline: 2026-11-26\n")
    assert validate_gate(meta) == []


def test_second_deadline_push_is_refused() -> None:
    first = {"gate": dict(DECISION)}
    pushed = {"gate": dict(DECISION, deadline="2026-12-03T18:00:00+00:00")}
    assert any("original_deadline" in e for e in check_gate_change(first, pushed))
    pushed["gate"]["original_deadline"] = DECISION["deadline"]
    assert check_gate_change(first, pushed) == []
    again = {"gate": dict(pushed["gate"], deadline="2026-12-10T18:00:00+00:00")}
    assert any("second push is refused" in e for e in check_gate_change(pushed, again))
    erased = {"gate": {k: v for k, v in again["gate"].items() if k != "original_deadline"}}
    assert any("one-time record" in e for e in check_gate_change(pushed, erased))
    # Pulling the deadline earlier is always fine.
    earlier = {"gate": dict(pushed["gate"], deadline="2026-11-30T18:00:00+00:00")}
    assert check_gate_change(pushed, earlier) == []


def _write(tmp_path: Path, gate: dict) -> Path:
    path = tmp_path / "task.md"
    post = frontmatter.Post("# task\n", state="waiting", created="2026-11-12", gate=gate)
    path.write_text(frontmatter.dumps(post))
    return path


def test_mutate_task_stamps_first_push_and_refuses_second(tmp_path: Path) -> None:
    path = _write(tmp_path, dict(DECISION))
    mutate_task(path, patch={"gate": dict(DECISION, deadline="2026-12-03T18:00:00+00:00")})
    gate = frontmatter.load(path).metadata["gate"]
    assert gate["original_deadline"] == DECISION["deadline"]
    with pytest.raises(TransitionError, match="second push is refused"):
        mutate_task(path, patch={"gate": dict(gate, deadline="2026-12-10T18:00:00+00:00")})


def test_mutate_task_refuses_new_invalid_gate(tmp_path: Path) -> None:
    path = _write(tmp_path, dict(DECISION))
    with pytest.raises(TransitionError, match="gate.kind"):
        mutate_task(path, patch={"gate": {"kind": "someday-maybe"}})
