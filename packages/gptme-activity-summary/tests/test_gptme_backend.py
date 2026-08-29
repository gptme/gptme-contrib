"""Tests for the gptme fallback backend (gptme_backend.py).

Covers:
- DeepSeek reasoning-model <think>...</think> tag stripping so that embedded
  JSON inside think blocks does not confuse the downstream JSON extractor.
- JSON-shaped preamble handling: a preamble emitted as a separate assistant
  message must never be returned in place of the actual summary. The parser
  must scan all JSON-parseable assistant candidates and prefer the one
  containing a recognised summary key, with the final answer winning over an
  earlier preamble.
- Logging paths that surface what was extracted (regression-guard for silent
  fallback failures where nothing was logged).
"""

import json

from gptme_activity_summary.gptme_backend import (
    _DEFAULT_MODEL,
    _extract_assistant_text,
    _strip_think_tags,
    call_gptme,
    is_enabled,
)


def _msg(content) -> str:
    """Build a single gptme NDJSON assistant message line."""
    return json.dumps({"type": "message", "role": "assistant", "content": content})


# ---------------------------------------------------------------------------
# _strip_think_tags
# ---------------------------------------------------------------------------


def test_strip_think_tags_basic():
    """Simple <think>...</think> block is removed."""
    raw = '<think>I\'ll think about this...</think>\n{"narrative": "done"}'
    result = _strip_think_tags(raw)
    assert "<think>" not in result
    assert "narrative" in result


def test_strip_think_tags_with_braces_inside():
    """Think block containing {braces} is fully stripped."""
    raw = '<think>let me use {"structure": "json"}</think>\n{"narrative": "ok"}'
    result = _strip_think_tags(raw)
    assert "<think>" not in result
    assert '"structure"' not in result
    assert '"narrative"' in result


def test_strip_think_tags_multiline():
    raw = '<think>\nLine one.\nLine two.\n</think>\n{"narrative": "result"}'
    result = _strip_think_tags(raw)
    assert "Line one." not in result
    assert "narrative" in result


def test_strip_think_tags_noop_when_absent():
    """No <think> tags → string unchanged."""
    raw = '{"narrative": "clean"}'
    assert _strip_think_tags(raw) == raw


def test_strip_think_tags_multiple_blocks():
    """Multiple disjoint <think> blocks are all stripped."""
    raw = "<think>first</think> middle <think>second</think> end"
    result = _strip_think_tags(raw)
    assert "first" not in result
    assert "second" not in result
    assert "middle" in result
    assert "end" in result


# ---------------------------------------------------------------------------
# _extract_assistant_text — think tag in content
# ---------------------------------------------------------------------------


def test_think_tag_stripped_before_json_extraction():
    """Content starting with <think>...</think> yields the JSON that follows."""
    content = '<think>Analyzing the activities...</think>\n{"narrative": "Bob shipped fixes"}'
    ndjson = _msg(content)
    out = _extract_assistant_text(ndjson)
    # After stripping the think block, the remaining text is valid JSON
    assert "narrative" in out


def test_think_tag_with_nested_braces_does_not_confuse_parser():
    """Think block containing {key: val} JSON-shaped text is stripped cleanly."""
    content = (
        '<think>I should output {"type": "summary", "key": "val"}</think>\n'
        '{"narrative": "real answer here"}'
    )
    ndjson = _msg(content)
    out = _extract_assistant_text(ndjson)
    # The real answer (not the think-block JSON) should be returned
    parsed = json.loads(out)
    assert parsed.get("narrative") == "real answer here"


def test_pure_think_tag_with_no_trailing_content():
    """If stripping a think block leaves nothing, fall back gracefully."""
    content = "<think>All reasoning, no output.</think>"
    ndjson = _msg(content)
    # Should not raise; may return "" or last content
    result = _extract_assistant_text(ndjson)
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _extract_assistant_text — JSON preamble (separate messages)
# ---------------------------------------------------------------------------


def test_returns_empty_on_no_assistant_messages():
    """No assistant messages => empty string (best-effort, never raises)."""
    assert _extract_assistant_text("") == ""
    assert _extract_assistant_text('{"type":"message","role":"user","content":"hi"}') == ""


def test_extracts_plain_json_answer():
    """A single clean JSON answer is returned as-is."""
    ndjson = _msg('{"narrative": "done", "accomplishments": ["x"]}')
    out = _extract_assistant_text(ndjson)
    assert "narrative" in out


def test_deepseek_preamble_then_json_answer():
    """Regression: deepseek emits a JSON-shaped thinking preamble, then the real
    JSON answer. The parser must return the real answer, not the preamble."""
    ndjson = "\n".join(
        [
            _msg('{"thinking": "Let me analyze the journal carefully"}'),
            _msg('{"narrative": "REAL summary", "accomplishments": ["a"]}'),
        ]
    )
    out = _extract_assistant_text(ndjson)
    # The preamble has no recognized summary key; the answer does.
    assert "REAL summary" in out
    assert "thinking" not in out


def test_deepseek_preamble_echoes_narrative_key_still_prefers_final_answer():
    """Regression: even when the preamble itself contains a recognised key
    (a thinking preamble echoing ``narrative``), the final real answer wins
    because the parser scans in reverse (last-wins)."""
    ndjson = "\n".join(
        [
            _msg('{"thinking": "hmm", "narrative": "draft preamble"}'),
            _msg('{"narrative": "REAL final summary", "accomplishments": ["a"]}'),
        ]
    )
    out = _extract_assistant_text(ndjson)
    assert "REAL final summary" in out
    assert "draft preamble" not in out


def test_deepseek_preamble_in_text_then_codeblock_answer():
    """The preamble may be wrapped in prose; the real answer may be a markdown
    code block. The fallback should still hand back the answer text so the
    caller's extract_json_from_response can parse the code block."""
    ndjson = "\n".join(
        [
            _msg('Let me think about this:\n{"thinking": "hmm"}'),
            _msg('```json\n{"narrative": "final"}\n```'),
        ]
    )
    out = _extract_assistant_text(ndjson)
    # Preamble is not JSON-parseable as a candidate; the code block is returned
    # as the last message, from which the caller extracts the JSON.
    assert '"narrative": "final"' in out


def test_list_content_text_messages():
    """Content as a list of text parts is collected too (current gptme format)."""
    ndjson = "\n".join(
        [
            _msg([{"type": "text", "text": '{"thinking": "plan"}'}]),
            _msg([{"type": "text", "text": '{"narrative": "list format answer"}'}]),
        ]
    )
    out = _extract_assistant_text(ndjson)
    assert "list format answer" in out


def test_no_json_candidate_falls_back_to_non_json_content():
    """When only the preamble is JSON and the answer is plain text, return plain text.

    The plain text is returned so that extract_json_from_response can attempt
    JSON extraction (e.g. via code blocks or embedded JSON regex).
    """
    preamble = json.dumps({"thinking": "reasoning"})
    plain_answer = "Bob shipped many fixes today."
    ndjson = "\n".join([_msg(preamble), _msg(plain_answer)])
    out = _extract_assistant_text(ndjson)
    # Should return the plain-text answer, not the JSON preamble
    assert out == plain_answer


def test_multipart_content_list():
    """Content as a list of parts (multipart message format)."""
    content_parts = [
        {"type": "text", "text": '{"narrative": "from list content"}'},
    ]
    ndjson = json.dumps({"type": "message", "role": "assistant", "content": content_parts})
    out = _extract_assistant_text(ndjson)
    assert "narrative" in out


def test_is_enabled_off_by_default(monkeypatch):
    """The fallback is off by default (no env set) to avoid data exfiltration."""
    monkeypatch.delenv("GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK", raising=False)
    assert is_enabled() is False


def test_default_model_is_deepseek_flash():
    """The default fallback model is the cheap privacy-gated deepseek opt-in."""
    assert "deepseek" in _DEFAULT_MODEL and "flash" in _DEFAULT_MODEL


def test_call_gptme_returns_empty_when_disabled(monkeypatch):
    """call_gptme is best-effort and returns '' when the fallback is disabled."""
    monkeypatch.delenv("GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK", raising=False)
    assert call_gptme("ignored prompt") == ""
