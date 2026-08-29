"""Tests for the gptme fallback backend (gptme_backend.py).

Covers the deepseek reasoning-model preamble gap: a JSON-shaped thinking
preamble (e.g. ``{"thinking": "..."}``) emitted before the real answer must
never be returned in place of the actual summary. The parser must scan all
JSON-parseable assistant candidates and prefer the one containing a recognised
summary key, with the final answer winning over an earlier preamble.
"""

from gptme_activity_summary.gptme_backend import (
    _DEFAULT_MODEL,
    _extract_assistant_text,
    call_gptme,
    is_enabled,
)


def _msg(content) -> str:
    """Build a single gptme NDJSON assistant message line."""
    import json

    return json.dumps({"type": "message", "role": "assistant", "content": content})


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


def test_is_enabled_off_by_default():
    """The fallback is off by default (no env set) to avoid data exfiltration."""
    import os

    os.environ.pop("GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK", None)
    assert is_enabled() is False


def test_default_model_is_deepseek_flash():
    """The default fallback model is the cheap privacy-gated deepseek opt-in."""
    assert "deepseek" in _DEFAULT_MODEL and "flash" in _DEFAULT_MODEL


def test_call_gptme_returns_empty_when_disabled():
    """call_gptme is best-effort and returns '' when the fallback is disabled."""
    import os

    os.environ.pop("GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK", None)
    assert call_gptme("ignored prompt") == ""
