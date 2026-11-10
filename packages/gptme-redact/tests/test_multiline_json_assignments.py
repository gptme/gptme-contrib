"""Quoted mapping keys allow line breaks without consuming empty env values."""

import json

import pytest
from redact import redact
from redact.cli import main
from redact.memory_boundary import preview_migration


@pytest.mark.parametrize("padding", ["\n", "\r\n", " \t\n  ", "\n\n\t"])
@pytest.mark.parametrize("key", ["PASSWORD", "SERVICE_API_KEY"])
def test_json_value_after_newline_is_fully_masked(padding, key):
    text = f'{{"{key}":{padding}"hunter2", "public": "ok"}}'
    result = redact(text, literals=[])
    assert result.blocked
    assert result.redacted == text.replace("hunter2", "[REDACTED:secret_assignment]")
    assert json.loads(result.redacted) == {
        key: "[REDACTED:secret_assignment]",
        "public": "ok",
    }
    assert [(f.category, f.count) for f in result.findings] == [
        ("secret_assignment", 1)
    ]


@pytest.mark.parametrize("text", ["API_KEY=\nPUBLIC=ok", "API_KEY:\nPUBLIC=ok"])
def test_empty_line_assignment_does_not_consume_next_setting(text):
    result = redact(text, literals=[])
    assert not result.blocked
    assert result.redacted == text
    assert result.findings == []


def test_multiline_json_cli_refuses_output(tmp_path):
    source = tmp_path / "input.json"
    output = tmp_path / "output.json"
    source.write_text('{"PASSWORD":\n"hunter2"}')
    assert main([str(source), "--out", str(output), "--no-env-secrets"]) == 2
    assert not output.exists()


def test_multiline_json_memory_migration_is_refused(tmp_path):
    memory = tmp_path / "project-memory"
    memory.mkdir()
    (memory / "note.json").write_text('{"PASSWORD":\n"hunter2"}')
    assert not preview_migration(tmp_path).safe_to_proceed
