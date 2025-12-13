"""Unit tests for gptme_consortium plugin."""

from unittest.mock import patch

import pytest

from gptme_consortium.tools.consortium import (
    ConsortiumResult,
    query_consortium,
    _synthesize_consensus,
)


class TestQueryConsortium:
    """Tests for query_consortium function."""

    def test_query_consortium_basic(self):
        """Test basic consortium query with default models."""
        with (
            patch(
                "gptme_consortium.tools.consortium._query_single_model"
            ) as mock_query,
            patch(
                "gptme_consortium.tools.consortium._synthesize_consensus"
            ) as mock_synth,
        ):
            # Mock individual model responses (5 default models)
            mock_query.side_effect = [
                "Response from model 1",
                "Response from model 2",
                "Response from model 3",
                "Response from model 4",
                "Response from model 5",
            ]

            # Mock synthesis result
            mock_synth.return_value = {
                "consensus": "Test consensus",
                "confidence": 0.85,
                "reasoning": "Test reasoning",
            }

            result = query_consortium(question="Test question")

            assert isinstance(result, ConsortiumResult)
            assert result.question == "Test question"
            assert result.consensus == "Test consensus"
            # Confidence is now: base * success_rate * (0.5 + 0.5 * agreement)
            # With agreement_score calculated from responses
            assert 0.6 < result.confidence < 0.9  # Reasonable range
            assert result.synthesis_reasoning == "Test reasoning"
            assert len(result.models_used) == 5  # Default models (5 frontier models)
            assert result.arbiter_model == "anthropic/claude-sonnet-4-5"
            assert hasattr(result, "agreement_score")
            assert 0.0 <= result.agreement_score <= 1.0

    def test_query_consortium_custom_models(self):
        """Test consortium query with custom model list."""
        custom_models = ["anthropic/claude-sonnet-4-5", "openai/gpt-5.1"]

        with (
            patch(
                "gptme_consortium.tools.consortium._query_single_model"
            ) as mock_query,
            patch(
                "gptme_consortium.tools.consortium._synthesize_consensus"
            ) as mock_synth,
        ):
            mock_query.side_effect = ["Response 1", "Response 2"]
            mock_synth.return_value = {
                "consensus": "Custom consensus",
                "confidence": 0.9,
                "reasoning": "Custom reasoning",
            }

            result = query_consortium(
                question="Test",
                models=custom_models,
            )

            assert len(result.models_used) == 2
            assert result.models_used == custom_models

    def test_query_consortium_custom_arbiter(self):
        """Test consortium query with custom arbiter model."""
        custom_arbiter = "openai/gpt-5.1"

        with (
            patch(
                "gptme_consortium.tools.consortium._query_single_model"
            ) as mock_query,
            patch(
                "gptme_consortium.tools.consortium._synthesize_consensus"
            ) as mock_synth,
        ):
            mock_query.return_value = "Response"
            mock_synth.return_value = {
                "consensus": "Test",
                "confidence": 0.8,
                "reasoning": "Test",
            }

            result = query_consortium(
                question="Test",
                arbiter=custom_arbiter,
            )

            assert result.arbiter_model == custom_arbiter

    def test_query_consortium_error_handling(self):
        """Test that individual model errors are captured."""
        with (
            patch(
                "gptme_consortium.tools.consortium._query_single_model"
            ) as mock_query,
            patch(
                "gptme_consortium.tools.consortium._synthesize_consensus"
            ) as mock_synth,
            patch("gptme_consortium.tools.consortium.time.sleep"),
        ):  # Mock sleep for faster tests
            # Account for retries: each error will be retried up to 3 times
            # Model 1: Success (1 call)
            # Model 2: Fail all retries (4 calls: initial + 3 retries)
            # Model 3: Success (1 call)
            # Model 4: Fail all retries (4 calls: initial + 3 retries)
            error = Exception("API Error")
            mock_query.side_effect = [
                "Success 1",  # Model 1
                error,
                error,
                error,
                error,  # Model 2: all attempts fail
                "Success 2",  # Model 3
                error,
                error,
                error,
                error,  # Model 4: all attempts fail
            ]

            mock_synth.return_value = {
                "consensus": "Partial consensus",
                "confidence": 0.6,
                "reasoning": "Based on available responses",
            }

            result = query_consortium(question="Test")

            # Should complete despite errors
            assert isinstance(result, ConsortiumResult)
            assert "Success 1" in str(result.responses.values())
            assert "Error: API Error" in str(result.responses.values())
            assert hasattr(result, "agreement_score")


class TestSynthesizeConsensus:
    """Tests for _synthesize_consensus function."""

    def test_synthesis_valid_json(self):
        """Test synthesis with valid JSON response."""
        responses = {
            "model1": "Response 1",
            "model2": "Response 2",
        }

        with patch(
            "gptme_consortium.tools.consortium._query_single_model"
        ) as mock_query:
            mock_query.return_value = '{"consensus": "Final answer", "confidence": 0.9, "reasoning": "Models agree"}'

            result = _synthesize_consensus(
                question="Test",
                responses=responses,
                arbiter="test-arbiter",
                threshold=0.8,
            )

            assert result["consensus"] == "Final answer"
            assert result["confidence"] == 0.9
            assert result["reasoning"] == "Models agree"

    def test_synthesis_invalid_json(self):
        """Test synthesis fallback for invalid JSON."""
        responses = {"model1": "Response"}

        with patch(
            "gptme_consortium.tools.consortium._query_single_model"
        ) as mock_query:
            mock_query.return_value = "This is not JSON"

            result = _synthesize_consensus(
                question="Test",
                responses=responses,
                arbiter="test-arbiter",
                threshold=0.8,
            )

            # Should fallback gracefully
            assert result["consensus"] == "This is not JSON"
            assert result["confidence"] == 0.5  # Default
            assert "Unable to parse" in result["reasoning"]

    def test_synthesis_missing_fields(self):
        """Test synthesis with incomplete JSON."""
        responses = {"model1": "Response"}

        with patch(
            "gptme_consortium.tools.consortium._query_single_model"
        ) as mock_query:
            mock_query.return_value = '{"consensus": "Answer only"}'

            result = _synthesize_consensus(
                question="Test",
                responses=responses,
                arbiter="test-arbiter",
                threshold=0.8,
            )

            # Should use defaults for missing fields
            assert result["consensus"] == "Answer only"
            assert result["confidence"] == 0.5  # Default
            assert result["reasoning"] == "No reasoning provided"  # Default


class TestConsortiumResult:
    """Tests for ConsortiumResult dataclass."""

    def test_consortium_result_creation(self):
        """Test creating ConsortiumResult with all fields."""
        result = ConsortiumResult(
            question="Test question",
            consensus="Test consensus",
            confidence=0.85,
            responses={"model1": "Response 1"},
            synthesis_reasoning="Test reasoning",
            models_used=["model1"],
            arbiter_model="arbiter",
            agreement_score=0.9,
        )

        assert result.question == "Test question"
        assert result.consensus == "Test consensus"
        assert result.confidence == 0.85
        assert result.responses == {"model1": "Response 1"}
        assert result.agreement_score == 0.9
        assert result.synthesis_reasoning == "Test reasoning"
        assert result.models_used == ["model1"]
        assert result.arbiter_model == "arbiter"


@pytest.mark.slow
@pytest.mark.requires_api_keys
class TestConsortiumIntegration:
    """Integration tests requiring real API access."""

    def test_consortium_real_models(self):
        """Test with actual frontier models.

        Requires API keys for Anthropic, OpenAI, Google.
        """
        result = query_consortium(
            question="What is 2+2?",
            models=[
                "anthropic/claude-sonnet-4-5",
                "openai/gpt-5.1",
            ],
            confidence_threshold=0.7,
        )

        assert isinstance(result, ConsortiumResult)
        assert result.consensus  # Should have some answer
        assert result.confidence >= 0  # Should have confidence score
        assert len(result.responses) == 2
        assert result.models_used == ["anthropic/claude-sonnet-4-5", "openai/gpt-5.1"]

    @pytest.mark.slow
    def test_consensus_convergence(self):
        """Test that diverse models can reach consensus.

        Complex question to test synthesis capability.
        """
        result = query_consortium(
            question="Compare microservices vs monolith for a 3-person team",
            models=[
                "openai/gpt-4o",
                "openai/gpt-4o-mini",
            ],
            arbiter="openai/gpt-4o",
            confidence_threshold=0.4,  # Lower threshold to account for agreement scoring
        )

        assert isinstance(result, ConsortiumResult)
        assert result.confidence >= 0.4  # Adjusted for agreement-based confidence
        # Verify we got responses from all models
        assert (
            len([r for r in result.responses.values() if not r.startswith("Error:")])
            >= 2
        )
        assert len(result.synthesis_reasoning) > 100  # Substantive reasoning
