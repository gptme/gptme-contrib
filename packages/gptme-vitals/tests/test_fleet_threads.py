"""Join only explicit session identities; no cross-agent evidence inference."""

import copy

import pytest
from gptme_vitals.thread_view import join_thread_exports, render_threads


def session(sid, agent, start, **extra):
    return {
        "id": sid,
        "agent": agent,
        "start": start,
        "end": start + 10,
        "thread": sid,
        "predecessor": None,
        "title": sid,
        **extra,
    }


def test_join_resolves_explicit_cross_agent_predecessor_without_mutation():
    exports = [
        {
            "sessions": [
                session(
                    "alice-child",
                    "alice",
                    30,
                    predecessor="bob-root",
                    thread="bob-root",
                    kind="parent",
                    key="bob-root",
                    weight=1,
                )
            ],
            "excerpt": "PRIVATE EXTRA",
        },
        {"sessions": [session("bob-root", "bob", 10)]},
    ]
    before = copy.deepcopy(exports)
    joined = join_thread_exports(exports)
    assert exports == before
    assert [r["id"] for r in joined["sessions"]] == ["bob-root", "alice-child"]
    assert {r["thread"] for r in joined["sessions"]} == {"bob-root"}
    assert joined["stats"] == {"sessions": 2, "linked": 1, "threads": 1}
    pages = render_threads(joined)
    assert all("Outside export" not in p for p in pages)
    assert all("PRIVATE EXTRA" not in p for p in pages)
    assert 'href="#session-0"' in pages[1]


def test_join_recomputes_root_when_formerly_external_predecessor_is_now_known():
    joined = join_thread_exports(
        [
            {
                "sessions": [
                    session("root", "bob", 1),
                    session("middle", "bob", 20, predecessor="root", thread="root"),
                ]
            },
            {
                "sessions": [
                    session("child", "alice", 40, predecessor="middle", thread="middle")
                ]
            },
        ]
    )
    assert [r["thread"] for r in joined["sessions"]] == ["root"] * 3


def test_join_deduplicates_identical_overlap_and_ignores_extra_fields():
    row = session("same", "bob", 1)
    joined = join_thread_exports(
        [{"sessions": [row]}, {"sessions": [{**row, "excerpt": "HIDDEN"}]}]
    )
    assert len(joined["sessions"]) == 1
    assert "HIDDEN" not in str(joined)


@pytest.mark.parametrize(
    "changes",
    [{"agent": "alice"}, {"title": "different"}, {"start": 2}, {"thread": "other"}],
)
def test_join_rejects_conflicting_ids_instead_of_guessing(changes):
    row = session("collision", "bob", 1)
    with pytest.raises(ValueError, match="conflicting session id"):
        join_thread_exports([{"sessions": [row]}, {"sessions": [{**row, **changes}]}])


def test_join_keeps_dangling_reference_honest_and_does_not_infer_from_titles():
    joined = join_thread_exports(
        [
            {"sessions": [session("bob", "bob", 1, title="same task")]},
            {
                "sessions": [
                    session(
                        "alice",
                        "alice",
                        20,
                        title="same task",
                        predecessor="missing",
                        thread="missing",
                    )
                ]
            },
        ]
    )
    assert joined["sessions"][1]["predecessor"] == "missing"
    assert "Outside export" in render_threads(joined)[1]
    assert joined["stats"]["threads"] == 2


def test_join_rejects_cross_export_time_inversion():
    with pytest.raises(ValueError, match="predecessor must be earlier"):
        join_thread_exports(
            [
                {"sessions": [session("child", "alice", 1, predecessor="parent")]},
                {"sessions": [session("parent", "bob", 20)]},
            ]
        )


def test_join_empty_and_invalid_exports():
    assert join_thread_exports([]) == {
        "sessions": [],
        "stats": {"sessions": 0, "linked": 0, "threads": 0},
    }
    with pytest.raises(ValueError):
        join_thread_exports([{"sessions": []}, {"sessions": "bad"}])


def test_cli_multiple_inputs_join_only_in_private_root(tmp_path):
    import json
    import stat

    from gptme_vitals.cli import main

    first, second = tmp_path / "a.json", tmp_path / "b.json"
    first.write_text(json.dumps({"sessions": [session("root", "bob", 1)]}))
    second.write_text(
        json.dumps(
            {
                "sessions": [
                    session("child", "alice", 20, predecessor="root", thread="root")
                ]
            }
        )
    )
    private, public = tmp_path / "private", tmp_path / "public"
    assert (
        main(
            [
                "threads",
                "--input",
                str(first),
                "--input",
                str(second),
                "--private-root",
                str(private),
                "--public-root",
                str(public),
            ]
        )
        == 0
    )
    result = private / "threads" / "log.html"
    assert "bob" in result.read_text() and "alice" in result.read_text()
    assert "Outside export" not in result.read_text()
    assert stat.S_IMODE(result.stat().st_mode) == 0o600
    assert not public.exists()


def test_cli_conflicting_join_is_usage_error_without_partial_output(tmp_path):
    import json

    from gptme_vitals.cli import main

    first, second = tmp_path / "a.json", tmp_path / "b.json"
    first.write_text(json.dumps({"sessions": [session("collision", "bob", 1)]}))
    second.write_text(json.dumps({"sessions": [session("collision", "alice", 1)]}))
    private, public = tmp_path / "private", tmp_path / "public"
    assert (
        main(
            [
                "threads",
                "--input",
                str(first),
                "--input",
                str(second),
                "--private-root",
                str(private),
                "--public-root",
                str(public),
            ]
        )
        == 2
    )
    assert not private.exists()


def test_join_groups_only_from_predecessors_not_stale_source_thread_labels():
    joined = join_thread_exports(
        [
            {"sessions": [session("a", "bob", 1, thread="shared")]},
            {"sessions": [session("b", "alice", 20, thread="shared")]},
        ]
    )
    assert [r["thread"] for r in joined["sessions"]] == ["a", "b"]
    assert joined["stats"]["threads"] == 2
    joined = join_thread_exports(
        [
            {"sessions": [session("a", "bob", 1, predecessor="missing", thread="x")]},
            {
                "sessions": [
                    session("b", "alice", 20, predecessor="missing", thread="y")
                ]
            },
        ]
    )
    assert [r["thread"] for r in joined["sessions"]] == ["missing", "missing"]
