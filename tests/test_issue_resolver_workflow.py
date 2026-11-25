"""Exercise credential selection and shell guards without a paid resolver run."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/issue-resolver.yml"


@pytest.fixture
def workflow() -> dict:
    # BaseLoader preserves the Actions `on` key rather than treating it as bool.
    result = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    assert isinstance(result, dict)
    return result


def _provider_key(step: dict, secrets: dict[str, str]) -> str:
    """Resolve the workflow's secret-only fallback expression using fake values."""
    expression = step["env"]["GPTME_PROVIDER_KEY"]
    assert expression.startswith("${{ ") and expression.endswith(" }}")
    terms = expression[4:-3].split(" || ")
    for term in terms:
        assert re.fullmatch(r"secrets\.[\w-]+", term), term
    return next(
        (secrets.get(term[8:], "") for term in terms if secrets.get(term[8:])), ""
    )


@pytest.mark.parametrize(
    ("secrets", "expected"),
    [
        # Both direct issue triggers only have the stored repository secret.
        ({"GPTME_RESOLVER_PROVIDER_KEY": "sk-or-direct-label"}, "sk-or-direct-label"),
        (
            {"GPTME_RESOLVER_PROVIDER_KEY": "sk-or-direct-comment"},
            "sk-or-direct-comment",
        ),
        # Reusable callers keep their public hyphenated parameter.
        ({"gptme-provider-key": "sk-ant-caller"}, "sk-ant-caller"),
        (
            {"gptme-provider-key": "", "GPTME_RESOLVER_PROVIDER_KEY": "sk-or-fallback"},
            "sk-or-fallback",
        ),
        (
            {
                "gptme-provider-key": "sk-ant-caller",
                "GPTME_RESOLVER_PROVIDER_KEY": "sk-or-inherited",
            },
            "sk-ant-caller",
        ),
    ],
    ids=[
        "issues",
        "issue_comment",
        "workflow_call",
        "empty-caller",
        "caller-precedence",
    ],
)
def test_provider_secret_reaches_resolver(workflow: dict, secrets: dict, expected: str):
    steps = workflow["jobs"]["resolve"]["steps"]
    resolve = next(step for step in steps if step.get("id") == "resolve")
    assert _provider_key(steps[0], secrets) == expected
    assert _provider_key(resolve, secrets) == expected


@pytest.mark.parametrize("key", ["", "sk-or-fake-do-not-log"])
def test_credential_check_runs_before_setup(workflow: dict, key: str):
    steps = workflow["jobs"]["resolve"]["steps"]
    check = steps[0]
    assert check["name"] == "Check provider credential"
    assert _provider_key(check, {"GPTME_RESOLVER_PROVIDER_KEY": key}) == key
    result = subprocess.run(
        ["bash", "-e", "-c", check["run"]],
        env={"PATH": os.defpath, "GPTME_PROVIDER_KEY": key},
        capture_output=True,
        text=True,
    )
    assert result.returncode == (0 if key else 1)
    if not key:
        assert "::error::" in result.stdout
        assert "GPTME_RESOLVER_PROVIDER_KEY" in result.stdout
        assert "gptme-provider-key" in result.stdout
    else:
        assert key not in result.stdout + result.stderr


def test_reusable_secret_contract_is_preserved(workflow: dict):
    assert "issues" in workflow["on"]
    assert "issue_comment" in workflow["on"]
    declarations = workflow["on"]["workflow_call"]["secrets"]
    for name in ("gptme-provider-key", "GPTME_RESOLVER_PROVIDER_KEY"):
        assert declarations[name]["required"] == "false"
