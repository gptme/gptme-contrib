"""CLI entry point for the secret-safe export redactor."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (
    DEFAULT_ALLOWLIST,
    Result,
    Severity,
    collect_secret_literals,
    redact,
    scan_prompt_injection,
    trufflehog_scan,
)
from .sanitize_document import scan_document


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Secret-safe export redactor — layered fail-closed redaction.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("file", help="file to redact (use - for stdin)")
    ap.add_argument(
        "--out", help="write redacted text here (default: stdout unless --report)"
    )
    ap.add_argument(
        "--report",
        action="store_true",
        help="print findings report instead of redacted text",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="machine-readable report (implies --report)",
    )
    ap.add_argument(
        "--literal",
        action="append",
        default=[],
        help="extra literal secret to redact (repeatable)",
    )
    ap.add_argument(
        "--allowlist",
        action="append",
        default=[],
        help="exact known-public literal to exempt from non-critical redaction (repeatable)",
    )
    ap.add_argument(
        "--no-default-allowlist",
        action="store_true",
        help="don't apply the built-in known-public allowlist (DEFAULT_ALLOWLIST)",
    )
    ap.add_argument(
        "--trufflehog",
        action="store_true",
        help="enable layer 3 TruffleHog scanning (gracefully skips when binary absent)",
    )
    ap.add_argument(
        "--scan-injection",
        action="store_true",
        help="enable layer 6 prompt-injection surface scan (advisory, does not block)",
    )
    ap.add_argument(
        "--block-severity",
        default="critical",
        help="minimum severity that blocks the whole artifact (default: critical)",
    )
    ap.add_argument(
        "--no-env-secrets",
        action="store_true",
        help="skip layer-1 env-value redaction (useful for deterministic tests)",
    )
    return ap


def _main_redact(argv: list[str] | None = None) -> int:
    """Original redact entry point (secrets + patterns)."""
    ap = _build_parser()
    args = ap.parse_args(argv)

    try:
        block_sev = Severity.parse(args.block_severity)
    except KeyError:
        print(f"error: unknown severity {args.block_severity!r}", file=sys.stderr)
        return 1

    text = (
        sys.stdin.read()
        if args.file == "-"
        else Path(args.file).read_text(encoding="utf-8")
    )
    literals = (
        collect_secret_literals(args.literal)
        if not args.no_env_secrets
        else list(args.literal)
    )
    allowlist = list(args.allowlist)
    if not args.no_default_allowlist:
        allowlist.extend(DEFAULT_ALLOWLIST)
    result = redact(
        text, literals=literals, allowlist=allowlist, block_severity=block_sev
    )

    # Layer 3: optional TruffleHog scan.
    if args.trufflehog:
        th_findings = trufflehog_scan(text)
        existing_cats = {f.category for f in result.findings}
        for f in th_findings:
            if f.category in existing_cats:
                for existing in result.findings:
                    if existing.category == f.category:
                        existing.count += 1
                        break
            else:
                result.findings.append(f)
                existing_cats.add(f.category)
        # Recompute the fail-closed gate over the merged finding set. Rebuilding
        # block_reasons from the (category-keyed) findings keeps exactly one
        # reason per blocking category and avoids the previous fragile substring
        # dedup (``f.category in br``), which could drop a reason whose category
        # string was a substring of another reason, or duplicate one otherwise.
        result.findings.sort(key=lambda f: (-int(f.severity), f.category))
        blocking = [f for f in result.findings if f.severity >= block_sev]
        result.block_reasons = [
            f"{f.count}× {f.category} ({f.severity.name.lower()})" for f in blocking
        ]
        result.blocked = bool(blocking)

    # Layer 6: optional prompt-injection surface scan. Advisory only — findings
    # are reported but never flip result.blocked, regardless of severity.
    if args.scan_injection:
        existing_cats = {f.category for f in result.findings}
        for f in scan_prompt_injection(text):
            if f.category not in existing_cats:
                result.findings.append(f)
                existing_cats.add(f.category)

    result.findings.sort(key=lambda f: (-int(f.severity), f.category))

    if args.json:
        print(result.to_json())
    elif args.report:
        print_report(result)
    else:
        if result.blocked:
            print(
                "error: artifact blocked (fail-closed); run with --report for reasons",
                file=sys.stderr,
            )
            return 2
        out = result.redacted
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(out)
        else:
            sys.stdout.write(out)

    return 2 if result.blocked else 0


def print_report(result: Result) -> None:
    """Print a human-readable findings report."""
    status = "BLOCKED (fail-closed)" if result.blocked else "OK"
    print(f"status: {status}")
    if result.block_reasons:
        print("block reasons: " + ", ".join(result.block_reasons))
    print(f"input chars: {result.input_chars}")
    if result.findings:
        print("findings:")
        for f in result.findings:
            print(
                f"  - {f.severity.name.lower():8} {f.category:18} x{f.count:<4} {f.preview}"
            )
    else:
        print("findings: none")


def _build_sanitize_doc_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="redact sanitize-doc",
        description="Scan a Markdown document for formula / HTML / metadata injection.",
    )
    ap.add_argument("file", help="file to scan (use - for stdin)")
    ap.add_argument(
        "--report",
        action="store_true",
        help="print findings report (default: silent unless --json)",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="machine-readable JSON report (implies --report)",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero on any finding, not only when blocked",
    )
    ap.add_argument(
        "--block-severity",
        default="high",
        help="minimum severity that sets result.blocked (default: high)",
    )
    return ap


def _main_sanitize_doc(argv: list[str] | None = None) -> int:
    """Entry point for the ``redact sanitize-doc`` subcommand."""
    ap = _build_sanitize_doc_parser()
    args = ap.parse_args(argv)

    try:
        block_sev = Severity.parse(args.block_severity)
    except KeyError:
        print(f"error: unknown severity {args.block_severity!r}", file=sys.stderr)
        return 1

    text = (
        sys.stdin.read()
        if args.file == "-"
        else Path(args.file).read_text(encoding="utf-8")
    )
    result = scan_document(text, block_severity=block_sev)

    if args.json:
        print(json.dumps(result.to_dict()))
    elif args.report or result.findings:
        status = (
            "BLOCKED" if result.blocked else ("FINDINGS" if result.findings else "OK")
        )
        print(f"status: {status}")
        print(f"input chars: {result.input_chars}")
        if result.findings:
            print("findings:")
            for f in result.findings:
                print(
                    f"  - {f.severity.name.lower():8} {f.category:18} x{f.count:<4} {f.preview}"
                )
        else:
            print("findings: none")

    if args.strict and result.findings:
        return 2
    return 2 if result.blocked else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code (0 = ok, 2 = blocked)."""
    if argv is None:
        argv = sys.argv[1:]

    # Dispatch to sanitize-doc subcommand before the main redact parser sees it.
    if argv and argv[0] == "sanitize-doc":
        return _main_sanitize_doc(argv[1:])

    return _main_redact(argv)


if __name__ == "__main__":
    sys.exit(main())
