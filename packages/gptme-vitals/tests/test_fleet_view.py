"""Private fleet cards: safe projections, honest freshness and strict inputs."""

import copy
import html
from datetime import datetime, timedelta, timezone

import pytest
from gptme_vitals.fleet import CORE_SCHEMA
from gptme_vitals.fleet_view import render_fleet
from gptme_vitals.theme import render_head_assets, render_theme_toggle

NOW = datetime(2026, 11, 26, 21, tzinfo=timezone.utc)


def core(**values):
    return {
        "schema": CORE_SCHEMA,
        "agent": "bob",
        "generated_at": NOW.isoformat(),
        "status": "healthy",
        "activity": {"status": "unknown", "sessions": 2},
        **values,
    }


def test_actual_responsive_cards_with_shared_theme_and_no_mutation():
    cores = [core(), core(agent="alice", status="no-data")]
    before = copy.deepcopy(cores)
    page = render_fleet(cores, now=NOW)
    assert page.count('<article class="fleet-card"') == 2
    assert 'class="fleet-grid"' in page
    assert "display:grid" in page and "repeat(auto-fit,minmax(" in page
    assert "<table" not in page
    assert render_head_assets() in page and render_theme_toggle() in page
    assert 'name="viewport"' in page
    assert "Private" in page and "alice" in page and "bob" in page
    assert "no-data" in page and "unknown" in page
    assert cores == before


def test_empty_is_honest_not_healthy():
    page = render_fleet([], now=NOW)
    assert "No agents" in page
    assert '<article class="fleet-card"' not in page
    assert 'data-status="healthy"' not in page


def test_only_safe_fields_are_shown_without_links_or_nested_details():
    page = render_fleet(
        [
            core(
                detail={"status": "healthy", "numeric_secret": 987654321},
                headline="SECRET-HEADLINE",
                url="https://secret.invalid/",
                activity={
                    "status": "unknown",
                    "sessions": 2,
                    "zero": 0,
                    "cost": 1.25,
                    "detail": 987654321,
                    "text": "SECRET-TEXT",
                    "flag": True,
                    "url": "https://secret.invalid/",
                    "nested": {"n": 987654321},
                    "list": [987654321],
                },
                delivery={"status": "degraded", "pending": 4},
            )
        ],
        now=NOW,
    )
    assert "sessions: 2" in page and "zero: 0" in page and "cost: 1.25" in page
    assert "delivery" in page and "pending: 4" in page
    assert "SECRET" not in page and "987654321" not in page
    assert "secret.invalid" not in page and "<a " not in page
    assert "nested" not in page and "flag" not in page


def test_agent_dimension_and_metric_names_are_escaped():
    attack = '"><img src=x onerror="bad()">&'
    page = render_fleet(
        [core(agent=attack, **{attack: {"status": "unknown", attack: 3}})], now=NOW
    )
    assert attack not in page and html.escape(attack) in page
    assert "<img" not in page


@pytest.mark.parametrize(
    "status", ["healthy", "degraded", "unknown", "no-data", "stale"]
)
def test_staleness_is_separate_and_effective_status_is_stale(status):
    stamp = (NOW - timedelta(seconds=901)).isoformat()
    page = render_fleet([core(status=status, generated_at=stamp)], now=NOW)
    assert 'data-status="stale"' in page
    assert f"Reported status: {status}" in page
    assert "Freshness: Stale" in page and "Effective status: " in page
    assert stamp in page


def test_down_is_not_hidden_by_staleness():
    page = render_fleet(
        [core(status="down", generated_at=(NOW - timedelta(days=2)).isoformat())],
        now=NOW,
    )
    assert 'data-status="down"' in page and "Freshness: Stale" in page


@pytest.mark.parametrize(
    "seconds,threshold,expected",
    [(900, 900, "healthy"), (901, 900, "stale"), (1, 0, "stale"), (5, 10, "healthy")],
)
def test_staleness_threshold(seconds, threshold, expected):
    page = render_fleet(
        [core(generated_at=(NOW - timedelta(seconds=seconds)).isoformat())],
        now=NOW,
        stale_after_seconds=threshold,
    )
    assert f'data-status="{expected}"' in page


@pytest.mark.parametrize("status,expected", [("healthy", "unknown"), ("down", "down")])
def test_future_over_sixty_seconds_is_unknown_unless_down(status, expected):
    page = render_fleet(
        [core(status=status, generated_at=(NOW + timedelta(seconds=61)).isoformat())],
        now=NOW,
    )
    assert f'data-status="{expected}"' in page
    assert "Freshness: Future timestamp" in page
    assert f"Reported status: {status}" in page


def test_sixty_second_clock_skew_is_tolerated_and_timezone_offsets_work():
    stamp = (
        (NOW + timedelta(seconds=60))
        .astimezone(timezone(timedelta(hours=2)))
        .isoformat()
    )
    page = render_fleet([core(generated_at=stamp)], now=NOW)
    assert 'data-status="healthy"' in page and "Freshness: Fresh" in page
    assert stamp in page
    assert "Freshness: Fresh" in render_fleet(
        [core(generated_at="2026-11-26T21:00:00Z")], now=NOW
    )


def test_default_now_is_aware():
    page = render_fleet([core(generated_at=datetime.now(timezone.utc).isoformat())])
    assert "Freshness: Fresh" in page


@pytest.mark.parametrize("value", [None, {}, "[]", [None], [[]], [{}]])
def test_invalid_container_and_core_raise(value):
    with pytest.raises(ValueError):
        render_fleet(value, now=NOW)


@pytest.mark.parametrize("field", ["schema", "agent", "generated_at", "status"])
def test_missing_required_fields_raise(field):
    value = core()
    del value[field]
    with pytest.raises(ValueError):
        render_fleet([value], now=NOW)


@pytest.mark.parametrize(
    "values",
    [
        {"schema": "fleet-vitals/headline@1"},
        {"agent": ""},
        {"agent": "  "},
        {"agent": None},
        {"agent": []},
        {"status": "critical"},
        {"status": None},
        {"status": []},
        {"activity": []},
        {"activity": {}},
        {"activity": {"status": "critical"}},
        {"activity": {"status": None}},
    ],
)
def test_invalid_fields_raise(values):
    with pytest.raises(ValueError):
        render_fleet([core(**values)], now=NOW)


@pytest.mark.parametrize(
    "stamp",
    [
        None,
        123,
        "",
        "yesterday",
        "2026-11-26",
        "2026-11-26T21:00:00",
        "2026-02-30T21:00:00Z",
        "2026-11-26T25:00:00Z",
        "<img src=x>",
    ],
)
def test_invalid_or_naive_generated_timestamps_raise(stamp):
    with pytest.raises(ValueError):
        render_fleet([core(generated_at=stamp)], now=NOW)


def test_duplicate_agents_are_rejected_even_after_trimming():
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        render_fleet([core(), core(agent="bob")], now=NOW)
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        render_fleet([core(), core(agent=" bob ")], now=NOW)


@pytest.mark.parametrize(
    "number", [float("nan"), float("inf"), -float("inf"), 10**1000]
)
def test_nonfinite_or_unrepresentable_metrics_raise(number):
    with pytest.raises(ValueError):
        render_fleet([core(activity={"status": "healthy", "count": number})], now=NOW)


@pytest.mark.parametrize(
    "threshold", [float("nan"), float("inf"), -1, True, "900", None, 10**1000]
)
def test_invalid_thresholds_raise(threshold):
    with pytest.raises(ValueError):
        render_fleet([], now=NOW, stale_after_seconds=threshold)


@pytest.mark.parametrize("now", [datetime(2026, 11, 26), "2026-11-26T21:00:00Z", 0])
def test_invalid_now_raises(now):
    with pytest.raises(ValueError):
        render_fleet([], now=now)


def test_malformed_dimension_or_metric_names_raise():
    for value in [
        core(**{"": {"status": "healthy"}}),
        core(activity={"status": "healthy", 1: 2}),
    ]:
        with pytest.raises(ValueError):
            render_fleet([value], now=NOW)


def test_all_cores_validated_not_just_first():
    with pytest.raises(ValueError):
        render_fleet([core(), core(agent="alice", generated_at="bad")], now=NOW)


def fleet_wrapper(**values):
    return {
        "schema": "fleet-vitals/fleet@1",
        "generated_at": NOW.isoformat(),
        "fleet_status": "down",
        "agents": [
            {
                "agent": "bob",
                "reachable": True,
                "self_status": "healthy",
                "observed_status": "down",
                "effective_status": "down",
                "observed_reasons": ["SECRET-REASON"],
                "age_seconds": 0,
                "core": core(),
                "headline": {"title": "SECRET-HEADLINE"},
                "probe": {"error": "SECRET-PROBE", "numeric_secret": 987654321},
            }
        ],
        **values,
    }


def test_fleet_wrapper_distinguishes_self_and_observed_and_excludes_details():
    payload = fleet_wrapper()
    before = copy.deepcopy(payload)
    page = render_fleet(payload, now=NOW)
    assert 'data-status="down"' in page
    assert "Reported status: healthy" in page
    assert "Observed status: down" in page and "Reachable: yes" in page
    assert "Freshness: Fresh" in page
    assert "SECRET" not in page and "987654321" not in page
    assert payload == before


def test_unreachable_wrapper_cannot_look_healthy_even_with_missing_core():
    for native in [core(), None]:
        payload = fleet_wrapper(
            agents=[
                {
                    "agent": "bob",
                    "reachable": False,
                    "core": native,
                    "self_status": "healthy",
                    "observed_status": "healthy",
                    "effective_status": "healthy",
                }
            ]
        )
        page = render_fleet(payload, now=NOW)
        assert 'data-status="down"' in page
        assert "Reachable: no" in page
        if native is None:
            assert "No core export" in page


def test_wrapper_effective_down_survives_stale_core():
    payload = fleet_wrapper()
    row = payload["agents"][0]
    row["core"]["generated_at"] = (NOW - timedelta(days=1)).isoformat()
    row["observed_status"] = "unknown"
    page = render_fleet(payload, now=NOW)
    assert 'data-status="down"' in page and "Freshness: Stale" in page


@pytest.mark.parametrize(
    "change",
    [
        {"reachable": "yes"},
        {"observed_status": "critical"},
        {"effective_status": "critical"},
        {"self_status": "critical"},
        {"agent": "alice"},
    ],
)
def test_malformed_wrapper_rows_raise(change):
    payload = fleet_wrapper()
    payload["agents"][0].update(change)
    with pytest.raises(ValueError):
        render_fleet(payload, now=NOW)


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": "wrong", "agents": []},
        {
            "schema": "fleet-vitals/fleet@1",
            "generated_at": NOW.isoformat(),
            "agents": {},
        },
        fleet_wrapper(generated_at="bad"),
    ],
)
def test_malformed_wrapper_raises(payload):
    with pytest.raises(ValueError):
        render_fleet(payload, now=NOW)


def test_empty_wrapper_is_honest():
    assert "No agents" in render_fleet(fleet_wrapper(agents=[]), now=NOW)


@pytest.mark.parametrize("missing", [None, {}])
def test_reachable_wrapper_without_core_stays_unknown(missing):
    payload = fleet_wrapper(
        agents=[
            {
                "agent": "gordon",
                "reachable": True,
                "self_status": "no-data",
                "observed_status": "healthy",
                "effective_status": "healthy",
                "core": missing,
            }
        ]
    )
    page = render_fleet(payload, now=NOW)
    assert 'data-status="unknown"' in page
    assert "Freshness: Unknown (no core export)" in page
    assert "Self status: no-data" in page and "Observed status: healthy" in page


@pytest.mark.parametrize("malformed", [{"status": "healthy"}, "", [], False])
def test_nonempty_malformed_core_never_treated_as_missing(malformed):
    payload = fleet_wrapper()
    payload["agents"][0]["core"] = malformed
    with pytest.raises(ValueError):
        render_fleet(payload, now=NOW)


def placeholder_wrapper():
    payload = fleet_wrapper()
    row = payload["agents"][0]
    row.update(
        self_report_missing=True,
        self_status="no-data",
        observed_status="healthy",
        effective_status="healthy",
        core={"agent": "bob", "status": "no-data", "generated_at": None},
    )
    return payload


def test_native_explicit_missing_self_report_placeholder_is_absent_not_fresh():
    page = render_fleet(placeholder_wrapper(), now=NOW)
    assert 'data-status="unknown"' in page
    assert "No core export" in page
    assert "Freshness: Unknown (no core export)" in page
    assert "Freshness: Fresh" not in page and "<time>" not in page
    assert "Self status: no-data" in page and "Observed status: healthy" in page


@pytest.mark.parametrize("flag", [False, None, 1, "true"])
def test_placeholder_requires_exact_true_missing_flag(flag):
    payload = placeholder_wrapper()
    payload["agents"][0]["self_report_missing"] = flag
    with pytest.raises(ValueError):
        render_fleet(payload, now=NOW)


@pytest.mark.parametrize(
    "change",
    [
        {"agent": "alice"},
        {"status": "healthy"},
        {"generated_at": NOW.isoformat()},
        {"extra": 1},
    ],
)
def test_placeholder_rejects_spoofed_or_inexact_shape(change):
    payload = placeholder_wrapper()
    payload["agents"][0]["core"].update(change)
    with pytest.raises(ValueError):
        render_fleet(payload, now=NOW)


def test_standalone_placeholder_is_not_a_native_core():
    with pytest.raises(ValueError):
        render_fleet([placeholder_wrapper()["agents"][0]["core"]], now=NOW)
