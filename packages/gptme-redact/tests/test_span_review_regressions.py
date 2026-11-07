"""Regression coverage for cross-layer masking of original input spans."""

import pytest
from redact import Severity, redact


@pytest.mark.parametrize("literal", ["ghp_", "A" * 8, "ghp_" + "A" * 36])
def test_literal_cannot_hide_credential_shape(literal):
    token = "ghp_" + "A" * 36
    result = redact(f"before {token} after", literals=[literal])
    assert result.redacted == "before [REDACTED:literal] after"
    assert result.blocked
    assert all(f.preview == "****" for f in result.findings)


def test_literal_cannot_hide_assignment_name():
    result = redact("PASSWORD=hunter2", literals=["PASSWORD"])
    assert result.redacted == "[REDACTED:literal]=[REDACTED:secret_assignment]"
    assert result.blocked


def test_pattern_overlap_cannot_leave_assignment_tail():
    result = redact("PASSWORD=ghp_" + "A" * 36 + ",tail", literals=[])
    assert "tail" not in result.redacted
    assert result.blocked
    assert all(f.severity == Severity.CRITICAL for f in result.findings)


@pytest.mark.parametrize("value", ["first}second", "]hunter2", "}hunter2", "a[b]c{d}"])
def test_unquoted_brackets_are_secret_material(value):
    result = redact(f"PASSWORD={value}\nPUBLIC=ok", literals=[])
    assert result.blocked
    assert result.redacted == "PASSWORD=[REDACTED:secret_assignment]\nPUBLIC=ok"


@pytest.mark.parametrize("value", ["first}second", "]hunter2"])
def test_cli_bracket_password_refuses_artifact(value, tmp_path):
    from redact.cli import main

    source = tmp_path / "input.txt"
    output = tmp_path / "output.txt"
    source.write_text(f"PASSWORD={value}")
    assert main([str(source), "--out", str(output), "--no-env-secrets"]) == 2
    assert not output.exists()


@pytest.mark.parametrize(
    "handler", ["onpointerover", "onanimationstart", "onbeforeinput", "ONCONTEXTMENU"]
)
@pytest.mark.parametrize("spacing", [" ", "\n", "/"])
def test_html_event_attribute_family_is_blocked(handler, spacing):
    from redact.sanitize_document import scan_document

    result = scan_document(f'<div{spacing}{handler}="alert(1)">example</div>')
    assert result.blocked
    assert [(f.category, f.count) for f in result.findings] == [("html_script", 1)]


@pytest.mark.parametrize(
    "text",
    [
        'The attribute onpointerover="example" is discussed here.',
        '&lt;div onpointerover="alert(1)"&gt;example&lt;/div&gt;',
        '<div data-onpointerover="example">example</div>',
        '<div title=" onpointerover=example">example</div>',
    ],
)
def test_event_attribute_prose_stays_clean(text):
    from redact.sanitize_document import scan_document

    assert not scan_document(text).blocked
