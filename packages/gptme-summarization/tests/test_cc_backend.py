"""Tests for cc_backend module."""

from gptme_summarization.cc_backend import extract_json_from_response


def test_extract_json_plain():
    """Test extracting plain JSON."""
    response = '{"key": "value", "list": [1, 2, 3]}'
    result = extract_json_from_response(response)
    assert result == {"key": "value", "list": [1, 2, 3]}


def test_extract_json_code_block():
    """Test extracting JSON from markdown code block."""
    response = """Here's the result:

```json
{"accomplishments": ["did thing 1", "did thing 2"]}
```

That's all."""
    result = extract_json_from_response(response)
    assert result["accomplishments"] == ["did thing 1", "did thing 2"]


def test_extract_json_code_block_no_lang():
    """Test extracting JSON from code block without language tag."""
    response = """```
{"key": "value"}
```"""
    result = extract_json_from_response(response)
    assert result == {"key": "value"}


def test_extract_json_embedded():
    """Test extracting JSON embedded in text."""
    response = 'The result is {"key": "value"} as requested.'
    result = extract_json_from_response(response)
    assert result == {"key": "value"}


def test_extract_json_empty_response():
    """Test handling empty response."""
    result = extract_json_from_response("")
    assert result == {}


def test_extract_json_no_json():
    """Test handling response with no JSON."""
    result = extract_json_from_response("This is just text with no JSON at all.")
    assert result == {}


def test_extract_json_invalid_json():
    """Test handling invalid JSON."""
    result = extract_json_from_response("{invalid: json}")
    assert result == {}


def test_extract_json_complex():
    """Test extracting complex JSON structure."""
    response = """```json
{
    "accomplishments": ["feature X done"],
    "decisions": [{"topic": "arch", "decision": "use Y", "rationale": "faster"}],
    "narrative": "Worked on feature X, decided to use Y for performance."
}
```"""
    result = extract_json_from_response(response)
    assert len(result["accomplishments"]) == 1
    assert result["decisions"][0]["topic"] == "arch"
    assert "feature X" in result["narrative"]
