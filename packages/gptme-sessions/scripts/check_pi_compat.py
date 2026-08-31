#!/usr/bin/env python3
"""Check the pinned Pi parser contract against an upstream release source archive."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
import urllib.error
import urllib.request
from collections.abc import Sequence

PINNED_PI_VERSION = "0.84.4"
PINNED_PI_TAG = f"v{PINNED_PI_VERSION}"
PINNED_PI_COMMIT = "b79e4cc834970cca69daebffab7df1da7d1e52c4"
PINNED_SESSION_VERSION = 3
PINNED_ENTRY_TYPES = frozenset(
    {
        "message",
        "model_change",
        "thinking_level_change",
        "compaction",
        "branch_summary",
        "custom",
        "custom_message",
        "label",
        "session_info",
    }
)
PINNED_STOP_REASONS = frozenset(
    {"pending", "stop", "length", "toolUse", "error", "aborted", "deferred"}
)
PINNED_MODEL_CATALOG_HASHES = {
    ".manifest.json": "68eafe33e43efce9dac778d9290ed75ff65ed680d45828a1aa26b947b2fef237",
    "openai-codex.json": "2712c2924a4a75213dddc743c0e5f08d50a781fe807f16d5afb5fb65b41c64c7",
    "xai.json": "9668b607ac69237089e84efa3f0590dc1e28d8e24c9d1afa3a2b7cf72efa30e0",
}
PINNED_FIXTURE_MODELS = frozenset({("openai-codex", "gpt-5.6-luna"), ("xai", "grok-4.6")})

_RELEASE_URL = (
    "https://github.com/earendil-works/pi/releases/download/"
    f"{PINNED_PI_TAG}/pi-{PINNED_PI_VERSION}-source.tar.gz"
)
_ARCHIVE_ROOT = f"pi-{PINNED_PI_VERSION}"
_SESSION_MANAGER = f"{_ARCHIVE_ROOT}/packages/coding-agent/src/core/session-manager.ts"
_AI_TYPES = f"{_ARCHIVE_ROOT}/packages/ai/src/types.ts"
_MODEL_DATA_DIR = f"{_ARCHIVE_ROOT}/packages/ai/src/providers/data"

_SESSION_VERSION_RE = re.compile(r"CURRENT_SESSION_VERSION\s*=\s*(\d+)")
_ENTRY_UNION_RE = re.compile(r"export type SessionEntry\s*=\s*(.*?);", re.DOTALL)
_ENTRY_INTERFACE_RE = re.compile(
    r'export interface (\w+Entry)(?:<[^>]+>)?[^\{;]*\{\s*type:\s*"([^"]+)";',
    re.DOTALL,
)
_STOP_REASON_RE = re.compile(r"export type StopReason\s*=\s*(.*?);", re.DOTALL)
_STRING_LITERAL_RE = re.compile(r'"([^"]+)"')


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "gptme-sessions-pi-compat"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data: bytes = response.read()
        return data


def _read_archive_member(archive: tarfile.TarFile, name: str) -> bytes:
    member = archive.extractfile(name)
    if member is None:
        raise ValueError(f"Pi source archive is missing {name}")
    return member.read()


def _extract_contract(
    session_manager: str, ai_types: str
) -> tuple[int, frozenset[str], frozenset[str]]:
    version_match = _SESSION_VERSION_RE.search(session_manager)
    if version_match is None:
        raise ValueError("could not locate CURRENT_SESSION_VERSION in Pi source")

    union_match = _ENTRY_UNION_RE.search(session_manager)
    if union_match is None:
        raise ValueError("could not locate SessionEntry union in Pi source")
    union_names = set(re.findall(r"\b\w+Entry\b", union_match.group(1)))
    interface_types = dict(_ENTRY_INTERFACE_RE.findall(session_manager))
    missing_interfaces = sorted(union_names - interface_types.keys())
    if missing_interfaces:
        raise ValueError(
            "could not resolve Pi SessionEntry interfaces: " + ", ".join(missing_interfaces)
        )

    stop_match = _STOP_REASON_RE.search(ai_types)
    if stop_match is None:
        raise ValueError("could not locate StopReason union in Pi source")
    stop_reasons = frozenset(_STRING_LITERAL_RE.findall(stop_match.group(1)))
    return (
        int(version_match.group(1)),
        frozenset(interface_types[name] for name in union_names),
        stop_reasons,
    )


def _catalog_models(raw_catalog: bytes) -> frozenset[tuple[str, str]]:
    catalog = json.loads(raw_catalog)
    if not isinstance(catalog, dict):
        raise ValueError("Pi model catalog root is not an object")
    models: set[tuple[str, str]] = set()
    for api_models in catalog.values():
        if not isinstance(api_models, dict):
            raise ValueError("Pi model catalog API entry is not an object")
        for model in api_models.values():
            if not isinstance(model, dict):
                raise ValueError("Pi model catalog model entry is not an object")
            provider = model.get("provider")
            model_id = model.get("id")
            if isinstance(provider, str) and isinstance(model_id, str):
                models.add((provider, model_id))
    return frozenset(models)


def check_archive(archive_bytes: bytes) -> list[str]:
    """Return compatibility failures found in a Pi release source archive."""
    failures: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        session_manager = _read_archive_member(archive, _SESSION_MANAGER).decode("utf-8")
        ai_types = _read_archive_member(archive, _AI_TYPES).decode("utf-8")
        session_version, entry_types, stop_reasons = _extract_contract(session_manager, ai_types)

        if session_version != PINNED_SESSION_VERSION:
            failures.append(
                f"session version drift: expected {PINNED_SESSION_VERSION}, got {session_version}"
            )
        if entry_types != PINNED_ENTRY_TYPES:
            failures.append(
                "session entry-type drift: "
                f"added={sorted(entry_types - PINNED_ENTRY_TYPES)}, "
                f"removed={sorted(PINNED_ENTRY_TYPES - entry_types)}"
            )
        if stop_reasons != PINNED_STOP_REASONS:
            failures.append(
                "stop-reason drift: "
                f"added={sorted(stop_reasons - PINNED_STOP_REASONS)}, "
                f"removed={sorted(PINNED_STOP_REASONS - stop_reasons)}"
            )

        observed_models: set[tuple[str, str]] = set()
        for filename, expected_hash in PINNED_MODEL_CATALOG_HASHES.items():
            raw = _read_archive_member(archive, f"{_MODEL_DATA_DIR}/{filename}")
            observed_hash = hashlib.sha256(raw).hexdigest()
            if observed_hash != expected_hash:
                failures.append(
                    f"model catalog drift in {filename}: "
                    f"expected sha256:{expected_hash}, got sha256:{observed_hash}"
                )
            if filename != ".manifest.json":
                observed_models.update(_catalog_models(raw))

        missing_models = sorted(PINNED_FIXTURE_MODELS - observed_models)
        if missing_models:
            failures.append(f"fixture models missing from Pi catalog: {missing_models}")

    return failures


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail visibly when Pi's parser or model-catalog contract drifts"
    )
    parser.add_argument(
        "--url",
        default=_RELEASE_URL,
        help="Pi release source archive URL (defaults to the pinned release)",
    )
    return parser.parse_args(argv)


def _write_line(message: str, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(f"{message}\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        archive_bytes = _download(args.url)
        failures = check_archive(archive_bytes)
    except (OSError, ValueError, tarfile.TarError, urllib.error.URLError) as exc:
        _write_line(f"ERROR: could not verify Pi compatibility: {exc}", error=True)
        return 2

    if failures:
        _write_line(
            f"Pi compatibility check FAILED for {PINNED_PI_TAG} ({PINNED_PI_COMMIT}):",
            error=True,
        )
        for failure in failures:
            _write_line(f"- {failure}", error=True)
        return 1

    _write_line(
        f"Pi compatibility check passed for {PINNED_PI_TAG} ({PINNED_PI_COMMIT}): "
        f"session v{PINNED_SESSION_VERSION}, {len(PINNED_ENTRY_TYPES)} entry types, "
        f"{len(PINNED_STOP_REASONS)} stop reasons, and pinned route catalogs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
