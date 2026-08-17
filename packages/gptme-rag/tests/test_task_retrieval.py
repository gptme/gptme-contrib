"""Tests for task-scoped TF-IDF retrieval (Phase 2.3)."""

from pathlib import Path

import pytest

from gptme_rag.indexing.document import Document
from gptme_rag.lexical import TfidfIndex

sklearn = pytest.importorskip("sklearn")

from gptme_rag.task_retrieval import (  # noqa: E402 — after importorskip guard
    TASK_RELEVANCE_FLOOR,
    TaskHit,
    apply_task_silence_rule,
    format_task_injection,
    load_task_documents,
    query_tasks,
    rank_tasks,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task_doc(
    content: str,
    source: str = "tasks/my-task.md",
    state: str = "active",
    title: str = "My Task",
) -> Document:
    return Document(
        content=content,
        metadata={
            "type": "task",
            "source": source,
            "title": title,
            "task_state": state,
            "task_archived": state in {"done", "cancelled", "archived"},
        },
        source_path=Path(source),
    )


def _build_index(docs: list[Document]) -> TfidfIndex:
    idx = TfidfIndex()
    idx.index(docs)
    return idx


# ---------------------------------------------------------------------------
# rank_tasks
# ---------------------------------------------------------------------------


class TestRankTasks:
    def test_returns_most_relevant_first(self):
        docs = [
            _task_doc(
                "Upstream gptme-rag retrieval into contrib package",
                source="tasks/rag.md",
                title="RAG upstream",
            ),
            _task_doc(
                "Write blog post about ActivityWatch releases",
                source="tasks/blog.md",
                title="Blog post",
            ),
            _task_doc(
                "Fix the gptme-rag TF-IDF backend sklearn import",
                source="tasks/fix.md",
                title="Fix lexical",
            ),
        ]
        idx = _build_index(docs)
        hits = rank_tasks(idx, "gptme-rag retrieval upstream contrib")
        assert hits, "Expected at least one hit"
        assert hits[0].path == "tasks/rag.md"

    def test_returns_task_hit_with_correct_fields(self):
        docs = [
            _task_doc(
                "Fix the broken pipeline",
                source="tasks/fix-pipeline.md",
                title="Fix Pipeline",
                state="active",
            )
        ]
        idx = _build_index(docs)
        hits = rank_tasks(idx, "fix pipeline", n_results=1)
        assert len(hits) == 1
        h = hits[0]
        assert isinstance(h, TaskHit)
        assert h.title == "Fix Pipeline"
        assert h.state == "active"
        assert not h.closed
        assert h.score > 0

    def test_closed_tasks_included_by_default(self):
        docs = [
            _task_doc("deploy server", source="tasks/deploy.md", state="done", title="Deploy"),
            _task_doc(
                "write tests for deploy", source="tasks/tests.md", state="active", title="Tests"
            ),
        ]
        idx = _build_index(docs)
        hits = rank_tasks(idx, "deploy", n_results=5)
        paths = [h.path for h in hits]
        assert "tasks/deploy.md" in paths

    def test_include_closed_false_filters_done_tasks(self):
        docs = [
            _task_doc("deploy server", source="tasks/deploy.md", state="done", title="Deploy Done"),
            _task_doc(
                "deploy monitoring setup",
                source="tasks/monitor.md",
                state="active",
                title="Monitor Deploy",
            ),
        ]
        idx = _build_index(docs)
        hits = rank_tasks(idx, "deploy", n_results=5, include_closed=False)
        for h in hits:
            assert not h.closed, f"Closed task should be filtered: {h.path}"

    def test_include_closed_false_returns_open_when_closed_dominate(self):
        # Regression for: early break in loop when closed tasks outnumber open
        # in the over-fetched set. All docs share the same query terms so scoring
        # is roughly equal — closed tasks must not crowd out the open ones.
        closed_docs = [
            _task_doc(
                "deploy server monitoring infrastructure",
                source=f"tasks/closed-{i}.md",
                state="done",
                title=f"Closed Deploy {i}",
            )
            for i in range(5)
        ]
        open_docs = [
            _task_doc(
                "deploy server monitoring infrastructure",
                source=f"tasks/open-{i}.md",
                state="active",
                title=f"Open Deploy {i}",
            )
            for i in range(3)
        ]
        idx = _build_index(closed_docs + open_docs)
        hits = rank_tasks(idx, "deploy server monitoring", n_results=3, include_closed=False)
        assert len(hits) == 3, f"Expected 3 open hits, got {len(hits)}"
        for h in hits:
            assert not h.closed, f"Closed task leaked through filter: {h.path}"

    def test_exclude_paths_skips_document(self):
        docs = [
            _task_doc("gptme retrieval task", source="tasks/a.md", title="Task A"),
            _task_doc("gptme retrieval task", source="tasks/b.md", title="Task B"),
        ]
        idx = _build_index(docs)
        hits = rank_tasks(idx, "gptme retrieval", n_results=5, exclude_paths={"tasks/a.md"})
        assert all(h.path != "tasks/a.md" for h in hits)

    def test_empty_query_returns_empty(self):
        docs = [_task_doc("some task content")]
        idx = _build_index(docs)
        assert rank_tasks(idx, "") == []
        assert rank_tasks(idx, "   ") == []

    def test_empty_index_returns_empty(self):
        idx = TfidfIndex()
        idx.index([])
        assert rank_tasks(idx, "any query") == []

    def test_n_results_caps_output(self):
        docs = [
            _task_doc(f"gptme task number {i}", source=f"tasks/task-{i}.md", title=f"Task {i}")
            for i in range(10)
        ]
        idx = _build_index(docs)
        hits = rank_tasks(idx, "gptme task", n_results=2)
        assert len(hits) <= 2

    def test_n_results_zero_returns_empty(self):
        docs = [_task_doc("gptme task", source="tasks/a.md", title="Task A")]
        idx = _build_index(docs)
        hits = rank_tasks(idx, "gptme task", n_results=0)
        assert hits == []

    def test_n_results_negative_returns_empty(self):
        docs = [_task_doc("gptme task", source="tasks/a.md", title="Task A")]
        idx = _build_index(docs)
        hits = rank_tasks(idx, "gptme task", n_results=-1)
        assert hits == []


# ---------------------------------------------------------------------------
# apply_task_silence_rule
# ---------------------------------------------------------------------------


class TestApplySilenceRule:
    def _hit(self, score: float, state: str = "active") -> TaskHit:
        doc = _task_doc("content", state=state)
        return TaskHit(document=doc, score=score, title="T", path="tasks/t.md", state=state)

    def test_returns_empty_when_top_below_floor(self):
        hits = [self._hit(0.10), self._hit(0.05)]
        assert apply_task_silence_rule(hits, floor=0.20) == []

    def test_returns_hits_when_top_above_floor(self):
        hits = [self._hit(0.50), self._hit(0.45)]
        result = apply_task_silence_rule(hits, floor=0.20)
        assert len(result) == 2

    def test_drops_trailing_hits_below_ratio(self):
        hits = [self._hit(0.50), self._hit(0.10)]
        # 0.10 / 0.50 = 0.20, trailing_ratio default 0.55 → drops 0.10
        result = apply_task_silence_rule(hits, floor=0.05, trailing_ratio=0.55)
        assert len(result) == 1
        assert result[0].score == 0.50

    def test_returns_empty_for_empty_input(self):
        assert apply_task_silence_rule([]) == []

    def test_default_floor_matches_constant(self):
        """Silence rule with one hit just below the default floor returns []."""
        hits = [self._hit(TASK_RELEVANCE_FLOOR - 0.01)]
        assert apply_task_silence_rule(hits) == []

    def test_single_hit_at_floor_survives(self):
        hits = [self._hit(TASK_RELEVANCE_FLOOR)]
        result = apply_task_silence_rule(hits)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# query_tasks
# ---------------------------------------------------------------------------


class TestQueryTasks:
    def test_returns_relevant_hits(self):
        docs = [
            _task_doc(
                "upstream gptme retrieval system to contrib",
                source="tasks/rag.md",
                title="RAG Upstream",
            ),
            _task_doc("weekly review of blog posts", source="tasks/blog.md", title="Blog Review"),
        ]
        idx = _build_index(docs)
        hits = query_tasks(idx, "upstream gptme retrieval")
        assert len(hits) >= 1
        assert hits[0].path == "tasks/rag.md"

    def test_returns_empty_for_irrelevant_query(self):
        docs = [_task_doc("utterly unrelated xyzzy content", source="tasks/x.md", title="X")]
        idx = _build_index(docs)
        # A very high floor means only perfect matches survive
        hits = query_tasks(idx, "pineapple pizza recipe", floor=0.99)
        assert hits == []


# ---------------------------------------------------------------------------
# format_task_injection
# ---------------------------------------------------------------------------


class TestFormatTaskInjection:
    def _hit(self, title: str, state: str, score: float, path: str) -> TaskHit:
        doc = _task_doc("", state=state, source=path, title=title)
        return TaskHit(document=doc, score=score, title=title, path=path, state=state)

    def test_returns_empty_string_for_no_hits(self):
        assert format_task_injection([]) == ""

    def test_output_contains_title_and_state(self):
        hits = [self._hit("Fix Pipeline", "active", 0.45, "tasks/fix.md")]
        out = format_task_injection(hits)
        assert "Fix Pipeline" in out
        assert "active" in out
        assert "open lane" in out
        assert "0.45" in out

    def test_closed_task_marked_already_attempted(self):
        hits = [self._hit("Old Feature", "done", 0.35, "tasks/archive/old.md")]
        out = format_task_injection(hits)
        assert "already attempted" in out

    def test_multiple_hits_all_appear(self):
        hits = [
            self._hit("Task A", "active", 0.60, "tasks/a.md"),
            self._hit("Task B", "waiting", 0.40, "tasks/b.md"),
        ]
        out = format_task_injection(hits)
        assert "Task A" in out
        assert "Task B" in out

    def test_output_has_header_and_footer(self):
        hits = [self._hit("T", "active", 0.40, "tasks/t.md")]
        out = format_task_injection(hits)
        assert "Possibly-Related Existing Tasks" in out
        assert "ignore this block" in out


# ---------------------------------------------------------------------------
# load_task_documents
# ---------------------------------------------------------------------------


class TestLoadTaskDocuments:
    def _write_task(self, path: Path, state: str = "active", title: str = "") -> None:
        heading = f"# {title}\n\n" if title else ""
        path.write_text(
            f"---\nstate: {state}\n---\n{heading}Task body text.\n",
            encoding="utf-8",
        )

    def test_loads_task_files(self, tmp_path: Path):
        self._write_task(tmp_path / "task-a.md", state="active", title="Task A")
        self._write_task(tmp_path / "task-b.md", state="done", title="Task B")
        docs = load_task_documents(tmp_path)
        assert len(docs) == 2
        states = {d.metadata["task_state"] for d in docs}
        assert states == {"active", "done"}

    def test_skips_template_dir(self, tmp_path: Path):
        tpl = tmp_path / "templates"
        tpl.mkdir()
        self._write_task(tpl / "default.md")
        self._write_task(tmp_path / "real-task.md", title="Real")
        docs = load_task_documents(tmp_path)
        sources = [d.metadata["source"] for d in docs]
        assert not any("templates" in s for s in sources)

    def test_archive_tasks_included_by_default(self, tmp_path: Path):
        arc = tmp_path / "archive"
        arc.mkdir()
        self._write_task(arc / "old.md", state="done", title="Old")
        self._write_task(tmp_path / "new.md", state="active", title="New")
        docs = load_task_documents(tmp_path)
        assert len(docs) == 2

    def test_archive_tasks_excluded_when_flagged(self, tmp_path: Path):
        arc = tmp_path / "archive"
        arc.mkdir()
        self._write_task(arc / "old.md", state="done", title="Old")
        self._write_task(tmp_path / "new.md", state="active", title="New")
        docs = load_task_documents(tmp_path, include_archived=False)
        assert len(docs) == 1
        assert docs[0].metadata["title"] == "New"

    def test_title_extracted_from_heading(self, tmp_path: Path):
        (tmp_path / "t.md").write_text("---\nstate: active\n---\n# My Great Task\n\nBody.\n")
        docs = load_task_documents(tmp_path)
        assert docs[0].metadata["title"] == "My Great Task"

    def test_title_falls_back_to_stem(self, tmp_path: Path):
        (tmp_path / "fix-pipeline-auth.md").write_text("---\nstate: active\n---\nBody only.\n")
        docs = load_task_documents(tmp_path)
        assert "Fix" in docs[0].metadata["title"] or "fix" in docs[0].metadata["title"].lower()

    def test_documents_have_task_type_metadata(self, tmp_path: Path):
        self._write_task(tmp_path / "t.md")
        docs = load_task_documents(tmp_path)
        assert all(d.metadata.get("type") == "task" for d in docs)

    def test_empty_dir_returns_empty_list(self, tmp_path: Path):
        assert load_task_documents(tmp_path) == []
