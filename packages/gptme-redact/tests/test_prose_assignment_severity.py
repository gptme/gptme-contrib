"""Prose that merely mentions a key or token must not fail-close outbound (#2202).

The unquoted secret_assignment branch accepts any non-space run after
``key:``/``token =``/``KEY=``. Status reports routinely say things like
``drained key: the probe ...``. Those values are still masked (HIGH) so exports
never leak a tail, but they no longer reach the CRITICAL gate that
``guard_outbound`` blocks on.
"""

import pytest
from redact import Severity, redact

PROSE = [
    "Closed 3 positions at 0.06/token = -$12.34 total",
    'thread sseToken through to EventSource ?token= (<a href="https://example.com">#1</a>)',
    "dropped the assumed data key: <code> from the loader",
    "Adapter output parsing: emit KEY=VALUE output and parse it",
    "Verified the fix against the live drained key: the probe now reports quota",
    "Refunded token = -$1,234.56 to the account",
    "Set API_KEY=<your-key> in the environment",
    "export OPENAI_API_KEY=xxxxxxxx before running",
    # Inline-code-wrapped identifiers and placeholders are prose (#2202 shape 3).
    "dropped the assumed data key: <code>sleep_score</code> from the loader",
    "dropped the assumed data key: <code>sleep_score</code>, then reloaded",
    "the cache key: <tt>user_profile_index</tt> is rebuilt nightly",
    "renamed the lookup key: `sleep_score` in the loader",
    "the dedup token: <code>the</code> was too generic",
    "Set API_KEY=<code>VALUE</code> in the environment",
    "Set API_KEY=`xxxxxxxx` in the environment",
]


@pytest.mark.parametrize("text", PROSE)
def test_prose_assignment_does_not_block(text):
    result = redact(text, literals=[])
    assert not result.blocked, result.block_reasons
    assert all(f.severity < Severity.CRITICAL for f in result.findings)


@pytest.mark.parametrize("text", PROSE)
def test_prose_assignment_still_masked_and_preview_hidden(text):
    # Downgraded, not ignored: export redaction keeps masking the value and the
    # HIGH preview never echoes a prefix/suffix window of it.
    result = redact(text, literals=[])
    findings = [f for f in result.findings if f.category == "secret_assignment"]
    assert findings and findings[0].severity == Severity.HIGH
    assert findings[0].preview == "****"
    assert redact(text, literals=[], block_severity=Severity.HIGH).blocked


@pytest.mark.parametrize(
    "text",
    [
        "COHERE_API_KEY=Xj4kL9mNb2vC8wQ1rT5yU7pA3sD6fG0hJ2kZ9xV",
        "db_password: hunter2_longvalue_XYZ123",
        'PASSWORD="hunter2"',  # quoted values are deliberate assignments
        "PASSWORD='abc'",
        "PASSWORD=$1$saltsalt$hashhashhash",  # crypt hash, not a currency amount
        "API_KEY=<abcdefgh",  # '<' without a tag shape is password bytes
        # Markup must not hide a credential suffix (review ce55fb5132a4).
        "key: <b>Xj4kL9mNb2vC8wQ1",
        "API_KEY=<b>Xj4kL9mNb2vC8wQ1",
        "API_KEY=</b>Xj4kL9mNb2vC8wQ1",
        "API_KEY=<your-key>Xj4kL9mNb2vC8wQ1",
        "API_KEY=<code>Xj4kL9mNb2vC8wQ1</code>",
        # Inline-code wrapping is transparent: only clearly non-credential
        # content is downgraded, so wrapped credential shapes keep blocking.
        "key: <code>Xj4kL9mNb2vC8wQ1</code>",
        "key: `Xj4kL9mNb2vC8wQ1`",
        "password: <code>xmqbtrvlkzphwdsa</code>",  # app-password shape
        "password: <code>correct-horse-battery-staple</code>",  # diceware
        "secret: <code>sleep_score2</code>",  # digits: not a plain identifier
        "key: <code>sleep_score</code>Xj4kL9mNb2vC8wQ1",  # suffix after wrapper
        "key: <code>abc</code><code>Xj4kL9mNb2vC8wQ1</code>",  # second wrapper
        "key: <code>sleep_score</tt>",  # mismatched closing tag
        "key: <b>sleep_score</b>",  # not an inline-code wrapper
        # Tight NAME=value stays env/shell syntax even when wrapped.
        "API_KEY=<code>sleep_score</code>",
        "PASSWORD=`hunter2`",
        # Tight NAME=value is env/shell syntax: short values still block.
        "PASSWORD=hunter2",
        "TOKEN=x",
        "DATABASE_PASSWORD=x",
        # Long values after a spaced/colon separator are not prose-shaped.
        "the token: Xj4kL9mNb2vC8wQ1",
    ],
)
def test_real_assignments_still_critical(text):
    result = redact(text, literals=[])
    assert result.blocked
    assert any(
        f.category == "secret_assignment" and f.severity == Severity.CRITICAL
        for f in result.findings
    )


@pytest.mark.parametrize(
    "text",
    [
        "drained key: the probe; COHERE_API_KEY=Xj4kL9mNb2vC8wQ1rT5yU7pA3sD6fG0hJ2kZ9xV",
        "COHERE_API_KEY=Xj4kL9mNb2vC8wQ1rT5yU7pA3sD6fG0hJ2kZ9xV; drained key: the probe",
    ],
)
def test_prose_hit_cannot_mask_a_later_critical_assignment(text):
    # Findings aggregate per category: the escalated severity must win
    # regardless of which hit came first.
    result = redact(text, literals=[])
    assert result.blocked
    (finding,) = (f for f in result.findings if f.category == "secret_assignment")
    assert finding.severity == Severity.CRITICAL
    assert finding.count == 2
    assert finding.preview == "****"


def test_short_value_overlapping_literal_stays_critical():
    # A Layer-1 literal is CRITICAL regardless of the assignment downgrade.
    result = redact("token: abc12", literals=["abc12"])
    assert result.blocked
