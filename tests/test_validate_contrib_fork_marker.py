"""Tests for the agent-neutral contrib fork-marker validator."""

import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "precommit" / "validators"))

import validate_contrib_fork_marker as validator  # noqa: E402


def make_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Create an agent-like tree: scripts/ + a contrib submodule with scripts/."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    contrib_scripts = tmp_path / "gptme-contrib" / "scripts"
    contrib_scripts.mkdir(parents=True)
    return scripts, contrib_scripts


def run(tmp_path: Path, argv: list[str]) -> int:
    with patch.object(validator, "get_repo_root", return_value=tmp_path):
        return validator.main(argv)


def test_no_contrib_counterpart_passes(tmp_path):
    """A scripts/ file with no same-path contrib original is not enforced."""
    scripts, _ = make_tree(tmp_path)
    f = scripts / "only-here.sh"
    f.write_text("#!/bin/sh\necho hi\n")
    assert run(tmp_path, [str(f)]) == 0


def test_counterpart_without_marker_fails(tmp_path, capsys):
    """A fork of a contrib script lacking any marker exits 1 and is named."""
    scripts, contrib_scripts = make_tree(tmp_path)
    (contrib_scripts / "shared.sh").write_text("#!/bin/sh\necho orig\n")
    fork = scripts / "shared.sh"
    fork.write_text("#!/bin/sh\necho local\n")
    assert run(tmp_path, [str(fork)]) == 1
    assert "shared.sh" in capsys.readouterr().out


def test_universal_marker_passes(tmp_path):
    """The universal AGENT-LOCAL token satisfies the check by default."""
    scripts, contrib_scripts = make_tree(tmp_path)
    (contrib_scripts / "shared.sh").write_text("#!/bin/sh\necho orig\n")
    fork = scripts / "shared.sh"
    fork.write_text("#!/bin/sh\n# AGENT-LOCAL FORK: adds foo\necho local\n")
    assert run(tmp_path, [str(fork)]) == 0


def test_per_agent_override_marker_passes(tmp_path, monkeypatch):
    """CONTRIB_FORK_MARKER accepts an agent-specific token like BOB-LOCAL."""
    monkeypatch.setenv("CONTRIB_FORK_MARKER", "BOB-LOCAL")
    scripts, contrib_scripts = make_tree(tmp_path)
    (contrib_scripts / "shared.sh").write_text("#!/bin/sh\necho orig\n")
    fork = scripts / "shared.sh"
    fork.write_text("#!/bin/sh\n# BOB-LOCAL FORK: bob extension\necho local\n")
    assert run(tmp_path, [str(fork)]) == 0


def test_universal_still_accepted_with_override(tmp_path, monkeypatch):
    """Setting an override does not disable the universal AGENT-LOCAL token."""
    monkeypatch.setenv("CONTRIB_FORK_MARKER", "BOB-LOCAL")
    scripts, contrib_scripts = make_tree(tmp_path)
    (contrib_scripts / "shared.sh").write_text("#!/bin/sh\necho orig\n")
    fork = scripts / "shared.sh"
    fork.write_text("#!/bin/sh\n# AGENT-LOCAL FORK: neutral marker\necho local\n")
    assert run(tmp_path, [str(fork)]) == 0


def test_marker_is_case_insensitive(tmp_path):
    """Lowercase 'agent-local' satisfies the check."""
    scripts, contrib_scripts = make_tree(tmp_path)
    (contrib_scripts / "shared.sh").write_text("#!/bin/sh\necho orig\n")
    fork = scripts / "shared.sh"
    fork.write_text("#!/bin/sh\n# agent-local fork\necho local\n")
    assert run(tmp_path, [str(fork)]) == 0


def test_marker_beyond_check_window_fails(tmp_path):
    """A marker past the first 30 lines does not count."""
    scripts, contrib_scripts = make_tree(tmp_path)
    (contrib_scripts / "shared.sh").write_text("#!/bin/sh\necho orig\n")
    fork = scripts / "shared.sh"
    body = "#!/bin/sh\n" + "\n".join(f"# line {i}" for i in range(40))
    body += "\n# AGENT-LOCAL FORK: too late\n"
    fork.write_text(body)
    assert run(tmp_path, [str(fork)]) == 1


def test_no_contrib_submodule_passes(tmp_path):
    """Without a gptme-contrib/scripts submodule, the check is a no-op."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    fork = scripts / "shared.sh"
    fork.write_text("#!/bin/sh\necho local\n")
    assert run(tmp_path, [str(fork)]) == 0


def test_no_args_passes(tmp_path):
    """No staged files → nothing to check."""
    make_tree(tmp_path)
    assert run(tmp_path, []) == 0
