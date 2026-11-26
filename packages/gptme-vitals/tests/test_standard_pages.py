"""Native export contracts, honest unknowns and text-only private rendering."""

import copy
import html

import pytest
from gptme_vitals.fleet import CORE_SCHEMA
from gptme_vitals.standard_pages import render_standard_page
from gptme_vitals.theme import render_head_assets, render_theme_toggle


def _session(**values):
    fields = "start_time session_name model harness duration_seconds trajectory_grade"
    return dict.fromkeys(fields.split()) | {
        "session_id": "session-1",
        "outcome": "unknown",
        "timestamp": "NEVER-USE-RECORD-TIME",
        **values,
    }


def _tasks(**values):
    row = dict.fromkeys("priority assigned_to next_action waiting_for".split())
    row.update(id="task-slug", name="task-slug", state="todo", pool="general")
    return {"tasks": [row | values], "count": 1}


def _vitals(**values):
    return {
        "schema": CORE_SCHEMA,
        "agent": "bob",
        "status": "unknown",
        "generated_at": None,
        "activity": {"status": "no-data", "pages_unknown": 2},
        **values,
    }


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("sessions", [_session()]),
        ("tasks", _tasks()),
        ("vitals", _vitals()),
    ],
)
def test_shared_theme_and_no_input_mutation(kind, payload):
    before = copy.deepcopy(payload)
    page = render_standard_page(kind, payload)
    assert render_head_assets() in page and render_theme_toggle() in page
    assert 'name="viewport"' in page
    assert "Private export view" in page
    assert payload == before


def test_sessions_preserve_unknowns_and_ignore_unlisted_private_fields():
    page = render_standard_page(
        "sessions",
        [
            _session(
                cost_usd=99876,
                error="SECRET-ERROR",
                journal_path="SECRET-PATH",
                legacy="SECRET-LEGACY",
            )
        ],
    )
    assert "session-1" in page and "Unknown" in page
    assert "NEVER-USE-RECORD-TIME" not in page
    assert "SECRET" not in page and "99876" not in page
    assert "<td>0</td>" not in page
    page = render_standard_page(
        "sessions",
        [
            _session(
                start_time="2026-11-26T21:00:00Z",
                duration_seconds=0,
                trajectory_grade=0.0,
                model="gpt-test",
                harness="codex",
            )
        ],
    )
    assert "2026-11-26T21:00:00Z" in page and "gpt-test" in page and "codex" in page
    assert "<td>0</td>" in page and "<td>0.0</td>" in page


def test_task_slugs_and_blockers_are_rendered_without_body_or_readiness_guess():
    page = render_standard_page(
        "tasks",
        _tasks(
            next_action="Run probe",
            waiting_for="External review",
            title="SECRET-TITLE",
            body="SECRET-BODY",
            probe="SECRET-COMMAND",
        ),
    )
    assert "task-slug" in page and "Run probe" in page and "External review" in page
    assert "SECRET" not in page
    assert "State does not establish readiness" in page


def test_vitals_only_exposes_statuses_and_direct_finite_metrics():
    page = render_standard_page(
        "vitals",
        _vitals(
            detail={"secret": "SECRET-DETAIL"},
            activity={
                "status": "no-data",
                "pages_unknown": 2,
                "detail": {"secret": 99999},
                "notes": "SECRET-NOTES",
                "boolean": True,
            },
            services={"status": "degraded", "failed": 3},
        ),
    )
    assert "no-data" in page and "degraded" in page and "failed: 3" in page
    assert "pages_unknown: 2" in page
    assert "SECRET" not in page and "99999" not in page and "boolean" not in page


@pytest.mark.parametrize("kind", ["sessions", "tasks", "vitals"])
def test_all_displayed_strings_are_html_escaped(kind):
    attack = '"><img src=x onerror="bad()">&'
    fields = {
        "sessions": "session_id session_name model harness outcome start_time",
        "tasks": "id name state priority assigned_to pool next_action waiting_for",
        "vitals": "agent generated_at",
    }[kind]
    hostile = dict.fromkeys(fields.split(), attack)
    payload = {
        "sessions": [_session(**hostile)],
        "tasks": _tasks(**hostile),
        "vitals": _vitals(**hostile, activity={"status": "unknown", attack: 3}),
    }[kind]
    page = render_standard_page(kind, payload)
    assert attack not in page and html.escape(attack) in page
    assert "<img" not in page


@pytest.mark.parametrize(
    "kind,payload,empty",
    [
        ("sessions", [], "No sessions"),
        ("tasks", {"tasks": [], "count": 0}, "No tasks"),
        (
            "vitals",
            {"schema": CORE_SCHEMA, "agent": "bob", "status": "unknown"},
            "No dimensions",
        ),
    ],
)
def test_empty_exports_are_honest(kind, payload, empty):
    assert empty in render_standard_page(kind, payload)


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("bad", {}),
        ("sessions", {}),
        ("sessions", [None]),
        ("sessions", [{}]),
        ("tasks", []),
        ("tasks", {"tasks": [], "count": 1}),
        ("tasks", {"tasks": [None], "count": 1}),
        ("tasks", _tasks(id=None)),
        ("tasks", _tasks(next_action=[])),
        ("vitals", []),
        ("vitals", {"agent": "bob"}),
        ("vitals", _vitals(agent=None)),
        ("vitals", _vitals(status="critical")),
        ("vitals", _vitals(services=[])),
    ],
)
def test_malformed_exports_raise_readable_value_errors(kind, payload):
    with pytest.raises(ValueError) as error:
        render_standard_page(kind, payload)
    assert str(error.value)


@pytest.mark.parametrize("field", ["duration_seconds", "trajectory_grade"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "1", []])
def test_invalid_session_numeric_values_are_rejected(field, value):
    with pytest.raises(ValueError):
        render_standard_page("sessions", [_session(**{field: value})])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_vitals_metrics_are_rejected(value):
    with pytest.raises(ValueError):
        render_standard_page(
            "vitals", _vitals(activity={"status": "unknown", "count": value})
        )


def test_extensible_core_dimensions_are_rendered_without_scalar_metadata():
    page = render_standard_page(
        "vitals",
        _vitals(
            delivery={
                "status": "degraded",
                "pending": 4,
                "detail": {"secret": "PRIVATE"},
            },
            inference={"status": "unknown", "capacity": 2},
            metadata="SECRET-SCALAR",
        ),
    )
    assert "delivery" in page and "pending: 4" in page
    assert "inference" in page and "capacity: 2" in page
    assert "PRIVATE" not in page and "SECRET-SCALAR" not in page


def test_dimension_names_are_escaped():
    attack = '<img src=x onerror="bad()">'
    page = render_standard_page(
        "vitals", _vitals(**{attack: {"status": "unknown", "n": 1}})
    )
    assert attack not in page and html.escape(attack) in page


def test_status_shaped_tasks_export_needs_no_list_count():
    payload = _tasks()
    del payload["count"]
    payload["summary"] = {"total": 1, "by_state": {"todo": 1}}
    assert "task-slug" in render_standard_page("tasks", payload)


def test_reserved_root_detail_never_becomes_a_dimension():
    page = render_standard_page(
        "vitals",
        _vitals(
            detail={"status": "healthy", "numeric_secret": 246813579},
            delivery={"status": "degraded", "pending": 4},
        ),
    )
    assert "numeric_secret" not in page and "246813579" not in page
    assert "delivery" in page and "pending: 4" in page
