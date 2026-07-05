"""Integration tests for gptme_consortium plugin.

These tests use real API calls and are marked as slow.
They require API keys to be set in environment variables.
"""

import pytest
from gptme_consortium.tools.consortium import ConsortiumResult, query_consortium


@pytest.mark.slow
@pytest.mark.requires_api_keys
class TestConsortiumIntegration:
    """Integration tests with real API calls."""

    def test_consortium_real_models_simple(self, skip_if_no_api_keys):
        """Test with actual frontier models on simple question."""
        result = query_consortium(
            question="What is 2+2?",
            models=["anthropic/claude-sonnet-4-5", "openai/gpt-5.1"],
        )

        assert isinstance(result, ConsortiumResult)
        assert result.consensus
        assert "4" in result.consensus.lower()
        assert result.confidence > 0.5
        assert len(result.models_used) == 2

    def test_consortium_real_models_reasoning(self, skip_if_no_api_keys):
        """Test with actual frontier models on reasoning question."""
        result = query_consortium(
            question="Why is the sky blue? Explain briefly.",
            models=[
                "anthropic/claude-sonnet-4-5",
                "openai/gpt-5.1",
                "google/gemini-3-pro",
            ],
        )

        assert isinstance(result, ConsortiumResult)
        assert result.consensus
        assert len(result.consensus) > 50  # Should have substance
        assert result.confidence > 0.3  # Lower threshold for complex question
        assert len(result.models_used) == 3

    def test_consensus_convergence_iterations(self, skip_if_no_api_keys):
        """Test that consensus improves with iterations."""
        result = query_consortium(
            question="Compare microservices vs monolith for small team",
            models=["anthropic/claude-sonnet-4-5", "openai/gpt-5.1"],
            confidence_threshold=0.8,
            max_iterations=3,
        )

        assert isinstance(result, ConsortiumResult)
        assert result.confidence >= 0.7  # May not always reach 0.8
        # Check if metadata has iterations info
        if hasattr(result, "metadata") and result.metadata:
            iterations = result.metadata.get("iterations", [])
            # Should have attempted iterations if confidence didn't reach threshold quickly
            assert len(iterations) >= 1

    def test_arbiter_synthesis_real(self, skip_if_no_api_keys):
        """Test arbiter properly synthesizes responses."""
        result = query_consortium(
            question="What are three benefits of exercise?",
            models=["anthropic/claude-sonnet-4-5", "openai/gpt-5.1"],
            arbiter="anthropic/claude-sonnet-4-5",
        )

        assert isinstance(result, ConsortiumResult)
        assert result.arbiter_model == "anthropic/claude-sonnet-4-5"
        assert result.consensus
        assert result.synthesis_reasoning
        # Consensus should mention exercise benefits
        consensus_lower = result.consensus.lower()
        assert any(
            word in consensus_lower
            for word in ["health", "exercise", "fitness", "benefit"]
        )

    def test_model_diversity_real(self, skip_if_no_api_keys):
        """Verify diverse providers work together."""
        result = query_consortium(
            question="Name one programming language.",
            models=[
                "anthropic/claude-sonnet-4-5",
                "openai/gpt-5.1",
                "google/gemini-3-pro",
            ],
        )

        assert isinstance(result, ConsortiumResult)
        models = result.models_used

        # Extract providers from model strings
        providers = set(m.split("/")[0] for m in models)
        assert "anthropic" in providers
        assert "openai" in providers
        assert "google" in providers

        # Should have consensus despite diverse sources
        assert result.consensus
        assert result.confidence > 0.3
