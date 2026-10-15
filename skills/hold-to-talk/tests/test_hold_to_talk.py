"""Exercise the subprocess boundary without audio hardware or a model API."""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def voice():
    script = Path(__file__).resolve().parents[1] / "scripts" / "hold-to-talk.py"
    spec = importlib.util.spec_from_file_location("hold_to_talk", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_gptme(tmp_path, monkeypatch, body):
    executable = tmp_path / "gptme"
    executable.write_text(f"#!{sys.executable}\n" + body)
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


def test_prompt_and_quoted_options_reach_cli(tmp_path, monkeypatch, voice):
    args_file = tmp_path / "args.json"
    fake_gptme(
        tmp_path,
        monkeypatch,
        "import json,sys\n"
        f"open({str(args_file)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        "print(json.dumps({'type':'message','role':'assistant','content':'Hello.'}))\n",
    )
    monkeypatch.setattr(voice, "GPTME_ARGS", '--system "Reply briefly" -m mock/echo')
    assert voice.ask_gptme("--spoken words") == "Hello."
    args = json.loads(args_file.read_text())
    assert "-p" not in args
    assert args[-2:] == ["--", "--spoken words"]
    assert args[args.index("--system") + 1] == "Reply briefly"


def test_failed_cli_does_not_become_silent_answer(tmp_path, monkeypatch, voice):
    fake_gptme(
        tmp_path,
        monkeypatch,
        "import sys\nprint('bad option', file=sys.stderr)\nsys.exit(2)\n",
    )
    with pytest.raises(RuntimeError, match="bad option"):
        voice.ask_gptme("hello")


def test_only_final_assistant_message_is_spoken(tmp_path, monkeypatch, voice):
    events = [
        {"type": "message", "role": "user", "content": "private prompt"},
        {"type": "message", "role": "assistant", "content": "First"},
        {"type": "message", "role": "assistant", "content": "Final"},
        {"type": "log", "role": "assistant", "content": "diagnostic"},
    ]
    fake_gptme(
        tmp_path,
        monkeypatch,
        f"print({chr(10).join(json.dumps(e) for e in events)!r})\n",
    )
    assert voice.ask_gptme("hello") == "Final"


def test_missing_assistant_does_not_speak_raw_protocol(tmp_path, monkeypatch, voice):
    fake_gptme(tmp_path, monkeypatch, "print('[1,2]')\nprint('not JSON')\n")
    with pytest.raises(RuntimeError, match="assistant"):
        voice.ask_gptme("hello")


def test_audio_file_survives_transcription_failure(tmp_path, monkeypatch, voice):
    from click.testing import CliRunner

    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"fixture")

    def fail(*args):
        raise RuntimeError("cannot transcribe")

    monkeypatch.setattr(voice, "transcribe", fail)
    result = CliRunner().invoke(voice.main, ["--audio-file", str(audio)])
    assert result.exit_code != 0
    assert audio.read_bytes() == b"fixture"


def test_recording_is_removed_after_transcription_failure(tmp_path, monkeypatch, voice):
    from click.testing import CliRunner

    audio = tmp_path / "recording.wav"
    audio.write_bytes(b"fixture")
    monkeypatch.setattr(voice, "record_for_duration", lambda _: audio)

    def fail(*args):
        raise RuntimeError("cannot transcribe")

    monkeypatch.setattr(voice, "transcribe", fail)
    result = CliRunner().invoke(voice.main, ["--duration", "1"])
    assert result.exit_code != 0
    assert not audio.exists()


def test_audio_file_demo_reaches_answer_without_capture(tmp_path, monkeypatch, voice):
    from click.testing import CliRunner

    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"fixture")
    monkeypatch.setattr(voice, "transcribe", lambda *_: "Hello from audio")
    fake_gptme(
        tmp_path,
        monkeypatch,
        "import json,sys\n"
        "assert sys.argv[-1] == 'Hello from audio'\n"
        "print(json.dumps({'type':'message','role':'assistant','content':'Hello back.'}))\n",
    )
    result = CliRunner().invoke(voice.main, ["--audio-file", str(audio), "--no-tts"])
    assert result.exit_code == 0, result.output
    assert "Hello back." in result.output
    assert audio.exists()


class _FakeKey:
    space = object()
    ctrl_l = object()


class _FakeKeyCode:
    @staticmethod
    def from_char(char):
        if len(char) != 1:
            raise ValueError("expected a single character")
        return ("char", char)


class _FakeKeyboard:
    Key = _FakeKey
    KeyCode = _FakeKeyCode


def fake_tts(tmp_path, monkeypatch, voice, body):
    executable = tmp_path / "tts"
    executable.write_text(f"#!{sys.executable}\n" + body)
    executable.chmod(0o755)
    monkeypatch.setattr(voice, "_tts_command", lambda: [str(executable)])


def test_named_and_single_char_keys_resolve(voice):
    assert voice._resolve_hold_key(_FakeKeyboard, "space") is _FakeKey.space
    assert voice._resolve_hold_key(_FakeKeyboard, "x") == ("char", "x")


def test_unknown_multi_char_key_raises_usage_error(voice):
    import click

    with pytest.raises(click.UsageError, match="Unknown key name"):
        voice._resolve_hold_key(_FakeKeyboard, "ctrl_zzz")


def test_dash_leading_answer_is_not_parsed_as_a_tts_option(
    tmp_path, monkeypatch, voice
):
    args_file = tmp_path / "tts-args.json"
    fake_tts(
        tmp_path,
        monkeypatch,
        voice,
        "import json,sys\n"
        f"open({str(args_file)!r}, 'w').write(json.dumps(sys.argv[1:]))\n",
    )
    voice.speak("--not-an-option")
    assert json.loads(args_file.read_text()) == [" --not-an-option"]


def test_broken_tts_surfaces_a_warning(tmp_path, monkeypatch, voice, capsys):
    fake_tts(tmp_path, monkeypatch, voice, "import sys\nsys.exit(3)\n")
    voice.speak("hello")
    assert "TTS command failed" in capsys.readouterr().err
