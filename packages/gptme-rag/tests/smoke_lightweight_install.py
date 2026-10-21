"""Run with a clean, normally resolved base install; no pytest or workspace imports.

CI invokes this with ``python -I`` after installing the package without extras.
"""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from gptme_rag import scan_lessons, score_lessons


for module in ("torch", "chromadb", "sentence_transformers", "onnxruntime", "tiktoken"):
    assert importlib.util.find_spec(module) is None, f"unexpected dense dependency: {module}"

with tempfile.TemporaryDirectory() as directory:
    workspace = Path(directory)
    lessons = workspace / "lessons"
    lessons.mkdir()
    (workspace / "gptme.toml").write_text('[lessons]\ndirs = ["lessons"]\n')
    (lessons / "safe.md").write_text(
        '---\nmatch:\n  keywords: ["merge conflict"]\nstatus: active\n---\n'
        "# Resolve Safely\nUse scoped edits.\n"
    )
    (lessons / "archived.md").write_text(
        '---\nmatch:\n  keywords: ["merge conflict"]\nstatus: archived\n---\n'
        "# Archived Advice\nNever inject this.\n"
    )
    matches = score_lessons(scan_lessons([lessons]), "resolve merge conflict")
    assert [match["title"] for match in matches] == ["Resolve Safely"], matches

    # Copy the real harness out of the checkout: workspace discovery must not
    # accidentally find a developer's gptme.toml or re-exec into their full venv.
    root = Path(__file__).resolve().parents[3]
    hook = workspace / "match-lessons.py"
    hook.write_bytes((root / "scripts/claude-code-hooks/match-lessons.py").read_bytes())
    env = os.environ.copy()
    for name in ("PYTHONPATH", "VIRTUAL_ENV", "HOLDOUT_LESSONS", "GPTME_CURRENT_REPO"):
        env.pop(name, None)
    env.update(LESSON_DROPOUT_EPSILON="0", MATCH_LESSONS_VENV_REEXEC="1")
    result = subprocess.run(
        [sys.executable, "-I", str(hook)],
        input=json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "lightweight-install-smoke",
                "prompt": "resolve merge conflict using scoped edits",
            }
        ),
        text=True,
        capture_output=True,
        cwd=workspace,
        env=env,
        check=True,
        timeout=30,
    )
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Resolve Safely" in context and "Use scoped edits." in context, result.stdout
    assert "Archived Advice" not in context, result.stdout
    print("Base install: dense dependencies absent; matcher and real hook emission passed.")
