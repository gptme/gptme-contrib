"""Regression coverage for empty audit values and shell value fragments."""

import pytest
from redact import redact
from redact.memory_boundary import audit_workspace, preview_migration


@pytest.mark.parametrize("separator", ["=", ":", "= \t", ": \t"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_audit_empty_placeholder_does_not_consume_next_line(
    separator, newline, tmp_path
):
    memory = tmp_path / "project-memory"
    memory.mkdir()
    note = memory / "note.md"
    text = f"API_KEY{separator}{newline}PUBLIC=ok"
    note.write_text(text)
    audit = audit_workspace(tmp_path)
    assert len(audit.verdicts) == 1
    assert audit.verdicts[0].classification == "durable_project_memory"
    assert preview_migration(tmp_path).safe_to_proceed
    assert note.read_text() == text.replace("\r\n", "\n")


@pytest.mark.parametrize("value", ["x", " \tx", '"synthetic"', "'synthetic'"])
def test_audit_nonempty_placeholder_still_refused(value, tmp_path):
    memory = tmp_path / "project-memory"
    memory.mkdir()
    (memory / "note.md").write_text(f"API_KEY={value}\nPUBLIC=ok")
    assert not preview_migration(tmp_path).safe_to_proceed


@pytest.mark.parametrize(
    "value",
    [
        "'first'last",
        '"first"last',
        "first'last'",
        'first"last"',
        "'first'\"middle\"last",
        "''last",
        '""last',
        "first''last",
        r'"first"last\ word',
        "'[REDACTED:literal]'last",
    ],
)
def test_shell_fragments_are_masked_as_one_value(value):
    result = redact(f"PASSWORD={value}\nPUBLIC=ok", literals=[])
    assert result.blocked
    assert result.redacted == "PASSWORD=[REDACTED:secret_assignment]\nPUBLIC=ok"
    assert [(f.category, f.count) for f in result.findings] == [
        ("secret_assignment", 1)
    ]


@pytest.mark.parametrize("text", ["PASSWORD=''", 'PASSWORD=""'])
def test_empty_quoted_assignment_stays_empty(text):
    result = redact(text, literals=[])
    assert result.redacted == text
    assert not result.blocked


def test_json_quoted_password_preserves_structure():
    result = redact('{"PASSWORD": "first", "public": "last"}', literals=[])
    assert (
        result.redacted
        == '{"PASSWORD": "[REDACTED:secret_assignment]", "public": "last"}'
    )


def test_cli_fragment_password_refuses_output(tmp_path):
    from redact.cli import main

    source = tmp_path / "input.txt"
    output = tmp_path / "output.txt"
    source.write_text("PASSWORD='first'last")
    assert main([str(source), "--out", str(output), "--no-env-secrets"]) == 2
    assert not output.exists()
