"""Feature tests for consortium plugin.

Tests specific features, error handling, edge cases, and performance.
Phase 4: End-to-end feature validation.
"""

import time
from unittest.mock import patch

import pytest

from gptme_consortium.tools.consortium import (
    _synthesize_consensus,
    query_consortium,
)


class TestModelCombinations:
    """Test different model combinations and configurations."""

    @patch("gptme_consortium.tools.consortium._query_single_model")
    @patch("gptme_consortium.tools.consortium._synthesize_consensus")
    def test_default_models(self, mock_synthesize, mock_query):
        """Test consortium with default model set."""
        mock_query.return_value = "Test response"
        mock_synthesize.return_value = {
            "consensus": "Consensus answer",
            "confidence": 0.85,
            "reasoning": "All models agreed",
        }

        result = query_consortium(question="Test question")

        # Should use default frontier models
        assert len(result.models_used) == 4
        assert "anthropic/claude-sonnet-4-5" in result.models_used
        assert "openai/gpt-5.1" in result.models_used
        assert "google/gemini-3-pro" in result.models_used
        assert "xai/grok-4" in result.models_used

    @patch("gptme_consortium.tools.consortium._query_single_model")
    @patch("gptme_consortium.tools.consortium._synthesize_consensus")
    def test_custom_models(self, mock_synthesize, mock_query):
        """Test consortium with custom model list."""
        mock_query.return_value = "Test response"
        mock_synthesize.return_value = {
            "consensus": "Consensus answer",
            "confidence": 0.85,
            "reasoning": "Models agreed",
        }

        custom_models = ["model1", "model2", "model3"]
        result = query_consortium(question="Test", models=custom_models)

        assert result.models_used == custom_models
        assert mock_query.call_count == 3

    @patch("gptme_consortium.tools.consortium._query_single_model")
    @patch("gptme_consortium.tools.consortium._synthesize_consensus")
    def test_single_model(self, mock_synthesize, mock_query):
        """Test consortium with single model (edge case)."""
        mock_query.return_value = "Single response"
        mock_synthesize.return_value = {
            "consensus": "Single model answer",
            "confidence": 1.0,
            "reasoning": "Only one model",
        }

        result = query_consortium(question="Test", models=["model1"])

        assert len(result.models_used) == 1
        assert result.confidence == 1.0


class TestArbiterFunctionality:
    """Test arbiter model configuration and behavior."""

    @patch("gptme_consortium.tools.consortium._query_single_model")
    @patch("gptme_consortium.tools.consortium._synthesize_consensus")
    def test_default_arbiter(self, mock_synthesize, mock_query):
        """Test default arbiter selection."""
        mock_query.return_value = "Response"
        mock_synthesize.return_value = {
            "consensus": "Answer",
            "confidence": 0.8,
            "reasoning": "Reason",
        }

        result = query_consortium(question="Test")

        assert result.arbiter_model == "anthropic/claude-sonnet-4-5"

    @patch("gptme_consortium.tools.consortium._query_single_model")
    @patch("gptme_consortium.tools.consortium._synthesize_consensus")
    def test_custom_arbiter(self, mock_synthesize, mock_query):
        """Test custom arbiter model."""
        mock_query.return_value = "Response"
        mock_synthesize.return_value = {
            "consensus": "Answer",
            "confidence": 0.8,
            "reasoning": "Reason",
        }

        result = query_consortium(question="Test", arbiter="custom/arbiter-model")

        assert result.arbiter_model == "custom/arbiter-model"

    def test_synthesis_with_different_responses(self):
        """Test synthesis handles diverse responses correctly."""
        responses = {
            "model1": "Option A is best",
            "model2": "Option B is better",
            "model3": "Option A has advantages",
        }

        result = _synthesize_consensus(
            question="Which option?",
            responses=responses,
            arbiter="arbiter/model",
            threshold=0.7,
        )

        assert "consensus" in result
        assert "confidence" in result
        assert "reasoning" in result


class TestConfidenceThreshold:
    """Test confidence scoring and threshold enforcement."""

    @patch("gptme_consortium.tools.consortium._query_single_model")
    def test_high_confidence_response(self, mock_query):
        """Test response with high confidence."""
        mock_query.side_effect = [
            "Response A",
            "Response A",
            "Response A",
            '{"consensus": "A", "confidence": 0.95, "reasoning": "All agreed"}',
        ]

        result = query_consortium(
            question="Test", models=["m1", "m2", "m3"], confidence_threshold=0.8
        )

        assert result.confidence >= 0.8

    @patch("gptme_consortium.tools.consortium._query_single_model")
    def test_low_confidence_response(self, mock_query):
        """Test response with low confidence."""
        mock_query.side_effect = [
            "Response A",
            "Response B",
            "Response C",
            '{"consensus": "Mixed", "confidence": 0.3, "reasoning": "Disagreement"}',
        ]

        result = query_consortium(
            question="Test", models=["m1", "m2", "m3"], confidence_threshold=0.8
        )

        # Should still return result, but with low confidence
        assert result.confidence < 0.8

    @patch("gptme_consortium.tools.consortium._query_single_model")
    @patch("gptme_consortium.tools.consortium._synthesize_consensus")
    def test_confidence_threshold_values(self, mock_synthesize, mock_query):
        """Test different confidence threshold values."""
        mock_query.return_value = "Response"

        thresholds = [0.5, 0.7, 0.8, 0.9, 0.95]

        for threshold in thresholds:
            mock_synthesize.return_value = {
                "consensus": "Answer",
                "confidence": threshold,
                "reasoning": f"Confidence: {threshold}",
            }

            result = query_consortium(question="Test", confidence_threshold=threshold)

            assert result.confidence == threshold


class TestErrorHandling:
    """Test error handling for various failure scenarios."""

    @patch("gptme_consortium.tools.consortium._query_single_model")
    @patch("gptme_consortium.tools.consortium._synthesize_consensus")
    def test_single_model_failure(self, mock_synthesize, mock_query):
        """Test handling when one model fails."""
        mock_query.side_effect = [
            "Response 1",
            Exception("API Error"),
            "Response 3",
        ]
        mock_synthesize.return_value = {
            "consensus": "Best effort consensus",
            "confidence": 0.6,
            "reasoning": "One model failed",
        }

        result = query_consortium(question="Test", models=["m1", "m2", "m3"])

        # Should still complete with remaining responses
        assert "Error: API Error" in result.responses["m2"]
        assert result.consensus == "Best effort consensus"

    @patch("gptme_consortium.tools.consortium._query_single_model")
    @patch("gptme_consortium.tools.consortium._synthesize_consensus")
    def test_all_models_fail(self, mock_synthesize, mock_query):
        """Test handling when all models fail."""
        mock_query.side_effect = Exception("API Down")
        mock_synthesize.return_value = {
            "consensus": "Unable to reach consensus",
            "confidence": 0.0,
            "reasoning": "All models failed",
        }

        result = query_consortium(question="Test", models=["m1", "m2"])

        # Should handle gracefully
        assert all("Error" in r for r in result.responses.values())
        assert result.confidence == 0.0

    def test_malformed_json_response(self):
        """Test handling of malformed JSON from arbiter."""
        responses = {"m1": "Response 1", "m2": "Response 2"}

        with patch(
            "gptme_consortium.tools.consortium._query_single_model"
        ) as mock_query:
            mock_query.return_value = "Not valid JSON"

            result = _synthesize_consensus(
                question="Test", responses=responses, arbiter="arbiter", threshold=0.8
            )

            # Should handle gracefully with fallback
            assert result["consensus"]
            assert isinstance(result["confidence"], (int, float))

    def test_missing_json_fields(self):
        """Test handling when JSON response missing required fields."""
        responses = {"m1": "Response"}

        with patch(
            "gptme_consortium.tools.consortium._query_single_model"
        ) as mock_query:
            # Missing 'reasoning' field
            mock_query.return_value = '{"consensus": "Answer", "confidence": 0.8}'

            result = _synthesize_consensus(
                question="Test", responses=responses, arbiter="arbiter", threshold=0.8
            )

            assert result["consensus"] == "Answer"
            assert result["confidence"] == 0.8
            assert "reasoning" in result  # Should have default value


class TestEdgeCases:
    """Test edge cases and unusual inputs."""

    @patch("gptme_consortium.tools.consortium._query_single_model")
    @patch("gptme_consortium.tools.consortium._synthesize_consensus")
    def test_very_long_question(self, mock_synthesize, mock_query):
        """Test with extremely long question."""
        mock_query.return_value = "Response"
        mock_synthesize.return_value = {
            "consensus": "Answer",
            "confidence": 0.8,
            "reasoning": "Reason",
        }

        long_question = "What should I do? " * 1000  # Very long
        result = query_consortium(question=long_question, models=["m1"])

        assert result.question == long_question

    @patch("gptme_consortium.tools.consortium._query_single_model")
    @patch("gptme_consortium.tools.consortium._synthesize_consensus")
    def test_unicode_in_question(self, mock_synthesize, mock_query):
        """Test Unicode characters in question."""
        mock_query.return_value = "Response"
        mock_synthesize.return_value = {
            "consensus": "Answer",
            "confidence": 0.8,
            "reasoning": "Reason",
        }

        unicode_question = "Test 测试 тест テスト 🤔❓"
        result = query_consortium(question=unicode_question, models=["m1"])

        assert result.question == unicode_question

    @patch("gptme_consortium.tools.consortium._query_single_model")
    @patch("gptme_consortium.tools.consortium._synthesize_consensus")
    def test_empty_responses(self, mock_synthesize, mock_query):
        """Test handling of empty responses."""
        mock_query.return_value = ""  # Empty response
        mock_synthesize.return_value = {
            "consensus": "Unable to synthesize from empty responses",
            "confidence": 0.0,
            "reasoning": "No content to analyze",
        }

        result = query_consortium(question="Test", models=["m1", "m2"])

        assert all(r == "" for r in result.responses.values())

    @patch("gptme_consortium.tools.consortium._query_single_model")
    @patch("gptme_consortium.tools.consortium._synthesize_consensus")
    def test_special_characters_in_responses(self, mock_synthesize, mock_query):
        """Test handling special characters in model responses."""
        mock_query.return_value = (
            'Response with "quotes", \\backslashes\\ and\nnewlines'
        )
        mock_synthesize.return_value = {
            "consensus": "Synthesized answer",
            "confidence": 0.8,
            "reasoning": "Handled special chars",
        }

        result = query_consortium(question="Test", models=["m1"])

        assert '"quotes"' in result.responses["m1"]
        assert "\\backslashes\\" in result.responses["m1"]


@pytest.mark.slow
class TestPerformance:
    """Performance tests for consortium queries."""

    @patch("gptme_consortium.tools.consortium._query_single_model")
    @patch("gptme_consortium.tools.consortium._synthesize_consensus")
    def test_query_time_with_multiple_models(self, mock_synthesize, mock_query):
        """Test query time with multiple models."""
        mock_query.return_value = "Response"
        mock_synthesize.return_value = {
            "consensus": "Answer",
            "confidence": 0.8,
            "reasoning": "Reason",
        }

        start_time = time.time()
        result = query_consortium(question="Test", models=["m1", "m2", "m3", "m4"])
        elapsed = time.time() - start_time

        # Should complete quickly with mocked models
        assert elapsed < 5.0, f"Query took {elapsed}s, expected < 5s"
        assert len(result.responses) == 4

    @patch("gptme_consortium.tools.consortium._query_single_model")
    @patch("gptme_consortium.tools.consortium._synthesize_consensus")
    def test_multiple_queries(self, mock_synthesize, mock_query):
        """Test multiple consortium queries in sequence."""
        mock_query.return_value = "Response"
        mock_synthesize.return_value = {
            "consensus": "Answer",
            "confidence": 0.8,
            "reasoning": "Reason",
        }

        start_time = time.time()

        # Run 3 queries
        for i in range(3):
            result = query_consortium(question=f"Question {i}", models=["m1", "m2"])
            assert result.question == f"Question {i}"

        elapsed = time.time() - start_time

        # All 3 should complete reasonably quickly
        assert elapsed < 10.0, f"3 queries took {elapsed}s, expected < 10s"
