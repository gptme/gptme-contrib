"""Tests for the gptme fallback backend (gptme_backend.py).

Covers:
- DeepSeek reasoning-model <think>...</think> tag stripping so that embedded
  JSON inside think blocks does not confuse the downstream JSON extractor.
- JSON-shaped preamble handling: a preamble emitted as a separate assistant
  message must never be returned in place of the actual summary. The parser
  must scan all JSON-parseable assistant candidates and prefer the one
  containing a recognised summary key, with the final answer winning over an
  earlier preamble.
- JSON answer then later prose: later prose only wins if it *embeds* a
  summary JSON object. Completion-ack messages ("I have completed the
  analysis") and gptme trailers must not displace a JSON answer. This is
  the 2026-08-30 dancing-sad-monster regression.
- Logging paths that surface what was extracted (regression-guard for silent
  fallback failures where nothing was logged).
"""

import json

from gptme_activity_summary.cc_backend import extract_json_from_response
from gptme_activity_summary.gptme_backend import (
    _DEFAULT_MODEL,
    _extract_assistant_text,
    _has_summary_json,
    _is_gptme_trailer,
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


def test_json_answer_then_completion_ack_keeps_json():
    """Completion-ack prose must not displace a JSON summary.

    Live 2026-08-30 failure (gptme log ``dancing-sad-monster``): DeepSeek
    emitted a complete ``narrative`` JSON, then "I have completed the
    analysis. The full JSON summary ... was emitted in my previous
    message." That ack mentions the word ``narrative`` but has no JSON
    object. Preferring it made ``extract_json_from_response`` return {} and
    the systemd unit failed 12/12 even though the real summary was in the
    previous assistant message.
    """
    answer = json.dumps({"narrative": "REAL summary", "accomplishments": ["a"]})
    ack = (
        "I have completed the analysis. The full JSON summary for 2026-08-29 "
        "was emitted in my previous message. The work is done — it has a "
        "top-level narrative field as requested.\n\n```complete\n```"
    )
    ndjson = "\n".join([_msg(answer), _msg(ack)])
    out = _extract_assistant_text(ndjson)
    parsed = json.loads(out)
    assert parsed.get("narrative") == "REAL summary"
    assert "I have completed" not in out


def test_json_draft_then_later_prose_with_summary_json_prefers_later():
    """Later prose still wins when it *embeds* a summary JSON object.

    A model can emit a JSON draft, then a fenced JSON revision. That later
    payload is the real answer; keep preferring it over the draft.
    """
    draft = json.dumps({"narrative": "draft preamble", "accomplishments": ["x"]})
    prose = "Final answer:\n" + json.dumps(
        {"narrative": "revised summary", "accomplishments": ["y"]}
    )
    ndjson = "\n".join([_msg(draft), _msg(prose)])
    out = _extract_assistant_text(ndjson)
    assert out == prose
    assert _has_summary_json(out)
    parsed = extract_json_from_response(out)
    assert parsed.get("narrative") == "revised summary"


def test_json_answer_then_gptme_trailer_keeps_json():
    """gptme's no-tool-call auto-reply must not displace a JSON summary.

    Production path: first assistant message is the JSON answer, then gptme
    injects "No tool call detected ... use the `complete` tool". Preferring
    that later prose would return the trailer instead of the real answer and
    re-break the daily-summary fallback.
    """
    answer = json.dumps({"narrative": "REAL summary", "accomplishments": ["a"]})
    trailer = (
        "<system>No tool call detected in last message. Did you mean to finish? "
        "If so, make sure you are completely done and then use the `complete` "
        "tool to end the session.</system>"
    )
    ndjson = "\n".join([_msg(answer), _msg(trailer)])
    out = _extract_assistant_text(ndjson)
    parsed = json.loads(out)
    assert parsed.get("narrative") == "REAL summary"


def test_json_draft_then_ack_then_trailer_keeps_json():
    """Completion-ack between a JSON answer and the gptme trailer still loses."""
    draft = json.dumps({"narrative": "kept summary"})
    ack = "I have completed the analysis. The full JSON summary was emitted above."
    trailer = "No tool call detected in last message."
    ndjson = "\n".join([_msg(draft), _msg(ack), _msg(trailer)])
    out = _extract_assistant_text(ndjson)
    parsed = json.loads(out)
    assert parsed.get("narrative") == "kept summary"


def test_truncated_think_json_then_complete_json_then_ack():
    """Live shape: truncated think-JSON, complete think-JSON, then an ack.

    Mirrors the 2026-08-30 dancing-sad-monster fallback: first assistant
    message is cut off mid-JSON inside ``<think>``, second is a complete
    summary JSON after a think block, third is a completion-ack. The
    extractor must return the complete JSON, not the ack.
    """
    truncated = (
        "<think>I'll emit JSON.</think>\n"
        '{"accomplishments": ["partial"], "blockers": [{"issue": "unterminated"'
    )
    complete = "<think>I was cut off. Finishing the JSON.</think>\n" + json.dumps(
        {
            "narrative": "recovered daily summary",
            "accomplishments": ["parser fix"],
            "blockers": [{"issue": "oauth expired", "status": "active"}],
        }
    )
    ack = (
        "I have completed the analysis. The full JSON summary was emitted in "
        "my previous message. It includes narrative, accomplishments, and "
        "blockers."
    )
    ndjson = "\n".join([_msg(truncated), _msg(complete), _msg(ack)])
    out = _extract_assistant_text(ndjson)
    parsed = extract_json_from_response(out)
    assert parsed.get("narrative") == "recovered daily summary"
    assert parsed.get("accomplishments") == ["parser fix"]


def test_thinking_json_then_prose_then_trailer_skips_trailer():
    """No-summary JSON plus later prose must not lose to gptme's trailer.

    When no JSON candidate has a summary key, the fallback used to return
    ``non_json[-1]``, which is the trailer if gptme auto-replied after the
    plain-text answer.
    """
    preamble = json.dumps({"thinking": "reasoning"})
    prose = "Bob shipped many fixes today."
    trailer = (
        "<system>No tool call detected in last message. Did you mean to finish? "
        "If so, make sure you are completely done and then use the `complete` "
        "tool to end the session.</system>"
    )
    ndjson = "\n".join([_msg(preamble), _msg(prose), _msg(trailer)])
    out = _extract_assistant_text(ndjson)
    assert out == prose


def test_plain_text_then_trailer_skips_trailer():
    """With no JSON at all, skip gptme's trailer and return the last prose."""
    prose = "Bob shipped the think-tag parser."
    trailer = "No tool call detected in last message."
    ndjson = "\n".join([_msg(prose), _msg(trailer)])
    out = _extract_assistant_text(ndjson)
    assert out == prose


def test_complete_tool_fence_is_gptme_trailer():
    """A bare complete tool fence is a gptme protocol trailer, not content."""
    trailer = "```complete\n```"
    assert _is_gptme_trailer(trailer)
    assert _extract_assistant_text("\n".join([_msg("real prose"), _msg(trailer)])) == ("real prose")


def test_malformed_summary_then_complete_fence_recovers_root_json():
    """Recover a one-token malformed root summary before a complete trailer.

    Live 2026-08-30 failure (gptme log ``running-hungry-monster``): DeepSeek
    closed one object in the root ``decisions`` array with ``]`` instead of
    ``}``, then emitted a bare ``complete`` fence. Returning that fence made
    the caller find an unrelated nested news object rather than the root
    summary.
    """
    malformed = json.dumps(
        {
            "accomplishments": ["parser fix"],
            "decisions": [
                {
                    "topic": "fallback parser",
                    "decision": "repair one closing delimiter",
                    "rationale": "the rest of the root JSON is complete",
                }
            ],
            "blockers": [{"issue": "quota", "status": "active"}],
            "themes": ["reliability"],
            "work_in_progress": ["missing-day live verification"],
            "narrative": "REAL summary",
            "interactions": [{"type": "conversation", "person": "Erik"}],
        }
    )
    bad_delimiter = malformed.index("}", malformed.index('"rationale"'))
    malformed = malformed[:bad_delimiter] + "]" + malformed[bad_delimiter + 1 :]
    trailer = "```complete\n```"

    out = _extract_assistant_text("\n".join([_msg(malformed), _msg(trailer)]))

    parsed = extract_json_from_response(out)
    assert parsed.get("narrative") == "REAL summary"
    assert parsed.get("decisions", [{}])[0].get("topic") == "fallback parser"
    assert parsed.get("blockers") == [{"issue": "quota", "status": "active"}]
    assert parsed.get("themes") == ["reliability"]
    assert parsed.get("work_in_progress") == ["missing-day live verification"]
    assert parsed.get("interactions") == [{"type": "conversation", "person": "Erik"}]


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


def test_call_gptme_isolates_parent_session_logs(monkeypatch):
    """Nested gptme must not inherit the parent session's GPTME_LOGS_HOME/NAME.

    Incident 2026-08-30: a parent autonomous session exported
    GPTME_LOGS_HOME=/tmp/gptme-logs-<id> and GPTME_NAME=autonomous-<id>.
    call_gptme forwarded that env, so the fallback appended into the parent
    conversation.jsonl and stdout mixed parent+child assistant messages.
    """
    import subprocess
    from unittest.mock import patch

    monkeypatch.setenv("GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK", "1")
    monkeypatch.setenv("GPTME_LOGS_HOME", "/tmp/gptme-logs-parent-session")
    monkeypatch.setenv("GPTME_NAME", "autonomous-parent")
    monkeypatch.setenv("GPTME_AGENT_NAME", "bob")
    monkeypatch.setenv("GPTME_WORKSPACE", "/home/bob/bob")
    monkeypatch.setenv("GPTME_SUBPROCESS", "1")

    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        ndjson = _msg('{"narrative": "isolated"}')
        return subprocess.CompletedProcess(args=["gptme"], returncode=0, stdout=ndjson)

    with (
        patch("gptme_activity_summary.gptme_backend.shutil.which", return_value="/usr/bin/gptme"),
        patch("gptme_activity_summary.gptme_backend.subprocess.run", side_effect=fake_run),
    ):
        out = call_gptme("summarize")

    env = captured["env"]
    assert out == '{"narrative": "isolated"}'
    assert env.get("GPTME_SUBPROCESS") is None
    assert env.get("GPTME_NAME") is None
    assert env.get("GPTME_AGENT_NAME") is None
    assert env.get("GPTME_WORKSPACE") is None
    logs_home = env.get("GPTME_LOGS_HOME")
    assert logs_home, "must set a private GPTME_LOGS_HOME"
    assert logs_home != "/tmp/gptme-logs-parent-session"
    assert "gptme-activity-summary-" in logs_home
    # Isolated dir is cleaned up after the call.
    from pathlib import Path

    assert not Path(logs_home).exists()


def test_call_gptme_returns_empty_when_tempdir_fails(monkeypatch):
    """mkdtemp OSError must not escape call_gptme (never-raise contract).

    Greptile on gptme/gptme-contrib#1552: tempdir creation sat outside the
    try, so a full/unwritable temp filesystem raised before the adapter's
    error handler and crashed the activity-summary job instead of degrading.
    """
    from unittest.mock import patch

    monkeypatch.setenv("GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK", "1")

    with (
        patch(
            "gptme_activity_summary.gptme_backend.shutil.which",
            return_value="/usr/bin/gptme",
        ),
        patch(
            "gptme_activity_summary.gptme_backend.tempfile.mkdtemp",
            side_effect=OSError("No space left on device"),
        ),
        patch("gptme_activity_summary.gptme_backend.subprocess.run") as run_mock,
    ):
        out = call_gptme("summarize")

    assert out == ""
    run_mock.assert_not_called()
