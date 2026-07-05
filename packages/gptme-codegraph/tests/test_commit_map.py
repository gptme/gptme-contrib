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


def test_map_is_fresh_rejects_timezone_naive_generated():
    # A hand-crafted or older-tool artifact may omit the UTC offset.
    # datetime.fromisoformat("2024-01-01T12:00:00") succeeds but the
    # subsequent subtraction from timezone.utc raises TypeError — must return False, not crash.
    naive_iso = "2024-01-01T12:00:00"  # no "+00:00"
    existing = {
        "version": commit_map.ARTIFACT_VERSION,
        "generated": naive_iso,
        "source_digest": "digest",
    }
    assert commit_map._map_is_fresh(existing, Path("/tmp"), 365) is False


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
    # Git unavailable: can't verify, must treat as stale (not silently fresh).
    with patch.object(commit_map, "_git_sha", return_value=None):
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


def test_generate_map_directory_is_portable(tmp_path):
    # build_repo_map() returns an absolute build path, but the committed
    # artifact must be reproducible across clones/CI — the top-level directory
    # is pinned to "." regardless of where the map was generated.
    abs_dir_repo_map = {
        "directory": str(tmp_path),
        "files": [],
        "files_shown": 0,
    }
    with (
        patch.object(commit_map, "build_repo_map", return_value=abs_dir_repo_map),
        patch.object(commit_map, "_git_sha", return_value="sha"),
        patch.object(commit_map, "_source_digest", return_value=("digest", 1)),
    ):
        result = commit_map.generate_map(tmp_path)

    assert result["directory"] == "."


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


# ── Stat-fingerprint cache tests ──────────────────────────────────────


def test_generate_map_hit_returns_cached_result(tmp_path):
    cache_key = commit_map._repo_cache_key(tmp_path)
    cached = {
        "version": commit_map.ARTIFACT_VERSION,
        "source_digest": "cached-digest",
        "source_file_count": 3,
        "directory": str(tmp_path),
        "files_shown": 1,
        "symbols_shown": 5,
        "files": [{"path": "a.py", "symbols": [{"name": "f", "kind": "function"}]}],
        "git_sha": "cached-sha",
        "generated": "2025-01-01T00:00:00+00:00",
        "max_files": 20,
        "max_symbols_per_file": 12,
        "_stat_fingerprint": "match",
        "_cached_at": commit_map.time.time(),
    }
    commit_map._write_cache(cache_key, cached)

    # fingerprint matches → cache hit, build_repo_map should NOT be called
    with (
        patch.object(commit_map, "_stat_fingerprint", return_value=("match", 3)),
        patch.object(commit_map, "_source_digest", return_value=("fresh-digest", 3)),
        patch.object(commit_map, "build_repo_map") as mock_build,
    ):
        result = commit_map.generate_map(tmp_path)

    mock_build.assert_not_called()
    # The result is the cached map minus internal cache key
    assert result["source_digest"] == "cached-digest"
    assert "_cached_at" not in result


def test_generate_map_fingerprint_mismatch_does_full_build(tmp_path):
    cache_key = commit_map._repo_cache_key(tmp_path)
    cached = {
        "version": commit_map.ARTIFACT_VERSION,
        "source_digest": "old-digest",
        "source_file_count": 1,
        "files": [],
        "files_shown": 0,
        "git_sha": "old-sha",
        "generated": "2025-01-01T00:00:00+00:00",
        "_stat_fingerprint": "old-fp",
        "_cached_at": commit_map.time.time(),
    }
    commit_map._write_cache(cache_key, cached)

    fresh = {
        "files": [{"path": "b.py", "symbols": []}],
        "files_shown": 1,
        "symbols_shown": 2,
    }
    with (
        patch.object(commit_map, "_stat_fingerprint", return_value=("new-fp", 1)),
        patch.object(commit_map, "_source_digest", return_value=("new-digest", 1)),
        patch.object(commit_map, "_git_sha", return_value="new-sha"),
        patch.object(commit_map, "build_repo_map", return_value=fresh),
    ):
        result = commit_map.generate_map(tmp_path)

    assert result["source_digest"] == "new-digest"
    assert result["files_shown"] == 1


def test_generate_map_cache_hit_writes_back_timestamp(tmp_path):
    """Cache hit refreshes the _cached_at timestamp (keep-warm pattern)."""
    cache_key = commit_map._repo_cache_key(tmp_path)
    old_ts = commit_map.time.time() - 3600
    cached = {
        "version": commit_map.ARTIFACT_VERSION,
        "source_digest": "digest",
        "source_file_count": 1,
        "files": [],
        "files_shown": 0,
        "git_sha": "sha",
        "generated": "2025-01-01T00:00:00+00:00",
        "max_files": 20,
        "max_symbols_per_file": 12,
        "_stat_fingerprint": "match",
        "_cached_at": old_ts,
    }
    # Write the cache file directly — _write_cache would overwrite _cached_at
    # with time.time(), defeating the test.
    cache_path = commit_map._CACHE_DIR / f"{cache_key}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cached))

    with (
        patch.object(commit_map, "_stat_fingerprint", return_value=("match", 1)),
        patch.object(commit_map, "_source_digest", return_value=("digest", 1)),
    ):
        commit_map.generate_map(tmp_path)

    reloaded = commit_map._read_cache(cache_key)
    assert reloaded is not None
    assert reloaded["_cached_at"] > old_ts + 3500  # within ~100s slack
    # Fingerprint must survive the write-back — otherwise the next run sees
    # None != stat_fp and falls through to a full rebuild on every other hit.
    assert reloaded["_stat_fingerprint"] == "match"


def test_generate_map_no_cache_skips_cache(tmp_path):
    cache_key = commit_map._repo_cache_key(tmp_path)
    cached = {
        "version": commit_map.ARTIFACT_VERSION,
        "source_digest": "cached-digest",
        "source_file_count": 1,
        "files": [],
        "files_shown": 0,
        "git_sha": "old-sha",
        "generated": "2025-01-01T00:00:00+00:00",
        "_cached_at": commit_map.time.time(),
    }
    commit_map._write_cache(cache_key, cached)

    fresh = {"files": [], "files_shown": 3, "symbols_shown": 4}
    with (
        patch.object(commit_map, "_source_digest", return_value=("fresh-digest", 2)),
        patch.object(commit_map, "_git_sha", return_value="fresh-sha"),
        patch.object(commit_map, "build_repo_map", return_value=fresh),
    ):
        result = commit_map.generate_map(tmp_path, use_cache=False)

    assert result["source_digest"] == "fresh-digest"


def test_cmd_refresh_no_cache_skips_cache(tmp_path):
    output = tmp_path / commit_map.DEFAULT_OUTPUT_FILE

    # Write an existing map so refresh finds what to replace.
    existing = {
        "version": commit_map.ARTIFACT_VERSION,
        "generated": "2025-01-01T00:00:00+00:00",
        "source_digest": "old-digest",
        "git_sha": "old-sha",
        "files": [],
        "files_shown": 0,
    }
    output.write_text(json.dumps(existing))

    args = argparse.Namespace(
        directory=str(tmp_path),
        output=commit_map.DEFAULT_OUTPUT_FILE,
        stale_after_days=1,
        max_files=20,
        max_symbols_per_file=12,
        force=True,
        no_cache=True,
    )

    with (
        patch.object(commit_map, "_source_digest", return_value=("fresh-digest", 3)),
        patch.object(commit_map, "_git_sha", return_value="fresh-sha"),
        patch.object(
            commit_map,
            "build_repo_map",
            return_value={"files": [], "files_shown": 5, "symbols_shown": 6},
        ),
        patch.object(commit_map, "format_repo_map", return_value=""),
    ):
        result = commit_map.cmd_refresh(args)

    assert result == 0
    # Cache should NOT have been written (no-cache mode).
    cache_key = commit_map._repo_cache_key(tmp_path)
    cached = commit_map._read_cache(cache_key)
    assert cached is None


def test_generate_map_cache_miss_on_param_change(tmp_path):
    """Cache hit is rejected when max_files or max_symbols_per_file differ."""
    cache_key = commit_map._repo_cache_key(tmp_path)
    cached = {
        "version": commit_map.ARTIFACT_VERSION,
        "source_digest": "digest",
        "source_file_count": 1,
        "files": list(range(20)),  # 20-file result
        "files_shown": 20,
        "symbols_shown": 0,
        "git_sha": "sha",
        "generated": "2025-01-01T00:00:00+00:00",
        "max_files": 20,
        "max_symbols_per_file": 12,
        "_stat_fingerprint": "match",
        "_cached_at": commit_map.time.time(),
    }
    cache_path = commit_map._CACHE_DIR / f"{cache_key}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cached))

    fresh = {"files": list(range(5)), "files_shown": 5, "symbols_shown": 0}
    with (
        patch.object(commit_map, "_stat_fingerprint", return_value=("match", 1)),
        patch.object(commit_map, "_source_digest", return_value=("digest", 1)),
        patch.object(commit_map, "_git_sha", return_value="sha"),
        patch.object(commit_map, "build_repo_map", return_value=fresh),
    ):
        result = commit_map.generate_map(tmp_path, max_files=5)

    # Should have rebuilt — 5 files, not 20.
    assert result["files_shown"] == 5


def test_generate_map_cache_write_failure_non_fatal(tmp_path):
    """OSError during cache write must not propagate out of generate_map."""
    fresh = {"files": [], "files_shown": 7, "symbols_shown": 0}
    with (
        patch.object(commit_map, "_stat_fingerprint", return_value=("fp", 1)),
        patch.object(commit_map, "_source_digest", return_value=("digest", 1)),
        patch.object(commit_map, "_git_sha", return_value="sha"),
        patch.object(commit_map, "build_repo_map", return_value=fresh),
        patch.object(commit_map, "_write_cache", side_effect=OSError("disk full")),
    ):
        result = commit_map.generate_map(tmp_path)

    # The computed result should still be returned despite the write failure.
    assert result["files_shown"] == 7


def test_stat_fingerprint_changes_on_mtime(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.py").write_text("x = 1")
    (d / "b.ts").write_text("const y = 2;")

    with patch.object(
        commit_map,
        "_tracked_source_files",
        return_value=([d / "a.py", d / "b.ts"], tmp_path),
    ):
        fp1, count1 = commit_map._stat_fingerprint(d)
        fp2, count2 = commit_map._stat_fingerprint(d)

    assert fp1 is not None
    assert count1 == 2
    assert fp1 == fp2  # stable when nothing changes

    # Modify a file — fingerprint must change.
    # Use different-length content so the size field changes reliably; mtime_ns can be
    # identical for sequential writes on fast SSDs within the same kernel timer tick.
    (d / "a.py").write_text("x = 2\n")
    with patch.object(
        commit_map,
        "_tracked_source_files",
        return_value=([d / "a.py", d / "b.ts"], tmp_path),
    ):
        fp3, count3 = commit_map._stat_fingerprint(d)

    assert fp3 != fp1  # mtime_ns or size changed
    assert count3 == 2
