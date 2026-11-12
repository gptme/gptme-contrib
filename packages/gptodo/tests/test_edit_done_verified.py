"""A task with a success_criterion closes only with `verified` evidence."""

from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.frontmatter_compat import frontmatter


def make_task(tmp_path, monkeypatch, extra: str):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HOOK_TASK_DONE", raising=False)
    path = tasks_dir / "task.md"
    path.write_text(
        "---\nstate: active\ncreated: 2026-01-01T00:00:00+00:00\n"
        f"{extra}---\n# task\n\n- [x] complete\n"
    )
    return path


CRITERION = "success_criterion: motion under 20% on a 50-session sample\n"


def test_done_refused_without_verified(tmp_path, monkeypatch):
    path = make_task(tmp_path, monkeypatch, CRITERION)
    before = path.read_bytes()
    result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", "done"])
    assert result.exit_code != 0, result.output
    assert "verified" in result.output
    assert path.read_bytes() == before


def test_done_allowed_when_verified_set_in_same_command(tmp_path, monkeypatch):
    path = make_task(tmp_path, monkeypatch, CRITERION)
    result = CliRunner().invoke(
        cli,
        [
            "edit",
            "task",
            "--set",
            "state",
            "done",
            "--set",
            "verified",
            "50-session sample 2026-11-19: motion 14%",
        ],
    )
    assert result.exit_code == 0, result.output
    meta = frontmatter.load(path).metadata
    assert meta["state"] == "done"
    assert meta["verified"].startswith("50-session sample")


def test_done_allowed_when_already_verified(tmp_path, monkeypatch):
    path = make_task(tmp_path, monkeypatch, CRITERION + "verified: measured 14%\n")
    result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", "done"])
    assert result.exit_code == 0, result.output
    assert frontmatter.load(path)["state"] == "done"


def test_no_criterion_no_gate(tmp_path, monkeypatch):
    path = make_task(tmp_path, monkeypatch, "")
    result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", "done"])
    assert result.exit_code == 0, result.output
    assert frontmatter.load(path)["state"] == "done"


def test_allow_unverified_override(tmp_path, monkeypatch):
    path = make_task(tmp_path, monkeypatch, CRITERION)
    result = CliRunner().invoke(
        cli, ["edit", "task", "--set", "state", "done", "--allow-unverified"]
    )
    assert result.exit_code == 0, result.output
    assert frontmatter.load(path)["state"] == "done"


def test_recurring_task_exempt(tmp_path, monkeypatch):
    path = make_task(tmp_path, monkeypatch, CRITERION + "recur: 7d\n")
    result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", "done"])
    assert result.exit_code == 0, result.output
    assert frontmatter.load(path)["state"] != "active"


def test_json_mode_refusal_is_machine_readable(tmp_path, monkeypatch):
    make_task(tmp_path, monkeypatch, CRITERION)
    result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", "done", "--json"])
    assert result.exit_code != 0
    assert '"error"' in result.output and "verified" in result.output


def test_clearing_verified_does_not_count_as_evidence(tmp_path, monkeypatch):
    path = make_task(tmp_path, monkeypatch, CRITERION)
    before = path.read_bytes()
    result = CliRunner().invoke(
        cli, ["edit", "task", "--set", "state", "done", "--set", "verified", "none"]
    )
    assert result.exit_code != 0, result.output
    assert "verified" in result.output
    assert path.read_bytes() == before


def test_clearing_existing_verified_while_done_is_refused(tmp_path, monkeypatch):
    path = make_task(tmp_path, monkeypatch, CRITERION + "verified: measured 14%\n")
    before = path.read_bytes()
    result = CliRunner().invoke(
        cli, ["edit", "task", "--set", "state", "done", "--set", "verified", "none"]
    )
    assert result.exit_code != 0, result.output
    assert "verified" in result.output
    assert path.read_bytes() == before


def test_last_verified_assignment_wins(tmp_path, monkeypatch):
    make_task(tmp_path, monkeypatch, CRITERION + "verified: measured 14%\n")
    result = CliRunner().invoke(
        cli,
        [
            "edit",
            "task",
            "--set",
            "state",
            "done",
            "--set",
            "verified",
            "none",
            "--set",
            "verified",
            "re-measured 12%",
        ],
    )
    assert result.exit_code == 0, result.output
