"""Classify short-lived Claude Code authentication failures."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

DEFAULT_MAX_BYTES = 2000

_AUTH_FAILURE_RE = re.compile(
    rb"\b401\b|unauthorized|invalid authentication credentials|"
    rb"invalid bearer token|authentication[ _](?:error|failed)|"
    rb"oauth.{0,40}(?:expired|fail|error|invalid)|please run /login",
    re.IGNORECASE,
)


def is_transient_auth_death(output: str | bytes, max_bytes: int = DEFAULT_MAX_BYTES) -> bool:
    """Return whether a small output contains a Claude auth-failure signature.

    The size gate is required: a successful agent can produce a large transcript
    that discusses HTTP 401 without having failed authentication itself.
    """
    data = output.encode(errors="replace") if isinstance(output, str) else output
    return 0 < len(data) <= max_bytes and bool(_AUTH_FAILURE_RE.search(data))


def is_auth_death_file(path: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> bool:
    """Classify a captured process output file, returning False if unreadable."""
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return is_transient_auth_death(data, max_bytes=max_bytes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--classify-file", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args(argv)
    return 0 if is_auth_death_file(args.classify_file, args.max_bytes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
