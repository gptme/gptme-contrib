"""Private thread pages consume the existing export without exposing extra text."""

from __future__ import annotations

import copy
import html
import json
import os
import re
import stat
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from gptme_vitals.cli import main
from gptme_vitals.theme import render_head_assets, render_theme_toggle
from gptme_vitals.thread_view import render_threads, write_threads


class _Page(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__()
        self.text: list[str] = []
        self.links: list[str] = []
        self.attributes: list[tuple[str, str | None]] = []
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.attributes.extend(attrs)
        if tag == "a":
            self.links.extend(
                value for name, value in attrs if name == "href" and value
            )

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _session(sid: str, start: float, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": sid,
        "agent": "bob",
        "start": start,
        "end": start + 30,
        "title": f"Title {sid}",
        "predecessor": None,
        "kind": None,
        "key": None,
        "weight": None,
        "thread": sid,
    }
    row.update(overrides)
    return row


def _payload() -> dict[str, Any]:
    return {
        # Deliberately unsorted, including an equal-start tie across roots.
        "sessions": [
            _session(
                "child",
                120,
                title="Later child",
                predecessor="root-a",
                kind="task",
                key="task:threads",
                weight=0.8,
                thread="root-a",
            ),
            _session("root-z", 60, title="Second root"),
            _session("root-a", 60, title="First root"),
        ],
        "stats": {"sessions": 3, "linked": 1, "threads": 2},
    }


def test_render_uses_shared_theme_and_preserves_input() -> None:
    payload = _payload()
    before = copy.deepcopy(payload)
    pages = render_threads(payload)
    assert isinstance(pages, tuple) and len(pages) == 2
    for page in pages:
        assert render_head_assets() in page
        assert render_theme_toggle() in page
        assert all(
            title in page for title in ("First root", "Second root", "Later child")
        )
    assert payload == before


def test_log_is_chronological_with_id_ties_and_map_keeps_thread_evidence() -> None:
    map_page, log_page = render_threads(_payload())
    assert log_page.index("First root") < log_page.index("Second root")
    assert log_page.index("Second root") < log_page.index("Later child")
    for page in (map_page, log_page):
        text = " ".join(_Page(page).text)
        assert "task:threads" in text
        assert "task" in text
        assert "0.8" in text
        assert "No predecessor recorded" in text


def test_empty_export_is_honest_and_nullable_title_is_supported() -> None:
    for page in render_threads({"sessions": [], "stats": {"sessions": 0}}):
        assert "no sessions" in " ".join(_Page(page).text).lower()
    for page in render_threads(
        {"sessions": [_session("untitled", 1, title=None)], "stats": {}}
    ):
        assert "untitled" in page


def test_outside_export_ancestry_is_preserved_without_guessing() -> None:
    row = _session(
        "included",
        20,
        predecessor="missing-parent",
        thread="missing-root",
        kind="mention",
        key="session:missing-parent",
        weight=0.95,
    )
    for page in render_threads({"sessions": [row], "stats": {}}):
        text = " ".join(_Page(page).text)
        assert "missing-parent" in text
        assert "missing-root" in text
        assert "outside export" in text.lower()


def test_untrusted_text_is_escaped_and_links_use_generated_numeric_anchors() -> None:
    attack = '"><img src=x onerror="alert(1)"><script>alert(2)</script>&'
    root = _session(attack, 10, agent=attack, title=attack)
    child = _session(
        "child",
        20,
        predecessor=attack,
        thread=attack,
        kind="task",
        key=attack,
        weight=0.8,
        journal="NEVER-RENDER-JOURNAL-EXCERPT",
    )
    payload = {
        "sessions": [root, child],
        "stats": {"excerpt": "NEVER-RENDER-STATS-TEXT"},
    }
    for page in render_threads(payload):
        parsed = _Page(page)
        assert attack not in page
        assert html.escape(attack) in page
        assert attack in " ".join(parsed.text)
        assert "NEVER-RENDER" not in page
        assert not any(name.lower().startswith("on") for name, _ in parsed.attributes)
        fragments = [link.split("#", 1)[1] for link in parsed.links if "#" in link]
        assert fragments, "the thread pages need navigable session links"
        assert all(re.fullmatch(r"[A-Za-z_-]*\d+", fragment) for fragment in fragments)
        assert all("javascript:" not in link.lower() for link in parsed.links)


@pytest.mark.parametrize(
    "payload", [None, [], {}, {"sessions": {}}, {"sessions": [None], "stats": {}}]
)
def test_invalid_payload_shape_is_a_readable_value_error(payload: Any) -> None:
    with pytest.raises(ValueError) as error:
        render_threads(payload)
    assert str(error.value).strip()


@pytest.mark.parametrize("field", ["id", "agent", "start", "end", "thread"])
def test_missing_required_session_field_is_rejected(field: str) -> None:
    payload = _payload()
    del payload["sessions"][0][field]
    with pytest.raises(ValueError) as error:
        render_threads(payload)
    assert field in str(error.value)


@pytest.mark.parametrize("field", ["start", "end"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_timestamps_are_rejected(field: str, value: float) -> None:
    payload = _payload()
    payload["sessions"][0][field] = value
    with pytest.raises(ValueError):
        render_threads(payload)


def test_duplicate_ids_and_reversed_interval_are_rejected() -> None:
    payload = _payload()
    payload["sessions"].append(dict(payload["sessions"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        render_threads(payload)
    payload = _payload()
    payload["sessions"][0]["end"] = payload["sessions"][0]["start"] - 1
    with pytest.raises(ValueError, match="end"):
        render_threads(payload)


def test_private_write_is_atomic_repairs_permissions_and_leaves_public_tree_alone(
    tmp_path: Path,
) -> None:
    private, public = tmp_path / "private", tmp_path / "public"
    public.mkdir()
    sentinel = public / "index.html"
    sentinel.write_text("public page")
    sentinel.chmod(0o644)
    output = private / "threads"
    output.mkdir(parents=True)
    old_inodes = {}
    for name in ("index.html", "log.html"):
        path = output / name
        path.write_text("previous complete page")
        path.chmod(0o644)
        old_inodes[name] = path.stat().st_ino
    expected = render_threads(_payload())
    written = write_threads(_payload(), private_root=private, public_root=public)
    assert written == (output / "index.html", output / "log.html")
    for path, page in zip(written, expected):
        assert path.read_text() == page
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_ino != old_inodes[path.name]
    assert sorted(path.name for path in output.iterdir()) == ["index.html", "log.html"]
    assert list(public.iterdir()) == [sentinel]
    assert sentinel.read_text() == "public page"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o644


def test_invalid_input_does_not_partially_replace_existing_pages(
    tmp_path: Path,
) -> None:
    private, public = tmp_path / "private", tmp_path / "public"
    output = private / "threads"
    output.mkdir(parents=True)
    for name in ("index.html", "log.html"):
        (output / name).write_text("old page")
    payload = _payload()
    payload["sessions"][-1]["end"] = -1
    with pytest.raises(ValueError):
        write_threads(payload, private_root=private, public_root=public)
    assert {path.name: path.read_text() for path in output.iterdir()} == {
        "index.html": "old page",
        "log.html": "old page",
    }
    assert not public.exists()


@pytest.mark.parametrize(
    "kind", ["equal", "private-inside-public", "public-inside-private"]
)
def test_overlapping_roots_are_rejected_before_writing(
    tmp_path: Path, kind: str
) -> None:
    private, public = tmp_path / "private", tmp_path / "public"
    if kind == "equal":
        public = private
    elif kind == "private-inside-public":
        private = public / "private"
    else:
        public = private / "public"
    with pytest.raises(ValueError, match="overlap"):
        write_threads(_payload(), private_root=private, public_root=public)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("alias", ["directory", "index.html", "log.html", "served"])
def test_symlink_aliases_are_refused_before_any_page_is_written(
    tmp_path: Path, alias: str
) -> None:
    private, public = tmp_path / "private", tmp_path / "public"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("untouched")
    private.mkdir()
    if alias == "directory":
        (private / "threads").symlink_to(outside, target_is_directory=True)
    elif alias == "served":
        public.mkdir()
        (public / "alias").symlink_to(private, target_is_directory=True)
    else:
        (private / "threads").mkdir()
        (private / "threads" / alias).symlink_to(sentinel)
    with pytest.raises(ValueError, match="symlink"):
        write_threads(_payload(), private_root=private, public_root=public)
    assert sentinel.read_text() == "untouched"
    assert list(outside.iterdir()) == [sentinel]
    assert not any(
        path.is_file() and not path.is_symlink() for path in private.rglob("*.html")
    )


def test_replace_failure_keeps_the_previous_file_and_cleans_temporary_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private, public = tmp_path / "private", tmp_path / "public"
    output = private / "threads"
    output.mkdir(parents=True)
    target = output / "index.html"
    target.write_text("old complete page")

    def fail_replace(source: Any, destination: Any) -> None:
        assert Path(destination) == target
        assert Path(source).read_text() != "old complete page"
        assert target.read_text() == "old complete page"
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        write_threads(_payload(), private_root=private, public_root=public)
    assert list(output.iterdir()) == [target]
    assert target.read_text() == "old complete page"
    assert not public.exists()


def _args(source: Path, private: Path, public: Path) -> list[str]:
    return [
        "threads",
        "--input",
        str(source),
        "--private-root",
        str(private),
        "--public-root",
        str(public),
    ]


def test_threads_cli_needs_no_registry_and_writes_private_pages(tmp_path: Path) -> None:
    source, private, public = (
        tmp_path / "input.json",
        tmp_path / "private",
        tmp_path / "public",
    )
    source.write_text(json.dumps(_payload()))
    assert main(_args(source, private, public)) == 0
    assert "First root" in (private / "threads/index.html").read_text()
    assert "Later child" in (private / "threads/log.html").read_text()
    assert not public.exists()


@pytest.mark.parametrize("body", ["{broken", "[]", '{"sessions": [], "stats": []}'])
def test_threads_cli_bad_input_returns_usage_error_without_writes(
    tmp_path: Path, body: str
) -> None:
    source, private, public = (
        tmp_path / "input.json",
        tmp_path / "private",
        tmp_path / "public",
    )
    source.write_text(body)
    assert main(_args(source, private, public)) == 2
    assert not private.exists() and not public.exists()


def test_threads_cli_unsafe_roots_return_usage_error(tmp_path: Path) -> None:
    source, private = tmp_path / "input.json", tmp_path / "private"
    source.write_text(json.dumps(_payload()))
    assert main(_args(source, private, private / "served")) == 2
    assert not private.exists()


def test_threads_cli_input_io_error_returns_one(tmp_path: Path) -> None:
    assert (
        main(_args(tmp_path / "absent.json", tmp_path / "private", tmp_path / "public"))
        == 1
    )
