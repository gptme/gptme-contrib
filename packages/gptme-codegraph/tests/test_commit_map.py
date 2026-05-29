"""Tests for gptme_codegraph.commit_map (committed repo-map artifact contract)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from gptme_codegraph import commit_map


def test_generate_map_includes_source_digest_metadata(tmp_path):
    with (
        patch.object(
            commit_map,
            "build_repo_map",
            return_value={"directory": str(tmp_path), "files": [], "files_shown": 0},
        ),
        patch.object(commit_map, "_git_sha", return_value="abc123"),
        patch.object(commit_map, "_source_digest", return_value=("digest", 7)),
    ):
        result = commit_map.generate_map(tmp_path)

    assert result["version"] == commit_map.ARTIFACT_VERSION
    assert result["git_sha"] == "abc123"
    assert result["source_digest"] == "digest"
    assert result["source_file_count"] == 7
    assert result["generator"] == "gptme-codegraph-commit-map"


def test_map_is_fresh_prefers_source_digest_over_git_sha():
    # Source digest matches but git_sha differs -> still fresh. This is the
    # whole point of the contract: a committed artifact stays fresh after the
    # commit that contains it lands and moves HEAD.
    existing = {
        "version": commit_map.ARTIFACT_VERSION,
        "generated": datetime.now(timezone.utc).isoformat(),
        "source_digest": "digest",
        "git_sha": "old-sha",
    }

    with (
        patch.object(commit_map, "_source_digest", return_value=("digest", 9)),
        patch.object(commit_map, "_git_sha", return_value="new-sha"),
    ):
        assert commit_map._map_is_fresh(existing, Path("/tmp"), 1) is True


def test_map_is_fresh_rejects_digest_mismatch():
    existing = {
        "version": commit_map.ARTIFACT_VERSION,
        "generated": datetime.now(timezone.utc).isoformat(),
        "source_digest": "digest-a",
        "git_sha": "same-sha",
    }

    with patch.object(commit_map, "_source_digest", return_value=("digest-b", 9)):
        assert commit_map._map_is_fresh(existing, Path("/tmp"), 1) is False


def test_map_is_fresh_rejects_old_age():
    old = datetime.now(timezone.utc) - timedelta(days=3)
    existing = {
        "version": commit_map.ARTIFACT_VERSION,
        "generated": old.isoformat(),
        "source_digest": "digest",
    }
    # Age check fires before the digest check, so no source_digest patch needed.
    assert commit_map._map_is_fresh(existing, Path("/tmp"), 1) is False


def test_map_is_fresh_rejects_version_mismatch():
    existing = {
        "version": commit_map.ARTIFACT_VERSION + 1,
        "generated": datetime.now(timezone.utc).isoformat(),
        "source_digest": "digest",
    }
    assert commit_map._map_is_fresh(existing, Path("/tmp"), 1) is False


def test_map_is_fresh_backward_compat_git_sha_fallback():
    # Older artifacts without source_digest fall back to git_sha equality.
    existing = {
        "version": commit_map.ARTIFACT_VERSION,
        "generated": datetime.now(timezone.utc).isoformat(),
        "git_sha": "sha-1",
    }
    with patch.object(commit_map, "_git_sha", return_value="sha-1"):
        assert commit_map._map_is_fresh(existing, Path("/tmp"), 1) is True
    with patch.object(commit_map, "_git_sha", return_value="sha-2"):
        assert commit_map._map_is_fresh(existing, Path("/tmp"), 1) is False


def test_save_and_load_roundtrip(tmp_path):
    output = tmp_path / commit_map.DEFAULT_OUTPUT_FILE
    data = {"version": 1, "files": [], "source_digest": "d"}
    commit_map.save_map(tmp_path, output, data)

    assert output.exists()
    loaded = commit_map._load_existing_map(output)
    assert loaded == data
    # Atomic-write tmp file must not linger.
    assert not output.with_suffix(output.suffix + ".tmp").exists()


def test_load_existing_map_handles_corrupt_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    assert commit_map._load_existing_map(bad) is None


def test_cmd_check_missing_map_is_stale(tmp_path):
    args = argparse.Namespace(
        directory=str(tmp_path),
        output=commit_map.DEFAULT_OUTPUT_FILE,
        stale_after_days=1,
    )
    assert commit_map.cmd_check(args) == 1


def test_cmd_check_fresh_map_exits_zero(tmp_path):
    output = tmp_path / commit_map.DEFAULT_OUTPUT_FILE
    data = {
        "version": commit_map.ARTIFACT_VERSION,
        "generated": datetime.now(timezone.utc).isoformat(),
        "source_digest": "digest",
        "git_sha": "sha",
    }
    output.write_text(json.dumps(data))

    args = argparse.Namespace(
        directory=str(tmp_path),
        output=commit_map.DEFAULT_OUTPUT_FILE,
        stale_after_days=1,
    )
    with patch.object(commit_map, "_source_digest", return_value=("digest", 1)):
        assert commit_map.cmd_check(args) == 0


def test_generate_map_metadata_not_overwritten_by_repo_map(tmp_path):
    # If build_repo_map returns keys that collide with freshness metadata, the
    # explicit metadata values must win.
    colliding_repo_map = {
        "version": 999,
        "source_digest": "WRONG",
        "source_file_count": 0,
        "files": [],
        "files_shown": 2,
    }
    with (
        patch.object(commit_map, "build_repo_map", return_value=colliding_repo_map),
        patch.object(commit_map, "_git_sha", return_value="sha"),
        patch.object(commit_map, "_source_digest", return_value=("correct-digest", 5)),
    ):
        result = commit_map.generate_map(tmp_path)

    assert result["version"] == commit_map.ARTIFACT_VERSION
    assert result["source_digest"] == "correct-digest"
    assert result["source_file_count"] == 5


def test_cmd_refresh_force_regenerates_fresh_map(tmp_path):
    # --refresh --force must regenerate even when the map is already fresh.
    output = tmp_path / commit_map.DEFAULT_OUTPUT_FILE
    data = {
        "version": commit_map.ARTIFACT_VERSION,
        "generated": datetime.now(timezone.utc).isoformat(),
        "source_digest": "digest",
        "git_sha": "sha",
    }
    output.write_text(json.dumps(data))

    args = argparse.Namespace(
        directory=str(tmp_path),
        output=commit_map.DEFAULT_OUTPUT_FILE,
        stale_after_days=1,
        max_files=20,
        max_symbols_per_file=12,
        force=True,
    )

    with (
        patch.object(commit_map, "_source_digest", return_value=("digest", 1)),
        patch.object(
            commit_map,
            "build_repo_map",
            return_value={"files": [], "files_shown": 0, "symbols_shown": 0},
        ),
        patch.object(commit_map, "_git_sha", return_value="sha"),
        patch.object(commit_map, "format_repo_map", return_value=""),
    ):
        result = commit_map.cmd_refresh(args)

    assert result == 0
    # Map must have been rewritten (new generated timestamp differs from original).
    loaded = commit_map._load_existing_map(output)
    assert loaded is not None
    assert loaded.get("source_file_count") == 1
