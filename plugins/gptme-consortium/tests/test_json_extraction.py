"""Tests for JSON extraction in synthesis."""

from gptme_consortium.tools.consortium import _synthesize_consensus


class TestJSONExtraction:
    """Tests for improved JSON extraction from arbiter responses."""

    def test_json_in_markdown_code_block(self, monkeypatch):
        """Test extraction of JSON from markdown code block."""
        responses = {"model1": "Response 1"}

        # Mock the query to return JSON in markdown code block
        def mock_query(model, prompt):
            return """Here's the synthesis:
```json
{
    "consensus": "From markdown",
    "confidence": 0.85,
    "reasoning": "Extracted from code block"
}
```
"""

        monkeypatch.setattr(
            "gptme_consortium.tools.consortium._query_single_model", mock_query
        )

        result = _synthesize_consensus(
            question="Test", responses=responses, arbiter="test", threshold=0.8
        )

        assert result["consensus"] == "From markdown"
        assert result["confidence"] == 0.85
        assert result["reasoning"] == "Extracted from code block"

    def test_json_without_language_tag(self, monkeypatch):
        """Test extraction of JSON from code block without json tag."""
        responses = {"model1": "Response 1"}

        def mock_query(model, prompt):
            return """Response:
{
    "consensus": "No tag",
    "confidence": 0.75,
    "reasoning": "Block without tag"
}
"""

        monkeypatch.setattr(
            "gptme_consortium.tools.consortium._query_single_model", mock_query
        )

        result = _synthesize_consensus(
            question="Test", responses=responses, arbiter="test", threshold=0.8
        )

        assert result["consensus"] == "No tag"
        assert result["confidence"] == 0.75

    def test_json_embedded_in_text(self, monkeypatch):
        """Test extraction of JSON object embedded in text."""
        responses = {"model1": "Response 1"}

        def mock_query(model, prompt):
            return """After analyzing the responses, here is my synthesis:
{"consensus": "From text", "confidence": 0.9, "reasoning": "Found in text"}
This shows high agreement."""

        monkeypatch.setattr(
            "gptme_consortium.tools.consortium._query_single_model", mock_query
        )

        result = _synthesize_consensus(
            question="Test", responses=responses, arbiter="test", threshold=0.8
        )

        assert result["consensus"] == "From text"
        assert result["confidence"] == 0.9

    def test_arbiter_failure_handling(self, monkeypatch):
        """Test that arbiter failures return appropriate fallback."""
        responses = {"model1": "Response 1"}

        def mock_query(model, prompt):
            raise Exception("API rate limit exceeded")

        monkeypatch.setattr(
            "gptme_consortium.tools.consortium._query_single_model", mock_query
        )

        result = _synthesize_consensus(
            question="Test", responses=responses, arbiter="test", threshold=0.8
        )

        assert "Unable to synthesize consensus" in result["consensus"]
        assert result["confidence"] == 0.3
        assert "Arbiter model failed" in result["reasoning"]

    def test_confidence_as_float(self, monkeypatch):
        """Test that confidence is converted to float."""
        responses = {"model1": "Response 1"}

        def mock_query(model, prompt):
            return '{"consensus": "Test", "confidence": "0.95", "reasoning": "Test"}'

        monkeypatch.setattr(
            "gptme_consortium.tools.consortium._query_single_model", mock_query
        )

        result = _synthesize_consensus(
            question="Test", responses=responses, arbiter="test", threshold=0.8
        )

        assert isinstance(result["confidence"], float)
        assert result["confidence"] == 0.95
