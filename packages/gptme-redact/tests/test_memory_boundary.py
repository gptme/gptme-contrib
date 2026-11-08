"""Tests for the read-only memory-boundary auditor (slice 2.1).

Exercises the fixture workspace shipped in slice 1.1/1.2 of
``tasks/memory-boundary-auditor-5517.md`` and asserts the verdict contract in
``tests/fixtures/memory-boundary-auditor/verdict-spec.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from redact.memory_boundary import (
    CLASSIFICATIONS,
    CONFIDENCE_FLOOR,
    audit_workspace,
    classify_file,
    preview_migration,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "memory-boundary-auditor"
SPEC = json.loads((FIXTURE / "verdict-spec.json").read_text())


def _verdict_by_suffix(result, suffix: str):
    matches = [v for v in result.verdicts if v.path.endswith(suffix)]
    assert matches, f"no verdict for {suffix}: {[v.path for v in result.verdicts]}"
    return matches[0]


def test_fixture_and_spec_exist():
    assert FIXTURE.is_dir()
    assert SPEC["required"] == [
        "path",
        "classification",
        "confidence",
        "rationale",
        "provenance",
        "migration_preview",
    ]


def test_classifications_cover_the_four_categories():
    result = audit_workspace(FIXTURE)
    found = {v.classification for v in result.verdicts}
    assert found == set(CLASSIFICATIONS), found


def test_durable_project_memory():
    result = audit_workspace(FIXTURE)
    for suffix in ("project-memory/core-goal.md", "project-memory/archive-policy.md"):
        verdict = _verdict_by_suffix(result, suffix)
        assert verdict.classification == "durable_project_memory"
        assert verdict.migration_preview["action"] == "migrate_verbatim"
        assert verdict.confidence >= CONFIDENCE_FLOOR


def test_cache_is_dropped():
    verdict = _verdict_by_suffix(audit_workspace(FIXTURE), "cache/rag-results.md")
    assert verdict.classification == "cache"
    assert verdict.migration_preview["action"] == "drop"
    assert verdict.migration_preview["target_layer"] is None


def test_transcript_is_resumable_session_state():
    verdict = _verdict_by_suffix(audit_workspace(FIXTURE), "transcript-snippet.md")
    assert verdict.classification == "resumable_session_state"
    assert verdict.migration_preview["action"] == "migrate_if_session_active"


def test_secrets_are_refused_not_persisted():
    verdict = _verdict_by_suffix(audit_workspace(FIXTURE), "secrets-fixture.md")
    assert verdict.classification == "ambiguous_leakage"
    assert verdict.migration_preview["action"] == "refuse"
    assert verdict.migration_preview["target_layer"] is None
    assert verdict.confidence >= 0.9
    assert any("credential" in w for w in verdict.migration_preview["warnings"])


def test_layered_root_conflict_is_reported_and_resolved():
    result = audit_workspace(FIXTURE)
    conflict = next((c for c in result.conflicts if c["name"] == "shared-policy"), None)
    assert conflict is not None, result.conflicts

    project = _verdict_by_suffix(result, "conflicting-roots/project-layer.md")
    user = _verdict_by_suffix(result, "conflicting-roots/user-layer.md")

    assert project.classification == "durable_project_memory"
    assert user.classification == "durable_project_memory"
    assert project.provenance["layer"] == "project"
    assert user.provenance["layer"] == "user"
    assert conflict["resolved_layer"] == "project"
    # The conflict is durable information: both sides carry the collision warning.
    for verdict in (project, user):
        assert any("name collision" in w for w in verdict.migration_preview["warnings"])


def test_every_verdict_conforms_to_the_spec():
    result = audit_workspace(FIXTURE)
    assert result.verdicts
    for verdict in result.verdicts:
        payload = verdict.to_dict()
        for required in SPEC["required"]:
            assert required in payload, (verdict.path, required)
        assert payload["classification"] in SPEC["properties"]["classification"]["enum"]
        assert 0.0 <= payload["confidence"] <= 1.0
        assert (
            payload["provenance"]["layer"]
            in SPEC["properties"]["provenance"]["properties"]["layer"]["enum"]
        )
        assert (
            payload["migration_preview"]["action"]
            in SPEC["properties"]["migration_preview"]["properties"]["action"]["enum"]
        )


def test_read_only_audit_does_not_modify_files():
    before = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}
    audit_workspace(FIXTURE)
    after = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}
    assert before == after


def test_low_confidence_is_surfaced(tmp_path: Path):
    # A file with no boundary signal is refused, not silently classified.
    path = tmp_path / "notes.md"
    path.write_text("just a plain note with nothing distinctive\n")
    audit = audit_workspace(tmp_path)
    verdict = _verdict_by_suffix(audit, "notes.md")
    assert verdict.classification == "ambiguous_leakage"
    assert verdict.confidence < CONFIDENCE_FLOOR
    assert verdict.migration_preview["action"] == "refuse"
    assert verdict.migration_preview["target_layer"] is None
    assert audit.to_dict()["summary"]["below_confidence_floor"] == ["notes.md"]

    preview = preview_migration(tmp_path)
    assert preview.safe_to_proceed is False
    assert [entry["path"] for entry in preview.actions["refuse"]] == ["notes.md"]
    assert preview.requires_operator == [
        {"path": "notes.md", "reason": "refused by classification"}
    ]


def test_unclassifiable_file_falls_back_to_refusal(tmp_path: Path):
    stray = tmp_path / "notes.md"
    stray.write_text("just a plain note with nothing distinctive\n")
    verdict = classify_file(stray, root=tmp_path)
    assert verdict.classification == "ambiguous_leakage"
    assert verdict.migration_preview["action"] == "refuse"
    assert verdict.confidence < CONFIDENCE_FLOOR
    assert any("operator" in w for w in verdict.migration_preview["warnings"])


def test_migration_preview_groups_actions():
    preview = preview_migration(FIXTURE)
    assert preview.to_dict()["dry_run"] is True
    assert {e["path"] for e in preview.actions["migrate_verbatim"]} >= {
        "workspace/project-memory/core-goal.md",
        "workspace/project-memory/archive-policy.md",
    }
    assert [e["path"] for e in preview.actions["drop"]] == [
        "workspace/cache/rag-results.md"
    ]
    assert [e["path"] for e in preview.actions["refuse"]] == [
        "workspace/secrets-fixture.md"
    ]


def test_migration_preview_blocks_on_secrets():
    preview = preview_migration(FIXTURE)
    assert preview.safe_to_proceed is False
    blocked = {item["path"] for item in preview.requires_operator}
    assert "workspace/secrets-fixture.md" in blocked
    assert any("refused" in item["reason"] for item in preview.requires_operator)


def test_migration_preview_preserves_conflict_resolution():
    preview = preview_migration(FIXTURE)
    conflict = next(c for c in preview.conflicts if c["name"] == "shared-policy")
    assert conflict["resolved_layer"] == "project"
    # Both files still preview as durable migrations; the conflict is information,
    # not a reason to silently drop the shadowed entry.
    migrated = {e["path"] for e in preview.actions["migrate_verbatim"]}
    assert "workspace/conflicting-roots/project-layer.md" in migrated
    assert "workspace/conflicting-roots/user-layer.md" in migrated


def test_migration_preview_is_read_only():
    before = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}
    preview_migration(FIXTURE)
    after = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}
    assert before == after


@pytest.mark.parametrize(
    "filename", ["README.md", "verdict-spec.md", "verdict-spec.json"]
)
@pytest.mark.parametrize("workspace_dir", ["", "workspace"])
@pytest.mark.parametrize("content", ["Durable goal.", "token=placeholder"])
def test_audit_does_not_exempt_documentation_filenames(
    tmp_path: Path, filename: str, workspace_dir: str, content: str
):
    project = tmp_path / workspace_dir / "project-memory"
    project.mkdir(parents=True)
    path = project / filename
    path.write_text(content)

    verdict = _verdict_by_suffix(audit_workspace(tmp_path), filename)
    assert verdict.path == str(path.relative_to(tmp_path))
    preview = preview_migration(tmp_path)
    if content == "token=placeholder":
        assert verdict.classification == "ambiguous_leakage"
        assert verdict.migration_preview["action"] == "refuse"
        assert preview.safe_to_proceed is False
        assert [item["path"] for item in preview.requires_operator] == [verdict.path]
    else:
        assert verdict.classification == "durable_project_memory"
        assert verdict.migration_preview["action"] == "migrate_verbatim"
        assert preview.safe_to_proceed is True


@pytest.mark.parametrize(
    "key",
    [
        "sk-proj-" + "Ab3_-" * 20,
        "sk-svcacct-" + "Ab3_-" * 20,
        "sk-ant-api03-" + "Ab3_-" * 20,
        "sk-ant-oat01-" + "Ab3_-" * 20,
        "sk-ant-ort01-" + "Ab3_-" * 20,
        "sk-ant-admin01-" + "Ab3_-" * 20,
        "github_pat_" + "A" * 22 + "_" + "b" * 59,
    ],
    ids=[
        "openai-project",
        "openai-service",
        "anthropic-api",
        "anthropic-access",
        "anthropic-refresh",
        "anthropic-admin",
        "github-finegrained",
    ],
)
def test_modern_keys_in_project_memory_are_refused(tmp_path: Path, key: str):
    project = tmp_path / "project-memory"
    project.mkdir()
    path = project / "goal.md"
    # Prose (not an assignment), with every durable signal present.
    path.write_text("---\nname: goal\nstatus: living\n---\n\nDurable goal. " + key)

    verdict = classify_file(path, root=tmp_path)
    assert verdict.classification == "ambiguous_leakage"
    assert verdict.confidence >= 0.9
    assert verdict.migration_preview["action"] == "refuse"
    assert verdict.migration_preview["target_layer"] is None
    preview = preview_migration(tmp_path)
    assert preview.safe_to_proceed is False
    assert preview.actions["migrate_verbatim"] == []
    assert [item["path"] for item in preview.requires_operator] == [
        "project-memory/goal.md"
    ]
    assert key not in json.dumps(verdict.to_dict())
    assert key not in json.dumps(preview.to_dict())


@pytest.mark.parametrize(
    "content",
    ["token=x", "sk-" + "Z" * 16, "-----BEGIN PRIVATE KEY-----"],
)
def test_stricter_auditor_placeholder_checks_are_preserved(
    tmp_path: Path, content: str
):
    path = tmp_path / "goal.md"
    path.write_text("---\nname: goal\nstatus: living\n---\n\n" + content)
    verdict = classify_file(path, root=tmp_path)
    assert verdict.classification == "ambiguous_leakage"
    assert verdict.migration_preview["action"] == "refuse"
    assert verdict.confidence >= 0.9


def test_shared_scanner_high_severity_credentials_are_refused(tmp_path: Path):
    path = tmp_path / "goal.md"
    path.write_text(
        "---\nname: goal\nstatus: living\n---\n\n"
        "Integration endpoint: https://discord.com/api/webhooks/"
        + "1" * 18
        + "/"
        + "A" * 60
    )
    assert classify_file(path).migration_preview["action"] == "refuse"
    assert preview_migration(tmp_path).safe_to_proceed is False


def test_contextual_pii_is_not_a_credential_signal(tmp_path: Path):
    path = tmp_path / "goal.md"
    path.write_text(
        "---\nname: goal\nstatus: living\n---\n\n"
        "Contact maintainer@example.com; workspace /home/alice/project."
    )
    assert classify_file(path).classification == "durable_project_memory"
    assert preview_migration(tmp_path).safe_to_proceed is True


def test_safe_preview_on_clean_workspace(tmp_path: Path):
    project = tmp_path / "workspace" / "project-memory"
    project.mkdir(parents=True)
    (project / "goal.md").write_text(
        "---\nname: goal\nstatus: living\n---\n\nDurable goal.\n"
    )
    preview = preview_migration(tmp_path)
    assert preview.safe_to_proceed is True
    assert preview.requires_operator == []
    assert len(preview.actions["migrate_verbatim"]) == 1


@pytest.mark.parametrize("field", ["name", "supersedes", "type"])
@pytest.mark.parametrize(
    "secret", ["sk-proj-" + "Ab3_-" * 20, "sk-" + "Z" * 16, "token=x"]
)
def test_credential_metadata_never_enters_reports(
    tmp_path: Path, field: str, secret: str
):
    for layer in ("project", "user"):
        path = tmp_path / layer / "goal.md"
        path.parent.mkdir()
        path.write_text(f"---\nname: goal\nstatus: living\n{field}: {secret}\n---\n")
        verdict = classify_file(path, root=tmp_path)
        assert secret not in json.dumps(verdict.to_dict())
    audit = audit_workspace(tmp_path)
    assert secret not in json.dumps(audit.to_dict())
    assert secret not in json.dumps(preview_migration(tmp_path).to_dict())


def test_redacted_names_do_not_create_false_collisions(tmp_path: Path):
    for layer, key in (
        ("project", "sk-proj-" + "A" * 50),
        ("user", "sk-proj-" + "B" * 50),
    ):
        path = tmp_path / layer / "goal.md"
        path.parent.mkdir()
        path.write_text(f"---\nname: {key}\nstatus: living\n---\n")
    assert audit_workspace(tmp_path).conflicts == []


def test_credential_paths_are_sanitized_and_refused(tmp_path: Path):
    secret = "sk-proj-" + "Ab3_-" * 20
    root = tmp_path / secret
    root.mkdir()
    path = root / (secret + ".md")
    path.write_text("---\nname: safe-name\nstatus: living\n---\n")
    verdict = classify_file(path, root=root)
    assert secret not in json.dumps(verdict.to_dict())
    assert verdict.migration_preview["action"] == "refuse"
    preview = preview_migration(root)
    assert secret not in json.dumps(preview.to_dict())
    assert preview.safe_to_proceed is False


def test_safe_metadata_identity_is_preserved_without_env_dependence(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("TEST_API_KEY", "safe-policy-name")
    for layer in ("project", "user"):
        path = tmp_path / layer / "goal.md"
        path.parent.mkdir()
        path.write_text(
            "---\nname: safe-policy-name\nsupersedes: old-policy\nstatus: living\n---\n"
        )
    audit = audit_workspace(tmp_path)
    assert audit.conflicts[0]["name"] == "safe-policy-name"
    assert audit.conflicts[0]["resolved_layer"] == "project"
    assert all(v.provenance["supersedes"] == "old-policy" for v in audit.verdicts)


@pytest.mark.parametrize("kind", ["missing", "file"])
@pytest.mark.parametrize("operation", [audit_workspace, preview_migration])
def test_invalid_audit_root_is_rejected(tmp_path: Path, kind: str, operation):
    root = tmp_path / kind
    if kind == "file":
        root.write_text("not a directory")
    with pytest.raises(ValueError, match="directory"):
        operation(root)


@pytest.mark.parametrize("kind", ["missing", "file"])
@pytest.mark.parametrize(
    "flags", [[], ["--json"], ["--dry-run"], ["--dry-run", "--json"]]
)
def test_cli_invalid_root_fails_gracefully(
    tmp_path: Path, kind: str, flags: list[str], capsys
):
    from redact.memory_boundary import main

    root = tmp_path / kind
    if kind == "file":
        root.write_text("not a directory")
    with pytest.raises(SystemExit) as exc:
        main([str(root), *flags])
    assert exc.value.code != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "directory" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    "flags", [[], ["--json"], ["--dry-run"], ["--dry-run", "--json"]]
)
def test_secret_collision_metadata_is_safe_in_cli_and_json(
    tmp_path: Path, flags: list[str], capsys
):
    from redact.memory_boundary import main

    key = "sk-proj-" + "Ab3_-" * 20
    for layer in ("project", "user"):
        path = tmp_path / layer / "goal.md"
        path.parent.mkdir()
        path.write_text(f"---\nname: {key}\nsupersedes: {key}\nstatus: living\n---\n")
    audit = audit_workspace(tmp_path)
    assert len(audit.conflicts) == 1
    assert audit.conflicts[0]["resolved_layer"] == "project"
    assert audit.conflicts[0]["name"] != key
    assert all(
        "name collision" in v.migration_preview["warnings"][-1] for v in audit.verdicts
    )
    assert key not in json.dumps(audit.to_dict())
    assert key not in json.dumps(preview_migration(tmp_path).to_dict())
    assert main([str(tmp_path), *flags]) == 0
    captured = capsys.readouterr()
    assert key not in captured.out + captured.err


def test_empty_existing_directory_is_a_valid_root(tmp_path: Path):
    assert audit_workspace(tmp_path).verdicts == []
    assert preview_migration(tmp_path).safe_to_proceed is True
