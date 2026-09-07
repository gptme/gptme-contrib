"""Voice post-call completion is an observable effect on the generic route.

The generic notification-triage route (``run_item``) completes a voice
post-call by running ``post-call.sh`` inside the session; that script writes a
terminal trace row plus a journal linking the record. The native
``pm-run-item-slot`` path grades that ``effect=observed`` explicitly, but the
generic effect derivation (``derive_effect_signal``) had no signal to read for
a threadless, PR-less ``voice_postcall`` item and graded ``unknown``. This is
the parity check that closes that gap.
"""

from datetime import datetime, timezone

from gptme_runloops.worker_records import (
    voice_postcall_effect_observed,
    voice_postcall_journal_path,
    voice_postcall_record_paths,
)


def test_record_paths_extraction():
    detail = (
        "record=/data/calls/a.wav; remote_party=+46765784797; record=/data/calls/b.wav"
    )
    assert voice_postcall_record_paths(detail) == [
        "/data/calls/a.wav",
        "/data/calls/b.wav",
    ]


def test_record_paths_empty_when_no_token():
    assert voice_postcall_record_paths("just some prose") == []


def test_journal_path_is_deterministic_and_sha_based():
    when = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    path = voice_postcall_journal_path("/ws", ["/data/a.wav"], when)
    assert path == voice_postcall_journal_path("/ws", ["/data/a.wav"], when)
    assert str(path).startswith(
        "/ws/journal/2026-09-06/autonomous-session-voice-postcall-"
    )
    assert len(path.name.rsplit("-", 1)[-1].removesuffix(".md")) == 8


def test_effect_observed_when_trace_and_journal_agree(tmp_path):
    record = "/data/calls/one.wav"
    when = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    journal = voice_postcall_journal_path(tmp_path, [record], when)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(f"## Notes\n**Archive**: `{record}`\n", encoding="utf-8")

    trace = tmp_path / "post-call-events.tsv"
    trace.write_text(
        f"2026-09-06T12:01:00Z\trun_completed\t{record}\n",
        encoding="utf-8",
    )

    assert voice_postcall_effect_observed(
        f"record={record}", tmp_path, now=when, trace_file=trace
    )


def test_not_observed_when_trace_has_only_failed_phase(tmp_path):
    record = "/data/calls/one.wav"
    when = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    journal = voice_postcall_journal_path(tmp_path, [record], when)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(f"**Archive**: `{record}`\n", encoding="utf-8")

    trace = tmp_path / "post-call-events.tsv"
    trace.write_text(
        f"2026-09-06T12:01:00Z\trun_failed\t{record}\n",
        encoding="utf-8",
    )

    assert not voice_postcall_effect_observed(
        f"record={record}", tmp_path, now=when, trace_file=trace
    )


def test_not_observed_when_journal_missing(tmp_path):
    record = "/data/calls/one.wav"
    when = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    trace = tmp_path / "post-call-events.tsv"
    trace.write_text(
        f"2026-09-06T12:01:00Z\trun_completed\t{record}\n",
        encoding="utf-8",
    )

    assert not voice_postcall_effect_observed(
        f"record={record}", tmp_path, now=when, trace_file=trace
    )


def test_not_observed_when_no_records_in_detail(tmp_path):
    assert not voice_postcall_effect_observed("", tmp_path)


def test_grouped_records_require_all_present_in_one_trace_row(tmp_path):
    a, b = "/data/a.wav", "/data/b.wav"
    when = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    journal = voice_postcall_journal_path(tmp_path, [a, b], when)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(f"**Archive**: `{a}`\n**Archive**: `{b}`\n", encoding="utf-8")

    trace = tmp_path / "post-call-events.tsv"
    # One row names both records -> observed.
    trace.write_text(
        f"t\trun_completed\t{a},{b}\n",
        encoding="utf-8",
    )
    assert voice_postcall_effect_observed(
        f"record={a}; record={b}", tmp_path, now=when, trace_file=trace
    )

    # Split rows each name one record -> the group is not complete in any row.
    trace.write_text(
        f"t\trun_completed\t{a}\nt\trun_completed\t{b}\n",
        encoding="utf-8",
    )
    assert not voice_postcall_effect_observed(
        f"record={a}; record={b}", tmp_path, now=when, trace_file=trace
    )


def test_midnight_crossing_uses_session_start_date(tmp_path):
    """Journal written at 23:58 on day X must be found when run_post_session runs at 00:02 on day X+1.

    Without passing ``now=session_start``, voice_postcall_effect_observed falls
    back to the current time (day X+1) and looks in the wrong journal directory.
    """
    record = "/data/calls/one.wav"
    call_time = datetime(2026, 9, 6, 23, 58, 0, tzinfo=timezone.utc)
    next_day = datetime(2026, 9, 7, 0, 2, 0, tzinfo=timezone.utc)

    # Journal written by post-call.sh during the session (at call_time's date).
    journal = voice_postcall_journal_path(tmp_path, [record], call_time)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(f"**Archive**: `{record}`\n", encoding="utf-8")

    trace = tmp_path / "post-call-events.tsv"
    trace.write_text(
        f"2026-09-06T23:59:00Z\trun_completed\t{record}\n",
        encoding="utf-8",
    )

    # Passing the session start date finds the journal correctly.
    assert voice_postcall_effect_observed(
        f"record={record}", tmp_path, now=call_time, trace_file=trace
    )
    # Passing the next day's date misses the journal → False.
    assert not voice_postcall_effect_observed(
        f"record={record}", tmp_path, now=next_day, trace_file=trace
    )
