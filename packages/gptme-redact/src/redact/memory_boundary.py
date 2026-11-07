"""Read-only memory-boundary auditor (slice 2.1 of memory-boundary-auditor-5517).

Classifies files in a memory workspace into the four boundary categories the
unified memory CLI (gptme/gptme#3734) and cross-session KB
(gptme/gptme#3596) must distinguish before a migration:

    durable_project_memory  -> persist verbatim
    resumable_session_state -> persist only if gated on an active session
    cache                   -> drop (regenerable)
    ambiguous_leakage       -> refuse to migrate, surface to operator

The auditor is **read-only**: it classifies, attributes, and previews. It
never moves, rewrites, or persists anything. The migration verbs live in
``verdict["migration_preview"]["action"]`` as a preview, never an execution.

Contract: every verdict conforms to
``tests/fixtures/memory-boundary-auditor/verdict-spec.json``.

Fail-closed: anything that cannot be confidently classified is reported as
``ambiguous_leakage`` with a low confidence and refused. Silently treating a
credential dump as durable memory is the exact failure this auditor exists to
prevent, so the fallback direction is "refuse", not "migrate".

CLI::

    python -m redact.memory_boundary <fixture-root> [--json] [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from redact import Severity, redact

CLASSIFICATIONS = (
    "durable_project_memory",
    "resumable_session_state",
    "cache",
    "ambiguous_leakage",
)

# classification -> (action, migrates). "migrates" controls whether the
# verdict carries a target layer; drop/refuse target nothing.
_MIGRATION: dict[str, tuple[str, bool]] = {
    "durable_project_memory": ("migrate_verbatim", True),
    "resumable_session_state": ("migrate_if_session_active", True),
    "cache": ("drop", False),
    "ambiguous_leakage": ("refuse", False),
}

# Nearest-root resolution order: on a name collision project beats agent beats
# user beats explicit beats unknown. The conflict itself is still reported.
_LAYER_RANK = {"project": 0, "agent": 1, "user": 2, "explicit": 3, "unknown": 4}

# Confidence below this is surfaced to the operator rather than silently
# classified (verdict-spec.md, "Why this contract").
CONFIDENCE_FLOOR = 0.6

# Variable-name + value-shape credential patterns. Deliberately matches the
# *pattern*, not real secrets — the fixture uses placeholders and the auditor
# must still refuse the file.
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?im)^\s*(?:"
    r"api[_-]?key|secret|token|password|passwd|passphrase|"
    r"github[_-]?pat|openrouter[_-]?key|anthropic[_-]?key|openai[_-]?key|"
    r"aws[_-]?(?:access|secret)[_-]?key|access[_-]?key[_-]?id|"
    r"private[_-]?key|client[_-]?secret|bearer"
    r")\s*[=:]\s*\S+"
)
# Conservative legacy/placeholder shapes, including short tokens and incomplete
# private-key blocks. Keep these stricter auditor checks alongside the shared
# scanner rather than duplicating its evolving vendor-specific credential list.
_CREDENTIAL_SHAPE = re.compile(
    r"(?:"
    r"sk-[A-Za-z0-9]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r")"
)
# Report fields may embed assignments in prose or paths rather than at a line
# start. Reuse the auditor's strict vocabulary without another vendor deny-list.
_METADATA_ASSIGNMENT = re.compile(
    _CREDENTIAL_ASSIGNMENT.pattern.replace(r"^\s*", r"\b")
)


def _safe_metadata(value: str) -> str:
    """Opaque, deterministic identity for credential-bearing report fields.

    Hash the entire field, not just the matched substring: no secret tail can
    escape, distinct unsafe names do not collapse into a false collision, and
    equal names still participate in nearest-root resolution. No env reads.
    """
    if (
        _METADATA_ASSIGNMENT.search(value)
        or _CREDENTIAL_SHAPE.search(value)
        or redact(value, block_severity=Severity.HIGH).blocked
    ):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"[REDACTED:sha256:{digest}]"
    return value


_CREDENTIAL_FILENAME = re.compile(
    r"(?:secret|credential|password|passwd|token|api[_-]?key|\.env)", re.I
)

# Session-transcript shape: bracketed timestamped speaker lines.
_TRANSCRIPT_LINE = re.compile(r"^\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\]\s*\S+")
_TRANSCRIPT_FILENAME = re.compile(r"transcript", re.I)

_CACHE_CONTENT = re.compile(r'"regenerable"\s*:\s*true|"ttl_seconds"')
_CACHE_DIRNAME = {"cache", ".cache", "__pycache__", "caches"}
_DURABLE_STATUS = {"living", "historical", "durable"}

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


@dataclass
class Verdict:
    """One classified file. ``to_dict()`` conforms to verdict-spec.json."""

    path: str
    classification: str
    confidence: float
    rationale: str
    provenance: dict[str, Any]
    migration_preview: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "classification": self.classification,
            "confidence": round(self.confidence, 2),
            "rationale": self.rationale,
            "provenance": self.provenance,
            "migration_preview": self.migration_preview,
        }


@dataclass
class AuditResult:
    """All verdicts from one workspace walk, plus cross-file findings."""

    verdicts: list[Verdict] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdicts": [v.to_dict() for v in self.verdicts],
            "conflicts": self.conflicts,
            "warnings": self.warnings,
            "summary": {
                "total": len(self.verdicts),
                "by_classification": {
                    c: sum(1 for v in self.verdicts if v.classification == c)
                    for c in CLASSIFICATIONS
                },
                "below_confidence_floor": [
                    v.path for v in self.verdicts if v.confidence < CONFIDENCE_FLOOR
                ],
            },
        }


@dataclass
class MigrationPreview:
    """Slice 2.2: what a unified-memory-CLI migration *would* move.

    Read-only. Groups the audit verdicts by preview action, records layered-root
    resolution, and names the files that require operator attention before any
    migration runs.
    """

    root: str
    actions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    requires_operator: list[dict[str, Any]] = field(default_factory=list)
    safe_to_proceed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "dry_run": True,
            "actions": self.actions,
            "conflicts": self.conflicts,
            "requires_operator": self.requires_operator,
            "safe_to_proceed": self.safe_to_proceed,
            "note": "preview only — no file is moved, rewritten, or persisted",
        }


def preview_migration(root: str | Path) -> MigrationPreview:
    """Preview what a migration would do, without doing anything.

    Consumes :func:`audit_workspace` verdicts and projects each into its preview
    action. A refusal (``ambiguous_leakage``) or a below-floor confidence
    blocks ``safe_to_proceed`` — the dry-run surfaces the problem instead of
    quietly persisting it.
    """
    audit = audit_workspace(root)
    actions: dict[str, list[dict[str, Any]]] = {
        action: [] for action, _ in _MIGRATION.values()
    }
    requires_operator: list[dict[str, Any]] = []

    for verdict in audit.verdicts:
        preview = verdict.migration_preview
        entry = {
            "path": verdict.path,
            "classification": verdict.classification,
            "confidence": round(verdict.confidence, 2),
            "target_layer": preview["target_layer"],
            "warnings": list(preview["warnings"]),
        }
        actions[preview["action"]].append(entry)

        if preview["action"] == "refuse" or verdict.confidence < CONFIDENCE_FLOOR:
            requires_operator.append(
                {
                    "path": verdict.path,
                    "reason": (
                        "refused by classification"
                        if preview["action"] == "refuse"
                        else f"confidence {verdict.confidence:.2f} below floor {CONFIDENCE_FLOOR}"
                    ),
                }
            )

    for action in actions:
        actions[action].sort(key=lambda e: e["path"])

    return MigrationPreview(
        root=_safe_metadata(str(root)),
        actions=actions,
        conflicts=audit.conflicts,
        requires_operator=requires_operator,
        safe_to_proceed=not requires_operator,
    )


def _frontmatter_scalars(text: str) -> dict[str, str]:
    """Scalar-only frontmatter reader for the boundary classifier.

    Reads the flat ``key: value`` fields that carry boundary signal (``name``,
    ``description``, ``status``, ``type``, ``layer``, ``supersedes``). Nested
    structures are intentionally out of scope.

    This is NOT a general frontmatter loader: the canonical
    ``frontmatter_compat.loads`` lives in the brain-repo ``scripts/`` tree and
    in ``gptodo``, neither of which an installed workspace package may import.
    Renaming (rather than delegating) keeps that dependency boundary explicit;
    if a canonical frontmatter helper ever lands in ``bobutils``, this should
    delegate to it.
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def _infer_layer(rel_path: Path, frontmatter: dict[str, str]) -> str:
    """Own layer of the file: explicit frontmatter wins, else path heuristic."""
    explicit = frontmatter.get("layer", "").strip().lower()
    if explicit in _LAYER_RANK:
        return explicit
    parts = [p.lower() for p in rel_path.parts]
    stem = rel_path.stem.lower()
    if any("project" in p for p in parts) or "project-layer" in stem:
        return "project"
    if any("user" in p for p in parts) or "user-layer" in stem:
        return "user"
    if any("agent" in p for p in parts):
        return "agent"
    return "unknown"


def _classify(
    text: str, rel_path: Path, frontmatter: dict[str, str]
) -> tuple[str, float, str]:
    """Return ``(classification, confidence, rationale)``.

    Fail-closed ordering: credential signal first, then transcript, then
    cache, then durable. A credential-shaped file must never be downgraded to
    durable because it happens to carry frontmatter.
    """
    stem = rel_path.name
    parent_parts = {p.lower() for p in rel_path.parts[:-1]}

    # 1. Leakage — always wins.
    if (
        _CREDENTIAL_ASSIGNMENT.search(text)
        or _CREDENTIAL_SHAPE.search(text)
        # Reuse credential coverage without collecting process-env literals or
        # treating MEDIUM-severity contextual PII as a credential. The redacted
        # copy is discarded: this auditor never rewrites source content.
        or redact(text, block_severity=Severity.HIGH).blocked
    ):
        return (
            "ambiguous_leakage",
            0.99,
            "content matches credential-like pattern (variable name + value shape)",
        )
    if _CREDENTIAL_FILENAME.search(stem):
        return (
            "ambiguous_leakage",
            0.85,
            "filename matches credential-like pattern",
        )

    # 2. Resumable session state — transcript shape.
    transcript_lines = sum(
        1 for line in text.splitlines() if _TRANSCRIPT_LINE.match(line)
    )
    if transcript_lines >= 2:
        return (
            "resumable_session_state",
            0.95,
            f"content has {transcript_lines} timestamped speaker lines (session transcript shape)",
        )
    if _TRANSCRIPT_FILENAME.search(stem):
        return (
            "resumable_session_state",
            0.8,
            "filename marks a session transcript",
        )

    # 3. Cache — regenerable by directory or content marker.
    if parent_parts & _CACHE_DIRNAME:
        return "cache", 0.95, "lives in a cache directory"
    if _CACHE_CONTENT.search(text):
        return (
            "cache",
            0.9,
            "content declares regenerable/ttl metadata (regenerable cache)",
        )

    # 4. Durable project memory — frontmatter markers.
    status = frontmatter.get("status", "").lower()
    ftype = frontmatter.get("type", "").lower()
    if "project-memory" in parent_parts or "project-memory" in {
        p for p in rel_path.parts
    }:
        return (
            "durable_project_memory",
            0.95,
            "lives in a project-memory directory (durable, cold or living)",
        )
    if frontmatter.get("name") and status in _DURABLE_STATUS:
        kind = f" (type={_safe_metadata(ftype)})" if ftype else ""
        return (
            "durable_project_memory",
            0.9,
            f"frontmatter status={status}{kind}, no session or cache markers",
        )
    if frontmatter.get("name") and frontmatter.get("description"):
        return (
            "durable_project_memory",
            0.7,
            "frontmatter name+description with no session or cache markers",
        )

    # 5. Fail-closed fallback: refuse rather than silently persist.
    return (
        "ambiguous_leakage",
        0.3,
        "unclassifiable content; refusing by default (fail-closed)",
    )


def classify_file(
    path: str | Path,
    *,
    root: str | Path | None = None,
) -> Verdict:
    """Classify one file. ``root`` sets the base for the verdict path."""
    file_path = Path(path)
    base = Path(root) if root is not None else file_path.parent
    try:
        rel = file_path.relative_to(base)
    except ValueError:
        rel = Path(file_path.name)

    text = file_path.read_text(encoding="utf-8", errors="replace")
    frontmatter = _frontmatter_scalars(text)
    classification, confidence, rationale = _classify(text, rel, frontmatter)
    safe_path = _safe_metadata(str(rel))
    if safe_path != str(rel):
        classification, confidence, rationale = (
            "ambiguous_leakage",
            0.99,
            "path contains credential-like material",
        )

    layer = _infer_layer(rel, frontmatter)
    name = _safe_metadata(frontmatter.get("name") or rel.stem)
    supersedes = (
        _safe_metadata(frontmatter["supersedes"])
        if frontmatter.get("supersedes")
        else None
    )

    action, migrates = _MIGRATION[classification]
    warnings: list[str] = []
    if classification == "ambiguous_leakage":
        warnings.append("credential-like or unclassifiable content")
        warnings.append("would be persisted if not flagged")
    if confidence < CONFIDENCE_FLOOR:
        warnings.append(
            "confidence below 0.6 — surfaced to operator, not silently classified"
        )

    return Verdict(
        path=safe_path,
        classification=classification,
        confidence=confidence,
        rationale=_safe_metadata(rationale),
        provenance={"layer": layer, "name": name, "supersedes": supersedes},
        migration_preview={
            "action": action,
            "target_layer": layer if migrates else None,
            "warnings": warnings,
        },
    )


def _validate_root(root: str | Path) -> Path:
    base = Path(root)
    if not base.is_dir():
        # Do not echo the supplied path: even error metadata can hold secrets.
        raise ValueError("audit root must be an existing directory")
    return base


def audit_workspace(root: str | Path) -> AuditResult:
    """Walk ``root`` (or ``root/workspace`` when present) and classify every file.

    Read-only: no file is modified. Cross-file layered-root collisions are
    resolved by nearest root and reported as durable information.
    """
    base = _validate_root(root)
    scan_root = base / "workspace" if (base / "workspace").is_dir() else base

    result = AuditResult()
    for file_path in sorted(p for p in scan_root.rglob("*") if p.is_file()):
        result.verdicts.append(classify_file(file_path, root=base))

    _mark_conflicts(result)
    return result


def _mark_conflicts(result: AuditResult) -> None:
    """Detect same-name entries across layers; record resolution by nearest root."""
    by_name: dict[str, list[Verdict]] = {}
    for verdict in result.verdicts:
        by_name.setdefault(verdict.provenance["name"], []).append(verdict)

    for name, verdicts in by_name.items():
        layers = sorted({v.provenance["layer"] for v in verdicts})
        if len(layers) < 2:
            continue
        resolved = min(layers, key=lambda layer: _LAYER_RANK.get(layer, 99))
        result.conflicts.append(
            {
                "name": name,
                "layers": layers,
                "paths": [v.path for v in verdicts],
                "resolved_layer": resolved,
                "note": "nearest-root resolution (project > agent > user > explicit > unknown); conflict preserved as durable information",
            }
        )
        for verdict in verdicts:
            verdict.migration_preview["warnings"].append(
                f"name collision for '{name}' across layers {', '.join(layers)}; "
                f"nearest root '{resolved}' wins for the read path"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m redact.memory_boundary",
        description="Read-only memory-boundary auditor: classify, attribute, preview. Never migrates.",
    )
    parser.add_argument("root", help="workspace (or fixture) root to audit")
    parser.add_argument(
        "--json", action="store_true", help="emit the full audit as JSON"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="emit the migration preview (what would move) instead of the per-file audit",
    )
    args = parser.parse_args(argv)
    try:
        _validate_root(args.root)
    except ValueError as exc:
        parser.error(str(exc))

    if args.dry_run:
        migration = preview_migration(args.root)
        if args.json:
            print(json.dumps(migration.to_dict(), indent=2))
            return 0
        for action, entries in migration.actions.items():
            print(f"{action:<28} {len(entries)}")
        for item in migration.requires_operator:
            print(f"REQUIRES OPERATOR  {item['path']}: {item['reason']}")
        print(f"safe_to_proceed: {migration.safe_to_proceed}")
        return 0

    result = audit_workspace(args.root)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    for verdict in result.verdicts:
        action = verdict.migration_preview["action"]
        print(
            f"{verdict.classification:<24} {verdict.confidence:.2f}  "
            f"{verdict.path}  -> {action}"
        )
    for conflict in result.conflicts:
        print(
            f"CONFLICT name={conflict['name']} layers={conflict['layers']} "
            f"-> nearest root {conflict['resolved_layer']}"
        )
    below = result.to_dict()["summary"]["below_confidence_floor"]
    if below:
        print(f"surfaced to operator (confidence < {CONFIDENCE_FLOOR}): {below}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
