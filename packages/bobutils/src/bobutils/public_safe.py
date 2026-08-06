"""Public/private boundary sanitizer.

Prevents operational internals (workspace paths, internal hostnames, SSH
remotes) from leaking into external-facing outputs — tweets, GitHub comments,
status pages, blog posts.

Mirrors the hard boundary established by LoopX's ``public_safe_compact_text()``
/ ``validate_public_safe_text()`` pattern, adapted for Bob's workspace topology.
Complements ``packages/redact/`` (which strips *secrets*) by stripping
*structural* private identifiers that are not secrets but still should not
appear in public channels.

Usage::

    from bobutils.public_safe import public_safe, validate_public_safe

    # Sanitize before publishing
    tweet_body = public_safe(draft)

    # Dry-run audit
    violations = validate_public_safe(draft)
    for v in violations:
        logger.warning("[%s] %r at offset %d", v.kind, v.match, v.offset)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["public_safe", "validate_public_safe", "PublicSafeViolation"]


# ---------------------------------------------------------------------------
# Substitution table — applied in declaration order (most-specific first).
# ---------------------------------------------------------------------------

_SUBSTITUTIONS: list[tuple[re.Pattern[str], str]] = [
    # Absolute workspace root — most specific, must come before /home/bob/
    (re.compile(r"/home/bob/bob/"), "<workspace>/"),
    # Home directory
    (re.compile(r"/home/bob/"), "<home>/"),
    # Internal HTTP(S) endpoints (catch before bare-hostname pattern)
    (
        re.compile(r"https?://[a-z0-9.-]*\.bjareho\.lt(?::\d+)?(?:/[^\s]*)?"),
        "<internal-endpoint>",
    ),
    # Internal bare hostnames
    (re.compile(r"\b[a-z0-9-]+\.bjareho\.lt\b"), "<internal-host>"),
    # Known private SSH remotes
    (re.compile(r"\berb-hetzner-ax41\b"), "<ssh-remote>"),
    # Cluster node references
    (re.compile(r"\bcluster1(?:-node\d+)?\b"), "<cluster-node>"),
    # LXC container identifiers
    (re.compile(r"\bCT\d{3}\b"), "<lxc-container>"),
    (re.compile(r"\bpct exec \d+\b"), "pct exec <N>"),
]

# Validators mirror the same patterns for audit/dry-run reporting.
_VALIDATORS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"/home/bob/bob/"), "workspace absolute path"),
    (re.compile(r"/home/bob/"), "home directory path"),
    (re.compile(r"https?://[a-z0-9.-]*\.bjareho\.lt"), "internal endpoint URL"),
    (re.compile(r"\b[a-z0-9-]+\.bjareho\.lt\b"), "internal hostname"),
    (re.compile(r"\berb-hetzner-ax41\b"), "private SSH remote"),
    (re.compile(r"\bcluster1(?:-node\d+)?\b"), "cluster node reference"),
    (re.compile(r"\bCT\d{3}\b"), "LXC container reference"),
    (re.compile(r"\bpct exec \d+\b"), "LXC exec command"),
]


@dataclass
class PublicSafeViolation:
    """A single private-content finding."""

    kind: str
    match: str
    offset: int


def validate_public_safe(text: str) -> list[PublicSafeViolation]:
    """Return private-content violations found in *text* without mutating it.

    An empty list means the text passes all public-safety checks defined by
    this module.  Use this for dry-run / audit mode before publishing.

    Note: patterns are applied independently, so a URL that matches both the
    endpoint pattern and the hostname pattern will appear twice — once per
    matched rule.
    """
    violations: list[PublicSafeViolation] = []
    for pattern, kind in _VALIDATORS:
        for m in pattern.finditer(text):
            violations.append(
                PublicSafeViolation(kind=kind, match=m.group(), offset=m.start())
            )
    return violations


def public_safe(text: str) -> str:
    """Return *text* with private operational details replaced by placeholders.

    Strips workspace paths, internal hostnames, SSH remotes, and LXC
    container references.  Suitable for tweets, GitHub comments, blog posts,
    and other external-facing content.

    Substitutions are deterministic and order-dependent — more-specific
    patterns (e.g. full URL) are applied before broader ones (bare hostname)
    to avoid double-replacement artifacts.

    For a non-mutating audit pass, use :func:`validate_public_safe`.
    """
    result = text
    for pattern, replacement in _SUBSTITUTIONS:
        result = pattern.sub(replacement, result)
    return result
