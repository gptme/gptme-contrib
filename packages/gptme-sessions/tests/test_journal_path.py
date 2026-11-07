"""Journal-path pre-allocation: reservation, collision suffixes and retries."""

import datetime
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gptme_sessions.journal_path import (
    JournalReservationError,
    allocate_journal_path,
    journal_stem,
    main,
)

# Use today's date so the suite never rots as the calendar advances.
_TODAY = datetime.date.today().isoformat()


def _alloc(
    journal_dir: Path,
    label: str,
    run_id: str,
    run_type: str = "autonomous",
    reservation_dir: Path | None = None,
) -> Path:
    return allocate_journal_path(
        journal_dir,
        run_type,
        label,
        run_id,
        date=_TODAY,
        reservation_dir=reservation_dir or (journal_dir / "reservations"),
    )


def test_first_allocation_is_canonical(tmp_path: Path):
    path = _alloc(tmp_path, "abcd", "11111111-0000-0000-0000-000000000000")
    assert path.name == "autonomous-session-abcd.md"
    assert path.exists()
    assert path.stat().st_size == 0  # empty placeholder, the session writes it


def test_second_allocation_same_label_gets_suffix(tmp_path: Path):
    first = _alloc(tmp_path, "abcd", "11111111-0000-0000-0000-000000000000")
    second = _alloc(tmp_path, "abcd", "22222222-0000-0000-0000-000000000000")
    assert first != second
    assert first.exists() and second.exists()
    assert second.name == "autonomous-session-abcd-2222.md"


def test_both_files_are_distinct_and_empty(tmp_path: Path):
    first = _alloc(tmp_path, "cafe", "aaaaaaaa-0000-0000-0000-000000000000")
    second = _alloc(tmp_path, "cafe", "bbbbbbbb-0000-0000-0000-000000000000")
    assert first.read_bytes() == b""
    assert second.read_bytes() == b""
    assert first.name != second.name


def test_concurrent_same_label_allocations_are_distinct(tmp_path: Path):
    run_ids = [f"0000000{n}-0000-0000-0000-000000000000" for n in range(1, 7)]
    with ThreadPoolExecutor(max_workers=len(run_ids)) as pool:
        paths = list(
            pool.map(
                lambda run_id: _alloc(tmp_path, "cafe", run_id),
                run_ids,
            )
        )
    assert len(set(paths)) == len(run_ids)
    assert all(path.exists() and path.read_bytes() == b"" for path in paths)


def test_cleanup_run_type_uses_autonomous_convention(tmp_path: Path):
    path = _alloc(tmp_path, "ef01", "11111111-0000-0000-0000-000000000000", "cleanup")
    assert path.name == "autonomous-session-ef01.md"


def test_non_autonomous_run_type_uses_runtype_prefix(tmp_path: Path):
    path = _alloc(tmp_path, "ef01", "11111111-0000-0000-0000-000000000000", "monitoring")
    assert path.name == "monitoring-session-ef01.md"


def test_retry_same_run_id_reuses_owned_canonical_path(tmp_path: Path):
    first = _alloc(tmp_path, "abcd", "11111111-0000-0000-0000-000000000000")
    retry = _alloc(tmp_path, "abcd", "11111111-0000-0000-0000-000000000000")
    assert retry == first


def test_retry_second_run_reuses_owned_suffixed_path(tmp_path: Path):
    _alloc(tmp_path, "abcd", "11111111-0000-0000-0000-000000000000")
    second = _alloc(tmp_path, "abcd", "22222222-0000-0000-0000-000000000000")
    retry = _alloc(tmp_path, "abcd", "22222222-0000-0000-0000-000000000000")
    assert retry == second


def test_historical_unowned_file_is_never_adopted(tmp_path: Path):
    date_dir = tmp_path / _TODAY
    date_dir.mkdir(parents=True)
    historical = date_dir / "autonomous-session-abcd.md"
    historical.write_text("historical bytes", encoding="utf-8")

    allocated = _alloc(tmp_path, "abcd", "22222222-0000-0000-0000-000000000000")

    assert allocated.name == "autonomous-session-abcd-2222.md"
    assert historical.read_text(encoding="utf-8") == "historical bytes"


def test_corrupt_owned_reservation_fails_closed(tmp_path: Path):
    path = _alloc(tmp_path, "abcd", "11111111-0000-0000-0000-000000000000")
    owner = tmp_path / "reservations" / _TODAY / f"{path.name}.json"
    owner.write_text('{"run_id":"wrong","journal_path":"elsewhere"}\n')

    with pytest.raises(JournalReservationError, match="integrity failure|mismatch"):
        _alloc(tmp_path, "abcd", "11111111-0000-0000-0000-000000000000")


def test_three_way_collision_uses_long_suffix(tmp_path: Path):
    _alloc(tmp_path, "abcd", "11111111-0000-0000-0000-000000000000")
    # Same 4-char prefix as the canonical run, different full run id.
    second = _alloc(tmp_path, "abcd", "11111111-2222-0000-0000-000000000000")
    assert second.name == "autonomous-session-abcd-1111.md"


def test_journal_stem_mapping():
    assert journal_stem("autonomous", "ab12") == "autonomous-session-ab12"
    assert journal_stem("self-review", "ab12") == "autonomous-session-ab12"
    assert journal_stem("monitoring", "ab12") == "monitoring-session-ab12"


def test_default_reservation_dir_is_outside_the_journal_tree(tmp_path: Path):
    journal_dir = tmp_path / "journal"
    path = allocate_journal_path(
        journal_dir, "autonomous", "abcd", "11111111-0000-0000-0000-000000000000"
    )
    assert path.exists()
    owner = tmp_path / "state" / "session-journal-reservations" / _TODAY / f"{path.name}.json"
    assert json.loads(owner.read_text())["journal_path"] == str(path)


def test_cli_prints_the_allocated_path(tmp_path: Path, capsys):
    assert (
        main(
            [
                "--journal-dir",
                str(tmp_path),
                "--reservation-dir",
                str(tmp_path / "reservations"),
                "--run-type",
                "autonomous",
                "--label",
                "abcd",
                "--uuid",
                "11111111-0000-0000-0000-000000000000",
                "--date",
                _TODAY,
            ]
        )
        == 0
    )
    assert Path(capsys.readouterr().out.strip()).name == "autonomous-session-abcd.md"


def test_cli_fails_closed_on_corrupt_reservation(tmp_path: Path, capsys):
    args = [
        "--journal-dir",
        str(tmp_path),
        "--reservation-dir",
        str(tmp_path / "reservations"),
        "--run-type",
        "autonomous",
        "--label",
        "abcd",
        "--uuid",
        "11111111-0000-0000-0000-000000000000",
        "--date",
        _TODAY,
    ]
    assert main(args) == 0
    capsys.readouterr()
    path = tmp_path / _TODAY / "autonomous-session-abcd.md"
    owner = tmp_path / "reservations" / _TODAY / f"{path.name}.json"
    owner.write_text('{"run_id":"wrong","journal_path":"elsewhere"}\n')

    assert main(args) == 1
    assert "integrity failure" in capsys.readouterr().err
