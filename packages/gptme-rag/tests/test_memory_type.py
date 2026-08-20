"""Tests for gptme_rag.memory_type — classification and ranking boost."""

import json
from pathlib import Path

import pytest

from gptme_rag.memory_type import (
    MEMORY_TYPE_BOOST,
    MEMORY_TYPE_PENALTY,
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


def test_classify_unsupported_exact_path_type_falls_through_to_glob():
    """A typo in exact_paths must not block a matching glob rule.

    When exact_paths maps a path to an unsupported type (e.g. a typo like
    ``"identiy"``) the function must fall through to glob_paths and return the
    valid type found there, rather than short-circuiting to ``None``.
    """
    rules = {
        "exact_paths": {"people/alice.md": "identiy"},  # typo
        "glob_paths": {"people/*.md": "preference"},
    }
    assert classify_memory_type("people/alice.md", {}, rules) == "preference"


# ---------------------------------------------------------------------------
# classify_memory_type — glob paths
# ---------------------------------------------------------------------------


def test_classify_glob_people(rules: dict):
    assert classify_memory_type("people/alice.md", {}, rules) == "preference"


def test_classify_glob_knowledge(rules: dict):
    # Matches knowledge/**/*.md with one intermediate directory
    result = classify_memory_type("knowledge/technical/design.md", {}, rules)
    assert result == "project"


def test_classify_glob_knowledge_direct_child(rules: dict):
    """Files directly under knowledge/ must match knowledge/**/*.md.

    fnmatch does not support ** as a zero-or-more-directories wildcard, so
    'knowledge/**/*.md' would not match 'knowledge/design.md' with plain
    fnmatch. The _glob_match_path helper is required.
    """
    result = classify_memory_type("knowledge/design.md", {}, rules)
    assert result == "project"


def test_classify_glob_no_match(rules: dict):
    assert classify_memory_type("scripts/build.sh", {}, rules) is None


def test_classify_glob_first_match_wins():
    """When multiple glob patterns match, first match in iteration order wins.

    Uses two overlapping patterns — tasks/*.md and tasks/f*.md — that both
    match 'tasks/foo.md', so the assertion can pin the exact expected type.
    """
    rules = {
        "glob_paths": {
            "tasks/*.md": "goal",
            "tasks/f*.md": "preference",  # also matches tasks/foo.md, but second
        }
    }
    result = classify_memory_type("tasks/foo.md", {}, rules)
    assert result == "goal"  # first pattern in dict wins


def test_classify_malformed_exact_paths_list():
    """Malformed rules where exact_paths is a list must not raise TypeError."""
    rules = {"exact_paths": ["SOUL.md", "ABOUT.md"]}
    # Should not raise; falls through to glob/task rules and returns None
    assert classify_memory_type("SOUL.md", {}, rules) is None


def test_classify_malformed_glob_paths_list():
    """Malformed rules where glob_paths is a list must not raise TypeError."""
    rules = {"glob_paths": ["tasks/*.md"]}
    assert classify_memory_type("tasks/foo.md", {}, rules) is None


def test_classify_malformed_task_rules_list():
    """Malformed rules where task_rules is a list must not raise TypeError."""
    rules = {"task_rules": ["goal_priorities"]}
    assert classify_memory_type("tasks/foo.md", {"priority": "high"}, rules) is None


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


def test_classify_task_tags_comma_separated_string(rules: dict):
    """Tags given as a comma-separated string must be split on commas.

    YAML sometimes leaves 'tags: preference, project' as a single string
    rather than a list.  _coerce_string_list must split on commas.
    """
    result = classify_memory_type("tasks/foo.md", {"tags": "preference, project"}, rules)
    assert result == "preference"


def test_classify_task_tags_list_with_comma_separated_item(rules: dict):
    """A YAML list item that is itself comma-separated must be split.

    YAML '- preference, project' parses to the list ['preference, project'];
    the list branch of _coerce_string_list must also split on commas.
    """
    result = classify_memory_type("tasks/foo.md", {"tags": ["preference, project"]}, rules)
    assert result == "preference"


def test_classify_task_null_rule_value():
    """JSON null values in task_rules must not raise TypeError.

    `{"goal_priorities": null}` is valid JSON; task_rules.get() returns None,
    and `priority in None` would raise TypeError without the `or []` guard.
    """
    rules = {
        "task_rules": {
            "goal_priorities": None,  # null in JSON
            "goal_states": None,
            "preference_tags": None,
            "project_tags": None,
            "default": "project",
        }
    }
    result = classify_memory_type("tasks/foo.md", {"priority": "high"}, rules)
    assert result == "project"  # falls through to default, no TypeError


def test_classify_none_metadata(rules: dict):
    """Passing None for metadata must not raise."""
    result = classify_memory_type("SOUL.md", None, rules)
    assert result == "identity"


# ---------------------------------------------------------------------------
# classify_document (content-based)
# ---------------------------------------------------------------------------


def test_classify_document_frontmatter_no_trailing_newline(rules: dict):
    """Frontmatter blocks without a trailing newline after closing --- must parse.

    _extract_frontmatter previously required '\\n---\\n' and would return {}
    for files saved without a final newline (ending with '\\n---').
    """
    content = "---\npriority: high\nstate: active\n---"  # no trailing newline
    result = classify_document("tasks/foo.md", content, rules)
    assert result == "goal"


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


# Guard sklearn-dependent imports so pure-Python tests above are not skipped
# when scikit-learn is absent.  Only the TfidfIndex integration tests need it.
try:
    from gptme_rag.lexical import TfidfIndex

    _SKLEARN_AVAILABLE = True
except ImportError:
    TfidfIndex = None  # type: ignore[assignment,misc]
    _SKLEARN_AVAILABLE = False

from gptme_rag.indexing.document import Document


@pytest.fixture()
def require_sklearn():
    """Skip the calling test if scikit-learn (and thus TfidfIndex) is not installed."""
    if not _SKLEARN_AVAILABLE:
        pytest.skip("scikit-learn is not installed (pip install gptme-rag[lexical])")


def _doc(text: str, source: str, memory_type: str | None = None) -> Document:
    metadata: dict = {"source": source}
    if memory_type is not None:
        metadata["memory_type"] = memory_type
    return Document(content=text, metadata=metadata, source_path=Path(source))


def test_search_without_memory_types_unchanged(require_sklearn):
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


def test_search_boost_reranks_matching_memory_type(require_sklearn):
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


def test_search_memory_types_unknown_type_ignored(require_sklearn):
    """Unknown types in memory_types must not raise; they are silently filtered."""
    idx = TfidfIndex()
    idx.index([_doc("hello world", "a.md", "goal")])
    hits = idx.search("hello", n_results=5, memory_types={"unknown_type"})
    # unknown_type is not in SUPPORTED_MEMORY_TYPES → treated as no preference
    assert len(hits) == 1


def test_search_memory_types_no_tagged_docs(require_sklearn):
    """memory_types param is safe when no documents carry a memory_type tag."""
    idx = TfidfIndex()
    idx.index([_doc("hello world", "a.md")])  # no memory_type
    hits = idx.search("hello", n_results=5, memory_types={"goal"})
    assert len(hits) == 1
    assert hits[0].document.metadata["source"] == "a.md"


def test_search_relevance_floor_uses_raw_score_before_penalty(require_sklearn):
    """A raw-relevant document survives even when its weighted score is lower."""
    idx = TfidfIndex(relevance_floor=0.95)
    idx.index([_doc("relevant query terms", "a.md", "project")])

    hits = idx.search("relevant query terms", n_results=5, memory_types={"goal"})

    assert len(hits) == 1
    assert hits[0].score == pytest.approx(MEMORY_TYPE_PENALTY)
    assert hits[0].score < idx.relevance_floor


def test_search_relevance_floor_rejects_raw_score_before_boost(require_sklearn):
    """A boost must not admit a document below the raw relevance floor."""
    idx = TfidfIndex(relevance_floor=0.0)
    idx.index(
        [
            _doc("query exact", "exact.md", "project"),
            _doc("query partial extra terms", "partial.md", "goal"),
        ]
    )

    # Derive a floor inside the interval where the partial match fails by raw
    # cosine but would pass after its memory-type boost.
    unfiltered_hits = idx.search("query exact", n_results=5)
    raw_scores = {hit.document.metadata["source"]: hit.score for hit in unfiltered_hits}
    partial_raw = raw_scores["partial.md"]
    idx.relevance_floor = (partial_raw + partial_raw * MEMORY_TYPE_BOOST) / 2
    assert partial_raw < idx.relevance_floor
    assert partial_raw * MEMORY_TYPE_BOOST > idx.relevance_floor
    assert raw_scores["exact.md"] > idx.relevance_floor

    boosted_hits = idx.search("query exact", n_results=5, memory_types={"goal"})
    assert [hit.document.metadata["source"] for hit in boosted_hits] == ["exact.md"]


def test_search_unknown_doc_memory_type_not_penalised(require_sklearn):
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
