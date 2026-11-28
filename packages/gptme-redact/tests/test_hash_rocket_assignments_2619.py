"""PHP/Ruby hash-rocket secrets without matching arrow functions (gptme/gptme-contrib#2619).

``'password' => 'secret'`` (PHP quoted key) and ``:password => 'secret'``
(Ruby symbol key) must be detected, while ordinary arrow functions and
``foreach`` loops (``key => key.trim()``, ``$key => $headerValue``) must not
fire. Fixtures are synthetic. ``redact()`` is called with ``literals=[]`` so
the env layer never participates.
"""

from __future__ import annotations

import pytest
from redact import redact

SECRET = "Zq9xVb7mN3kL2pR8"


def _redacted(text: str) -> str:
    return redact(text, literals=[]).redacted


@pytest.mark.parametrize(
    "text",
    [
        "'password' => '" + SECRET + "'",  # PHP single-quoted key
        '"password" => "' + SECRET + '"',  # PHP double-quoted key
        "'api_key' => '" + SECRET + "'",  # snake_case quoted key
        ":password => '" + SECRET + "'",  # Ruby symbol key
        ":api_key => '" + SECRET + "'",  # Ruby snake_case symbol
        ":password=>'" + SECRET + "'",  # no surrounding whitespace
        "'secret_key' => '" + SECRET + "'",  # prefixed quoted key
        ":secret_key => '" + SECRET + "'",  # prefixed Ruby symbol
    ],
)
def test_hash_rocket_secret_is_redacted(text: str) -> None:
    out = _redacted(text)
    assert SECRET not in out
    assert "[REDACTED:secret_assignment]" in out


@pytest.mark.parametrize(
    "text",
    [
        "'password' => '" + SECRET,  # truncated single-quoted value (EOF)
        '"password" => "' + SECRET,  # truncated double-quoted value (EOF)
        ":password => '" + SECRET,  # truncated Ruby symbol value
        "'api_key' => '" + SECRET + "\nnext line",  # truncated at newline
    ],
)
def test_truncated_hash_rocket_secret_is_redacted(text: str) -> None:
    out = _redacted(text)
    assert SECRET not in out
    assert "[REDACTED:secret_assignment]" in out


@pytest.mark.parametrize(
    "text",
    [
        "items.map(key => key.trim())",  # JS arrow function
        "const f = key => key.trim()",  # bare arrow
        "foreach ($data as $key => $headerValue) {}",  # PHP foreach
        "'password' => $hash",  # variable value: not statically a secret
        "'name' => 'Bob'",  # non-secret key
        "items.map(key => 'x')",  # arrow function with a string body
        "key => 'x'",  # bare `key` is not treated as a secret-named key
        "password => 'x'",  # bare key: valid JS arrow, invalid PHP/Ruby hash
        "'key' => 'x'",  # generic `key` array entry, not a credential
        ":key => 'x'",  # generic `key` symbol, not a credential
        "'keys' => 'x'",  # generic `keys` array entry
    ],
)
def test_arrow_functions_and_non_secrets_do_not_fire(text: str) -> None:
    assert "[REDACTED:secret_assignment]" not in _redacted(text)


def test_truncated_backslash_does_not_swallow_next_line() -> None:
    # A trailing backslash at end of line keeps the backslash but not the break,
    # so the redaction stops at the line boundary instead of consuming the rest
    # of the document.
    out = _redacted("'password' => '" + SECRET + "\\\nnext line")
    assert SECRET not in out
    assert "next line" in out
