"""Tests for ACE plugin."""


def test_import_plugin():
    """Test that the plugin can be imported."""
    from gptme_ace import plugin

    assert plugin.name == "ace"
    assert plugin.available is True


def test_import_hybrid_config():
    """Test that HybridConfig can be imported."""
    from gptme_ace import HybridConfig

    config = HybridConfig()
    assert config.keyword_weight == 0.25
    assert config.semantic_weight == 0.40
    assert config.effectiveness_weight == 0.25
    assert config.recency_weight == 0.10
    assert config.tool_bonus == 0.20


def test_import_hybrid_matcher():
    """Test that GptmeHybridMatcher can be imported."""
    from gptme_ace import GptmeHybridMatcher

    # Should be able to instantiate without embedder
    matcher = GptmeHybridMatcher()
    assert matcher.hybrid_enabled is False  # Default off without env var


def test_embedder_import():
    """Test that LessonEmbedder can be imported."""
    from gptme_ace import LessonEmbedder

    # Just verify import works
    assert LessonEmbedder is not None
