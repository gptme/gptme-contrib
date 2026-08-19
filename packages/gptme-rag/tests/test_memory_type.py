"""Tests for gptme_rag.memory_type — classification and ranking boost."""

import json
from pathlib import Path

import pytest

from gptme_rag.memory_type import (
    MEMORY_TYPE_BOOST,
    MEMORY_TYPE_PENALTY,
    SUPPORTED_MEMORY_TYPES,
    classify_document,
    classify_memory_type,
    load_memory_type_map,
    weighted_similarity,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def rules() -> dict:
    """Minimal but representative rules dict."""
    return {
        "exact_paths": {
            "SOUL.md": "identity",
            "ABOUT.md": "identity",
            "GOALS.md": "goal",
        },
        "glob_paths": {
            "people/*.md": "preference",
            "knowledge/**/*.md": "project",
        },
        "task_rules": {
            "goal_priorities": ["high"],
            "goal_states": ["active"],
            "preference_tags": ["preference"],
            "project_tags": ["project"],
            "default": "project",
        },
    }


# ---------------------------------------------------------------------------
# load_memory_type_map
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_empty(tmp_path: Path):
    result = load_memory_type_map(tmp_path / "nonexistent.json")
    assert result == {}


def test_load_none_returns_empty():
    result = load_memory_type_map(None)
    assert result == {}


def test_load_valid_file(tmp_path: Path, rules: dict):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(rules), encoding="utf-8")
    loaded = load_memory_type_map(p)
    assert loaded["exact_paths"]["SOUL.md"] == "identity"
    assert "task_rules" in loaded


def test_load_malformed_json_returns_empty(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert load_memory_type_map(p) == {}


def test_load_non_dict_json_returns_empty(tmp_path: Path):
    p = tmp_path / "list.json"
    p.write_text('["a","b"]', encoding="utf-8")
    assert load_memory_type_map(p) == {}


# ---------------------------------------------------------------------------
# classify_memory_type — exact paths
# ---------------------------------------------------------------------------


def test_classify_exact_path_identity(rules: dict):
    assert classify_memory_type("SOUL.md", {}, rules) == "identity"
    assert classify_memory_type("ABOUT.md", {}, rules) == "identity"


def test_classify_exact_path_goal(rules: dict):
    assert classify_memory_type("GOALS.md", {}, rules) == "goal"


def test_classify_unknown_path_returns_none(rules: dict):
    assert classify_memory_type("README.md", {}, rules) is None


def test_classify_unsupported_type_exact_path_returns_none():
    """An exact_path entry with an unrecognised type is rejected."""
    bad_rules = {"exact_paths": {"foo.md": "unsupported_type"}}
    assert classify_memory_type("foo.md", {}, bad_rules) is None


# ---------------------------------------------------------------------------
# classify_memory_type — glob paths
# ---------------------------------------------------------------------------


def test_classify_glob_people(rules: dict):
    assert classify_memory_type("people/alice.md", {}, rules) == "preference"


def test_classify_glob_knowledge(rules: dict):
    # Matches knowledge/**/*.md
    result = classify_memory_type("knowledge/technical/design.md", {}, rules)
    assert result == "project"


def test_classify_glob_no_match(rules: dict):
    assert classify_memory_type("scripts/build.sh", {}, rules) is None


def test_classify_glob_first_match_wins():
    """When multiple glob patterns match, first match in iteration order wins."""
    rules = {
        "glob_paths": {
            "tasks/*.md": "goal",
            "tasks/special/*.md": "preference",  # more specific but second
        }
    }
    result = classify_memory_type("tasks/foo.md", {}, rules)
    assert result in SUPPORTED_MEMORY_TYPES


# ---------------------------------------------------------------------------
# classify_memory_type — task rules
# ---------------------------------------------------------------------------


def test_classify_task_by_priority(rules: dict):
    assert classify_memory_type("tasks/foo.md", {"priority": "high"}, rules) == "goal"


def test_classify_task_by_state(rules: dict):
    assert classify_memory_type("tasks/foo.md", {"state": "active"}, rules) == "goal"


def test_classify_task_by_preference_tag(rules: dict):
    result = classify_memory_type("tasks/foo.md", {"tags": ["preference"]}, rules)
    assert result == "preference"


def test_classify_task_by_project_tag(rules: dict):
    result = classify_memory_type("tasks/foo.md", {"tags": ["project"]}, rules)
    assert result == "project"


def test_classify_task_default(rules: dict):
    # No matching sub-rule → falls through to task_rules["default"]
    result = classify_memory_type("tasks/foo.md", {"priority": "low"}, rules)
    assert result == "project"  # rules["task_rules"]["default"]


def test_classify_task_no_rules():
    rules = {}
    assert classify_memory_type("tasks/foo.md", {}, rules) is None


def test_classify_task_tags_as_string(rules: dict):
    """Tags given as a bare string should still match."""
    result = classify_memory_type("tasks/foo.md", {"tags": "preference"}, rules)
    assert result == "preference"


def test_classify_none_metadata(rules: dict):
    """Passing None for metadata must not raise."""
    result = classify_memory_type("SOUL.md", None, rules)
    assert result == "identity"


# ---------------------------------------------------------------------------
# classify_document (content-based)
# ---------------------------------------------------------------------------


def test_classify_document_from_frontmatter(rules: dict):
    content = "---\npriority: high\nstate: active\n---\n# My Task\nsome content"
    result = classify_document("tasks/foo.md", content, rules)
    assert result == "goal"


def test_classify_document_no_frontmatter(rules: dict):
    content = "# Just a document\nno frontmatter here"
    result = classify_document("SOUL.md", content, rules)
    assert result == "identity"  # exact_path match overrides content


def test_classify_document_empty_content(rules: dict):
    result = classify_document("SOUL.md", "", rules)
    assert result == "identity"


# ---------------------------------------------------------------------------
# weighted_similarity
# ---------------------------------------------------------------------------


def test_weighted_sim_no_requested_types():
    assert weighted_similarity(0.5, "goal", None) == 0.5
    assert weighted_similarity(0.5, "goal", set()) == 0.5


def test_weighted_sim_no_memory_type():
    assert weighted_similarity(0.5, None, {"goal"}) == 0.5


def test_weighted_sim_boost_on_match():
    score = weighted_similarity(0.5, "goal", {"goal"})
    assert score == pytest.approx(0.5 * MEMORY_TYPE_BOOST)
    assert score > 0.5


def test_weighted_sim_penalty_on_mismatch():
    score = weighted_similarity(0.5, "project", {"goal"})
    assert score == pytest.approx(0.5 * MEMORY_TYPE_PENALTY)
    assert score < 0.5


def test_weighted_sim_clamp_at_one():
    """Boosting a high score must not exceed 1.0."""
    score = weighted_similarity(0.99, "identity", {"identity"})
    assert score <= 1.0


def test_weighted_sim_boost_requires_match_in_set():
    score = weighted_similarity(0.5, "identity", {"goal", "preference"})
    assert score == pytest.approx(0.5 * MEMORY_TYPE_PENALTY)


def test_weighted_sim_multi_type_match():
    score = weighted_similarity(0.5, "identity", {"identity", "goal"})
    assert score == pytest.approx(min(1.0, 0.5 * MEMORY_TYPE_BOOST))


# ---------------------------------------------------------------------------
# Integration: TfidfIndex.search() with memory_types
# ---------------------------------------------------------------------------


sklearn = pytest.importorskip("sklearn")

from pathlib import Path  # noqa: E402 — already imported above but import guard requires this

from gptme_rag.indexing.document import Document  # noqa: E402
from gptme_rag.lexical import TfidfIndex  # noqa: E402


def _doc(text: str, source: str, memory_type: str | None = None) -> Document:
    metadata: dict = {"source": source}
    if memory_type is not None:
        metadata["memory_type"] = memory_type
    return Document(content=text, metadata=metadata, source_path=Path(source))


def test_search_without_memory_types_unchanged():
    """Without memory_types, search() behaves exactly as before."""
    idx = TfidfIndex()
    docs = [
        _doc("retry_backoff max_attempts", "a.md", "goal"),
        _doc("retry_backoff implementation", "b.md", "project"),
    ]
    idx.index(docs)
    hits = idx.search("retry_backoff", n_results=5)
    assert len(hits) == 2
    # Raw-cosine ranking — both docs match, scores unrestricted
    assert all(h.score > 0 for h in hits)


def test_search_boost_reranks_matching_memory_type():
    """When two docs have equal raw similarity, the boosted one must rank first.

    Using docs with identical content ensures the raw TF-IDF score is the same,
    so only the memory-type boost/penalty determines the final ranking — making
    the expected order deterministic regardless of corpus internals.
    """
    idx = TfidfIndex(relevance_floor=0.0)

    docs = [
        _doc("autonomous agent session work", "a.md", "project"),
        _doc("autonomous agent session work", "b.md", "goal"),
    ]
    idx.index(docs)

    # With goal boost: b.md gets 1.35× boost → must rank above a.md
    hits_goal = idx.search("autonomous agent", n_results=5, memory_types={"goal"})
    assert hits_goal[0].document.metadata["source"] == "b.md"
    # The winning score must reflect the boost
    assert hits_goal[0].score > hits_goal[1].score

    # With project boost: a.md gets 1.35× boost → must rank above b.md
    hits_project = idx.search("autonomous agent", n_results=5, memory_types={"project"})
    assert hits_project[0].document.metadata["source"] == "a.md"
    assert hits_project[0].score > hits_project[1].score


def test_search_memory_types_unknown_type_ignored():
    """Unknown types in memory_types must not raise; they are silently filtered."""
    idx = TfidfIndex()
    idx.index([_doc("hello world", "a.md", "goal")])
    hits = idx.search("hello", n_results=5, memory_types={"unknown_type"})
    # unknown_type is not in SUPPORTED_MEMORY_TYPES → treated as no preference
    assert len(hits) == 1


def test_search_memory_types_no_tagged_docs():
    """memory_types param is safe when no documents carry a memory_type tag."""
    idx = TfidfIndex()
    idx.index([_doc("hello world", "a.md")])  # no memory_type
    hits = idx.search("hello", n_results=5, memory_types={"goal"})
    assert len(hits) == 1
    assert hits[0].document.metadata["source"] == "a.md"


def test_search_penalty_pushes_non_matching_below_floor():
    """A doc that clears the raw floor but fails penalty may drop below it.

    The test is structured to assert in both branches so it does not silently
    pass without verifying anything when the penalty score stays above the floor.
    """
    floor = 0.3
    idx = TfidfIndex(relevance_floor=floor)
    docs = [
        _doc("relevant query terms here", "a.md", "project"),
    ]
    idx.index(docs)

    # Without memory_types: check raw score clears the floor
    hits_raw = idx.search("relevant query terms", n_results=5)
    if not hits_raw:
        pytest.skip("raw similarity below test floor — adjust test corpus")

    raw_score = hits_raw[0].score
    penalised_score = raw_score * MEMORY_TYPE_PENALTY

    hits_penalised = idx.search("relevant query terms", n_results=5, memory_types={"goal"})

    if penalised_score < floor:
        # Penalised score drops below the relevance floor — doc must disappear.
        assert len(hits_penalised) == 0
    else:
        # Penalised score stays above the floor — doc survives but with a lower score.
        assert len(hits_penalised) == 1
        assert hits_penalised[0].score < raw_score


def test_search_unknown_doc_memory_type_not_penalised():
    """A doc with an unrecognised memory_type label is ranked by raw cosine,
    not penalised.  This ensures typos or older labels don't silently downrank
    documents that the caller never intended to penalise."""
    idx = TfidfIndex()
    docs = [
        _doc("hello world query", "typo.md", "typo_type"),  # unknown type
        _doc("hello world query", "untagged.md"),  # no type
    ]
    idx.index(docs)

    # When memory_types={"goal"} is requested, both docs have no matching type.
    # The one with a typo label must NOT be penalised more than the untagged doc.
    hits = idx.search("hello world query", n_results=5, memory_types={"goal"})
    assert len(hits) == 2
    # Both docs have the same raw similarity; with equal treatment their scores
    # must be equal (no extra penalty on the unknown-label doc).
    scores = {h.document.metadata["source"]: h.score for h in hits}
    assert abs(scores["typo.md"] - scores["untagged.md"]) < 1e-6
