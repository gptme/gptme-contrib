"""Regressions for incomplete TOML, local PostgreSQL and encoded Markdown URIs."""

import pytest
from redact import Severity, redact
from redact.cli import main
from redact.sanitize_document import scan_document


@pytest.mark.parametrize("quote", ['"""', "'''"])
@pytest.mark.parametrize(
    "value", ["\nhunter2\n", "\r\nhunter2\r\n", "first\nlast", "\nlast\\"]
)
def test_unterminated_triple_quote_masks_to_eof(quote, value):
    text = f"PASSWORD={quote}{value}"
    result = redact(text, literals=[])
    assert result.blocked
    assert result.redacted == f"PASSWORD={quote}[REDACTED:secret_assignment]"
    assert result.findings[0].severity == Severity.CRITICAL


@pytest.mark.parametrize("suffix", ["/db", "/", "", "/db?host=/var/run/postgresql"])
@pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
def test_empty_postgres_host_still_masks_credentials(scheme, suffix):
    text = f"{scheme}://app:synthetic-password@{suffix}"
    result = redact(text, literals=[])
    assert result.blocked
    assert "synthetic-password" not in result.redacted
    assert result.redacted.startswith("[REDACTED:connection_string]")
    assert result.findings[0].category == "connection_string"


@pytest.mark.parametrize(
    "destination",
    [
        "java&#x73;cript:alert%281%29",
        "javascript&colon;alert%281%29",
        "vb&#115;cript:msgbox%281%29",
        "d&#97;ta:text/html,payload",
    ],
)
@pytest.mark.parametrize(
    "template",
    [
        "[click]({})",
        "[click](<{}>)",
        "[click][target]\n\n[target]: {}",
        "[click][target]\n\n[target]:\n  <{}>",
    ],
)
def test_encoded_markdown_destination_blocks_once(destination, template):
    text = template.format(destination)
    result = scan_document(text)
    assert result.blocked
    assert len(result.findings) == 1
    assert result.findings[0].category == "html_script"
    assert result.findings[0].count == 1
    assert result.input_chars == len(text)


@pytest.mark.parametrize(
    "text",
    [
        "[safe](https&colon;//example.com)",
        "[safe](java&amp;#x73;cript:example)",
        "Entity prose java&#x73;cript:example",
        '&lt;a href="java&#x73;cript:example"&gt;example&lt;/a&gt;',
    ],
)
def test_entity_prose_and_safe_markdown_stay_clean(text):
    assert not scan_document(text).blocked


@pytest.mark.parametrize(
    "text",
    [
        'PASSWORD="""\nhunter2\n',
        "postgresql://app:synthetic-password@/db",
        "[click](java&#x73;cript:alert%281%29)",
    ],
)
def test_cli_refuses_remaining_bypasses(text, tmp_path, capsys):
    source = tmp_path / "input.md"
    output = tmp_path / "output.md"
    source.write_text(text)
    args = (
        ["sanitize-doc", str(source)]
        if text.startswith("[click]")
        else ["--no-env-secrets", "--out", str(output), str(source)]
    )
    assert main(args) != 0
    assert not output.exists()
    capsys.readouterr()
