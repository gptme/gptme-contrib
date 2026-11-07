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
    first = _alloc(tmp_path, "abcd", "11111111-0000-0000-0000-000000000000")
    # All three share a 4-char prefix; the third must reach the 8-char suffix.
    second = _alloc(tmp_path, "abcd", "11111111-2222-0000-0000-000000000000")
    third = _alloc(tmp_path, "abcd", "11112222-3333-0000-0000-000000000000")
    assert second.name == "autonomous-session-abcd-1111.md"
    assert third.name == "autonomous-session-abcd-11112222.md"
    assert len({first, second, third}) == 3
    assert all(path.exists() for path in (first, second, third))


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
    owner = (
        tmp_path
        / "state"
        / "session-journal-reservations"
        / "journal"
        / path.parent.name
        / f"{path.name}.json"
    )
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


def test_interrupted_publication_never_adopts_history(tmp_path: Path, monkeypatch):
    import gptme_sessions.journal_path as allocator

    historical = tmp_path / _TODAY / "autonomous-session-abcd.md"
    historical.parent.mkdir()
    historical.write_text("historical bytes")
    original_link = allocator.os.link

    def interrupted_link(*args, **kwargs):
        original_link(*args, **kwargs)
        raise RuntimeError("simulated crash after publication")

    with monkeypatch.context() as patch:
        patch.setattr(allocator.os, "link", interrupted_link)
        with pytest.raises(RuntimeError, match="simulated crash"):
            _alloc(tmp_path, "abcd", "22222222-0000-0000-0000-000000000000")

    retry = _alloc(tmp_path, "abcd", "22222222-0000-0000-0000-000000000000")
    assert retry != historical
    assert historical.read_text() == "historical bytes"


def test_relative_and_absolute_directories_share_retry(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = _alloc(Path("journal"), "abcd", "11111111")
    assert first.is_absolute()
    assert _alloc(tmp_path / "journal", "abcd", "11111111") == first


def test_concurrent_same_run_allocations_share_one_path(tmp_path: Path):
    from threading import Barrier

    barrier = Barrier(8)

    def allocate(_):
        barrier.wait(timeout=10)
        return _alloc(tmp_path, "abcd", "11111111")

    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(allocate, range(8)))
    assert len(set(paths)) == 1
    assert len(list((tmp_path / _TODAY).glob("*.md"))) == 1


@pytest.mark.parametrize("value", [None, [], "text", 123, {}, {"journal_path": "bad"}])
def test_invalid_owner_shapes_fail_closed(tmp_path: Path, value):
    path = _alloc(tmp_path, "abcd", "11111111")
    owner = tmp_path / "reservations" / _TODAY / f"{path.name}.json"
    owner.write_text(json.dumps(value))
    with pytest.raises(JournalReservationError):
        _alloc(tmp_path, "abcd", "11111111")


@pytest.mark.parametrize("after_publication", [False, True])
def test_process_crash_releases_lock_and_retries_safely(tmp_path: Path, after_publication):
    import multiprocessing

    import gptme_sessions.journal_path as allocator

    def crash():
        original_link = allocator.os.link

        def exit_at_publication(*args, **kwargs):
            if after_publication:
                original_link(*args, **kwargs)
            allocator.os._exit(23)

        allocator.os.link = exit_at_publication
        _alloc(tmp_path, "abcd", "11111111")

    process = multiprocessing.get_context("fork").Process(target=crash)
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("allocator did not reach the crash point")
    assert process.exitcode == 23
    canonical = tmp_path / _TODAY / "autonomous-session-abcd.md"
    assert canonical.exists()
    retry = _alloc(tmp_path, "abcd", "11111111")
    assert (retry == canonical) is after_publication
    assert canonical.read_bytes() == b""
    assert _alloc(tmp_path, "abcd", "11111111") == retry


@pytest.mark.parametrize("value", [None, [], "text", 123])
def test_cli_handles_non_object_owner(tmp_path: Path, capsys, value):
    path = _alloc(tmp_path, "abcd", "11111111")
    owner = tmp_path / "reservations" / _TODAY / f"{path.name}.json"
    owner.write_text(json.dumps(value))
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
                "11111111",
                "--date",
                _TODAY,
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert not captured.out
    assert "integrity failure" in captured.err
    assert "Traceback" not in captured.err


def test_sibling_journal_trees_have_independent_default_reservations(tmp_path: Path):
    first = allocate_journal_path(tmp_path / "journal-a", "autonomous", "abcd", "11111111")
    second = allocate_journal_path(tmp_path / "journal-b", "autonomous", "abcd", "11111111")
    assert first != second
    assert first.name == second.name == "autonomous-session-abcd.md"
    assert first.exists() and second.exists()


def test_default_date_retry_across_midnight_reuses_original(tmp_path: Path, monkeypatch):
    import gptme_sessions.journal_path as allocator

    class Clock:
        day = "2026-11-07"

        @classmethod
        def now(cls, tz):
            return datetime.datetime.fromisoformat(cls.day).replace(tzinfo=tz)

    monkeypatch.setattr(allocator, "datetime", Clock)
    first = allocator.allocate_journal_path(tmp_path / "journal", "autonomous", "abcd", "11111111")
    first.write_text("session started yesterday")
    Clock.day = "2026-11-08"
    retry = allocator.allocate_journal_path(tmp_path / "journal", "autonomous", "abcd", "11111111")
    assert retry == first
    assert retry.read_text() == "session started yesterday"
    assert not (tmp_path / "journal" / Clock.day / first.name).exists()


@pytest.mark.parametrize(
    "owner_value",
    [
        {},
        {"journal_path": "placeholder"},
        {"run_id": ""},
        {"run_id": 123},
        {"run_id": "another-run"},
        {"run_id": "another-run", "journal_path": None},
        {"run_id": "another-run", "journal_path": "wrong-path"},
    ],
)
def test_default_date_retry_rejects_corrupt_historical_owner(
    tmp_path: Path, monkeypatch, capsys, owner_value
):
    import gptme_sessions.journal_path as allocator

    class Clock:
        day = "2026-11-07"

        @classmethod
        def now(cls, tz):
            return datetime.datetime.fromisoformat(cls.day).replace(tzinfo=tz)

    monkeypatch.setattr(allocator, "datetime", Clock)
    journal_dir = tmp_path / "journal"
    first = allocator.allocate_journal_path(journal_dir, "autonomous", "abcd", "11111111")
    first.write_text("session started yesterday")
    owner_path = (
        tmp_path
        / "state"
        / "session-journal-reservations"
        / "journal"
        / Clock.day
        / f"{first.name}.json"
    )
    owner_path.write_text(json.dumps(owner_value))
    Clock.day = "2026-11-08"

    with pytest.raises(JournalReservationError, match="invalid owner record|path mismatch"):
        allocator.allocate_journal_path(journal_dir, "autonomous", "abcd", "11111111")
    assert (
        main(
            [
                "--journal-dir",
                str(journal_dir),
                "--run-type",
                "autonomous",
                "--label",
                "abcd",
                "--uuid",
                "11111111",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert not captured.out
    assert "integrity failure" in captured.err
    assert "Traceback" not in captured.err
    assert first.read_text() == "session started yesterday"
    assert not (journal_dir / Clock.day).exists()


def test_default_date_lookup_skips_valid_historical_other_run(tmp_path: Path):
    first = allocate_journal_path(
        tmp_path / "journal", "autonomous", "abcd", "11111111", date="2000-01-01"
    )
    second = allocate_journal_path(tmp_path / "journal", "autonomous", "abcd", "22222222")
    assert first.parent != second.parent
    assert first.name == second.name == "autonomous-session-abcd.md"
    assert allocate_journal_path(tmp_path / "journal", "autonomous", "abcd", "22222222") == second
