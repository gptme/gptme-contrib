"""Real-Git regressions for #2012's post-divergent-squash materialization.

Publication and local preservation are separate assertions: a successful push
must not resurrect a session's deletion, overwrite a collision, or change its
index. No mocks, installed hooks, user config, or network remotes are involved.
"""

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

# Allows RED verification against `git show HEAD:...` without reverting a live
# implementation being repaired in parallel. Default always tests this checkout.
SAFE_PUSH = Path(
    os.environ.get(
        "TEST_SAFE_PUSH_SCRIPT",
        str(Path(__file__).resolve().parents[1] / "scripts/git/git-safe-push-master"),
    )
)


@dataclass
class Sandbox:
    root: Path
    remote: Path
    local: Path
    upstream: Path
    env: dict[str, str]

    def git(self, cwd: Path, *args: str) -> bytes:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self.env,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout

    def commit(self, cwd: Path, message: str) -> str:
        self.git(cwd, "add", "-A")
        self.git(cwd, "commit", "-m", message)
        return self.git(cwd, "rev-parse", "HEAD").decode().strip()

    def publish(self) -> None:
        """Prove squash publication independently, before examining local files."""
        local_before = self.git(self.local, "rev-parse", "HEAD").decode().strip()
        remote_before = self.git(self.remote, "rev-parse", "main").decode().strip()
        assert local_before != remote_before
        # Each side has one independent commit after their shared baseline.
        self.git(self.local, "fetch", "origin")
        assert self.git(
            self.local,
            "rev-list",
            "--left-right",
            "--count",
            f"{local_before}...{remote_before}",
        ).split() == [b"1", b"1"]
        expected_tree = (
            self.git(
                self.local, "merge-tree", "--write-tree", remote_before, local_before
            )
            .decode()
            .strip()
        )
        result = subprocess.run(
            [str(SAFE_PUSH)],
            cwd=self.local,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        diagnostic = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        assert result.returncode == 0, diagnostic
        published = self.git(self.remote, "rev-parse", "main").decode().strip()
        assert published not in (local_before, remote_before), diagnostic
        assert (
            self.git(self.remote, "show", "-s", "--format=%P", published)
            .decode()
            .strip()
            == remote_before
        ), diagnostic
        assert (
            self.git(self.remote, "rev-parse", f"{published}^{{tree}}").decode().strip()
            == expected_tree
        ), diagnostic
        assert (
            self.git(self.local, "rev-parse", "HEAD").decode().strip() == published
        ), diagnostic
        assert self.git(self.local, "symbolic-ref", "HEAD") == b"refs/heads/main\n"
        assert self.git(self.remote, "show", "main:local-only") == b"local committed\n"
        # This independent clean update must materialize even if the subject is dirty.
        assert (self.local / "clean-control").read_bytes().replace(
            b"\r\n", b"\n"
        ) == b"upstream control\n"
        assert self.git(self.local, "diff", "HEAD", "--", "clean-control") == b""


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    # Do not mutate os.environ: sibling tests and the parent agent share it.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    home = tmp_path / "home"
    home.mkdir()
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / "config"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_ALLOW_PROTOCOL="file",
        GIT_SAFE_PUSH_LOCK_TIMEOUT="5",
        GIT_SAFE_PUSH_RETRY_SLEEP="0",
    )
    config = {
        "user.name": "Materialization Test",
        "user.email": "test@example.invalid",
        "core.hooksPath": os.devnull,
        "commit.gpgsign": "false",
        "tag.gpgsign": "false",
        "core.autocrlf": "false",
        "core.filemode": "true",
        "core.symlinks": "true",
        "protocol.allow": "never",
        "protocol.file.allow": "always",
    }
    env["GIT_CONFIG_COUNT"] = str(len(config))
    for i, (key, value) in enumerate(config.items()):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    box = Sandbox(
        tmp_path,
        tmp_path / "remote.git",
        tmp_path / "local",
        tmp_path / "upstream",
        env,
    )
    box.git(tmp_path, "init", "--bare", "-b", "main", str(box.remote))
    box.git(tmp_path, "clone", str(box.remote), str(box.upstream))
    return box


def _write(root: Path, name: str, content: bytes = b"base\n") -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        for child in path.iterdir():
            _remove(child)
        path.rmdir()
    else:
        path.unlink()


def _install(root: Path, name: str, kind: str) -> None:
    path = root / name
    if path.exists() or path.is_symlink():
        _remove(path)
    if kind == "absent":
        return
    if kind == "directory":
        _write(root, name + "/child")
    elif kind in ("symlink", "dangling"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to("target" if kind == "symlink" else "missing-target")
    else:
        _write(root, name, b"upstream\n" if kind == "updated" else b"base\n")
        if kind == "executable":
            path.chmod(0o755)


def _snapshot(box: Sandbox, name: str) -> tuple[bytes, dict]:
    """Capture index entries and lstat disk state, including dangling links/dirs.

    Binary, NUL-delimited index output and Path names preserve spaces/newlines.
    Never hash through a symlink: its link text, not referent bytes, is local state.
    """
    entries = box.git(box.local, "ls-files", "--stage", "-z", "--", name)
    disk: dict[str, tuple[str, str] | tuple[str, int] | tuple[str, int, bytes]] = {}

    def visit(path: Path) -> None:
        mode = path.lstat().st_mode
        relative = str(path.relative_to(box.local))
        if stat.S_ISLNK(mode):
            disk[relative] = ("symlink", os.readlink(path))
        elif stat.S_ISDIR(mode):
            disk[relative] = ("directory", stat.S_IMODE(mode))
            for child in sorted(path.iterdir()):
                visit(child)
        else:
            disk[relative] = ("file", stat.S_IMODE(mode), path.read_bytes())

    path = box.local / name
    if path.exists() or path.is_symlink():
        visit(path)
    return entries, disk


def _prepare(box: Sandbox, name: str, baseline: str, upstream: str) -> None:
    _write(box.upstream, "target")
    _write(box.upstream, "other-target", b"other\n")
    _write(box.upstream, "clean-control", b"old control\n")
    # Makes ignored collision tests real ignored paths, not just untracked files.
    _write(box.upstream, ".gitignore", b"ignored-*\n")
    _install(box.upstream, name, baseline)
    if baseline != "absent":
        box.git(box.upstream, "add", "-f", "--", name)
    box.commit(box.upstream, "baseline")
    box.git(box.upstream, "push", "-u", "origin", "main")
    box.git(box.root, "clone", str(box.remote), str(box.local))
    _write(box.local, "local-only", b"local committed\n")
    box.commit(box.local, "local independent change")
    _install(box.upstream, name, upstream)
    if upstream != "absent":
        # Upstream may legitimately track a path ignored in the local checkout.
        box.git(box.upstream, "add", "-f", "--", name)
    _write(box.upstream, "clean-control", b"upstream control\n")
    box.commit(box.upstream, "upstream independent change")
    assert box.git(
        box.upstream, "diff", "HEAD^", "HEAD", "--", name
    ), "fixture must actually change the subject upstream (including ignored paths)"
    box.git(box.upstream, "push", "origin", "main")


def _dirty(box: Sandbox, name: str, change: str) -> None:
    path = box.local / name
    if change == "deletion":
        path.unlink()
    elif change in (
        "modified",
        "staged-modified",
        "staged-addition",
        "staged-and-unstaged",
    ):
        _write(box.local, name, b"session staged\n")
        if change != "modified":
            box.git(box.local, "add", "--", name)
        if change == "staged-and-unstaged":
            _write(box.local, name, b"session unstaged\n")
    elif change == "staged-deletion":
        box.git(box.local, "rm", "--", name)
    elif change in ("executable", "staged-executable"):
        path.chmod(0o755)
        if change == "staged-executable":
            box.git(box.local, "add", "--", name)
    elif change in ("untracked-file", "ignored-file"):
        _write(box.local, name, b"session collision\n")
    elif change == "staged-file-empty-directory":
        # The index has a file ancestor of an upstream-added descendant, while
        # disk superficially looks safe for writing that descendant. Restore
        # must not silently drop the ancestor's staged entry.
        _write(box.local, name, b"staged ancestor\n")
        box.git(box.local, "add", "--", name)
        path.unlink()
        path.mkdir()
    elif change in ("untracked-directory", "ignored-directory"):
        _install(box.local, name, "directory")
        _write(box.local, name + "/private child\n.txt", b"session private\n")
    elif change.startswith("link-"):
        path.unlink()
        # target contains the exact baseline file bytes: hashing through it is unsafe.
        path.symlink_to(
            {
                "link-same-content": "target",
                "link-different-content": "other-target",
                "link-dangling": "missing-target",
            }[change]
        )
    elif change in ("changed-link", "changed-dangling-link"):
        path.unlink()
        path.symlink_to(
            "other-target" if change == "changed-link" else "another-missing-target"
        )
    elif change.startswith("child-"):
        _dirty(box, name + "/child", change.removeprefix("child-"))
    elif change in ("private-descendant", "staged-descendant"):
        _write(box.local, name + "/private child\n.txt", b"private\n")
        if change == "staged-descendant":
            box.git(box.local, "add", "--", name + "/private child\n.txt")
    elif change == "ignored-descendant":
        _write(box.local, name + "/ignored-private", b"private ignored\n")
        assert box.git(box.local, "check-ignore", "--", name + "/ignored-private")
    elif change == "directory-replaces-file":
        _install(box.local, name, "directory")
        _write(box.local, name + "/child", b"session child\n")
    else:
        raise AssertionError(f"unknown dirty state: {change}")
    if change.startswith("ignored-") and change != "ignored-descendant":
        assert box.git(box.local, "check-ignore", "--", name)


# Each tuple is (initial committed type, upstream replacement, session change).
# A dirty structural boundary is kept intact: a replacement file cannot consume
# a directory containing session work, nor may a new directory unlink a dirty file.
DIRTY_CASES = [
    ("file", "updated", "deletion"),
    ("file", "absent", "modified"),
    ("file", "updated", "modified"),
    ("file", "updated", "staged-modified"),
    ("file", "updated", "staged-and-unstaged"),
    ("file", "updated", "staged-deletion"),
    ("absent", "updated", "staged-addition"),
    ("absent", "updated", "untracked-file"),
    ("absent", "updated", "ignored-file"),
    ("absent", "updated", "untracked-directory"),
    ("absent", "updated", "ignored-directory"),
    ("absent", "directory", "staged-file-empty-directory"),
    ("file", "updated", "executable"),
    ("file", "updated", "staged-executable"),
    ("file", "absent", "executable"),
    ("file", "updated", "link-same-content"),
    ("file", "updated", "link-different-content"),
    ("file", "updated", "link-dangling"),
    ("file", "absent", "link-dangling"),
    ("symlink", "updated", "changed-link"),
    ("dangling", "updated", "changed-dangling-link"),
    ("directory", "updated", "child-modified"),
    ("directory", "updated", "child-staged-modified"),
    ("directory", "updated", "child-deletion"),
    ("directory", "updated", "child-staged-deletion"),
    ("directory", "updated", "staged-descendant"),
    ("directory", "updated", "private-descendant"),
    ("directory", "updated", "ignored-descendant"),
    ("file", "directory", "modified"),
    ("file", "directory", "staged-modified"),
    ("file", "directory", "deletion"),
    ("file", "directory", "directory-replaces-file"),
    ("file", "updated", "directory-replaces-file"),
]


@pytest.mark.parametrize(
    "baseline,upstream,change",
    DIRTY_CASES,
    ids=[f"{a}-to-{b}-{c}" for a, b, c in DIRTY_CASES],
)
def test_squash_preserves_dirty_state(sandbox, baseline, upstream, change):
    name = "ignored-collision" if change.startswith("ignored-") else "subject"
    _prepare(sandbox, name, baseline, upstream)
    _dirty(sandbox, name, change)
    before_index, before_disk = _snapshot(sandbox, name)
    sandbox.publish()
    after_index, after_disk = _snapshot(sandbox, name)
    # Disk first: an index-only assertion must not mask actual lost bytes/type/mode.
    assert after_disk == before_disk, "successful squash clobbered session disk state"
    assert (
        after_index == before_index
    ), "successful squash rewrote session index entries"


CLEAN_CASES = [
    ("absent", "updated"),
    ("file", "updated"),
    ("file", "absent"),
    ("file", "executable"),
    ("executable", "file"),
    ("file", "symlink"),
    ("symlink", "updated"),
    ("symlink", "absent"),
    ("dangling", "updated"),
    ("dangling", "absent"),
    ("file", "directory"),
    ("directory", "updated"),
]


@pytest.mark.parametrize(
    "baseline,upstream", CLEAN_CASES, ids=[f"{a}-to-{b}" for a, b in CLEAN_CASES]
)
def test_squash_materializes_clean_upstream_state(sandbox, baseline, upstream):
    _prepare(sandbox, "subject", baseline, upstream)
    sandbox.publish()
    # Compare actual type/link text/content/mode against the upstream checkout.
    expected = Sandbox(
        sandbox.root, sandbox.remote, sandbox.upstream, sandbox.upstream, sandbox.env
    )
    assert _snapshot(sandbox, "subject") == _snapshot(expected, "subject")
    assert sandbox.git(sandbox.local, "status", "--porcelain=v1", "-z") == b""


@pytest.mark.parametrize(
    "name", ["subject with spaces.txt", "subject\nwith newline.txt"]
)
@pytest.mark.parametrize("change", ["clean", "deletion", "untracked-file"])
def test_squash_materialization_handles_literal_filenames(sandbox, name, change):
    baseline = "absent" if change == "untracked-file" else "file"
    _prepare(sandbox, name, baseline, "updated")
    if change != "clean":
        _dirty(sandbox, name, change)
    before = _snapshot(sandbox, name)
    sandbox.publish()
    if change == "clean":
        assert (sandbox.local / name).read_bytes() == b"upstream\n"
        assert sandbox.git(sandbox.local, "status", "--porcelain=v1", "-z") == b""
    else:
        assert _snapshot(sandbox, name) == before


@pytest.mark.parametrize("flag", ["--skip-worktree", "--assume-unchanged"])
def test_squash_preserves_flagged_index_entries(sandbox, flag):
    _prepare(sandbox, "subject", "file", "updated")
    sandbox.git(sandbox.local, "update-index", flag, "--", "subject")
    before = _snapshot(sandbox, "subject")
    flags = sandbox.git(sandbox.local, "ls-files", "-v", "-z", "--", "subject")
    sandbox.publish()
    assert _snapshot(sandbox, "subject") == before
    assert sandbox.git(sandbox.local, "ls-files", "-v", "-z", "--", "subject") == flags


def test_squash_preserves_normalized_but_byte_different_file(sandbox):
    _prepare(sandbox, "subject", "file", "updated")
    # Git-clean equivalence must not hide a user's distinct disk bytes.
    sandbox.git(sandbox.local, "config", "core.autocrlf", "true")
    # Override the fixture's process-level false setting for this test only.
    for i in range(int(sandbox.env["GIT_CONFIG_COUNT"])):
        if sandbox.env[f"GIT_CONFIG_KEY_{i}"] == "core.autocrlf":
            sandbox.env[f"GIT_CONFIG_VALUE_{i}"] = "true"
    (sandbox.local / "subject").write_bytes(b"base\r\n")
    assert sandbox.git(sandbox.local, "diff", "--", "subject") == b""
    before = _snapshot(sandbox, "subject")
    sandbox.publish()
    assert _snapshot(sandbox, "subject") == before


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling"])
def test_squash_preserves_obstructing_disk_ancestor(sandbox, kind):
    _prepare(sandbox, "subject/child", "absent", "updated")
    _install(sandbox.local, "subject", kind)
    before = _snapshot(sandbox, "subject")
    sandbox.publish()
    assert _snapshot(sandbox, "subject") == before


@pytest.mark.parametrize(
    "name", [":(glob)*", "subject[ab].txt", "--subject", "tab\tname.txt"]
)
def test_squash_uses_literal_restore_pathspec(sandbox, name):
    _prepare(sandbox, name, "file", "updated")
    sandbox.publish()
    assert (sandbox.local / name).read_bytes() == b"upstream\n"
    assert sandbox.git(sandbox.local, "status", "--porcelain=v1", "-z") == b""
