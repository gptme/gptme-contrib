"""Tests for the GPTME_HEARTBEAT event validator."""

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATOR_PATH = REPO_ROOT / "scripts" / "gptme-heartbeat-validate.py"
EXAMPLES_DIR = REPO_ROOT / "docs" / "protocols" / "examples"
SCHEMA_PATH = REPO_ROOT / "schemas" / "gptme-heartbeat-event.schema.json"


def _load_validator():
    spec = importlib.util.spec_from_file_location(
        "gptme_heartbeat_validate", VALIDATOR_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass module lookups resolve under
    # `from __future__ import annotations`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


def _valid_event(**overrides):
    event = {
        "protocol": "gptme-heartbeat",
        "version": "0.1",
        "event_id": "evt_1",
        "invocation_id": "inv_1",
        "agent_id": "bob",
        "type": "status",
        "occurred_at": "2026-06-09T01:34:00Z",
        "data": {"state": "running"},
    }
    event.update(overrides)
    return event


def _check(*events):
    lines = [json.dumps(e) for e in events]
    return validator.validate_stream(lines)


def test_valid_event_accepted():
    result = _check(_valid_event())
    assert result.ok, result.errors
    assert result.events_checked == 1


def test_example_files_accepted():
    for path in sorted(EXAMPLES_DIR.glob("*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            result = validator.validate_stream(fh)
        assert result.ok, f"{path.name}: {result.errors}"
        assert result.events_checked > 0


def test_missing_required_field_rejected():
    event = _valid_event()
    del event["event_id"]
    result = _check(event)
    assert not result.ok
    assert any("event_id" in e for e in result.errors)


def test_empty_required_field_rejected():
    result = _check(_valid_event(agent_id=""))
    assert not result.ok
    assert any("agent_id" in e for e in result.errors)


def test_wrong_protocol_rejected():
    result = _check(_valid_event(protocol="not-heartbeat"))
    assert not result.ok
    assert any("protocol" in e for e in result.errors)


def test_unknown_type_rejected():
    result = _check(_valid_event(type="bogus.event"))
    assert not result.ok
    assert any("unknown event type" in e for e in result.errors)


def test_bad_status_state_rejected():
    result = _check(_valid_event(data={"state": "exploded"}))
    assert not result.ok
    assert any("status state" in e for e in result.errors)


def test_bad_terminal_state_rejected():
    event = _valid_event(type="invocation.finished", data={"state": "running"})
    result = _check(event)
    assert not result.ok
    assert any("invocation.finished" in e for e in result.errors)


def test_cost_event_requires_provider_and_model():
    event = _valid_event(type="cost", data={"provider": "anthropic"})
    result = _check(event)
    assert not result.ok
    assert any("model" in e for e in result.errors)


def test_cost_event_valid():
    event = _valid_event(
        type="cost", data={"provider": "anthropic", "model": "claude-x"}
    )
    result = _check(event)
    assert result.ok, result.errors


def test_duplicate_event_id_within_invocation_rejected():
    result = _check(_valid_event(event_id="dup"), _valid_event(event_id="dup"))
    assert not result.ok
    assert any("duplicate event_id" in e for e in result.errors)


def test_same_event_id_across_invocations_allowed():
    result = _check(
        _valid_event(event_id="e1", invocation_id="inv_a"),
        _valid_event(event_id="e1", invocation_id="inv_b"),
    )
    assert result.ok, result.errors


def test_invalid_json_line_rejected():
    result = validator.validate_stream(["{not json}"])
    assert not result.ok
    assert any("invalid JSON" in e for e in result.errors)


def test_blank_lines_ignored():
    result = validator.validate_stream(["", "  ", json.dumps(_valid_event())])
    assert result.ok, result.errors
    assert result.events_checked == 1


def test_bad_sequence_rejected():
    result = _check(_valid_event(sequence=0))
    assert not result.ok
    assert any("sequence" in e for e in result.errors)


def test_schema_file_is_valid_json():
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        schema = json.load(fh)
    assert schema["title"].startswith("GPTME_HEARTBEAT")
    assert "event_id" in schema["required"]
