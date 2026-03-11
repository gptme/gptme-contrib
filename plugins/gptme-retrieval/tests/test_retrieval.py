"""Tests for gptme-retrieval plugin."""

from unittest.mock import MagicMock, patch

from gptme.message import Message
from gptme_retrieval import (
    _MAX_TRACKED_CONVS,
    DEFAULT_CONFIG,
    _doc_key,
    _injected_per_conv,
    get_retrieval_config,
    retrieve_context,
    step_pre_hook,
    turn_pre_hook,
)


def test_get_retrieval_config_defaults():
    """Test that defaults are returned when no config exists."""
    with patch("gptme_retrieval.get_config") as mock_config:
        mock_config.return_value = MagicMock(project=None, user=None)
        config = get_retrieval_config()
        assert config == DEFAULT_CONFIG


def test_retrieve_context_qmd_not_found():
    """Test graceful handling when qmd is not installed."""
    with patch("subprocess.run", side_effect=FileNotFoundError):
        results = retrieve_context("test query", backend="qmd")
        assert results == []


def test_retrieve_context_grep():
    """Test grep backend returns file matches."""
    mock_result = MagicMock()
    mock_result.stdout = "file1.md\nfile2.md\n"
    mock_result.returncode = 0

    with patch("subprocess.run", return_value=mock_result):
        results = retrieve_context("test", backend="grep", max_results=2)
        assert len(results) == 2
        assert results[0]["source"] == "file1.md"


def test_retrieve_context_threshold_filtering():
    """Test that results below threshold are filtered."""
    mock_result = MagicMock()
    mock_result.stdout = (
        '[{"content": "high", "score": 0.9}, {"content": "low", "score": 0.1}]'
    )
    mock_result.returncode = 0

    with patch("subprocess.run", return_value=mock_result):
        results = retrieve_context("test", backend="qmd", threshold=0.5)
        assert len(results) == 1
        assert results[0]["content"] == "high"


def test_doc_key_stable():
    """Test that _doc_key produces stable, unique keys."""
    doc = {"source": "lessons/ts.md", "content": "Thompson sampling"}
    key1 = _doc_key(doc)
    key2 = _doc_key(doc)
    assert key1 == key2
    assert "lessons/ts.md" in key1

    # Different content -> different key
    doc2 = {"source": "lessons/ts.md", "content": "Different content"}
    assert _doc_key(doc2) != key1


def test_step_pre_hook_no_user_message():
    """Test that step_pre_hook does nothing when no user message exists."""
    manager = MagicMock()
    manager.log.messages = []
    manager.log.name = "test-conv-no-user"

    with patch("gptme_retrieval.get_retrieval_config", return_value=DEFAULT_CONFIG):
        messages = list(step_pre_hook(manager))
    assert messages == []


def test_step_pre_hook_yields_context():
    """Test that step_pre_hook injects retrieved context as a system message."""
    manager = MagicMock()
    manager.log.messages = [Message(role="user", content="explain Thompson sampling")]
    manager.log.name = "test-conv-yields"

    # Clear any state from previous test runs
    _injected_per_conv.pop("test-conv-yields", None)

    mock_qmd_result = MagicMock()
    mock_qmd_result.returncode = 0
    mock_qmd_result.stdout = '[{"content": "Thompson sampling is a Bayesian approach", "path": "lessons/ts.md", "score": 0.9}]'

    config = {**DEFAULT_CONFIG, "backend": "qmd", "mode": "search", "threshold": 0.3}

    with (
        patch("gptme_retrieval.get_retrieval_config", return_value=config),
        patch("subprocess.run", return_value=mock_qmd_result),
    ):
        messages = list(step_pre_hook(manager))

    assert len(messages) == 1
    assert messages[0].role == "system"
    assert "Thompson sampling" in messages[0].content


def test_step_pre_hook_deduplicates():
    """Test that step_pre_hook does not inject the same document twice."""
    manager = MagicMock()
    manager.log.messages = [Message(role="user", content="explain Thompson sampling")]
    manager.log.name = "test-conv-dedup"

    # Clear state
    _injected_per_conv.pop("test-conv-dedup", None)

    mock_qmd_result = MagicMock()
    mock_qmd_result.returncode = 0
    mock_qmd_result.stdout = '[{"content": "Thompson sampling is a Bayesian approach", "path": "lessons/ts.md", "score": 0.9}]'

    config = {**DEFAULT_CONFIG, "backend": "qmd", "mode": "search", "threshold": 0.3}

    with (
        patch("gptme_retrieval.get_retrieval_config", return_value=config),
        patch("subprocess.run", return_value=mock_qmd_result),
    ):
        # First call injects the document
        messages1 = list(step_pre_hook(manager))
        assert len(messages1) == 1

        # Second call with same results — should inject nothing (already seen)
        messages2 = list(step_pre_hook(manager))
        assert len(messages2) == 0


def test_step_pre_hook_injects_new_doc_on_topic_change():
    """Test that step_pre_hook injects new documents when topic changes."""
    manager = MagicMock()
    manager.log.name = "test-conv-topic-change"

    # Clear state
    _injected_per_conv.pop("test-conv-topic-change", None)

    config = {**DEFAULT_CONFIG, "backend": "qmd", "mode": "search", "threshold": 0.3}

    first_result = MagicMock()
    first_result.returncode = 0
    first_result.stdout = (
        '[{"content": "Thompson sampling doc", "path": "ts.md", "score": 0.9}]'
    )

    second_result = MagicMock()
    second_result.returncode = 0
    second_result.stdout = (
        '[{"content": "Bayesian optimization doc", "path": "bo.md", "score": 0.9}]'
    )

    manager.log.messages = [Message(role="user", content="Thompson sampling")]
    with (
        patch("gptme_retrieval.get_retrieval_config", return_value=config),
        patch("subprocess.run", return_value=first_result),
    ):
        msgs1 = list(step_pre_hook(manager))
    assert len(msgs1) == 1
    assert "Thompson" in msgs1[0].content

    # Topic changes — new user message, new retrieval results
    manager.log.messages = [
        Message(role="user", content="Thompson sampling"),
        Message(role="assistant", content="..."),
        Message(role="user", content="Now explain Bayesian optimization"),
    ]
    with (
        patch("gptme_retrieval.get_retrieval_config", return_value=config),
        patch("subprocess.run", return_value=second_result),
    ):
        msgs2 = list(step_pre_hook(manager))
    assert len(msgs2) == 1
    assert "Bayesian" in msgs2[0].content


def test_step_pre_hook_disabled():
    """Test that step_pre_hook does nothing when disabled in config."""
    manager = MagicMock()
    manager.log.messages = [Message(role="user", content="test")]
    manager.log.name = "test-conv-disabled"

    config = {**DEFAULT_CONFIG, "enabled": False}

    with patch("gptme_retrieval.get_retrieval_config", return_value=config):
        messages = list(step_pre_hook(manager))
    assert messages == []


def test_step_pre_hook_none_name_shared_default_bucket():
    """Test that nameless conversations (log.name=None) share the "default" dedup bucket."""
    config = {**DEFAULT_CONFIG, "backend": "qmd", "mode": "search", "threshold": 0.3}
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = '[{"content": "doc content", "path": "doc.md", "score": 0.9}]'

    # Remove any existing "default" entry so both managers start fresh
    _injected_per_conv.pop("default", None)

    # Two separate managers both with log.name = None
    manager1 = MagicMock()
    manager1.log.messages = [Message(role="user", content="query")]
    manager1.log.name = None

    manager2 = MagicMock()
    manager2.log.messages = [Message(role="user", content="query")]
    manager2.log.name = None

    with (
        patch("gptme_retrieval.get_retrieval_config", return_value=config),
        patch("subprocess.run", return_value=mock_result),
    ):
        # First nameless conversation injects the doc
        msgs1 = list(step_pre_hook(manager1))
        assert len(msgs1) == 1, "First nameless conv should inject doc"

        # Second call to same-named "default" key sees it already injected (expected)
        # — both are bucketed under "default", so the second is a no-op.
        # This is acceptable: nameless convs share a bucket, not unrelated named convs.
        msgs2 = list(step_pre_hook(manager2))
        assert (
            len(msgs2) == 0
        ), "Same bucket (both None->default) deduplicates correctly"


def test_injected_per_conv_max_size():
    """Test that _injected_per_conv does not exceed _MAX_TRACKED_CONVS entries."""
    _injected_per_conv.clear()
    config = {**DEFAULT_CONFIG, "backend": "qmd", "mode": "search", "threshold": 0.3}
    mock_result = MagicMock()
    mock_result.returncode = 0

    try:
        for i in range(_MAX_TRACKED_CONVS + 10):
            mock_result.stdout = (
                f'[{{"content": "doc {i}", "path": "doc{i}.md", "score": 0.9}}]'
            )
            manager = MagicMock()
            manager.log.messages = [Message(role="user", content=f"query {i}")]
            manager.log.name = f"conv-{i}"
            with (
                patch("gptme_retrieval.get_retrieval_config", return_value=config),
                patch("subprocess.run", return_value=mock_result),
            ):
                list(step_pre_hook(manager))

        assert len(_injected_per_conv) <= _MAX_TRACKED_CONVS
    finally:
        _injected_per_conv.clear()


def test_injected_per_conv_lru_move_to_end():
    """Test that an existing conversation is moved to end (LRU) on re-entry, preventing eviction."""
    _injected_per_conv.clear()
    config = {**DEFAULT_CONFIG, "backend": "qmd", "mode": "search", "threshold": 0.3}
    mock_result = MagicMock()
    mock_result.returncode = 0

    try:
        # Add one conversation first so it sits at position 0 (oldest)
        mock_result.stdout = (
            '[{"content": "first doc", "path": "first.md", "score": 0.9}]'
        )
        early_manager = MagicMock()
        early_manager.log.messages = [Message(role="user", content="early query")]
        early_manager.log.name = "early-conv"
        with (
            patch("gptme_retrieval.get_retrieval_config", return_value=config),
            patch("subprocess.run", return_value=mock_result),
        ):
            list(step_pre_hook(early_manager))

        assert "early-conv" in _injected_per_conv

        # Fill up to _MAX_TRACKED_CONVS - 1 more conversations so early-conv is oldest
        for i in range(_MAX_TRACKED_CONVS - 1):
            mock_result.stdout = (
                f'[{{"content": "doc {i}", "path": "doc{i}.md", "score": 0.9}}]'
            )
            m = MagicMock()
            m.log.messages = [Message(role="user", content=f"query {i}")]
            m.log.name = f"filler-{i}"
            with (
                patch("gptme_retrieval.get_retrieval_config", return_value=config),
                patch("subprocess.run", return_value=mock_result),
            ):
                list(step_pre_hook(m))

        assert len(_injected_per_conv) == _MAX_TRACKED_CONVS

        # Re-enter early-conv — this should trigger move_to_end (else branch)
        mock_result.stdout = '[{"content": "new doc", "path": "new.md", "score": 0.9}]'
        with (
            patch("gptme_retrieval.get_retrieval_config", return_value=config),
            patch("subprocess.run", return_value=mock_result),
        ):
            list(step_pre_hook(early_manager))

        # early-conv should now be last (most-recently-used), not evicted
        keys = list(_injected_per_conv.keys())
        assert (
            keys[-1] == "early-conv"
        ), "Re-entered conv should be at end after move_to_end"

        # Adding one more new conversation should evict filler-0 (oldest), not early-conv
        mock_result.stdout = (
            '[{"content": "late doc", "path": "late.md", "score": 0.9}]'
        )
        m = MagicMock()
        m.log.messages = [Message(role="user", content="late query")]
        m.log.name = "late-conv"
        with (
            patch("gptme_retrieval.get_retrieval_config", return_value=config),
            patch("subprocess.run", return_value=mock_result),
        ):
            list(step_pre_hook(m))

        assert (
            "early-conv" in _injected_per_conv
        ), "early-conv must survive eviction after LRU refresh"
        assert "filler-0" not in _injected_per_conv, "Oldest filler should be evicted"
    finally:
        _injected_per_conv.clear()


# Backward-compat: turn_pre_hook tests still pass
def test_turn_pre_hook_no_user_message():
    """Test that turn_pre_hook does nothing when no user message exists."""
    manager = MagicMock()
    manager.log.messages = []

    with patch("gptme_retrieval.get_retrieval_config", return_value=DEFAULT_CONFIG):
        messages = list(turn_pre_hook(manager))
    assert messages == []


def test_turn_pre_hook_yields_context():
    """Test that turn_pre_hook injects retrieved context as a system message."""
    manager = MagicMock()
    manager.log.messages = [Message(role="user", content="explain Thompson sampling")]

    mock_qmd_result = MagicMock()
    mock_qmd_result.returncode = 0
    mock_qmd_result.stdout = '[{"content": "Thompson sampling is a Bayesian approach", "path": "lessons/ts.md", "score": 0.9}]'

    config = {**DEFAULT_CONFIG, "backend": "qmd", "mode": "search", "threshold": 0.3}

    with (
        patch("gptme_retrieval.get_retrieval_config", return_value=config),
        patch("subprocess.run", return_value=mock_qmd_result),
    ):
        messages = list(turn_pre_hook(manager))

    assert len(messages) == 1
    assert messages[0].role == "system"
    assert "Thompson sampling" in messages[0].content


def test_turn_pre_hook_disabled():
    """Test that turn_pre_hook does nothing when disabled in config."""
    manager = MagicMock()
    manager.log.messages = [Message(role="user", content="test")]

    config = {**DEFAULT_CONFIG, "enabled": False}

    with patch("gptme_retrieval.get_retrieval_config", return_value=config):
        messages = list(turn_pre_hook(manager))
    assert messages == []
