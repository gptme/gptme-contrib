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


@pytest.mark.parametrize(
    "destination",
    [
        r"javascript\:alert%281%29",
        r"java\script:example",
        r"vbscript\:example",
        r"data\:text/html,payload",
    ],
)
@pytest.mark.parametrize("template", ["[click]({})", "[click][target]\n\n[target]: {}"])
def test_markdown_punctuation_escapes_follow_markdown_rules(destination, template):
    result = scan_document(template.format(destination))
    # Markdown only unescapes ASCII punctuation, not letters.
    assert result.blocked == (destination != r"java\script:example")


@pytest.mark.parametrize(
    "value", ["first,second", "first;second", "first,second;third"]
)
def test_unquoted_punctuation_secret_has_no_surviving_suffix(value):
    result = redact(f"PASSWORD={value}", literals=[])
    assert result.blocked
    assert result.redacted == "PASSWORD=[REDACTED:secret_assignment]"


@pytest.mark.parametrize(
    "literals",
    [
        ["abcdefghij", "fghijklmno"],
        ["fghijklmno", "abcdefghij"],
        ["abcdefghij", "fghijklmno", "ijklmnoXYZ"],
    ],
)
def test_crossing_literal_intervals_mask_their_union(literals):
    text = "abcdefghijklmnoXYZ" if len(literals) == 3 else "abcdefghijklmno"
    result = redact(text, literals=literals)
    assert result.blocked
    assert result.redacted == "[REDACTED:literal]"


def test_adjacent_literals_and_empty_literals_preserve_public_text():
    result = redact("prefix abcdef suffix", literals=["", "abc", "def", "abc"])
    assert result.redacted == "prefix [REDACTED:literal][REDACTED:literal] suffix"
