"""Native-export private pages go through the normal CLI without collectors."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from gptme_vitals.cli import main


@pytest.mark.parametrize(
    ("command", "payload", "visible"),
    [
        (
            "sessions",
            [{"session_id": "test-session", "start_time": None}],
            "test-session",
        ),
        ("tasks", {"tasks": [{"id": "test-task", "name": "test-task"}]}, "test-task"),
        (
            "vitals",
            {
                "schema": "fleet-vitals/core@1",
                "agent": "test-agent",
                "status": "unknown",
            },
            "test-agent",
        ),
    ],
)
def test_private_page_cli_uses_native_export_without_public_writes(
    tmp_path: Path, command: str, payload: object, visible: str
) -> None:
    source, private, public = (
        tmp_path / "export.json",
        tmp_path / "private",
        tmp_path / "public",
    )
    source.write_text(json.dumps(payload))
    public.mkdir()
    sentinel = public / "index.html"
    sentinel.write_text("existing public dashboard")
    assert (
        main(
            [
                command,
                "--input",
                str(source),
                "--private-root",
                str(private),
                "--public-root",
                str(public),
            ]
        )
        == 0
    )
    result = private / command / "index.html"
    assert visible in result.read_text()
    assert stat.S_IMODE(result.stat().st_mode) == 0o600
    assert list(public.iterdir()) == [sentinel]
    assert sentinel.read_text() == "existing public dashboard"


@pytest.mark.parametrize("command", ["sessions", "tasks", "vitals"])
def test_bad_native_export_is_usage_error_before_writes(
    tmp_path: Path, command: str
) -> None:
    source, private, public = (
        tmp_path / "export.json",
        tmp_path / "private",
        tmp_path / "public",
    )
    source.write_text("null")
    assert (
        main(
            [
                command,
                "--input",
                str(source),
                "--private-root",
                str(private),
                "--public-root",
                str(public),
            ]
        )
        == 2
    )
    assert not private.exists()
    assert not public.exists()


@pytest.mark.parametrize("command", ["threads", "sessions", "tasks", "vitals"])
def test_oversized_json_integer_is_usage_error_before_writes(
    tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    limit = sys.get_int_max_str_digits()
    if limit == 0:
        pytest.skip("Python integer conversion limit is disabled")
    source = tmp_path / "export.json"
    private, public = tmp_path / "private", tmp_path / "public"
    source.write_text('{"sessions": [], "ignored": ' + "1" * (limit + 1) + "}")
    assert (
        main(
            [
                command,
                "--input",
                str(source),
                "--private-root",
                str(private),
                "--public-root",
                str(public),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "gptme-vitals:" in captured.err
    assert "Traceback" not in captured.err
    assert not captured.out
    assert not private.exists()
    assert not public.exists()


@pytest.mark.parametrize("command", ["threads", "sessions", "tasks", "vitals"])
def test_deeply_nested_json_is_usage_error_before_writes(
    tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    depth = max(10000, sys.getrecursionlimit() * 10)
    source = tmp_path / "export.json"
    private, public = tmp_path / "private", tmp_path / "public"
    source.write_text(
        '{"sessions": [], "ignored": ' + "[" * depth + "0" + "]" * depth + "}"
    )
    assert (
        main(
            [
                command,
                "--input",
                str(source),
                "--private-root",
                str(private),
                "--public-root",
                str(public),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "gptme-vitals:" in captured.err
    assert "Traceback" not in captured.err
    assert not captured.out
    assert not private.exists()
    assert not public.exists()
