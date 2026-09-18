"""Tests for the shared symlink-hygiene scanner (check_symlinks).

Covers both modes and the two behaviours the arc relies on:
  - default mode: verbatim contrib copies are flagged (should be symlinks),
    name collisions warn, and trivial/empty files never produce false matches;
  - forked mode: non-symlink scripts must be symlinks or on the allowlist,
    which is now loadable from a file instead of baked into this shared script.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "precommit" / "validators"))

import check_symlinks as scanner  # noqa: E402


def make_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Create an agent-like tree: scripts/ + a contrib submodule with scripts/."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    contrib_scripts = tmp_path / "gptme-contrib" / "scripts"
    contrib_scripts.mkdir(parents=True)
    return scripts, contrib_scripts


def run(tmp_path: Path, extra: list[str]) -> int:
    return scanner.main([str(tmp_path), *extra])


# --- default mode -----------------------------------------------------------


def test_verbatim_copy_is_error(tmp_path):
    """A script whose contents equal a contrib file should be a symlink → exit 1."""
    scripts, contrib = make_tree(tmp_path)
    body = "#!/bin/sh\necho shared logic\n"
    (contrib / "shared.sh").write_text(body)
    (scripts / "shared.sh").write_text(body)  # verbatim copy, not a symlink
    assert run(tmp_path, ["--mode", "default"]) == 1


def test_symlinked_copy_is_clean(tmp_path):
    """A proper symlink to the contrib original is not flagged → exit 0."""
    scripts, contrib = make_tree(tmp_path)
    body = "#!/bin/sh\necho shared logic\n"
    original = contrib / "shared.sh"
    original.write_text(body)
    os.symlink(original, scripts / "shared.sh")
    assert run(tmp_path, ["--mode", "default"]) == 0


def test_name_collision_is_warning_not_error(tmp_path):
    """Same name, different content is a drift *warning* (exit 0), not an error."""
    scripts, contrib = make_tree(tmp_path)
    (contrib / "setup.sh").write_text("#!/bin/sh\necho contrib setup\n")
    (scripts / "setup.sh").write_text("#!/bin/sh\necho agent setup — different\n")
    assert run(tmp_path, ["--mode", "default"]) == 0


def test_trivial_empty_file_is_not_a_false_copy(tmp_path):
    """Empty __init__.py files must never hash-match each other (the #96 fix)."""
    scripts, contrib = make_tree(tmp_path)
    (contrib / "__init__.py").write_text("")
    (scripts / "__init__.py").write_text("")
    assert run(tmp_path, ["--mode", "default"]) == 0


def test_no_overlap_is_clean(tmp_path):
    """A purely agent-specific script with no contrib counterpart → exit 0."""
    scripts, _ = make_tree(tmp_path)
    (scripts / "only-here.py").write_text("print('local')\n")
    assert run(tmp_path, ["--mode", "default"]) == 0


# --- forked mode ------------------------------------------------------------


def test_forked_unexpected_regular_file_is_error(tmp_path):
    """A non-symlink script not on the allowlist is unexpected → exit 1."""
    scripts, _ = make_tree(tmp_path)
    (scripts / "sneaky.py").write_text("print('should be a symlink')\n")
    assert run(tmp_path, ["--mode", "forked"]) == 1


def test_forked_builtin_allowlist_accepted(tmp_path):
    """Built-in default allowlist entries pass forked mode → exit 0."""
    make_tree(tmp_path)
    # DEFAULT_AGENT_SPECIFIC_SCRIPTS contains workspace-relative paths.
    entry = next(iter(scanner.DEFAULT_AGENT_SPECIFIC_SCRIPTS))
    fpath = tmp_path / entry
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text("#!/bin/sh\necho agent-specific\n")
    assert run(tmp_path, ["--mode", "forked"]) == 0


def test_forked_symlink_accepted(tmp_path):
    """A symlink (to anywhere) is always accepted in forked mode → exit 0."""
    scripts, contrib = make_tree(tmp_path)
    original = contrib / "shared.sh"
    original.write_text("#!/bin/sh\necho hi\n")
    os.symlink(original, scripts / "shared.sh")
    assert run(tmp_path, ["--mode", "forked"]) == 0


def test_forked_custom_allowlist_file_replaces_builtin(tmp_path):
    """--allowlist-file replaces the built-in set: only listed paths pass."""
    scripts, _ = make_tree(tmp_path)
    (scripts / "my-tool.py").write_text("print('local tool')\n")
    # A built-in default path NOT in our custom list must now fail.
    builtin = next(iter(scanner.DEFAULT_AGENT_SPECIFIC_SCRIPTS))
    builtin_fpath = tmp_path / builtin
    builtin_fpath.parent.mkdir(parents=True, exist_ok=True)
    builtin_fpath.write_text("#!/bin/sh\necho x\n")

    allow = tmp_path / "allow.txt"
    allow.write_text("# my agent's local scripts\nscripts/my-tool.py\n")
    # scripts/my-tool.py allowed, but the builtin path is no longer → exit 1.
    assert run(tmp_path, ["--mode", "forked", "--allowlist-file", str(allow)]) == 1


def test_forked_custom_allowlist_all_listed_passes(tmp_path):
    """When every local script is on the custom allowlist → exit 0."""
    scripts, _ = make_tree(tmp_path)
    (scripts / "my-tool.py").write_text("print('local tool')\n")
    allow = tmp_path / "allow.txt"
    allow.write_text("scripts/my-tool.py\n")
    assert run(tmp_path, ["--mode", "forked", "--allowlist-file", str(allow)]) == 0


def test_missing_allowlist_file_is_bad_args(tmp_path):
    """A nonexistent --allowlist-file is a usage error → exit 2."""
    make_tree(tmp_path)
    assert run(tmp_path, ["--mode", "forked", "--allowlist-file", "/no/such/file"]) == 2


# --- allowlist parsing ------------------------------------------------------


def test_load_allowlist_ignores_comments_and_blanks(tmp_path):
    f = tmp_path / "allow.txt"
    f.write_text(
        "\n"
        "# a comment\n"
        "scripts/keep.sh\n"
        "   \n"
        "scripts/also.py  # trailing comment\n"
    )
    assert scanner.load_allowlist(f) == {"scripts/keep.sh", "scripts/also.py"}


# --- arg handling -----------------------------------------------------------


def test_bad_agent_dir_is_exit_2(tmp_path):
    assert scanner.main([str(tmp_path / "does-not-exist"), "--mode", "default"]) == 2


def test_missing_contrib_dir_is_exit_1(tmp_path):
    """Default mode must fail closed (exit 1) when gptme-contrib is absent."""
    # No gptme-contrib submodule → scanner should not silently report clean.
    assert scanner.main([str(tmp_path), "--mode", "default"]) == 1


def test_escaping_check_dir_is_exit_2(tmp_path):
    """--check-dirs with a path that escapes the workspace must be rejected."""
    assert scanner.main([str(tmp_path), "--check-dirs", "../escape"]) == 2


def test_absolute_check_dir_is_exit_2(tmp_path):
    """--check-dirs with an absolute path must be rejected."""
    assert scanner.main([str(tmp_path), "--check-dirs", "/etc"]) == 2
