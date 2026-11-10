"""Secret-safe export redactor — reusable package for idea backlog #362.

A layered, fail-closed redactor that makes publishing derived agent content
safe enough to actually do. Stolen from the ``badlogic/pi-share-hf`` redaction
pipeline.

The full pipeline pi describes has five layers:

    1. Deterministic redaction   — strip exact secrets (env values + literals)
    2. Pattern filtering         — regex deny-list (this module)
    3. TruffleHog scanning       — any finding blocks  (optional, ``--trufflehog``)
    4. LLM fit-to-publish review — judge safety         (NOT in this package)
    5. Human gate                — dry-run / list / reject (report mode here)
    6. Prompt-injection scan     — detect injection surfaces in untrusted text

This package implements layers 1, 2, 3 (optional via ``trufflehog_scan()``),
6 (optional via ``scan_prompt_injection()``), and a report/dry-run flavor of 5.

Fail-closed: any finding at a *blocking* severity (default: ``critical``) rejects
the whole artifact rather than silently redacting it.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path


class Severity(IntEnum):
    """Ordered so thresholds compare numerically (critical is highest)."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @staticmethod
    def parse(name: str) -> Severity:
        return Severity[name.upper()]


@dataclass
class Pattern:
    """A single deny-list entry."""

    regex: re.Pattern
    severity: Severity
    category: str
    # When True, only the named ``value`` group is redacted and the rest of the
    # match (e.g. the ``NAME=`` prefix) is preserved. Used by the secret-assignment
    # pattern so output keeps the key name for debuggability while masking the value.
    value_only: bool = False


# --- Layer 2 deny-list -------------------------------------------------------
# Ordering matters: specific high-severity patterns run before generic
# ones so a token is categorized by its strongest match. All patterns discover
# original-input spans; declared order breaks category ties in overlapping spans.
_PATTERNS: list[Pattern] = [
    # CRITICAL — live API tokens with well-known key formats.
    # Anthropic API keys are "sk-ant-api03-" + ~95 base64url chars (incl "_" and
    # "-"). The original char class here was base62-only and length-capped
    # ([A-Za-z0-9]{24,48}): a real key broke at the first "_"/"-", so the generic
    # openai_key pattern below then matched only a 64-char window, partial-leaking
    # the token tail into the "redacted" text AND mislabeling it openai_key — the
    # exact failure the OAuth/Admin patterns below document at length. Directly
    # relevant here: this is the credential Bob's own env most often carries, so a
    # partial leak is the worst case. Base64url + unbounded {20,} consumes the
    # whole token first; declared first so it keeps its own category before any
    # generic sk- pattern runs.
    Pattern(
        re.compile(r"(?:sk-)?ant-api(?:key)?03?-[A-Za-z0-9_-]{20,}"),
        Severity.CRITICAL,
        "anthropic_key",
    ),
    # Anthropic OAuth tokens — Claude Code subscription auth uses sk-ant-oat01-
    # (access) and sk-ant-ort01- (refresh) prefixes, distinct from the
    # sk-ant-api03- *API key* format above. Directly relevant here: this is
    # Bob's PRIMARY credential (~/.claude/.credentials.json), so it is the token
    # most likely present in Bob's env — same reasoning the ya29./hf_ patterns
    # use. MUST be declared before the generic openai_key pattern: that pattern
    # caps at 64 base62 chars after "sk-", so on a real (longer) OAuth token it
    # would redact only a 64-char window and leave the tail exposed — a partial
    # leak that defeats the fail-closed gate. Unbounded {20,} here consumes the
    # whole token first. The char class includes "_-" because OAuth tokens are
    # base64url, unlike the hex/base62 API key format.
    Pattern(
        re.compile(r"sk-ant-(?:oat|ort)\d{2}-[A-Za-z0-9_-]{20,}"),
        Severity.CRITICAL,
        "anthropic_oauth_token",
    ),
    # Anthropic Admin API keys: "sk-ant-admin01-" + ~93 base64url chars. These
    # are the highest-severity Anthropic credential — the Admin API can create,
    # list, and revoke API keys org-wide, so a leak is org-takeover material.
    # The "ant-admin" infix means openai_project_key ("sk-(?:proj|svcacct|admin)-")
    # does NOT match (its prefix is "sk-admin-", not "sk-ant-admin-"). MUST be
    # declared before the generic openai_key pattern: that pattern *does* match
    # "sk-ant-admin01-..." but caps at 64 chars after "sk-", leaving the tail
    # exposed AND mislabeling it as openai_key — the exact partial-leak bug the
    # patterns above guard against. Unbounded {20,} consumes the whole key first.
    # The char class includes "_-" because admin keys are base64url.
    Pattern(
        re.compile(r"sk-ant-admin\d{2}-[A-Za-z0-9_-]{20,}"),
        Severity.CRITICAL,
        "anthropic_admin_key",
    ),
    # Modern OpenRouter API keys: "sk-or-v1-" + 64 hex (73 chars total).
    # Directly relevant here: OpenRouter is one of Bob's PRIMARY LLM providers
    # (scoped keys in config.local.toml), so this token is likely present in the
    # env — same reasoning the ya29./hf_ patterns use. MUST be declared before
    # the generic openai_key pattern: the "or-v1-" prefix is base62, so openai_key
    # *does* match, but it caps at 64 chars after "sk-" and would leave a ~6-char
    # tail exposed AND mislabel the token as openai_key — the exact partial-leak
    # bug the anthropic_oauth_token pattern above guards against. Unbounded {20,}
    # consumes the whole token first.
    Pattern(
        re.compile(r"sk-or-v1-[A-Za-z0-9]{20,}"),
        Severity.CRITICAL,
        "openrouter_key",
    ),
    # Modern OpenAI keys: project ("sk-proj-"), service-account ("sk-svcacct-")
    # and admin ("sk-admin-") keys. These are long (~130-200 base64url chars),
    # far longer than the legacy "sk-" + 48 format. MUST be declared before the
    # generic openai_key pattern: that pattern caps at 64 chars after "sk-", so
    # on a real sk-proj- key it would redact only a 64-char window and leave the
    # ~100-char tail exposed AND mislabel it as openai_key — the exact
    # partial-leak bug the openrouter_key / anthropic_oauth_token patterns above
    # guard against. Unbounded {20,} consumes the whole token first. The char
    # class includes "_-" because these keys are base64url.
    Pattern(
        re.compile(r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}"),
        Severity.CRITICAL,
        "openai_project_key",
    ),
    # Legacy/bare-hex sk- tokens (e.g. some self-hosted proxies / older
    # OpenRouter keys). The modern "sk-or-v1-" OpenRouter format is handled by
    # the dedicated pattern above. MUST be declared before the generic openai_key
    # pattern: that pattern caps at 64 chars after "sk-", so on a pure-hex token
    # LONGER than 64 chars it would redact only a 64-char window and leave the hex
    # tail exposed — the exact partial-leak the other specific patterns above
    # guard against (the fail-closed gate still blocks, but the *redacted text*
    # must never carry a token tail). Unbounded-ish {32,128} consumes the whole
    # hex token first. The trailing negative lookahead keeps this fix safe for the
    # common case: it fires only on a *pure*-hex token, so a base62 key that
    # merely begins with >=32 hex chars (e.g. a legacy "sk-" + mixed-case key)
    # fails the lookahead and correctly falls through to openai_key, which redacts
    # it whole. (Declared after openai_key, this pattern was dead code — openai_key
    # matched first for every hex sk- token and silently truncated the long ones.)
    Pattern(
        re.compile(r"(?<![A-Za-z])sk-[a-f0-9]{32,128}(?![A-Za-z0-9_-])"),
        Severity.CRITICAL,
        "openrouter_key",
    ),
    # Negative lookbehind: prevent matching `sk-` that is part of a longer word
    # (e.g. `risk-and-taste-based-self-merge` contains `sk-and-taste-…` which is
    # 26 alphanumeric+hyphen chars — a false positive that blocked call-receipt
    # emails for 3 days, 2026-10-23..26). Real API keys appear after whitespace,
    # punctuation, or an assignment character (`=`/`:`) — never after a letter.
    Pattern(
        re.compile(r"(?<![A-Za-z])(?:sk|pk)-[A-Za-z0-9_-]{24,64}"),
        Severity.CRITICAL,
        "openai_key",
    ),
    Pattern(
        re.compile(r"ghp_[A-Za-z0-9]{36}"),
        Severity.CRITICAL,
        "github_token",
    ),
    Pattern(
        re.compile(r"gho_[A-Za-z0-9]{36}"),
        Severity.CRITICAL,
        "github_oauth",
    ),
    Pattern(
        re.compile(r"ghu_[A-Za-z0-9]{16,36}"),
        Severity.CRITICAL,
        "github_user_token",
    ),
    # Fine-grained PAT: github_pat_ + 22 base62 + "_" + 59 base62 (82 total).
    # The legacy ghp_/gho_/ghu_ patterns above do NOT cover this newer format.
    Pattern(
        re.compile(r"github_pat_[A-Za-z0-9_]{82}"),
        Severity.CRITICAL,
        "github_finegrained_pat",
    ),
    Pattern(
        re.compile(r"AKIA[0-9A-Z]{16}"),
        Severity.CRITICAL,
        "aws_access_key",
    ),
    Pattern(
        re.compile(r"xox[baprs]-[A-Za-z0-9-]{24,80}"),
        Severity.CRITICAL,
        "slack_token",
    ),
    # Slack app-level tokens (Socket Mode) carry the "xapp-" prefix, which the
    # xox[baprs]- pattern above deliberately does NOT match. They grant
    # connections:write across a workspace, so a leak is a real takeover vector.
    Pattern(
        re.compile(r"xapp-\d-[A-Za-z0-9_-]{20,}"),
        Severity.CRITICAL,
        "slack_app_token",
    ),
    # Slack incoming-webhook URLs are bearer credentials: anyone holding the URL
    # can post to the target channel. The path segments (T.../B.../secret) are
    # never matched by the token patterns above.
    Pattern(
        re.compile(
            r"https://hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]{20,}"
        ),
        Severity.CRITICAL,
        "slack_webhook_url",
    ),
    Pattern(
        re.compile(r"AIza[0-9A-Za-z_-]{35}"),
        Severity.CRITICAL,
        "google_api_key",
    ),
    # Google OAuth2 access tokens carry the distinctive "ya29." prefix. The
    # AIza pattern above only matches static API keys, never OAuth bearer
    # tokens, so a leaked ya29. token would otherwise slip through. Directly
    # relevant here: Bob connects to Gmail/Calendar/Drive via Google OAuth, so
    # a ya29. token is exactly the credential likely present in the env.
    Pattern(
        re.compile(r"ya29\.[A-Za-z0-9_-]{20,}"),
        Severity.CRITICAL,
        "google_oauth_token",
    ),
    # Hugging Face user access tokens (hf_ + 34 base62). Especially relevant
    # here: this package exists to make publishing derived content *to Hugging
    # Face* safe, so an hf_ write token is exactly the credential likely present
    # in the publishing environment. No other pattern matches the hf_ prefix.
    Pattern(
        re.compile(r"hf_[A-Za-z0-9]{34,}"),
        Severity.CRITICAL,
        "huggingface_token",
    ),
    # GitLab personal/project/group access tokens (glpat- + >=20 chars).
    Pattern(
        re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),
        Severity.CRITICAL,
        "gitlab_token",
    ),
    # npm automation/publish tokens (npm_ + 36 base62).
    Pattern(
        re.compile(r"npm_[A-Za-z0-9]{36}"),
        Severity.CRITICAL,
        "npm_token",
    ),
    # PyPI upload tokens (pypi- + base64url payload). Leak lets an attacker
    # publish malicious package releases.
    Pattern(
        re.compile(r"pypi-[A-Za-z0-9_-]{16,}"),
        Severity.CRITICAL,
        "pypi_token",
    ),
    # SendGrid API keys are "SG." + a 22-char base64url id + "." + a 43-char
    # base64url secret. The leading "SG." plus two base64url segments is
    # distinctive; no other pattern matches it. Leak grants full mail-send.
    Pattern(
        re.compile(r"SG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{30,}"),
        Severity.CRITICAL,
        "sendgrid_api_key",
    ),
    # xAI (Grok) API keys carry the distinctive "xai-" prefix followed by ~80
    # base62 chars — NOT an "sk-" prefix. The sk-[a-f0-9] "xai_key" pattern below
    # only catches short hex sk- tokens (a generic fallback that predates xAI's
    # real key format); a genuine xai- key matches none of the sk-/pk- patterns
    # and would otherwise leak in full. Declared first so the real format is
    # caught before the legacy hex fallback.
    Pattern(
        re.compile(r"xai-[A-Za-z0-9]{20,}"),
        Severity.CRITICAL,
        "xai_key",
    ),
    Pattern(
        re.compile(r"(?<![A-Za-z])sk-[a-f0-9]{16,48}"),
        Severity.CRITICAL,
        "xai_key",
    ),
    # Perplexity API keys carry the distinctive "pplx-" prefix + a long base62
    # payload — NOT an "sk-"/"pk-" prefix, so none of the generic patterns above
    # catch them. Directly relevant here: Bob uses Perplexity for research search
    # (see research-when-stumbling lesson), so a pplx- key is exactly the kind of
    # credential likely present in the env, and it would otherwise leak in full.
    Pattern(
        re.compile(r"pplx-[A-Za-z0-9]{20,}"),
        Severity.CRITICAL,
        "perplexity_api_key",
    ),
    # Groq API keys use the distinctive "gsk_" UNDERSCORE prefix (not the
    # hyphenated sk-/pk- forms), so the generic patterns never match them.
    Pattern(
        re.compile(r"gsk_[A-Za-z0-9]{20,}"),
        Severity.CRITICAL,
        "groq_api_key",
    ),
    # Replicate API tokens carry the distinctive "r8_" prefix + ~37 base62 chars.
    # No other pattern matches the r8_ prefix; leak grants full inference/billing.
    Pattern(
        re.compile(r"r8_[A-Za-z0-9]{20,}"),
        Severity.CRITICAL,
        "replicate_api_key",
    ),
    # Twilio API Key SID: uppercase "SK" + exactly 32 hex chars (no dash).
    # Directly relevant here: Bob runs Twilio telephony (voice standup calls), so
    # this credential can surface in call logs or pasted config — same env-presence
    # reasoning the ya29./hf_ patterns use. The generic openai_key / openrouter_key
    # sk- patterns do NOT match it: they require a *lowercase* "sk-" with a literal
    # dash, whereas a Twilio key is uppercase "SK" followed directly by hex. The
    # Twilio auth token is a prefix-less 32-hex string (no distinctive vector) and
    # is left to the env-name layer per the module policy above. Lookbehind/ahead
    # anchor the 2-char prefix and exact length so it can't partial-match a slice
    # of a longer hex run (e.g. an "SK"-prefixed 40-char git SHA).
    Pattern(
        re.compile(r"(?<![A-Za-z0-9])SK[0-9a-f]{32}(?![0-9a-f])"),
        Severity.CRITICAL,
        "twilio_api_key",
    ),
    # Stripe secret/restricted keys use an UNDERSCORE prefix (sk_live_, rk_test_),
    # so the hyphenated sk-/pk- patterns above never match them. Publishable keys
    # (pk_live_/pk_test_) are public by design and deliberately excluded to avoid
    # over-redacting legitimately-published content.
    Pattern(
        re.compile(r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
        Severity.CRITICAL,
        "stripe_secret_key",
    ),
    # Stripe webhook signing secret (whsec_) — gates request-authenticity, leak
    # lets an attacker forge webhook events.
    Pattern(
        re.compile(r"whsec_[A-Za-z0-9]{16,}"),
        Severity.CRITICAL,
        "stripe_webhook_secret",
    ),
    # PostgreSQL connection strings — the password is embedded in the URL between
    # the username and the @host. Leak grants full database access. The
    # `postgres(ql)?://` prefix is distinctive; the pattern requires a password
    # segment (`:password@`) so passwordless URLs are not matched.
    Pattern(
        re.compile(
            r"postgres(?:ql)?://[A-Za-z0-9._~!$&'()*+,;=%-]+:[^@\s]+@"
            r"(?:\[[A-Za-z0-9:.%_-]+\]|[a-zA-Z0-9_.-]+)?"
            r"(?::[0-9]+)?(?:/[a-zA-Z0-9_-]*)?"
        ),
        Severity.CRITICAL,
        "connection_string",
    ),
    # Discord bot tokens — three base64url segments (encoded user-id snowflake
    # ".", encoded timestamp ".", HMAC). The snowflake segment begins with
    # M/N/O for current Discord epoch IDs, so it never starts with "eyJ" and the
    # jwt pattern below cannot catch it; a bare bot token has no "Bearer " prefix
    # either. Directly relevant here: Bob engages on Discord (see SOCIAL.md), so a
    # bot token is exactly the credential likely present — and leaking it grants
    # full bot-account takeover.
    Pattern(
        re.compile(
            r"[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,38}"
        ),
        Severity.CRITICAL,
        "discord_bot_token",
    ),
    # Telegram bot tokens — `{bot_id}:{hash}` where bot_id is 8–10 digits and
    # hash is ~35 base64url chars (HMAC-SHA256). The colon separator and the
    # digit prefix make this shape distinctive; no other pattern matches it.
    Pattern(
        re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,40}\b"),
        Severity.CRITICAL,
        "telegram_bot_token",
    ),
    Pattern(
        re.compile(
            r"-----BEGIN (?P<key_label>(?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?)-----"
            r".*?"
            r"-----END (?P=key_label)-----",
            re.DOTALL,
        ),
        Severity.CRITICAL,
        "private_key",
    ),
    Pattern(
        re.compile(
            r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}",
            re.IGNORECASE,
        ),
        Severity.CRITICAL,
        "bearer_header",
    ),
    # JWT (JSON Web Token) — header.payload.signature in base64url. The header
    # and payload are base64url-encoded JSON objects, which may include JSON
    # whitespace. Validate decoded objects rather than assuming an eyJ prefix.
    # Supabase service-role keys, OAuth access
    # tokens, and session tokens commonly ship as *bare* JWTs without a
    # "Bearer " prefix, so the bearer_header pattern above never catches them.
    # Declared after bearer_header so Bearer-prefixed tokens keep that category.
    Pattern(
        re.compile(
            r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}"
            r"\.[A-Za-z0-9_-]{10,}(?![A-Za-z0-9_-])"
        ),
        Severity.CRITICAL,
        "jwt",
    ),
    # HIGH — less distinctive but still valuable.
    Pattern(
        re.compile(r"ghs_[A-Za-z0-9]{36}"),
        Severity.HIGH,
        "github_app_token",
    ),
    Pattern(
        re.compile(r"ghr_[A-Za-z0-9]{36}"),
        Severity.HIGH,
        "github_refresh_token",
    ),
    # Discord webhook URLs — leaking one lets anyone post messages into the bound
    # channel (spam/phishing as the integration), though not take over the bot,
    # hence HIGH rather than CRITICAL. The URL shape is unambiguous, so there is
    # no over-redaction risk.
    Pattern(
        re.compile(
            r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/(?:v\d+/)?"
            r"webhooks/[0-9]{17,20}/[A-Za-z0-9_-]{50,}"
        ),
        Severity.HIGH,
        "discord_webhook_url",
    ),
    # MEDIUM — contextual info that shouldn't be public.
    Pattern(
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        Severity.MEDIUM,
        "email",
    ),
    Pattern(
        re.compile(r"/home/(bob|alice|gordon|sven)(?:/|$)"),
        Severity.MEDIUM,
        "agent_home_path",
    ),
    Pattern(
        re.compile(r"/home/[^/]{2,32}(?:/|$)"),
        Severity.MEDIUM,
        "other_home_path",
    ),
    Pattern(
        re.compile(r"\+[0-9]{7,15}"),
        Severity.MEDIUM,
        "phone_number",
    ),
    Pattern(
        re.compile(r"0x[a-fA-F0-9]{40}"),
        Severity.MEDIUM,
        "ethereum_address",
    ),
    # Shell NAME=VALUE words can concatenate quoted and unquoted fragments.
    # Discover the entire word, including quote bytes, when multiple fragments
    # occur. The ordinary fallback below preserves quotes for a single quoted
    # value and supports JSON/TOML/truncated snippets. Restrict this extra form
    # to '=' so JSON's closing quote/comma cannot become a shell fragment.
    Pattern(
        re.compile(
            r"\b(?:[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*_)?"
            r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)\b"
            r"[ \t]*=[ \t]*"
            r"(?!\"\"\"|''')"
            r"(?P<value>(?:[^\s'\"\\]++|\\[^\r\n]"
            r"|'(?:\\[\s\S]|[^'\\])*'|\"(?:\\[\s\S]|[^\"\\])*\"){2,})"
            r"(?<![,;])(?=\s|\Z)",
            re.IGNORECASE,
        ),
        Severity.CRITICAL,
        "secret_assignment",
        value_only=True,
    ),
    # Secret-named assignment in free text (e.g. an exported .env snippet or a
    # logged `export COHERE_API_KEY=...` line) — the catch-all net of LAST resort.
    # This is the in-text counterpart of the Layer-1 env-name collector: the
    # "Deliberately NOT pattern-matched" boundary below relies on os.environ to
    # catch PREFIX-LESS secrets (Cohere, Together, Deepgram, Twilio auth token,
    # ...), but that only fires when the value is in THIS process's env. A literal
    # `NAME=VALUE` assignment inside the text being redacted would otherwise leak
    # in full. Matching the SECRET-NAMED key as required context is what makes
    # this safe where a bare-token regex could not be: a 40-hex value alone is
    # indistinguishable from a commit hash, but `*_KEY=<40hex>` is unambiguous.
    # Applied after specific CRITICAL patterns but BEFORE lower-severity
    # patterns: token/PII substitution must not hide a CRITICAL assignment.
    # Specific CRITICAL patterns keep their own category. Only the value group is
    # redacted (value_only), preserving the key name for debuggability. The name
    # must be a secret keyword or SNAKE_CASE ending in that keyword (so
    # "monkey", "MONKEY_BUSINESS", and prose like "the api key" never match).
    Pattern(
        re.compile(
            r"\b(?:[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*_)?"
            r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)\b"
            # A closing mapping-key quote followed by ':' permits JSON's
            # multiline whitespace. Line-based assignments keep horizontal
            # padding so an empty value cannot consume the next setting.
            r"(?:['\"][ \t\r\n]*:[ \t\r\n]*|['\"]?[ \t]*[:=][ \t]*)"
            # Quoted values include punctuation/spaces and escaped delimiters.
            # Prefer a complete quoted value, including embedded newlines.
            # Truncated snippets end at a line boundary or EOF instead of a
            # closing quote; a final backslash is still part of the secret.
            # Unquoted values retain punctuation, including brackets: these can
            # be environment password bytes. Prefer masking to a leaked tail;
            # quoted JSON values already have their own delimiter handling.
            r"(?P<quote>(?P<triple>\"\"\"|''')|['\"])?"
            r"(?P<value>(?(quote)"
            r"(?:(?:\\[\s\S]|(?!(?P=quote))[^\\])+(?=(?P=quote))"
            # An incomplete triple-quoted value extends to EOF, not the first
            # newline. Single-line snippets keep their line-boundary fallback.
            r"|(?(triple)(?:(?!(?P=quote))[\s\S])+\Z"
            r"|(?:\\[^\r\n]|\\(?=[\r\n]|\Z)|(?!(?P=quote))[^\\\r\n])+))"
            r"|(?:\[REDACTED:[A-Za-z0-9_:.-]+\][^\s'\"]*"
            r"|[^\s'\"]+)))"
            r"(?(quote)(?:(?P=quote)|(?=[\r\n]|\Z)))",
            re.IGNORECASE,
        ),
        Severity.CRITICAL,
        "secret_assignment",
        value_only=True,
    ),
]

# Deliberately NOT pattern-matched (design boundary — read before "adding the
# missing vendor"). A Layer-2 pattern only earns its place when the credential
# has a DISTINCTIVE token shape (prefix or structure) that a regex can match
# without over-redacting ordinary content. Two classes fail that test and are
# intentionally excluded:
#   1. Bare-token secrets with no distinctive prefix — e.g. a Twilio auth token
#      is 32 hex chars, indistinguishable from any MD5/commit-ish hash. A regex
#      for it would redact legitimate hashes everywhere. These are instead caught
#      by the Layer-1 env-name collector below (`TWILIO_AUTH_TOKEN` matches the
#      "TOKEN" naming rule, so its value is redacted regardless of shape).
#   2. Public identifiers that merely look credential-ish — e.g. a Twilio Account
#      SID (`AC` + 32 hex) is a username-equivalent that appears in API URLs and
#      docs; redacting it would over-redact published content for no secrecy gain.
# So "vendor X isn't in the list" is not automatically a gap: confirm the leak
# vector is a distinctive standalone token before adding a pattern, or rely on
# the env-name layer for prefix-less secrets.

# Regex matching env var names that look like secrets — catches common naming
# patterns (KEY, TOKEN, SECRET, etc.) without needing an exhaustive explicit list.
_SECRET_ENV_RE = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API)", re.IGNORECASE
)
_MIN_SECRET_LEN = 8  # don't redact trivially-short env values (e.g. "true", "1")

# Known-public literals exempt from layer-2 redaction by default. These are
# genuinely public (Bob's canonical address appears in README/SOCIAL.md, the
# gptme site is published) and otherwise trip the MEDIUM email/path layers,
# over-redacting legitimate published content. Allowlisting is exact-match and
# — by design — only ever exempts non-CRITICAL matches (see ``redact``), so a
# real leaked key can never be allowlisted past the fail-closed gate.
DEFAULT_ALLOWLIST: tuple[str, ...] = (
    "bob@superuserlabs.org",
    "erik@bjareho.lt",
    "erik.bjareholt@gmail.com",
    "alice@superuserlabs.org",
    "gptme.org",
    "gptme.ai",
    "github.com/gptme/gptme",
    "github.com/ErikBjare/bob",
    "github.com/TimeToBuildBob",
    "timetobuildbob.github.io",
)


@dataclass
class Finding:
    """One discovered matching pattern with count and safe preview."""

    category: str
    severity: Severity
    count: int
    preview: str

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity.name.lower(),
            "count": self.count,
            "preview": self.preview,
        }

    def __hash__(self) -> int:
        return hash(self.category)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Finding):
            return NotImplemented
        return self.category == other.category


@dataclass
class Result:
    """Redaction outcome with findings and block status."""

    input_chars: int
    redacted: str
    findings: list[Finding] = field(default_factory=list)
    blocked: bool = False
    block_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "input_chars": self.input_chars,
            "blocked": self.blocked,
            "block_reasons": self.block_reasons,
            "findings": [f.to_dict() for f in self.findings],
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def _mask_preview(
    category: str, sample: str, severity: Severity = Severity.MEDIUM
) -> str:
    """Build a safe preview that never echoes raw secret material.

    CRITICAL-severity matches always return ``"****"`` — the guarantee is keyed
    on severity, not on the category *name*, so distinctively-formatted tokens
    like ``github_oauth`` or ``bearer_header`` (whose category strings contain
    neither "key" nor "token") can never leak a ``prefix…suffix`` window.
    Emails show domain only. Internal paths show their prefix.
    Other values get a 4-char truncation window.
    """
    if severity >= Severity.CRITICAL:
        return "****"
    if category in ("private_key",):
        return "****"
    if category.startswith("trufflehog:"):
        return "****"
    if "key" in category or "token" in category:
        return "****"
    if category == "email":
        parts = sample.split("@", 1)
        return "****@" + parts[1] if len(parts) == 2 else "****"
    if len(sample) <= 8:
        return "****"
    return sample[:4] + "…" + sample[-4:]


def _secret_env_items(environ: dict[str, str] | None = None):
    """(name, value) pairs that look like secrets: name rule + value length floor."""
    source = os.environ if environ is None else environ
    for name, value in source.items():
        if value and len(value) >= _MIN_SECRET_LEN and _SECRET_ENV_RE.search(name):
            yield name, value


def secret_env_names() -> list[str]:
    """Env-var names whose values the layer-1 collector treats as secret literals.

    This is the single definition of "secret-shaped env var", shared by the
    redactor and by test isolation (``tests/conftest.py``), so the two cannot
    drift. A name qualifies when it matches ``_SECRET_ENV_RE`` *and* its value
    is at least ``_MIN_SECRET_LEN`` chars — the length floor keeps counters
    such as ``GPTME_SHELL_TRUNC_POST_TOKENS=4`` out of the set.
    """
    return [name for name, _ in _secret_env_items()]


def collect_secret_literals(extra: list[str] | None = None) -> list[str]:
    """Layer 1 source: env-var values that look like secrets, plus user literals."""
    literals: list[str] = [value for _, value in _secret_env_items()]
    if extra:
        literals.extend(s for s in extra if s)
    # longest-first so overlapping literals redact the broadest match
    return sorted(set(literals), key=len, reverse=True)


def redact(
    text: str,
    literals: list[str] | None = None,
    allowlist: list[str] | None = None,
    block_severity: Severity = Severity.CRITICAL,
) -> Result:
    """Run the full deterministic+pattern redaction pipeline (layers 1-2, lite 5).

    Returns a ``Result`` with redacted text, categorized findings, and whether
    the artifact is blocked. Findings at or above ``block_severity`` block.

    ``allowlist`` holds exact literals known to be public. A layer-2 match
    equal to an allowlisted literal is left untouched and not recorded as a
    finding — but **only** when its severity is below ``CRITICAL``. This keeps
    the fail-closed invariant absolute.
    """
    allow = set(allowlist or ())
    result = Result(input_chars=len(text), redacted=text)
    findings: dict[str, Finding] = {}

    def record(category: str, severity: Severity, sample: str) -> None:
        f = findings.get(category)
        if f is None:
            findings[category] = Finding(
                category=category,
                severity=severity,
                count=1,
                preview=_mask_preview(category, sample, severity),
            )
        else:
            f.count += 1

    # Discover every layer against the original text. Replacing any layer first
    # can destroy the context another detector needs (even an assignment key).
    spans: list[tuple[int, int, int, str, Severity]] = []

    # Layer 1: find intervals in the original text before replacing anything.
    # Longest-first substitution misses crossing overlaps (abcde + defgh).
    intervals: list[tuple[int, int]] = []
    for literal in set(literals or []):
        if not literal:
            continue
        start = 0
        while (start := text.find(literal, start)) != -1:
            intervals.append((start, start + len(literal)))
            start += 1  # include self-overlapping occurrences too
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    for start, end in merged:
        spans.append((start, end, 0, "literal", Severity.CRITICAL))

    # Layer 2: credential-specific patterns, assignment fallback, then PII.
    # The assignment fallback is declared last for readability, not priority.
    patterns = [p for p in _PATTERNS if p.severity >= Severity.CRITICAL]
    patterns.extend(p for p in _PATTERNS if p.severity < Severity.CRITICAL)
    for priority, pat in enumerate(patterns, start=1):

        def discover(m: re.Match[str], _p: Pattern = pat) -> bool:
            if _p.value_only:
                value = m.group("value")
                # Exempt only a whole marker, never a marker-prefixed value.
                if re.fullmatch(r"\[REDACTED:[A-Za-z0-9_:.-]+\]", value):
                    return False
                # Allowlist exemption never applies to CRITICAL (fail-closed).
                if value in allow and _p.severity < Severity.CRITICAL:
                    return False
                spans.append((*m.span("value"), priority, _p.category, _p.severity))
                return True
            matched = m.group(0)
            if _p.category == "jwt":
                try:
                    header, payload, _ = matched.split(".")
                    objects = [
                        json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))
                        for s in (header, payload)
                    ]
                except (ValueError, UnicodeDecodeError, binascii.Error, RecursionError):
                    return False
                if not all(isinstance(obj, dict) for obj in objects):
                    return False
                if not isinstance(objects[0].get("alg"), str):
                    return False
            # Exact-literal allowlist exemption — never for CRITICAL matches.
            if matched in allow and _p.severity < Severity.CRITICAL:
                return False
            spans.append((*m.span(), priority, _p.category, _p.severity))
            return True

        if pat.category == "jwt":
            # Rejected candidates may overlap a real JWT after a dotted prefix.
            # Retry from the next character on rejection, but consume accepted
            # tokens once so their segments cannot produce duplicate findings.
            search_from = 0
            while match := pat.regex.search(text, search_from):
                accepted = discover(match)
                search_from = match.end() if accepted else match.start() + 1
        else:
            for match in pat.regex.finditer(text):
                discover(match)

    # Mask the union of crossing spans, retaining the highest-priority detector's
    # category (literal, specific credential, assignment, PII). Adjacent spans
    # remain separate. No detector ever sees generated replacement markers.
    unions: list[tuple[int, int, int, str, Severity]] = []
    for start, end, priority, category, severity in sorted(spans):
        if unions and start < unions[-1][1]:
            old_start, old_end, old_priority, old_category, old_severity = unions[-1]
            if old_priority <= priority:
                priority, category = old_priority, old_category
            unions[-1] = (
                old_start,
                max(old_end, end),
                priority,
                category,
                max(old_severity, severity),
            )
        else:
            unions.append((start, end, priority, category, severity))
    parts: list[str] = []
    cursor = 0
    for start, end, _, category, severity in unions:
        parts.extend((text[cursor:start], f"[REDACTED:{category}]"))
        record(category, severity, text[start:end])
        cursor = end
    parts.append(text[cursor:])
    result.redacted = "".join(parts)
    result.findings = sorted(
        findings.values(), key=lambda f: (-int(f.severity), f.category)
    )

    # Layer 5 (lite): fail-closed gate.
    blocking = [f for f in result.findings if f.severity >= block_severity]
    if blocking:
        result.blocked = True
        result.block_reasons = [
            f"{f.count}× {f.category} ({f.severity.name.lower()})" for f in blocking
        ]
    return result


def trufflehog_scan(text: str) -> list[Finding]:
    """Layer 3: run TruffleHog on ``text`` and return CRITICAL findings.

    Writes ``text`` to a temp file, runs ``trufflehog filesystem --json``,
    and parses the JSON output. Every finding is treated as CRITICAL because
    TruffleHog only fires on confirmed secrets.

    Returns empty list when the binary is absent (graceful degradation).
    Raises RuntimeError on execution/protocol failure; a failed scan is not clean.
    """
    binary = shutil.which("trufflehog")
    if binary is None:
        # Graceful skip — TruffleHog is optional infra.
        return []

    findings: list[Finding] = []
    with tempfile.TemporaryDirectory(prefix="redact-trufflehog-") as tmpdir:
        tmpfile = Path(tmpdir) / "input.txt"
        tmpfile.write_text(text, encoding="utf-8")
        try:
            result = subprocess.run(
                [binary, "filesystem", "--directory", tmpdir, "--json"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError):
            # Do not echo exceptions/stdout/stderr: scanner diagnostics may
            # contain the input secrets we are trying to protect.
            raise RuntimeError("TruffleHog scan could not complete") from None

    if result.returncode != 0:
        raise RuntimeError(f"TruffleHog scan failed (exit {result.returncode})")

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            raise RuntimeError("TruffleHog returned invalid JSON") from None
        if not isinstance(record, dict):
            raise RuntimeError("TruffleHog returned an invalid finding")
        detector = record.get("DetectorName", "") or record.get("detector_name", "")
        raw = record.get("Raw", "") or record.get("raw", "")
        if not isinstance(detector, str) or not detector or not isinstance(raw, str):
            raise RuntimeError("TruffleHog returned an invalid finding")
        preview = (
            _mask_preview(
                "trufflehog:" + detector.replace(" ", "_"), raw, Severity.CRITICAL
            )
            if raw
            else detector
        )
        findings.append(
            Finding(
                category="trufflehog:" + detector.replace(" ", "_"),
                severity=Severity.CRITICAL,
                count=1,
                preview=preview,
            )
        )

    return findings


# --- Layer 6: prompt-injection detection -------------------------------------
# These patterns detect common prompt injection surfaces in untrusted text.
# Unlike layers 1-3, this scan is ADVISORY — it flags suspicious content
# without redacting it. Use it as a preflight gate before feeding scraped or
# user-supplied text into an LLM context.
#
# Drawn from OWASP LLM Top 10, known jailbreak taxonomies, and gptme's own
# injection incident history.

_INJECTION_PATTERNS: list[Pattern] = [
    # HIGH — overt instruction override attempts. Low false-positive because
    # the exact phrasings are peculiar to prompt-injection attacks and rarely
    # appear in benign web content.
    Pattern(
        re.compile(
            r"(?:"
            r"Ignore (?:all )?(?:previous|prior|above) (?:instructions|directions|prompts|commands)"
            r"|Disregard (?:all )?(?:previous|prior|above) (?:instructions|directions|prompts)"
            r"|Forget (?:everything|all previous|your instructions)"
            r"|You are now (?:(?:an?|a different|not)\s+)?(?:\w+\s+){0,3}(?:AI|assistant|model|bot|agent|GPT)"
            r"|From now on (?:you are|act as|behave as)"
            r"|(?:^|\n)\s*system\s*:\s*you\s+(?:are|must|should|will)"
            r"|SYSTEM PROMPT OVERRIDE"
            r"|Reset (?:your )?(?:instructions|state|context|conversation)"
            r"|New (?:instructions|prompt|directive)(?::| below)"
            r")",
            re.IGNORECASE,
        ),
        Severity.HIGH,
        "injection:system_override",
    ),
    # HIGH — direct attempts to extract system prompts. Very low false-positive
    # in scraped web content; these phrasings are specific to jailbreak attempts.
    Pattern(
        re.compile(
            r"(?:"
            r"output (?:the |your )?(?:full |complete )?(?:system prompt|initial prompt|base prompt|core instructions)"
            r"|repeat (?:everything|the entire|your system) (?:prompt|instructions|above)"
            r"|show me your (?:system |base )?prompt"
            r"|what (?:were |are )?(?:your |the )?(?:original |initial )?(?:instructions|prompt)"
            r"|reveal (?:your |system )?(?:prompt|instructions)"
            r"|print your (?:system )?prompt"
            r"|how (?:are you |were you )?(?:instructed|programmed|configured|designed)"
            r"|what (?:instructions|prompt) (?:were you |are you )?(?:given|provided with|trained with)"
            r"|what is your (?:base |core )?prompt"
            r")",
            re.IGNORECASE,
        ),
        Severity.HIGH,
        "injection:instruction_leak",
    ),
    # MEDIUM — goal hijacking / role reversal. More common in benign roleplay
    # content, so MEDIUM to avoid flooding.
    Pattern(
        re.compile(
            r"(?:"
            r"What would (?:an? |the )?AI say (?:about|to|if)"
            r"|pretend you are.*?and (?:say|tell|do|answer)"
            r"|act as (?:if|though) you (?:are|were)"
            r"|you are (?:now )?(?:an? |the )?(\w+ ){0,3}AI.*?and you (?:will|must)"
            r")",
            re.IGNORECASE,
        ),
        Severity.MEDIUM,
        "injection:role_reversal",
    ),
    # MEDIUM — payload smuggling indicators. Base64-encoded content that looks
    # like instructions (has imperative verbs) combined with decode instructions.
    Pattern(
        re.compile(
            r"(?:"
            r"decode.*base64.*(?:and|to).*(?:output|print|run|execute|follow)"
            r"|base64.*decode.*(?:then|and).*(?:instruction|prompt|command)"
            r"|this is (?:an? |a )?base64.*(?:instruction|prompt|message)"
            r")",
            re.IGNORECASE,
        ),
        Severity.MEDIUM,
        "injection:payload_smuggling",
    ),
    # LOW — delimiter injection. Stray model-specific control tokens are weak
    # signal alone but accumulate weight.
    Pattern(
        re.compile(
            r"(?:"
            r"<\|im_start\|>(?:system|user|assistant)"
            r"|<s>(?:system|user|assistant)?"
            r"|\[INST\](?:\]| )?"
            r"|<\|[a-z_]+\|>\s*(?:system|user|assistant)"
            r"|\[\/INST\]"
            r"|<\|eot_id\|>"
            r")",
            re.IGNORECASE,
        ),
        Severity.LOW,
        "injection:delimiter",
    ),
]


def scan_prompt_injection(text: str, *, canonicalize: bool = False) -> list[Finding]:
    """Layer 6: scan ``text`` for prompt-injection surface anomalies.

    Returns a list of findings ordered by severity descending. Unlike the
    secret-redaction pipeline, this scan does NOT modify the text — it is an
    advisory preflight. Consumers decide whether to block, warn, or ignore.

    A finding with severity MEDIUM or above in combination with untrusted
    provenance (e.g., ``structured_scrape`` content with
    ``content_is_untrusted: true``) should be treated as a block signal.

    Args:
        text: Input text to scan.
        canonicalize: When True, also scan a canonicalized version of ``text``
            (zero-width chars removed, Unicode confusables mapped to ASCII).
            Findings that only appear in the canonical scan have their severity
            upgraded by one level to flag likely evasion attempts. The original
            text is never modified.
    """
    findings: dict[str, Finding] = {}

    for pat in _INJECTION_PATTERNS:
        matches = list(pat.regex.finditer(text))
        if not matches:
            continue
        sample = matches[0].group(0)
        f = Finding(
            category=pat.category,
            severity=pat.severity,
            count=len(matches),
            preview=_mask_preview(pat.category, sample, pat.severity),
        )
        findings[pat.category] = f

    if canonicalize:
        # Lazy import avoids a circular dependency: canonicalize.py imports
        # scan_prompt_injection from this module at function-call time.
        from redact.canonicalize import canonicalize_for_injection_scan_variants

        for canonical_text in canonicalize_for_injection_scan_variants(text):
            for pat in _INJECTION_PATTERNS:
                matches = list(pat.regex.finditer(canonical_text))
                if not matches:
                    continue
                if pat.category in findings:
                    continue  # raw or earlier variant already detected it
                sample = matches[0].group(0)
                # Upgrade severity by one level — canonical-only = evasion attempt
                upgraded = Severity(min(int(pat.severity) + 1, int(Severity.CRITICAL)))
                findings[pat.category] = Finding(
                    category=pat.category,
                    severity=upgraded,
                    count=len(matches),
                    preview=_mask_preview(pat.category, sample, pat.severity),
                )

    return sorted(findings.values(), key=lambda f: (-int(f.severity), f.category))
