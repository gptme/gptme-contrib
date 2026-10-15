"""Security and preload regressions for the extracted reviewer engine."""

import subprocess

from gptme_review import core


def test_runtime_claims_never_execute_model_output(monkeypatch):
    calls = []

    def record_run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(core.subprocess, "run", record_run)
    finding = {
        "body": "This raises TypeError.\n```python\nprint('untrusted')\n```",
        "severity": "P0",
    }
    kept, dropped = core.drop_unverifiable_runtime_claims([finding])
    assert calls == [], "Model-generated snippets must never reach subprocess.run"
    assert kept == [finding]
    assert dropped == []
    assert "unverified_claim" not in finding


def test_preload_reserves_room_for_truncation_marker(monkeypatch, tmp_path):
    (tmp_path / "large.py").write_text("x" * 1000)
    monkeypatch.setattr(core, "_AGENT_CONTEXT_MAX_CHARS", 200)
    monkeypatch.setattr(core, "_AGENT_CONTEXT_MAX_FILE_CHARS", 1000)
    monkeypatch.setattr(
        core.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout=""),
    )
    context = core.build_agent_context(
        "owner/repo", "+++ b/large.py\n@@ -0,0 +1 @@\n+x", tmp_path
    )
    assert "### Current contents of `large.py`" in context
    assert "[… truncated; use read for the rest …]" in context
    assert "### Changed files omitted" not in context
    section = context[context.index("### Current contents") :]
    assert len(section) <= 200


def test_package_commit_root_requires_checkout_marker(tmp_path):
    import gptme_review

    checkout = tmp_path / "repo"
    package = checkout / "packages" / "gptme-review" / "src" / "gptme_review"
    package.mkdir(parents=True)
    (checkout / ".git").write_text("gitdir: /some/worktree\n")
    assert gptme_review._package_checkout(package / "__init__.py") == checkout
    installed = checkout / ".venv/lib/python3.13/site-packages/gptme_review"
    installed.mkdir(parents=True)
    assert gptme_review._package_checkout(installed / "__init__.py") is None


def test_files_api_diff_is_readable_by_diff_consumers():
    import json

    def run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                {"filename": "src/a.py", "patch": "@@ -1 +1 @@\n-old\n+new"}
            ),
        )

    diff = core.fetch_pr_diff_via_files_api("owner/repo", 1, run=run)
    assert core.changed_paths(diff) == ["src/a.py"]
    assert core.commentable_lines(diff) == {"src/a.py": {1}}
