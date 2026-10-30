# Shared-worktree Git helpers

`git-safe-commit`, `git-safe-pull` and `git-safe-push-master` share a commit
flock for cooperating operations. Ordinary editors and other filesystem writers
do not acquire it. Never remove the lock pathname to resolve contention.

## Safe-push publication and local materialization

`git-safe-push-master` derives the remote/trunk, fetches, and publishes either a
linear fast-forward or an off-tree squash. A squash preserves the resulting
committed tree but collapses the local commit window. Local trunk moves only
after successful publication, using a compare-and-swap ref update.

After a squash, the helper separately attempts to materialize upstream changes
into the index and working tree. Publication success does **not** mean every
local path now matches the published tree. The summary reports updated,
skipped and failed groups, including an all-skipped run.

A changed group is admitted only when its entire index matches the old local
commit (mode, object and stage) and disk matches its tracked file types, raw
bytes, symlink targets and executable modes. Missing tracked paths, untracked
or ignored collisions, staged changes, flagged index entries, special files
and submodules are retained rather than overwritten. Proper ancestors must be
real directories with no index leaf. File/directory transitions are checked as
a unit, including unexpected or dirty descendants; ordinary sibling paths can
still update independently. Names are NUL-delimited and pathspecs literal.

Checks intentionally use raw bytes without clean filters. A file that Git
considers clean only after normalization/filtering may therefore be skipped.
This is conservative preservation, not a promise to materialize every Git-clean
path. Git's restore may still apply configured checkout transformations to
admitted paths.

**Concurrency boundary:** quiesce uncoordinated filesystem writers during
materialization. Checking a path followed by restoring it is not atomic against
an editor changing it between those steps. The flock does not fix that race.
A failed restore can be partially applied; the helper stops further restores,
warns, and retains publication success. It does not attempt destructive rollback.

If a path is skipped or a local ref update fails, inspect disk, index and refs
separately. Preserve local work and coordinate path-scoped reconciliation with
its owner. Never recover a shared checkout with `git reset --hard`, stash,
force-push, or a blanket checkout. Re-running an already-in-sync push does not
replay a prior materialization step.

## Preview

`--help` performs no Git operation or lock acquisition. Unknown arguments fail
before Git. `--dry-run` takes the lock and fetches, then previews without
publication, local-trunk update, index changes or filesystem materialization.
Fetch updates tracking metadata and merge-tree computation may write Git objects.
A successful preview is not a publication receipt.

## Regression coverage

`tests/test_git_safe_push_trunk.py` covers CLI/trunk/publication behavior.
`tests/test_git_safe_push_materialization.py` uses isolated local/bare/peer
repositories and asserts remote publication independently of local preservation.
`TEST_SAFE_PUSH_SCRIPT` can point the latter at an exported baseline executable
for RED verification without replacing the current implementation.
