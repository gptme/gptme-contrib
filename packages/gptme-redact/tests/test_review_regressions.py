"""Behavioral regressions for the first packaged-scanner review."""

import json
import subprocess
from unittest.mock import Mock

import pytest
from redact import redact, trufflehog_scan
from redact.cli import main


@pytest.mark.parametrize(
    "value",
    ["abc+15555550100XYZtail", "person@example.com", "prefix/home/example/path"],
)
def test_assignment_blocks_before_medium_substitutions(value):
    result = redact(f"SERVICE_PASSWORD={value}", literals=[])
    assert result.blocked
    assert result.redacted == "SERVICE_PASSWORD=[REDACTED:secret_assignment]"
    assert all(f.preview == "****" for f in result.findings)


@pytest.mark.parametrize("quote", ['"', "'"])
def test_quoted_secret_key_blocks_and_preserves_structure(quote):
    value = "synthetic-prefixless-value-12345"
    text = f"{{{quote}COHERE_API_KEY{quote}: {quote}{value}{quote}}}"
    result = redact(text, literals=[])
    assert result.blocked
    assert result.redacted == text.replace(value, "[REDACTED:secret_assignment]")


def test_cli_assignment_is_rejected_without_artifact(tmp_path, capsys):
    source = tmp_path / "input.txt"
    source.write_text(' {"SERVICE_PASSWORD": "person@example.com"}')
    output = tmp_path / "output.txt"
    code = main([str(source), "--out", str(output), "--no-env-secrets"])
    assert code != 0
    assert not output.exists()
    assert "person@example.com" not in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["exit", "timeout", "oserror", "malformed"])
def test_trufflehog_failures_are_not_clean(failure, monkeypatch):
    monkeypatch.setattr("redact.shutil.which", lambda _: "/fake/trufflehog")
    run = Mock(return_value=subprocess.CompletedProcess([], 1, "", "sensitive stderr"))
    if failure == "timeout":
        run.side_effect = subprocess.TimeoutExpired("trufflehog", 60)
    elif failure == "oserror":
        run.side_effect = OSError("sensitive error")
    elif failure == "malformed":
        run.return_value = subprocess.CompletedProcess([], 0, "not JSON", "")
    monkeypatch.setattr("redact.subprocess.run", run)
    with pytest.raises(RuntimeError, match="[Tt]ruffle[Hh]og"):
        trufflehog_scan("ordinary text")


def test_cli_trufflehog_failure_has_no_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("redact.shutil.which", lambda _: "/fake/trufflehog")
    monkeypatch.setattr(
        "redact.subprocess.run",
        Mock(return_value=subprocess.CompletedProcess([], 2, "", "sensitive stderr")),
    )
    source = tmp_path / "input.txt"
    source.write_text("ordinary text")
    output = tmp_path / "output.txt"
    code = main([str(source), "--out", str(output), "--trufflehog", "--no-env-secrets"])
    assert code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "TruffleHog" in captured.err
    assert "sensitive stderr" not in captured.err
    assert not output.exists()


def test_trufflehog_success_clean_and_finding(monkeypatch):
    monkeypatch.setattr("redact.shutil.which", lambda _: "/fake/trufflehog")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr("redact.subprocess.run", run)
    assert trufflehog_scan("ordinary text") == []
    run.return_value = subprocess.CompletedProcess(
        [], 0, json.dumps({"DetectorName": "Example", "Raw": "synthetic-secret"}), ""
    )
    findings = trufflehog_scan("ordinary text")
    assert len(findings) == 1
    assert findings[0].category == "trufflehog:Example"
    assert findings[0].preview == "****"


def test_assignment_is_not_hidden_by_high_severity_token():
    result = redact("SERVICE_PASSWORD=" + "ghs_" + "A" * 36)
    assert result.blocked
    assert result.redacted == "SERVICE_PASSWORD=[REDACTED:secret_assignment]"


@pytest.mark.parametrize("quote", ['"', "'", ""])
@pytest.mark.parametrize(
    "value", ["!longpassword123", "abc!def$ghi%jkl", "(synthetic-password)"]
)
def test_assignment_password_punctuation_is_fully_masked(quote, value):
    text = f"SERVICE_PASSWORD={quote}{value}{quote}"
    result = redact(text, literals=[])
    assert result.blocked
    assert (
        result.redacted
        == f"SERVICE_PASSWORD={quote}[REDACTED:secret_assignment]{quote}"
    )


@pytest.mark.parametrize("quote", ['"', "'"])
def test_quoted_password_spaces_are_not_partial_redacted(quote):
    value = "a long synthetic password!"
    text = f"SERVICE_PASSWORD={quote}{value}{quote}, other=value"
    result = redact(text, literals=[])
    assert result.blocked
    assert result.redacted == text.replace(value, "[REDACTED:secret_assignment]")


@pytest.mark.parametrize(
    "value", [r"long\"synthetic password!", r"long\'synthetic password!"]
)
def test_escaped_quote_in_password_is_consumed(value):
    quote = '"' if '\\"' in value else "'"
    text = f"SERVICE_PASSWORD={quote}{value}{quote}"
    result = redact(text, literals=[])
    assert result.blocked
    assert (
        result.redacted
        == f"SERVICE_PASSWORD={quote}[REDACTED:secret_assignment]{quote}"
    )
