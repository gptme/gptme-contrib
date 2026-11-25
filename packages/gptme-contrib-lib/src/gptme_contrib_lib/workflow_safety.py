"""Prove a GitHub Actions workflow edit is a CI repair, not a trust change.

A merge-policy ``ci`` class needs more than a diff: permissions can move under
an unchanged parent key, and YAML aliases or quoted keys get past token scans.
So this compares the *parsed* base and head documents of one modified workflow
and lists every invariant the edit breaks. An empty list means the edit only
touches what a repair may touch (step bodies, timeouts, action ``@ref`` pins).

What must stay equal: triggers, top-level ``permissions``/``env``/``defaults``/
``concurrency``, the job set, each job's trust-bearing keys (including
``strategy``, whose matrix can retarget ``runs-on``/``environment`` through
``${{ matrix.* }}``, and job ``env``/``defaults``/``concurrency``/``with``), every
``secrets.*``/``vars.*`` reference, and the ``owner/repo`` of each ``uses:``.
Release, publish, deploy, signing and pages workflows or jobs are always out.

Secret-bearing workflows are frozen too, because an unchanged reference set
says nothing about what *consumes* a secret: a ``run:`` edit in a step that
holds ``secrets.X`` can send it anywhere. A secret does not stay where it is
declared — it reaches later steps through ``$GITHUB_ENV``, step outputs and
files written by credential actions, and it reaches other jobs through
``needs`` outputs, artifacts and caches. So once *any* job (or the workflow
``env``) references ``secrets``/``vars``, every step body in the workflow must
be unchanged, positionally, and only a step's ``@ref`` pin and
``timeout-minutes`` may differ. Reusable jobs with ``secrets: inherit`` are
secret-bearing too. The implicit ``GITHUB_TOKEN`` also counts, even without a
reference or an explicit checkout token input: step bodies are repairable only
when every job explicitly disables token permissions (``permissions: {}``, at
job or workflow level) and no job inherits or references secrets/vars. Missing,
read-only, write-capable and unknown permissions all freeze step bodies.

Everything fails closed: a missing YAML parser, unparseable YAML or a
non-mapping document is a violation, never a pass. PyYAML is imported lazily so
``gptme_contrib_lib`` keeps no hard dependency on it.
"""

from __future__ import annotations

import re
from typing import Any

_TOP_LEVEL_KEYS = ("on", "permissions", "env", "defaults", "concurrency")
_JOB_KEYS = (
    "permissions",
    "runs-on",
    "environment",
    "secrets",
    "container",
    "services",
    "if",
    "needs",
    "uses",
    "with",
    "strategy",
    "env",
    "defaults",
    "concurrency",
    "continue-on-error",
)
# A repair may still change these inside a frozen (secret-visible) step.
_STEP_REPAIR_KEYS = ("timeout-minutes",)
_FORBIDDEN_EVENTS = ("pull_request_target", "workflow_run")
_PRIVILEGED_NAME_RE = re.compile(r"(?i)(release|publish|deploy|sign|pages)")
# `secrets.X`, `secrets['X']` / `secrets["X"]`, a computed `secrets[expr]`, or the
# whole context (`toJSON(secrets)`). Literal indexes normalize to the dotted form.
_CONTEXT_REF_RE = re.compile(
    r"\b(secrets|vars)\b(?:\s*\.\s*([A-Za-z_][A-Za-z0-9_-]*)|\s*\[\s*([^\]]*?)\s*\])?"
)
_EXPRESSION_RE = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_QUOTED_NAME_RE = re.compile(r"""^(['"])([A-Za-z_][A-Za-z0-9_-]*)\1$""")


def _load(text: str, side: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        import yaml
    except ImportError:
        return None, "YAML parser unavailable"

    # Actions uses YAML 1.2 boolean spelling: `on`, `off`, `yes`, `no` are
    # strings, including in trigger filters. Do not mutate SafeLoader globally.
    class WorkflowLoader(yaml.SafeLoader):
        pass

    WorkflowLoader.yaml_implicit_resolvers = {
        key: [
            (tag, pattern)
            for tag, pattern in resolvers
            if tag != "tag:yaml.org,2002:bool"
        ]
        for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    WorkflowLoader.add_implicit_resolver(
        "tag:yaml.org,2002:bool",
        re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
        list("tTfF"),
    )
    try:
        doc = yaml.load(text, Loader=WorkflowLoader)
    except yaml.YAMLError as exc:
        return None, f"{side} workflow is not valid YAML ({type(exc).__name__})"
    if not isinstance(doc, dict):
        return None, f"{side} workflow is not a mapping"

    return doc, None


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in _strings(k) + _strings(v)]
    if isinstance(node, list):
        return [s for item in node for s in _strings(item)]
    return []


def _context_refs(doc: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for text in _strings(doc):
        # Dotted refs count anywhere (as before); other forms only inside ${{ }},
        # so prose such as a step named "Check secrets" is not a reference.
        expressions = _EXPRESSION_RE.findall(text)
        for chunk, in_expression in [(text, False)] + [(e, True) for e in expressions]:
            for context, dotted, index in _CONTEXT_REF_RE.findall(chunk):
                if dotted:
                    refs.add(f"{context}.{dotted}")
                elif not in_expression:
                    continue
                elif index:
                    quoted = _QUOTED_NAME_RE.match(index)
                    refs.add(
                        f"{context}.{quoted.group(2)}"
                        if quoted
                        else f"{context}[{index}]"
                    )
                else:
                    refs.add(f"{context}.*")
    return refs


def _events(on: Any) -> set[str]:
    """Event names directly under ``on``, not strings inside their filters."""
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {e for e in on if isinstance(e, str)}
    if isinstance(on, dict):
        return {str(k) for k in on}
    return set()


def _action_repo(uses: Any) -> str | None:
    """``owner/repo[/path]`` of a step ``uses:``; the ``@ref`` may change."""
    if not isinstance(uses, str):
        return None
    if uses.startswith(("./", "docker://")):
        return uses  # local action or image: the whole value must stay equal
    return uses.split("@", 1)[0]


def _step_actions(job: Any) -> list[str | None]:
    steps = job.get("steps") if isinstance(job, dict) else None
    if not isinstance(steps, list):
        return []
    return sorted(
        (
            _action_repo(step.get("uses"))
            for step in steps
            if isinstance(step, dict) and "uses" in step
        ),
        key=str,
    )


def _guarded_step(step: Any) -> Any:
    """A frozen step: only ``@ref`` and timeout may move."""
    if not isinstance(step, dict):
        return step
    out = {k: v for k, v in step.items() if k not in _STEP_REPAIR_KEYS}
    if "uses" in out:
        out["uses"] = _action_repo(out["uses"])
    return out


def _secret_step_violations(
    job_id: Any, old: dict[str, Any], new: dict[str, Any], scope_wide: bool
) -> list[str]:
    """Steps in a secret-bearing workflow must be unchanged (see the module docstring).

    ``scope_wide`` is true once any job has credentials or the workflow ``env``
    references a secret, which can travel to any step or job (env files, outputs,
    artifacts, caches). Neither side's step body may then differ; only a
    ``@ref`` pin and ``timeout-minutes`` survive ``_guarded_step``.
    """
    if not scope_wide:
        return []
    old_steps, new_steps = old.get("steps"), new.get("steps")
    old_steps = old_steps if isinstance(old_steps, list) else []
    new_steps = new_steps if isinstance(new_steps, list) else []
    if [_guarded_step(s) for s in old_steps] != [_guarded_step(s) for s in new_steps]:
        return [f"job `{job_id}`: a step changed in a secret-scoped job"]
    return []


def _job_has_credentials(doc: dict[str, Any], job: Any) -> bool:
    """Only explicit empty effective permissions prove token scopes disabled.

    Job permissions replace workflow permissions; omitted permissions inherit
    repository defaults, which cannot be established from these two documents.
    Even a read-only token can expose private repository contents. Inherited
    secrets are a structural declaration, not an expression reference.
    """
    if not isinstance(job, dict):
        return True
    permissions = job.get("permissions", doc.get("permissions"))
    return (
        permissions != {}
        or job.get("secrets") == "inherit"
        or bool(_context_refs({"job": job}))
    )


def _jobs(doc: dict[str, Any]) -> dict[str, Any] | None:
    jobs = doc.get("jobs")
    return jobs if isinstance(jobs, dict) else None


def ci_repair_violations(path: str, base_text: str, head_text: str) -> list[str]:
    """Invariants a modified workflow at *path* breaks; empty means CI repair."""
    name = path.rsplit("/", 1)[-1]
    if _PRIVILEGED_NAME_RE.search(name):
        return [f"{name}: release/publish/deploy/sign/pages workflow"]
    base, err = _load(base_text, "base")
    if err:
        return [err]
    head, err = _load(head_text, "head")
    if err:
        return [err]
    assert base is not None and head is not None

    violations: list[str] = []
    for side, doc in (("base", base), ("head", head)):
        events = _events(doc.get("on"))
        for event in _FORBIDDEN_EVENTS:
            if event in events:
                violations.append(f"{side} workflow triggers on {event}")
    for key in _TOP_LEVEL_KEYS:
        if base.get(key) != head.get(key):
            violations.append(f"top-level `{key}` changed")
    if _context_refs(base) != _context_refs(head):
        violations.append("secrets/vars references changed")
    workflow_env_secrets = bool(
        _context_refs({"base": base.get("env"), "head": head.get("env")})
    )

    base_jobs, head_jobs = _jobs(base), _jobs(head)
    if base_jobs is None or head_jobs is None:
        return violations + ["`jobs` is missing or not a mapping"]
    if set(base_jobs) != set(head_jobs):
        violations.append("job set changed")
    # Credentials anywhere can reach any job through outputs/artifacts/caches.
    # Include inherited secrets and implicit tokens, not only expression refs.
    secret_scope = workflow_env_secrets or any(
        _job_has_credentials(doc, job)
        for doc, jobs in ((base, base_jobs), (head, head_jobs))
        for job in jobs.values()
    )
    for job_id in sorted(set(base_jobs) & set(head_jobs), key=str):
        if _PRIVILEGED_NAME_RE.search(str(job_id)):
            violations.append(f"job `{job_id}`: release/publish/deploy/sign/pages job")
            continue
        old, new = base_jobs[job_id], head_jobs[job_id]
        if not isinstance(old, dict) or not isinstance(new, dict):
            violations.append(f"job `{job_id}` is not a mapping")
            continue
        for key in _JOB_KEYS:
            if old.get(key) != new.get(key):
                violations.append(f"job `{job_id}`: `{key}` changed")
        if _step_actions(old) != _step_actions(new):
            violations.append(f"job `{job_id}`: action set changed (only @ref may)")
        violations += _secret_step_violations(job_id, old, new, secret_scope)
    return violations
