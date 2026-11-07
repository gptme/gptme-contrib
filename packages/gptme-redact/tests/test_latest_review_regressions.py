"""Regressions for the packaged scanner's latest outage review."""

import base64
import json

import pytest
from redact import redact
from redact.cli import main
from redact.sanitize_document import scan_document


@pytest.mark.parametrize("value", ["x", "hunter2", "abcdefghij"])
@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_short_named_password_is_fully_masked(value, quote):
    text = f"DATABASE_PASSWORD={quote}{value}{quote}"
    result = redact(text, literals=[])
    assert result.blocked
    assert (
        result.redacted
        == f"DATABASE_PASSWORD={quote}[REDACTED:secret_assignment]{quote}"
    )


def _segment(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.mark.parametrize("whitespace", [" ", "\n", "\t", "  "])
@pytest.mark.parametrize("leading", ["", " \n"])
def test_bare_jwt_with_json_whitespace_is_blocked(whitespace, leading):
    header = _segment(leading + "{" + whitespace + '"alg": "HS256" }')
    payload = _segment(leading + "{" + whitespace + '"sub": "example" }')
    token = f"{header}.{payload}.{'c3ludGhldGlj' * 3}"
    result = redact(f"token: {token}", literals=[])
    assert result.blocked
    assert result.redacted == "token: [REDACTED:jwt]"
    assert any(f.category == "jwt" for f in result.findings)


@pytest.mark.parametrize(
    "text", ["release.version.number", "abc.def.ghi", "e30.e30.signature"]
)
def test_non_jwt_dotted_text_is_not_a_jwt(text):
    assert not any(f.category == "jwt" for f in redact(text, literals=[]).findings)


@pytest.mark.parametrize(
    "destination",
    ["javascript:alert%281%29", "vbscript:msgbox(1)", "data:text/html,payload"],
)
@pytest.mark.parametrize(
    "template", ["[target]: {uri}", "  [target]: <{uri}>", "[target]:\n  {uri}"]
)
def test_reference_link_dangerous_scheme_is_blocked(destination, template):
    result = scan_document("[click][target]\n\n" + template.format(uri=destination))
    assert result.blocked
    assert any(f.category == "html_script" and f.count == 1 for f in result.findings)


@pytest.mark.parametrize(
    "destination",
    ["https://example.com/javascript:guide", "mailto:someone@example.com"],
)
def test_reference_link_safe_scheme_is_not_blocked(destination):
    assert not scan_document(f"[click][target]\n\n[target]: {destination}").blocked


@pytest.mark.parametrize(
    "text",
    [
        "DATABASE_PASSWORD=abcdefghij",
        "[click][target]\n\n[target]: javascript:alert%281%29",
    ],
)
def test_cli_latest_bypasses_refuse_output(text, tmp_path, capsys):
    source = tmp_path / "input.md"
    source.write_text(text)
    output = tmp_path / "output.md"
    if text.startswith("DATABASE"):
        code = main([str(source), "--out", str(output), "--no-env-secrets"])
        assert code != 0
        assert not output.exists()
    else:
        code = main(["sanitize-doc", str(source), "--json"])
        assert code != 0
        assert json.loads(capsys.readouterr().out)["blocked"]
