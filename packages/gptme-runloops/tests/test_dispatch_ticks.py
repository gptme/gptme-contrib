import json
from datetime import datetime, timedelta, timezone

from click.testing import CliRunner
from gptme_runloops.cli import main
from gptme_runloops.dispatch_ticks import (
    append_tick,
    bindable_coverage,
    read_ticks,
)

T0 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_coverage_targets_granted_not_requested(tmp_path):
    log = tmp_path / "ticks.jsonl"
    _write(
        log,
        [
            # Covers its own request (1) but not the 6 granted slots.
            {
                "ts": T0.isoformat(),
                "granted": 6,
                "requested": 1,
                "bindable": 2,
                "pacing": "behind",
            },
            {"ts": T0.isoformat(), "granted": 3, "bindable": 3, "pacing": "behind"},
        ],
    )
    cov = bindable_coverage(read_ticks(log))
    assert (cov.behind, cov.measured, cov.covered) == (2, 2, 1)
    assert cov.share == 0.5


def test_unjudgeable_ticks_are_counted_not_coerced(tmp_path):
    log = tmp_path / "ticks.jsonl"
    _write(
        log,
        [
            {"ts": T0.isoformat(), "granted": 3, "bindable": 5, "pacing": "on-pace"},
            {"ts": T0.isoformat(), "granted": 3, "bindable": 5, "pacing": None},
            {"ts": T0.isoformat(), "granted": 3, "bindable": 5, "pacing": "unknown"},
            # null bindable is "not computed", never 0
            {"ts": T0.isoformat(), "granted": 3, "bindable": None, "pacing": "behind"},
            {"ts": T0.isoformat(), "granted": 0, "bindable": 0, "pacing": "behind"},
            {"ts": T0.isoformat(), "granted": True, "bindable": 0, "pacing": "behind"},
        ],
    )
    cov = bindable_coverage(read_ticks(log))
    assert cov.ticks == 6
    assert cov.unknown_pacing == 2
    assert cov.behind == 3
    assert cov.unknown_bindable == 1
    assert cov.no_capacity == 2
    assert cov.measured == 0
    assert cov.share is None


def test_read_skips_malformed_and_filters_since(tmp_path):
    log = tmp_path / "ticks.jsonl"
    old = (T0 - timedelta(days=2)).isoformat()
    log.write_text(
        "not json\n"
        "[1, 2]\n"
        + json.dumps(
            {
                "ts": "2026-11-24T09:00:00",
                "granted": 1,
                "bindable": 1,
                "pacing": "behind",
            }
        )
        + "\n"
        + json.dumps({"ts": old, "granted": 1, "bindable": 1, "pacing": "behind"})
        + "\n"
        + json.dumps(
            {"ts": T0.isoformat(), "granted": 1, "bindable": 1, "pacing": "behind"}
        )
        + "\n"
    )
    assert len(list(read_ticks(log))) == 2  # naive ts dropped
    assert len(list(read_ticks(log, since=T0 - timedelta(hours=1)))) == 1


def test_append_round_trips_and_keeps_extra_fields(tmp_path):
    log = tmp_path / "nested" / "ticks.jsonl"
    row = append_tick(
        log, granted=4, bindable=4, pacing="behind", extra={"spawned": 4}, now=T0
    )
    assert json.loads(log.read_text()) == row
    assert row["spawned"] == 4
    cov = bindable_coverage(read_ticks(log))
    assert cov.share == 1.0


def test_cli_prints_coverage_json(tmp_path):
    log = tmp_path / "ticks.jsonl"
    append_tick(log, granted=2, bindable=1, pacing="behind")
    result = CliRunner().invoke(main, ["tick-coverage", str(log), "--since-hours", "1"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["measured"] == 1 and out["covered"] == 0 and out["share"] == 0.0
