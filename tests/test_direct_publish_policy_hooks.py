"""Path-scoped direct-publish exception for the global pre-commit/pre-push hooks.

The hooks block direct master/main commits and pushes in every repo that is not
allow-listed. Some repos (a personal site, a profile README) are published by
their owner straight to the default branch, but only for *prose*: never for
code, workflows or anything runnable. ``direct-publish.json`` (a trusted config
next to the hooks) names exact repositories and path globs; the trusted checker
``scripts/git/direct-publish-policy.py`` approves a change only when every path
that was ever touched is in scope.

These tests drive the real hook executables from an install-root-shaped tree
(the layout ``scripts/git/install-global-hooks.sh`` produces), offline.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_SRC = ROOT / "dotfiles" / ".config" / "git" / "hooks"
POLICY_SRC = ROOT / "scripts" / "git" / "direct-publish-policy.py"
GUARD_SRC = ROOT / "scripts" / "git" / "guard-mass-delete.sh"

ZERO = "0" * 40
SITE = "git@github.com:TimeToBuildBob/TimeToBuildBob.github.io.git"
PROFILE = "git@github.com:TimeToBuildBob/TimeToBuildBob.git"
CONFIG = {
    "repositories": [
        {
            "repo": "github.com/timetobuildbob/timetobuildbob.github.io",
            "branch": "master",
            "allow": ["_posts/*.md", "_data/*.yml", "assets/images/og/*.png"],
        },
        {
            "repo": "github.com/timetobuildbob/timetobuildbob",
            "branch": "master",
            "allow": ["README.md"],
        },
    ]
}


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
        # The force-reset guard fetches origin; keep every network protocol dead.
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="protocol.allow",
        GIT_CONFIG_VALUE_0="never",
        GIT_TERMINAL_PROMPT="0",
    )
    return env


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_env(),
    ).stdout.strip()


@pytest.fixture()
def install(tmp_path: Path) -> Path:
    """An install-root-shaped tree: <root>/dotfiles/.config/git/{hooks,*.conf}."""
    root = tmp_path / "install"
    conf = root / "dotfiles" / ".config" / "git"
    shutil.copytree(HOOKS_SRC, conf / "hooks")
    (conf / "allowed-repos.conf").write_text("ALLOWED_PATTERNS=()\n")
    (conf / "allowed-identities.conf").write_text(
        'IDENTITY_ALLOWLIST=("author@example.com")\n'
    )
    (conf / "direct-publish.json").write_text(json.dumps(CONFIG))
    scripts = root / "scripts" / "git"
    scripts.mkdir(parents=True)
    shutil.copy2(GUARD_SRC, scripts / "guard-mass-delete.sh")
    if POLICY_SRC.exists():
        shutil.copy2(POLICY_SRC, scripts / "direct-publish-policy.py")
    return root


def _hook(install: Path, name: str) -> Path:
    return install / "dotfiles" / ".config" / "git" / "hooks" / name


def _repo(tmp_path: Path, origin: str = SITE) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "master", str(repo)], check=True, env=_env()
    )
    _git(repo, "config", "user.email", "author@example.com")
    _git(repo, "config", "user.name", "Bob")
    _git(repo, "remote", "add", "origin", origin)
    return repo


def _write(repo: Path, rel: str, spec: object) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        path.unlink()
    if spec is None:
        return
    if isinstance(spec, tuple) and spec[0] == "link":
        os.symlink(spec[1], path)
    elif isinstance(spec, tuple) and spec[0] == "exec":
        path.write_text(spec[1])
        path.chmod(0o755)
    else:
        path.write_text(str(spec))


def _commit(repo: Path, files: dict[str, object], msg: str = "c") -> str:
    for rel, spec in files.items():
        _write(repo, rel, spec)
    _git(repo, "add", "-A")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _stage(repo: Path, files: dict[str, object]) -> None:
    for rel, spec in files.items():
        _write(repo, rel, spec)
    _git(repo, "add", "-A")


def _run_commit_hook(install: Path, repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_hook(install, "pre-commit"))],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_env(),
    )


def _run_push_hook(
    install: Path,
    repo: Path,
    tuples: list[str],
    dest: str | None = None,
) -> subprocess.CompletedProcess[str]:
    url = dest if dest is not None else _git(repo, "remote", "get-url", "origin")
    return subprocess.run(
        [str(_hook(install, "pre-push")), "origin", url],
        cwd=repo,
        input="\n".join(tuples) + "\n",
        capture_output=True,
        text=True,
        env=_env(),
    )


def _tuple(local_sha: str, remote_sha: str, ref: str = "refs/heads/master") -> str:
    return f"refs/heads/work {local_sha} {ref} {remote_sha}"


def _blocked(proc: subprocess.CompletedProcess[str]) -> bool:
    return proc.returncode != 0


# --------------------------------------------------------------- pre-commit


def test_commit_baseline_prose_still_blocked_without_policy_config(
    install: Path, tmp_path: Path
) -> None:
    (install / "dotfiles/.config/git/direct-publish.json").unlink()
    repo = _repo(tmp_path)
    _commit(repo, {"_posts/seed.md": "seed\n"})
    _stage(repo, {"_posts/a.md": "prose\n"})
    proc = _run_commit_hook(install, repo)
    assert proc.returncode == 1
    assert "Cannot commit directly to master" in proc.stdout


def test_commit_site_prose_allowed(install: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, {"README.md": "seed\n"})
    _stage(repo, {"_posts/a.md": "prose\n", "_data/timeline.yml": "- a\n"})
    proc = _run_commit_hook(install, repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("mode", ["commit", "push"])
def test_symlink_install_uses_physical_checker_and_installed_config(
    install: Path, tmp_path: Path, mode: str
) -> None:
    conf = tmp_path / "home/.config/git"
    conf.mkdir(parents=True)
    conf.joinpath("hooks").symlink_to(_hook(install, "pre-commit").parent)
    # Only the installed config grants publication; the checkout copy denies it.
    for name in (
        "allowed-repos.conf",
        "allowed-identities.conf",
        "direct-publish.json",
    ):
        shutil.copy2(install / "dotfiles/.config/git" / name, conf / name)
    (install / "dotfiles/.config/git/direct-publish.json").write_text(
        '{"repositories": []}'
    )
    repo = _repo(tmp_path)
    base = _commit(repo, {"README.md": "seed\n"})
    if mode == "commit":
        _stage(repo, {"_posts/a.md": "prose\n"})
        args = [str(conf / "hooks/pre-commit")]
        stdin = None
    else:
        new = _commit(repo, {"_posts/a.md": "prose\n"})
        args = [str(conf / "hooks/pre-push"), "origin", SITE]
        stdin = _tuple(new, base) + "\n"
    proc = subprocess.run(
        args, cwd=repo, input=stdin, capture_output=True, text=True, env=_env()
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_commit_cannot_hide_staged_submodule_update(
    install: Path, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    first = _commit(repo, {"README.md": "seed\n"})
    second = _commit(repo, {"README.md": "next\n"})
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{first},vendor")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "gitlink")
    _write(repo, "_posts/a.md", "prose\n")
    _git(repo, "add", "_posts/a.md")
    _git(repo, "update-index", "--cacheinfo", f"160000,{second},vendor")
    _git(repo, "config", "diff.ignoreSubmodules", "all")
    assert _git(repo, "diff", "--cached", "--name-only") == "_posts/a.md"
    proc = _run_commit_hook(install, repo)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "symlink/submodule/executable" in proc.stderr


@pytest.mark.parametrize(
    "files",
    [
        {"_layouts/default.html": "<html>"},
        {".github/workflows/ci.yml": "on: push\n"},
        {"_posts/a.md": "p\n", "scripts/run.py": "print(1)\n"},
        {"_posts/../scripts/x.md": "p\n"},
        {"assets/images/og/x.svg": "<svg onload=1>"},
        {"assets/images/og/sub/x.png": "x"},
        {"_posts/a.md.js": "x"},
    ],
)
def test_commit_code_or_mixed_denied(
    install: Path, tmp_path: Path, files: dict[str, object]
) -> None:
    repo = _repo(tmp_path)
    _commit(repo, {"README.md": "seed\n"})
    _stage(repo, files)
    assert _blocked(_run_commit_hook(install, repo))


def test_commit_profile_scope_is_readme_only(install: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path, PROFILE)
    _commit(repo, {"README.md": "seed\n"})
    _stage(repo, {"README.md": "edited\n"})
    assert _run_commit_hook(install, repo).returncode == 0
    _git(repo, "add", "-A")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "x")
    _stage(repo, {"_posts/a.md": "prose\n"})
    assert _blocked(_run_commit_hook(install, repo))


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example/github.com/TimeToBuildBob/TimeToBuildBob.github.io",
        "https://github.com@evil.example/TimeToBuildBob/TimeToBuildBob.github.io.git",
        "git@github.com.evil.example:TimeToBuildBob/TimeToBuildBob.github.io.git",
        "git@github.com:TimeToBuildBob/TimeToBuildBob.github.io.evil",
        "git@github.com:evil/TimeToBuildBob.github.io.git",
        "git@evil.example:TimeToBuildBob/TimeToBuildBob.github.io.git",
        "https://github.com:8443/TimeToBuildBob/TimeToBuildBob.github.io",
        "https://github.com/TimeToBuildBob/TimeToBuildBob.github.io/extra",
    ],
)
def test_commit_hostile_origin_denied(
    install: Path, tmp_path: Path, origin: str
) -> None:
    repo = _repo(tmp_path, origin)
    _commit(repo, {"README.md": "seed\n"})
    _stage(repo, {"_posts/a.md": "prose\n"})
    assert _blocked(_run_commit_hook(install, repo))


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/TimeToBuildBob/TimeToBuildBob.github.io.git",
        "https://github.com/timetobuildbob/timetobuildbob.github.io",
        "ssh://git@github.com/TimeToBuildBob/TimeToBuildBob.github.io.git",
        "git@github.com:TimeToBuildBob/TimeToBuildBob.github.io",
    ],
)
def test_commit_equivalent_origin_spellings_allowed(
    install: Path, tmp_path: Path, origin: str
) -> None:
    repo = _repo(tmp_path, origin)
    _commit(repo, {"README.md": "seed\n"})
    _stage(repo, {"_posts/a.md": "prose\n"})
    assert _run_commit_hook(install, repo).returncode == 0


def test_commit_special_entries_denied(install: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, {"README.md": "seed\n"})
    special: tuple[dict[str, object], ...] = (
        {"_posts/link.md": ("link", "../scripts/run.py")},
        {"_posts/run.md": ("exec", "#!/bin/sh\n")},
        {"_posts/a\nb.md": "newline name\n"},
    )
    for files in special:
        _stage(repo, files)
        assert _blocked(_run_commit_hook(install, repo)), files
        _git(repo, "reset", "-q", "--", ".")
        _git(repo, "clean", "-qfd")


def test_commit_rename_into_code_denied_and_delete_allowed(
    install: Path, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    _commit(repo, {"_posts/a.md": "prose\n", "scripts/x.py": "print(1)\n"})
    _git(repo, "mv", "_posts/a.md", "scripts/a.md")
    assert _blocked(_run_commit_hook(install, repo))
    _git(repo, "reset", "-q", "--hard")
    _git(repo, "rm", "-q", "_posts/a.md")
    assert _run_commit_hook(install, repo).returncode == 0


def test_commit_wrong_branch_denied(install: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, {"README.md": "seed\n"})
    _git(repo, "branch", "-m", "master", "main")
    _stage(repo, {"_posts/a.md": "prose\n"})
    assert _blocked(_run_commit_hook(install, repo))


@pytest.mark.parametrize("malformed_first", [True, False])
def test_config_invalid_entry_denied_regardless_of_order(
    install: Path, tmp_path: Path, malformed_first: bool
) -> None:
    entries = (
        [None, *CONFIG["repositories"]]
        if malformed_first
        else [*CONFIG["repositories"], None]
    )
    (install / "dotfiles/.config/git/direct-publish.json").write_text(
        json.dumps({"repositories": entries})
    )
    repo = _repo(tmp_path)
    _commit(repo, {"README.md": "seed\n"})
    _stage(repo, {"_posts/a.md": "prose\n"})
    proc = _run_commit_hook(install, repo)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "malformed repository entry" in proc.stderr


@pytest.mark.parametrize("breakage", ["no-script", "bad-exit", "bad-config"])
def test_commit_broken_policy_machinery_fails_closed(
    install: Path, tmp_path: Path, breakage: str
) -> None:
    script = install / "scripts/git/direct-publish-policy.py"
    config = install / "dotfiles/.config/git/direct-publish.json"
    if breakage == "no-script":
        script.unlink()
    elif breakage == "bad-exit":
        script.write_text("import sys\nsys.exit(3)\n")
    else:
        config.write_text("{not json")
    repo = _repo(tmp_path)
    _commit(repo, {"README.md": "seed\n"})
    _stage(repo, {"_posts/a.md": "prose\n"})
    proc = _run_commit_hook(install, repo)
    assert proc.returncode == 1
    assert "Cannot commit directly to master" in proc.stdout


def test_commit_downstream_guards_still_run_for_approved_diff(
    install: Path, tmp_path: Path
) -> None:
    precommit = install / "scripts" / "precommit"
    precommit.mkdir(parents=True)
    (precommit / "validate_submodule_commits.py").write_text(
        "import sys\nsys.exit(1)\n"
    )
    repo = _repo(tmp_path)
    _commit(repo, {".gitmodules": "", "README.md": "seed\n"})
    _stage(repo, {"_posts/a.md": "prose\n"})
    proc = _run_commit_hook(install, repo)
    assert proc.returncode == 1
    assert "Submodule validation failed" in proc.stdout


@pytest.mark.parametrize("mode", ["commit", "push"])
@pytest.mark.parametrize(
    "layout", [".git/modules/sub/hooks", ".git/modules/dotfiles/.config/git/hooks"]
)
def test_copied_submodule_hook_never_executes_superproject_checker(
    install: Path, tmp_path: Path, mode: str, layout: str
) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"README.md": "seed\n"})
    marker = tmp_path / "untrusted-checker-ran"
    hooks = repo / layout
    hooks.mkdir(parents=True)
    # Place the payload exactly where the old blind four-level walk lands,
    # including the suffix-shaped gitdir case whose candidate is inside .git.
    scripts = hooks.parents[3] / "scripts/git"
    scripts.mkdir(parents=True)
    (scripts / "direct-publish-policy.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    shutil.copy2(_hook(install, f"pre-{mode}"), hooks / f"pre-{mode}")
    for name in (
        "direct-publish.json",
        "allowed-repos.conf",
        "allowed-identities.conf",
    ):
        shutil.copy2(install / "dotfiles/.config/git" / name, hooks.parent / name)
    if mode == "commit":
        _stage(repo, {"_posts/a.md": "prose\n"})
        args = [str(hooks / "pre-commit")]
        stdin = None
    else:
        new = _commit(repo, {"_posts/a.md": "prose\n"})
        args = [str(hooks / "pre-push"), "origin", SITE]
        stdin = _tuple(new, base) + "\n"
    proc = subprocess.run(
        args, cwd=repo, input=stdin, capture_output=True, text=True, env=_env()
    )
    assert not marker.exists(), proc.stdout + proc.stderr
    assert proc.returncode == 1, proc.stdout + proc.stderr


@pytest.mark.parametrize(
    "runner_action", ["auto-stage-mode", "stage-code", "safe-format"]
)
def test_commit_rechecks_final_index_after_runner(
    install: Path, tmp_path: Path, runner_action: str
) -> None:
    repo = _repo(tmp_path, PROFILE)
    _commit(repo, {"README.md": "seed\n", ".pre-commit-config.yaml": "repos: []\n"})
    _stage(repo, {"README.md": "edited\n"})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    runner = bin_dir / "prek"
    action = {
        "auto-stage-mode": "chmod +x README.md\nexit 1\n",
        "stage-code": "mkdir -p scripts\nprintf code > scripts/run.py\ngit add scripts/run.py\n",
        "safe-format": "printf formatted >> README.md\nexit 1\n",
    }[runner_action]
    runner.write_text(
        "#!/bin/bash\nif [ ! -f .runner-ran ]; then\n"
        "touch .runner-ran\n" + action + "fi\nexit 0\n"
    )
    runner.chmod(0o755)
    env = _env()
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    proc = subprocess.run(
        [str(_hook(install, "pre-commit"))],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    assert (repo / ".runner-ran").exists()
    if runner_action == "safe-format":
        assert proc.returncode == 0, proc.stdout + proc.stderr
    else:
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "direct-publish-policy" in proc.stderr


# ----------------------------------------------------------------- pre-push


def test_push_prose_range_allowed_and_downstream_guard_runs(
    install: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "tracking-ran"
    stub = _hook(install, "validate-worktree-tracking.sh")
    stub.write_text(f"#!/bin/bash\ncat >/dev/null\ntouch {marker}\n")
    stub.chmod(0o755)
    repo = _repo(tmp_path)
    base = _commit(repo, {"README.md": "seed\n"})
    new = _commit(repo, {"_posts/a.md": "prose\n"})
    proc = _run_push_hook(install, repo, [_tuple(new, base)])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert marker.exists()


def test_push_baseline_blocked_without_policy_config(
    install: Path, tmp_path: Path
) -> None:
    (install / "dotfiles/.config/git/direct-publish.json").unlink()
    repo = _repo(tmp_path)
    base = _commit(repo, {"README.md": "seed\n"})
    new = _commit(repo, {"_posts/a.md": "prose\n"})
    proc = _run_push_hook(install, repo, [_tuple(new, base)])
    assert proc.returncode == 1
    assert "Direct push to refs/heads/master blocked" in proc.stdout


def test_push_code_commit_in_range_denied(install: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"README.md": "seed\n"})
    _commit(repo, {"_posts/a.md": "prose\n"})
    new = _commit(repo, {"scripts/run.py": "print(1)\n"})
    assert _blocked(_run_push_hook(install, repo, [_tuple(new, base)]))


def test_push_cannot_hide_committed_submodule_update(
    install: Path, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    first = _commit(repo, {"README.md": "seed\n"})
    second = _commit(repo, {"README.md": "next\n"})
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{first},vendor")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "gitlink")
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "_posts/a.md", "prose\n")
    _git(repo, "add", "_posts/a.md")
    _git(repo, "update-index", "--cacheinfo", f"160000,{second},vendor")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "update")
    new = _git(repo, "rev-parse", "HEAD")
    _git(repo, "config", "diff.ignoreSubmodules", "all")
    assert _git(repo, "diff", "--name-only", base, new) == "_posts/a.md"
    proc = _run_push_hook(install, repo, [_tuple(new, base)])
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "symlink/submodule/executable" in proc.stderr


def test_push_graft_cannot_hide_code_add_and_revert(
    install: Path, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"README.md": "seed\n"})
    _commit(repo, {"scripts/run.py": "print(1)\n"})
    _commit(repo, {"scripts/run.py": None})
    new = _commit(repo, {"_posts/a.md": "prose\n"})
    (repo / ".git/info/grafts").write_text(f"{new} {base}\n")
    assert _git(repo, "rev-list", f"{base}..{new}") == new
    proc = _run_push_hook(install, repo, [_tuple(new, base)])
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "outside the direct-publish scope" in proc.stderr


def test_push_replacement_cannot_disguise_code_commit(
    install: Path, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"README.md": "seed\n"})
    code = _commit(repo, {"scripts/run.py": "print(1)\n"})
    _git(repo, "checkout", "-q", "-b", "prose", base)
    prose = _commit(repo, {"_posts/a.md": "prose\n"})
    _git(repo, "replace", code, prose)
    _git(repo, "checkout", "-q", "master")
    assert _git(repo, "diff", "--name-only", base, code) == "_posts/a.md"
    assert _git(repo, "--no-replace-objects", "diff", "--name-only", base, code) == (
        "scripts/run.py"
    )
    proc = _run_push_hook(install, repo, [_tuple(code, base)])
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "outside the direct-publish scope" in proc.stderr


def test_push_historical_code_add_and_revert_denied(
    install: Path, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"README.md": "seed\n"})
    _commit(repo, {"scripts/run.py": "print(1)\n"})
    new = _commit(repo, {"scripts/run.py": None, "_posts/a.md": "prose\n"})
    # Net diff is prose-only; the range still carried code.
    assert _git(repo, "diff", "--name-only", base, new) == "_posts/a.md"
    assert _blocked(_run_push_hook(install, repo, [_tuple(new, base)]))


def test_push_uses_tuple_range_not_head(install: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"README.md": "seed\n"})
    prose = _commit(repo, {"_posts/a.md": "prose\n"})
    code = _commit(repo, {"scripts/run.py": "print(1)\n"})
    # HEAD is code-bearing, but the pushed range ends at the prose commit.
    assert _git(repo, "rev-parse", "HEAD") == code
    assert _run_push_hook(install, repo, [_tuple(prose, base)]).returncode == 0
    # HEAD at prose, pushed range reaches the code commit.
    _git(repo, "reset", "-q", "--soft", prose)
    assert _blocked(_run_push_hook(install, repo, [_tuple(code, base)]))


def test_push_unknown_or_unsafe_ranges_denied(install: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"README.md": "seed\n"})
    new = _commit(repo, {"_posts/a.md": "prose\n"})
    _git(repo, "checkout", "-q", "-b", "side", base)
    side = _commit(repo, {"_posts/b.md": "other\n"})
    bogus = "1" * 40
    cases = {
        "new remote branch": [_tuple(new, ZERO)],
        "deletion": [_tuple(ZERO, base)],
        "missing old object": [_tuple(new, bogus)],
        "non fast-forward": [_tuple(side, new)],
        "malformed": ["only three fields here"],
        "empty range": [_tuple(base, base)],
        "target is main": [_tuple(new, base, "refs/heads/main")],
    }
    for label, tuples in cases.items():
        assert _blocked(_run_push_hook(install, repo, tuples)), label


def test_push_merge_commit_and_special_entries_denied(
    install: Path, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"README.md": "seed\n"})
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, {"_posts/b.md": "other\n"})
    _git(repo, "checkout", "-q", "master")
    _commit(repo, {"_posts/a.md": "prose\n"})
    _git(
        repo,
        "-c",
        "core.hooksPath=/dev/null",
        "merge",
        "-q",
        "--no-ff",
        "side",
        "-m",
        "m",
    )
    merged = _git(repo, "rev-parse", "HEAD")
    assert _blocked(_run_push_hook(install, repo, [_tuple(merged, base)]))

    special: tuple[dict[str, object], ...] = (
        {"_posts/l.md": ("link", "../scripts/run.py")},
        {"_posts/x.md": ("exec", "#!/bin/sh\n")},
        {"_posts/a\nb.md": "x\n"},
    )
    for files in special:
        repo2 = _repo(tmp_path / f"r{abs(hash(str(files)))}")
        base2 = _commit(repo2, {"README.md": "seed\n"})
        new2 = _commit(repo2, files)
        assert _blocked(_run_push_hook(install, repo2, [_tuple(new2, base2)])), files


def test_push_multi_ref_only_governs_default_branch_tuples(
    install: Path, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"README.md": "seed\n"})
    prose = _commit(repo, {"_posts/a.md": "prose\n"})
    code = _commit(repo, {"scripts/run.py": "print(1)\n"})
    feature = _tuple(code, ZERO, "refs/heads/feature")
    assert _run_push_hook(install, repo, [_tuple(prose, base), feature]).returncode == 0
    # A code tuple to a second protected ref cannot ride on the prose one.
    bad = _tuple(code, base, "refs/heads/main")
    assert _blocked(_run_push_hook(install, repo, [_tuple(prose, base), bad]))


@pytest.mark.parametrize(
    "dest",
    [
        "git@github.com:evil/TimeToBuildBob.github.io.git",
        "https://evil.example/github.com/TimeToBuildBob/TimeToBuildBob.github.io",
        "",
    ],
)
def test_push_destination_must_be_the_exact_repo(
    install: Path, tmp_path: Path, dest: str
) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"README.md": "seed\n"})
    new = _commit(repo, {"_posts/a.md": "prose\n"})
    assert _blocked(_run_push_hook(install, repo, [_tuple(new, base)], dest=dest))


def test_push_profile_scope_is_readme_only(install: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path, PROFILE)
    base = _commit(repo, {"README.md": "seed\n"})
    ok = _commit(repo, {"README.md": "edited\n"})
    assert _run_push_hook(install, repo, [_tuple(ok, base)]).returncode == 0
    bad = _commit(repo, {"_posts/a.md": "prose\n"})
    assert _blocked(_run_push_hook(install, repo, [_tuple(bad, base)]))


@pytest.mark.parametrize("breakage", ["no-script", "bad-exit", "bad-config"])
def test_push_broken_policy_machinery_fails_closed(
    install: Path, tmp_path: Path, breakage: str
) -> None:
    script = install / "scripts/git/direct-publish-policy.py"
    config = install / "dotfiles/.config/git/direct-publish.json"
    if breakage == "no-script":
        script.unlink()
    elif breakage == "bad-exit":
        script.write_text("import sys\nsys.exit(3)\n")
    else:
        config.write_text("{not json")
    repo = _repo(tmp_path)
    base = _commit(repo, {"README.md": "seed\n"})
    new = _commit(repo, {"_posts/a.md": "prose\n"})
    proc = _run_push_hook(install, repo, [_tuple(new, base)])
    assert proc.returncode == 1
    assert "Direct push to refs/heads/master blocked" in proc.stdout


def test_push_repo_supplied_policy_script_is_never_executed(
    install: Path, tmp_path: Path
) -> None:
    """The checker comes from the hook's own install root, never the pushed repo."""
    (install / "scripts/git/direct-publish-policy.py").unlink()
    repo = _repo(tmp_path)
    base = _commit(
        repo,
        {
            "README.md": "seed\n",
            "scripts/git/direct-publish-policy.py": "import sys\nsys.exit(0)\n",
        },
    )
    new = _commit(repo, {"scripts/run.py": "print(1)\n"})
    assert _blocked(_run_push_hook(install, repo, [_tuple(new, base)]))


def test_push_to_other_branches_never_consults_the_checker(
    install: Path, tmp_path: Path
) -> None:
    """Ordinary feature-branch pushes must stay silent and unaffected."""
    repo = _repo(tmp_path, "git@github.com:someone/else.git")
    base = _commit(repo, {"README.md": "seed\n"})
    new = _commit(repo, {"scripts/run.py": "print(1)\n"})
    proc = _run_push_hook(
        install, repo, [_tuple(new, ZERO, "refs/heads/feature")], dest=""
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "direct-publish-policy" not in proc.stderr
    assert base != new
