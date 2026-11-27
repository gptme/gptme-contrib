"""CLI entry point for the secret-safe export redactor."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
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
        help="extra literal secret; any occurrence is critical and blocks (repeatable)",
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


def _read_input(file: str) -> str | None:
    """The input text, or None (after an error message) when decoding fails.

    Newline bytes are preserved. stdin is decoded strictly with its declared
    encoding. `sys.stdin.read()` uses `errors="surrogateescape"`, so an invalid
    byte survives as a lone surrogate
    (`0xff` -> "\\udcff") and passed every pattern, while the same bytes from a
    file were refused; the same payload must not behave differently by how it
    was supplied.
    """
    try:
        if file == "-":
            buf = getattr(sys.stdin, "buffer", None)
            if buf is not None:
                return bytes(buf.read()).decode(sys.stdin.encoding or "utf-8")
            # A replaced stdin (tests) has no buffer; round-trip the text back
            # through the bytes surrogateescape produced.
            return sys.stdin.read().encode("utf-8", "surrogateescape").decode("utf-8")
        # Preserve non-secret newline bytes, as on the current base.
        with open(file, encoding="utf-8", newline="") as fh:
            return fh.read()
    except UnicodeDecodeError:
        # UTF-16 with a BOM (or other binary) does not decode at all.
        print(
            "error: input is not valid UTF-8 (binary or UTF-16?); refusing artifact",
            file=sys.stderr,
        )
        return None


def _refuse(result: Result, reason: str, args: argparse.Namespace) -> int:
    """Refuse the artifact (exit 2) while keeping the ``--json`` contract.

    ``--json`` callers parse stdout, so a refusal must still print a report
    there; the human-readable error goes to stderr in every mode.
    """
    print(f"error: {reason}; refusing artifact", file=sys.stderr)
    result.blocked = True
    result.block_reasons.append(reason)
    if args.json:
        print(result.to_json())
    return 2


def _main_redact(argv: list[str] | None = None) -> int:
    """Original redact entry point (secrets + patterns)."""
    ap = _build_parser()
    args = ap.parse_args(argv)

    try:
        block_sev = Severity.parse(args.block_severity)
    except KeyError:
        print(f"error: unknown severity {args.block_severity!r}", file=sys.stderr)
        return 1

    text = _read_input(args.file)
    if text is None:
        return 2
    if "\x00" in text:
        # BOM-less UTF-16 of ASCII decodes as "valid" UTF-8 with a NUL between
        # every character, which splits every secret past all patterns. Refuse
        # rather than report a NUL-interleaved (or binary) artifact as clean.
        print(
            "error: input contains NUL bytes (binary or UTF-16?); refusing artifact",
            file=sys.stderr,
        )
        return 2
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
        try:
            th_findings = trufflehog_scan(text)
        except RuntimeError as exc:
            return _refuse(result, str(exc), args)
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
            _write_atomic(Path(args.out), out)
        else:
            sys.stdout.write(out)

    return 2 if result.blocked else 0


def _stream_fd(path: Path) -> int | None:
    """The inherited fd *path* names, when it is a process stream.

    ``/dev/stdout`` is a symlink into ``/proc/self/fd``. Opening it for writing
    truncates the redirect target and uses a fresh file offset, so a caller
    that later writes to its own fd (``{ redact --out /dev/stdout f; echo ok; }
    > report``) overwrites or orphans what we wrote. Writing to the inherited
    fd keeps one shared offset and never truncates.
    """
    raw = os.fspath(path)
    fixed = {"/dev/stdin": 0, "/dev/stdout": 1, "/dev/stderr": 2}
    if raw in fixed:
        return fixed[raw]
    match = re.fullmatch(r"/(?:dev|proc/self)/fd/(\d+)", raw)
    return int(match.group(1)) if match else None


def _write_in_place(path: Path, data: bytes) -> None:
    """Write *data* to *path* without a sibling rename.

    Used when mkstemp or os.replace cannot run (read-only directory, sticky
    dir holding another user's file). ``open(path, "wb")`` truncates first, so
    a later write error would destroy the original; keep a copy and restore it.
    """
    try:
        original = path.read_bytes()
    except FileNotFoundError:
        original = None
    try:
        with open(path, "wb") as fh:
            fh.write(data)
            fh.flush()
    except BaseException:
        if original is not None:
            try:
                with open(path, "wb") as fh:
                    fh.write(original)
                    fh.flush()
            except OSError:
                pass
        raise


def _write_atomic(path: Path, text: str) -> None:
    """Write *text* to *path* via a sibling temp file and rename.

    ``open(path, "w")`` truncates before encoding, so an encode or disk error
    would destroy the existing file (e.g. ``redact - --out f < f``).
    """
    data = text.encode("utf-8")
    # A device, FIFO or /dev/stdout cannot be replaced by a rename: swapping
    # /dev/null for a regular file would capture every later write to it.
    # Write through such targets, as open(path, "w") always did.
    try:
        existing = os.stat(path)
    except FileNotFoundError:
        existing = None
    # os.stat follows symlinks, so `/dev/stdout` redirected to a regular file
    # looks regular. Renaming there would swap a new inode in at the redirect
    # target while the shell's fd 1 keeps the old one, losing later writes.
    stream_fd = _stream_fd(path)
    if stream_fd is not None:
        while data:
            n = os.write(stream_fd, data)
            # POSIX write() may return 0 on a non-blocking fd instead of
            # EAGAIN. data[0:] is a no-op, so the loop would spin forever.
            if n == 0:
                raise OSError("write returned 0 bytes")
            data = data[n:]
        return
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        with open(path, "wb") as fh:
            fh.write(data)
        return
    # Replace the symlink's target, not the link, and keep the file's mode
    # (a new file gets the mode open() would give it, not mkstemp's 0600).
    path = Path(os.path.realpath(path))
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    except OSError:
        # A writable file inside a non-writable directory: the atomic path
        # needs a temp file beside the target, the in-place write it replaced
        # did not, so keep the in-place write as the fallback.
        _write_in_place(path, data)
        return
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        try:
            os.replace(tmp, path)
        except PermissionError:
            # Sticky world-writable directory (e.g. /tmp) holding another
            # user's file: replacing is EPERM, writing the file itself is not.
            _write_in_place(path, data)
            Path(tmp).unlink(missing_ok=True)
            return
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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

    text = _read_input(args.file)
    if text is None:
        return 2
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
