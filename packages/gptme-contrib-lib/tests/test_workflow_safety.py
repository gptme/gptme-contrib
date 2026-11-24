"""workflow_safety: a workflow edit is a CI repair only when the trust boundary is unchanged."""

from __future__ import annotations

import builtins
import sys

import pytest
from gptme_contrib_lib import merge_policy
from gptme_contrib_lib.merge_policy import FileChange
from gptme_contrib_lib.workflow_safety import ci_repair_violations

pytest.importorskip("yaml")

PATH = ".github/workflows/test.yml"

BASE = """\
name: Test
on:
  push:
    branches: [master]
  pull_request:
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: make test
        env:
          TOKEN: ${{ secrets.CODECOV_TOKEN }}
"""


def check(head: str, base: str = BASE, path: str = PATH) -> list[str]:
    return ci_repair_violations(path, base, head)


def test_repair_edits_pass() -> None:
    head = (
        BASE.replace("timeout-minutes: 10", "timeout-minutes: 30")
        .replace("actions/checkout@v4", "actions/checkout@v5")
        .replace("run: make test", "run: make test PYTEST_ARGS=-x")
    )
    assert check(head) == []


def test_unchanged_workflow_passes() -> None:
    assert check(BASE) == []


def test_alias_moving_write_permissions_into_a_job_is_caught() -> None:
    head = BASE.replace(
        "permissions:\n  contents: read\n",
        "permissions: &perms\n  contents: read\nx-write: &w\n  contents: write\n",
    ).replace(
        "    runs-on: ubuntu-24.04\n",
        "    runs-on: ubuntu-24.04\n    permissions: *w\n",
    )
    violations = check(head)
    assert "job `test`: `permissions` changed" in violations


def test_quoted_permissions_key_is_caught() -> None:
    head = BASE.replace(
        "permissions:\n  contents: read", '"permissions":\n  contents: write'
    )
    assert "top-level `permissions` changed" in check(head)


def test_yaml11_boolean_on_key_is_normalized() -> None:
    # PyYAML reads `on:` as True; an equal trigger block must still compare equal,
    # and an added trigger must still be caught.
    assert check(BASE) == []
    head = BASE.replace(
        "  pull_request:\n", "  pull_request:\n  schedule:\n    - cron: '0 0 * * *'\n"
    )
    assert "top-level `on` changed" in check(head)


@pytest.mark.parametrize("event", ["pull_request_target", "workflow_run"])
def test_privileged_triggers_are_refused(event: str) -> None:
    head = BASE.replace("  pull_request:\n", f"  {event}:\n")
    assert f"head workflow triggers on {event}" in check(head)


def test_secret_added_through_step_env_is_caught() -> None:
    head = BASE.replace(
        "          TOKEN: ${{ secrets.CODECOV_TOKEN }}\n",
        "          TOKEN: ${{ secrets.CODECOV_TOKEN }}\n          PAT: ${{ secrets.ADMIN_PAT }}\n",
    )
    assert "secrets/vars references changed" in check(head)


def test_runs_on_change_is_caught() -> None:
    head = BASE.replace("runs-on: ubuntu-24.04", "runs-on: self-hosted")
    assert "job `test`: `runs-on` changed" in check(head)


def test_new_job_is_caught() -> None:
    head = (
        BASE
        + "  lint:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: make lint\n"
    )
    assert "job set changed" in check(head)


def test_new_action_repo_is_caught_but_ref_bump_is_not() -> None:
    assert check(BASE.replace("astral-sh/setup-uv@v5", "astral-sh/setup-uv@v6")) == []
    head = BASE.replace("astral-sh/setup-uv@v5", "evil/setup-uv@v5")
    assert "job `test`: action set changed (only @ref may)" in check(head)


@pytest.mark.parametrize(
    "path", [".github/workflows/release.yml", ".github/workflows/deploy-docs.yml"]
)
def test_privileged_workflow_names_are_always_out(path: str) -> None:
    assert check(BASE, path=path) != []


def test_privileged_job_name_is_out() -> None:
    base = BASE.replace("  test:\n", "  publish:\n")
    assert check(base, base=base) == [
        "job `publish`: release/publish/deploy/sign/pages job"
    ]


@pytest.mark.parametrize("bad", ["not: [valid", "- a list\n", ""])
def test_unparseable_or_non_mapping_fails_closed(bad: str) -> None:
    assert check(bad) != []
    assert check(BASE, base=bad) != []


def test_missing_parser_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def no_yaml(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "yaml":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "yaml", raising=False)
    monkeypatch.setattr(builtins, "__import__", no_yaml)
    assert check(BASE) == ["YAML parser unavailable"]


class _Classifier:
    def is_sensitive_path(self, path: str) -> bool:
        return False

    def is_lockfile(self, path: str) -> bool:
        return False

    def is_test_file(self, path: str) -> bool:
        return False

    def is_doc_file(self, path: str) -> bool:
        return False

    def is_spec_like_doc(self, path: str) -> bool:
        return False


ALLOWED = r"^(tests?|lint|ci)\.ya?ml$"


@pytest.mark.parametrize(
    ("change", "policy", "expected"),
    [
        # The shipped policy has no ci_allowed_name: verified or not, still ci-other.
        (FileChange(PATH, ci_safe=True), {}, "ci-other"),
        (FileChange(PATH, ci_safe=None), {"ci_allowed_name": ALLOWED}, "ci-other"),
        (FileChange(PATH, ci_safe=False), {"ci_allowed_name": ALLOWED}, "ci-other"),
        (FileChange(PATH, ci_safe=True), {"ci_allowed_name": ALLOWED}, "ci"),
        (
            FileChange(".github/workflows/build.yml", ci_safe=True),
            {"ci_allowed_name": ALLOWED},
            "ci-other",
        ),
        (
            FileChange(".github/actions/x/test.yml", ci_safe=True),
            {"ci_allowed_name": ALLOWED},
            "ci-other",
        ),
    ],
)
def test_file_class_ci_needs_verification_and_policy_name(
    change: FileChange, policy: dict, expected: str
) -> None:
    assert merge_policy.file_class(change, policy, _Classifier()) == expected


def test_shipped_policy_keeps_every_workflow_ci_other() -> None:
    policy = merge_policy.load_policy(merge_policy.POLICY_PATH)
    assert "ci_allowed_name" not in policy
    change = FileChange(PATH, ci_safe=True)
    assert merge_policy.file_class(change, policy, _Classifier()) == "ci-other"
