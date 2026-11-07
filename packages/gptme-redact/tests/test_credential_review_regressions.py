"""Regression cases from the c89e5c4a credential review (synthetic secrets only)."""

import pytest
from redact import redact
from redact.cli import main


@pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
@pytest.mark.parametrize("username", ["app.user", "app%40example", "app+user"])
@pytest.mark.parametrize("host", ["localhost", "[::1]", "[2001:db8::1]:5432"])
def test_postgres_userinfo_is_fully_masked(scheme, username, host):
    uri = f"{scheme}://{username}:synthetic-password@{host}/db"
    result = redact(uri, literals=[])
    assert result.blocked
    assert result.redacted == "[REDACTED:connection_string]"
    assert [(f.category, f.count, f.preview) for f in result.findings] == [
        ("connection_string", 1, "****")
    ]


def test_passwordless_postgres_uri_is_not_a_credential():
    uri = "postgresql://app.user@localhost/db"
    result = redact(uri, literals=[])
    assert not result.blocked
    assert not any(f.category == "connection_string" for f in result.findings)


PGP_PRIVATE = (
    "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
    "Version: synthetic fixture\n\n"
    "c3ludGhldGljLW5vdC1hLXJlYWwta2V5\n"
    "-----END PGP PRIVATE KEY BLOCK-----"
)


@pytest.mark.parametrize("label", ["PGP PRIVATE KEY BLOCK", "ENCRYPTED PRIVATE KEY"])
def test_private_key_armor_is_fully_masked(label):
    result = redact(PGP_PRIVATE.replace("PGP PRIVATE KEY BLOCK", label), literals=[])
    assert result.blocked
    assert result.redacted == "[REDACTED:private_key]"
    assert [(f.category, f.count, f.preview) for f in result.findings] == [
        ("private_key", 1, "****")
    ]


def test_pgp_public_key_is_not_a_private_key():
    result = redact(PGP_PRIVATE.replace("PRIVATE", "PUBLIC"), literals=[])
    assert not result.blocked
    assert not any(f.category == "private_key" for f in result.findings)


@pytest.mark.parametrize("quote", ["'", '"', "'''", '"""'])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("prefix", ["", "first-line"])
def test_multiline_password_is_fully_masked(quote, newline, prefix):
    value = f"{prefix}{newline}synthetic-password{newline}last-line{newline}"
    text = f"PASSWORD={quote}{value}{quote}\nNEXT_SETTING=public"
    result = redact(text, literals=[])
    assert result.blocked
    assert result.redacted == (
        f"PASSWORD={quote}[REDACTED:secret_assignment]{quote}\nNEXT_SETTING=public"
    )
    assert all(f.preview == "****" for f in result.findings)


@pytest.mark.parametrize(
    "text",
    [
        "postgresql://app.user:synthetic-password@localhost/db",
        PGP_PRIVATE,
        'PASSWORD="\nsynthetic-password\n"',
    ],
)
def test_cli_credential_bypasses_refuse_output(text, tmp_path, capsys):
    source = tmp_path / "input.txt"
    source.write_text(text)
    output = tmp_path / "output.txt"
    assert main([str(source), "--out", str(output), "--no-env-secrets"]) != 0
    assert not output.exists()
    assert "synthetic-password" not in capsys.readouterr().out
