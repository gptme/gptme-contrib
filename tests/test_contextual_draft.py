"""Real SQLite/subprocess fixtures; no model or personal archive required."""

import argparse
import importlib.util
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import tracemalloc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "contextual_draft.py"
spec = importlib.util.spec_from_file_location("contextual_draft", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

TARGET = {
    "schema_version": 1,
    "role": "AXTextArea",
    "secure": False,
    "editable": True,
    "text": "Can we meet?",
}


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "goldfish.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE snapshots (
                snapshot_id TEXT, content TEXT, app TEXT, window_title TEXT,
                url TEXT, domain TEXT, captured_at INTEGER, last_seen_at INTEGER,
                focused_name TEXT, focused_role TEXT, focused_path TEXT);
            CREATE VIRTUAL TABLE snapshots_fts USING fts5(content,
                content='snapshots', content_rowid='rowid');
            CREATE TABLE working_memory (snapshot_id TEXT, added_at INTEGER);
            INSERT INTO snapshots VALUES ('s1','A synthetic meeting conversation',
                'test','test',NULL,NULL,100,200,NULL,'AXTextArea',NULL);
            INSERT INTO snapshots_fts(rowid,content)
                SELECT rowid,content FROM snapshots;
            INSERT INTO working_memory VALUES ('s1',200);
        """)
    return path


def test_sqlite_retrieval_is_read_only(database):
    before = database.read_bytes()
    client = module.Goldfish(database)
    for query in (None, "meeting"):
        rows = client.snapshots(query, 1)
        assert rows[0]["content"] == "A synthetic meeting conversation"
        assert rows[0]["is_live_target"] is False
        assert rows[0]["captured_at"] == 100
    assert database.read_bytes() == before
    assert client.snapshots('" OR nonexistent', 1) == []


def test_missing_or_incompatible_database_fails_closed(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(module.DraftError):
        module.Goldfish(path).snapshots(None, 1)
    assert not path.exists()
    with sqlite3.connect(path):
        pass
    with pytest.raises(module.DraftError, match="schema"):
        module.Goldfish(path).snapshots(None, 1)


def test_impersonator_receives_no_requests_or_token(database, tmp_path):
    calls = []

    class Impersonator(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"service":"goldfish_d","status":"ok"}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Impersonator)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Legacy discovery files exist and advertise an impostor with the expected
    # health JSON. The adapter ignores them entirely, including the token file.
    (tmp_path / "effective-port").write_text(str(server.server_port))
    (tmp_path / "api-token").write_text("synthetic-test-token")
    try:
        assert module.Goldfish(database).snapshots(None, 1)
        assert calls == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_sqlite_source_truncation(database):
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE snapshots SET content=?", ("START" + "x" * 20000 + "LATEST",)
        )
    row = module.Goldfish(database).snapshots(None, 1)[0]
    assert row["content_truncated"] and len(row["content"]) <= 12000
    assert row["content"].startswith("START")
    assert row["content"].endswith("LATEST")
    assert "middle omitted" in row["content"]


@pytest.mark.parametrize(
    "update",
    [
        {"secure": True},
        {"secure": None},
        {"editable": False},
        {"schema_version": 2},
        {"text": None},
    ],
)
def test_unsafe_target_refused(update):
    with pytest.raises(module.DraftError):
        module.validate_target(TARGET | update)


def test_excerpts_are_not_full_evidence():
    with pytest.raises(module.DraftError):
        module.normalize_snapshot({"snapshot_id": "s", "content_excerpt": "partial"})


def test_private_output_no_overwrite_or_symlink(tmp_path):
    path = tmp_path / "request.json"
    module.private_write(path, "private")
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(module.DraftError):
        module.private_write(path, "replacement")
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(module.DraftError):
        module.private_write(link, "replacement")
    assert path.read_text() == "private"


def test_large_json_read_is_bounded(tmp_path):
    path = tmp_path / "huge.json"
    with path.open("wb") as out:
        out.truncate(32 * 1024 * 1024)
    tracemalloc.start()
    try:
        with pytest.raises(module.DraftError, match="byte limit"):
            module.read_json(path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1_000_000


@pytest.mark.parametrize("size,accepted", [(20000, True), (20001, False)])
def test_reference_actual_byte_limit(tmp_path, size, accepted):
    path = tmp_path / "context.txt"
    path.write_bytes(b"x" * size)
    if accepted:
        assert len(module.read_bounded(path, 20000)) == size
    else:
        with pytest.raises(module.DraftError, match="byte limit"):
            module.read_bounded(path, 20000)


def test_fifo_input_rejected_without_blocking(tmp_path):
    fifo = tmp_path / "input"
    os.mkfifo(fifo)
    code = "import runpy,pathlib; m=runpy.run_path(__import__('sys').argv[1]); m['read_bounded'](pathlib.Path(__import__('sys').argv[2]),20000)"
    result = subprocess.run(
        [sys.executable, "-c", code, str(SCRIPT), str(fifo)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "regular file" in result.stderr


def test_prepare_cli_combines_explicit_context(database, tmp_path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps(TARGET))
    context = tmp_path / "preferences.txt"
    context.write_text("Prefer meetings after lunch.")
    out = tmp_path / "request.json"
    cmd = [
        sys.executable,
        str(SCRIPT),
        "prepare",
        "--target",
        str(target),
        "--context",
        str(context),
        "--goldfish-db",
        str(database),
        "--instruction",
        "Draft a reply",
        "--output",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    request = json.loads(out.read_text())
    assert request["agent_context"][0]["content"] == context.read_text()
    assert len(request["observations"]) == 1
    assert "Prefer meetings" not in proc.stdout
    # The same read bound is used in preparation, before retrieval or publication.
    context.write_bytes(b"x" * 21000)
    rejected = tmp_path / "rejected.json"
    cmd[-1] = str(rejected)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode != 0 and "byte limit" in proc.stderr
    assert not rejected.exists()


def test_generate_requires_disclosure_flag():
    args = argparse.Namespace(allow_model_disclosure=False)
    with pytest.raises(module.DraftError, match="allow-model-disclosure"):
        module.generate(args)


@pytest.fixture
def generation(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "instruction": "Reply",
                "target": TARGET,
                "agent_context": [{"content": "Prefer meetings after lunch"}],
            }
        )
    )
    args = argparse.Namespace(
        allow_model_disclosure=True,
        request=request,
        output=tmp_path / "draft.txt",
        model="test/synthetic",
    )
    return args


def test_generation_subprocess_contract(generation, tmp_path):
    exe = tmp_path / "gptme"
    exe.write_text(
        "#!/usr/bin/env python3\nimport sys\nassert sys.argv[1:3] == ['llm','generate']\nassert '--model' in sys.argv\nassert 'Prefer meetings after lunch' in sys.stdin.read()\nprint('How about after lunch?')\n"
    )
    exe.chmod(0o700)
    env = os.environ | {"PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
    cmd = [
        sys.executable,
        str(SCRIPT),
        "generate",
        "--request",
        str(generation.request),
        "--model",
        generation.model,
        "--allow-model-disclosure",
        "--output",
        str(generation.output),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert generation.output.read_text() == "How about after lunch?"
    assert generation.output.stat().st_mode & 0o777 == 0o600
    assert "after lunch" not in proc.stdout
    assert subprocess.run(cmd, capture_output=True, env=env).returncode != 0


@pytest.mark.parametrize("mode", ["nonzero", "empty", "timeout"])
def test_generation_failure_never_publishes_or_echoes(
    generation, tmp_path, monkeypatch, capsys, mode
):
    exe = tmp_path / "gptme"
    body = {
        "nonzero": "print('partial private draft'); sys.exit(1)",
        "empty": "pass",
        "timeout": "time.sleep(10)",
    }[mode]
    exe.write_text(
        "#!/usr/bin/env python3\nimport sys,time\nprint('SENSITIVE_PROVIDER_ERROR',file=sys.stderr,flush=True)\n"
        + body
        + "\n"
    )
    exe.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(module, "GENERATION_TIMEOUT", 0.2)
    with pytest.raises(module.DraftError) as error:
        module.generate(generation)
    captured = capsys.readouterr()
    assert "SENSITIVE" not in str(error.value) + captured.out + captured.err
    assert "partial private" not in captured.out + captured.err
    assert not generation.output.exists()


def test_unicode_prepare_and_generate_under_ascii_locale(database, tmp_path):
    env = {
        **os.environ,
        "LC_ALL": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
    }
    # Confirm this really exercises ASCII defaults, not an implicitly UTF-8 host.
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import locale; print(locale.getpreferredencoding(False))",
        ],
        capture_output=True,
        env=env,
        check=True,
    )
    if probe.stdout.strip().lower() not in (b"ascii", b"ansi_x3.4-1968", b"us-ascii"):
        pytest.skip("Interpreter does not expose ASCII locale defaults")
    target = tmp_path / "unicode-target.json"
    target.write_text(
        json.dumps(TARGET | {"text": "café 日本語"}, ensure_ascii=False),
        encoding="utf-8",
    )
    context = tmp_path / "context.txt"
    context.write_text("café 日本語", encoding="utf-8")
    request = tmp_path / "request.json"
    prep = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "prepare",
            "--target",
            str(target),
            "--context",
            str(context),
            "--goldfish-db",
            str(database),
            "--instruction",
            "Reply",
            "--output",
            str(request),
        ],
        capture_output=True,
        env=env,
    )
    assert prep.returncode == 0, prep.stderr
    assert (
        json.loads(request.read_text(encoding="utf-8"))["target"]["text"]
        == "café 日本語"
    )
    exe = tmp_path / "gptme"
    exe.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        "p=sys.stdin.read()\nassert 'caf\\u00e9' in p\n"
        "print('caf\\u00e9 \\u65e5\\u672c\\u8a9e')\n",
        encoding="utf-8",
    )
    exe.chmod(0o700)
    output = tmp_path / "draft.txt"
    gen = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "generate",
            "--request",
            str(request),
            "--model",
            "test/synthetic",
            "--allow-model-disclosure",
            "--output",
            str(output),
        ],
        capture_output=True,
        env=env,
    )
    assert gen.returncode == 0, gen.stderr
    assert output.read_bytes() == "café 日本語".encode()


@pytest.mark.parametrize("failure", ["fsync", "link"])
def test_output_publication_failure_leaves_no_partial_file(
    tmp_path, monkeypatch, failure
):
    output = tmp_path / "draft.txt"

    def fail(*args, **kwargs):
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(module.os, failure, fail)
    with pytest.raises(module.DraftError, match="publish completed"):
        module.private_write(output, "private draft")
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_partial_write_failure_with_real_file_limit(tmp_path):
    if sys.platform == "win32":
        pytest.skip("POSIX file size limits required")
    output = tmp_path / "draft.txt"
    code = """
import runpy,resource,signal,sys
from pathlib import Path
m=runpy.run_path(sys.argv[1])
resource.setrlimit(resource.RLIMIT_FSIZE,(1024,1024))
signal.signal(signal.SIGXFSZ,signal.SIG_IGN)
try:
    m['private_write'](Path(sys.argv[2]), 'x'*100000)
except m['DraftError']:
    sys.exit(0)
sys.exit(2)
"""
    proc = subprocess.run(
        [sys.executable, "-c", code, str(SCRIPT), str(output)],
        capture_output=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_preview_prepare_only_uses_fixture_without_model(database, tmp_path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps(TARGET), encoding="utf-8")
    preview = SCRIPT.parent / "contextual-draft" / "preview.py"
    result = subprocess.run(
        [
            sys.executable,
            str(preview),
            "--target",
            str(target),
            "--goldfish-db",
            str(database),
            "--prepare-only",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    path = Path(result.stdout.split("Private request: ", 1)[1].strip())
    request = json.loads(path.read_text(encoding="utf-8"))
    assert request["target"] == TARGET
    assert request["observations"][0]["snapshot_id"] == "s1"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not list(path.parent.glob("**/draft.txt"))


@pytest.mark.parametrize("accept", [True, False])
def test_direct_retained_session_protocol(database, tmp_path, accept):
    native = tmp_path / "swift"
    target = TARGET | {"selection": {"location": 0, "length": 0}}
    native.write_text(
        "#!/usr/bin/env python3\nimport json,sys\n"
        + "print("
        + repr(json.dumps(target))
        + ",flush=True)\n"
        + "command=json.loads(sys.stdin.readline())\nassert command['draft']=='Synthetic contextual reply'\n"
        + ("print('{\"inserted\":true}',flush=True)\n" if accept else "sys.exit(1)\n"),
        encoding="utf-8",
    )
    native.chmod(0o700)
    generator = tmp_path / "gptme"
    generator.write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nprint('Synthetic contextual reply')\n",
        encoding="utf-8",
    )
    generator.chmod(0o700)
    direct = SCRIPT.parent / "contextual-draft" / "direct.py"
    # Call execute directly: no notifications, platform override or real AX writes.
    code = "import runpy,argparse,pathlib,sys; m=runpy.run_path(sys.argv[1]); m['execute'](argparse.Namespace(context=[],query=None,goldfish_db=pathlib.Path(sys.argv[2]),model='test/synthetic'))"
    result = subprocess.run(
        [sys.executable, "-c", code, str(direct), str(database)],
        env={
            **os.environ,
            "PYTHONPATH": str(direct.parent),
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert (result.returncode == 0) is accept, result.stderr
    assert ("Draft inserted. Nothing sent." in result.stdout) is accept


def test_target_window_retrieval_skips_empty_echo_and_keeps_recent_evidence(database):
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE snapshots SET content=?,window_title=?",
            ("Useful evidence " * 20, "PR target"),
        )
        db.execute(
            "INSERT INTO snapshots VALUES ('echo','Empty compose','test','PR target',NULL,NULL,300,400,NULL,'AXTextArea',NULL)"
        )
    rows = module.Goldfish(database).snapshots(None, 2, window_title="PR target")
    assert [row["snapshot_id"] for row in rows] == ["s1"]


@pytest.mark.parametrize("body", [" world", "\n    indented\n", "x" * 16001])
def test_generation_preserves_whitespace_and_bounds_size(
    generation, tmp_path, monkeypatch, body
):
    exe = tmp_path / "gptme"
    exe.write_text(
        "#!/usr/bin/env python3\nprint(" + repr(body) + ")\n", encoding="utf-8"
    )
    exe.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    if len(body.encode()) > module.MAX_DRAFT:
        with pytest.raises(module.DraftError, match="16000-byte"):
            module.generate(generation)
        assert not generation.output.exists()
    else:
        module.generate(generation)
        assert generation.output.read_text() == body
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert events[0]["stage"] == "generation_started"
    assert events[1]["returncode"] == 0
    # Compare decoded string values: json.dumps() escapes newlines, so a logged
    # multi-line body would never match its encoded form.
    values = [v for e in events for v in e.values() if isinstance(v, str)]
    assert all(body not in v for v in values)


def test_generation_preserves_raw_carriage_returns(generation, tmp_path, monkeypatch):
    exe = tmp_path / "gptme"
    exe.write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n"
        "sys.stdout.buffer.write(b'first\\r\\nsecond\\rthird\\n')\n",
        encoding="utf-8",
    )
    exe.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    module.generate(generation)
    # Only the single CLI framing newline is removed; no newline translation.
    assert generation.output.read_bytes() == b"first\r\nsecond\rthird"


def test_retry_after_failed_attempt_keeps_successful_draft(
    generation, tmp_path, monkeypatch
):
    exe = tmp_path / "gptme"
    attempt = tmp_path / "attempt"
    # First call fails with stderr; the retry succeeds and also emits stderr.
    exe.write_text(
        "#!/usr/bin/env python3\nimport sys,pathlib\nsys.stdin.read()\n"
        f"marker = pathlib.Path({str(attempt)!r})\n"
        "print('provider warning', file=sys.stderr)\n"
        "if not marker.exists():\n    marker.touch()\n    sys.exit(1)\n"
        "print('Retry draft')\n",
        encoding="utf-8",
    )
    exe.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    with pytest.raises(module.DraftError, match="Model process failed"):
        module.generate(generation)
    assert not generation.output.exists()
    module.generate(generation)
    assert generation.output.read_text() == "Retry draft"
    diagnostics = sorted(tmp_path.glob("draft.txt.provider-stderr-*.txt"))
    assert len(diagnostics) == 2
    for path in diagnostics:
        assert path.read_text() == "provider warning\n"
        assert path.stat().st_mode & 0o777 == 0o600


def _load_launcher(name, monkeypatch):
    directory = SCRIPT.parent / "contextual-draft"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_direct_read_line_partial_line_times_out(monkeypatch):
    direct = _load_launcher("direct", monkeypatch)
    helper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time; sys.stdout.write('{'); sys.stdout.flush(); time.sleep(30)",
        ],
        stdout=subprocess.PIPE,
    )
    try:
        started = time.monotonic()
        with pytest.raises(direct.core.DraftError, match="timed out"):
            direct.read_line(helper, 0.5)
        assert time.monotonic() - started < 3
    finally:
        helper.kill()
        helper.wait()


def test_direct_write_line_to_stalled_helper_times_out(monkeypatch):
    direct = _load_launcher("direct", monkeypatch)
    # Helper never reads stdin, so the pipe fills and a blocking write would hang.
    helper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
    )
    try:
        started = time.monotonic()
        with pytest.raises(direct.core.DraftError, match="timed out"):
            direct.write_line(helper, b"x" * 4_000_000 + b"\n", 0.5)
        assert time.monotonic() - started < 3
    finally:
        helper.kill()
        helper.wait()


def test_direct_read_line_joins_chunks_until_newline(monkeypatch):
    direct = _load_launcher("direct", monkeypatch)
    helper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time; sys.stdout.write('{\"a\"'); sys.stdout.flush(); "
            "time.sleep(0.2); sys.stdout.write(': 1}\\n'); sys.stdout.flush(); time.sleep(5)",
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert json.loads(direct.read_line(helper, 5)) == {"a": 1}
    finally:
        helper.kill()
        helper.wait()


def test_preview_generation_escalates_for_sigterm_ignoring_group(monkeypatch, tmp_path):
    preview = _load_launcher("preview", monkeypatch)
    beat = tmp_path / "heartbeat"
    ignore = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    # Leader and its model-like child both ignore SIGTERM, as a hung generator
    # might. The child's heartbeat shows whether it still runs: unlike PID
    # existence, it is independent of when init reaps the killed orphan.
    child = ignore + (
        "import pathlib,sys\n"
        "while True:\n"
        "    with open(sys.argv[1], 'a') as f: f.write('.')\n"
        "    time.sleep(0.02)"
    )
    code = (
        ignore
        + "import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
        + repr(child)
        + ",sys.argv[1]]); time.sleep(60)"
    )
    generation = preview.Generation([sys.executable, "-c", code, str(beat)])
    deadline = time.monotonic() + 10
    while not beat.exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    started = time.monotonic()
    generation.stop(grace=0.3)
    assert time.monotonic() - started < 5
    assert generation.proc.returncode == -signal.SIGKILL
    size = beat.stat().st_size
    time.sleep(0.3)
    assert beat.stat().st_size == size, "SIGTERM-ignoring child survived"


def test_preview_generation_never_signals_reaped_group(monkeypatch):
    preview = _load_launcher("preview", monkeypatch)
    generation = preview.Generation([sys.executable, "-c", "pass"])
    assert generation.wait() == 0
    signalled = []
    monkeypatch.setattr(preview.os, "killpg", lambda *a: signalled.append(a))
    # Once reaped, the pid/pgid may be reused: closing must not signal it.
    generation.stop()
    assert signalled == []


def test_prepare_fits_multibyte_observations_to_byte_budget(database, tmp_path):
    rows = [(f"cjk{i}", "文" * 12000, 300 - i) for i in range(5)]
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM snapshots")
        db.executemany(
            "INSERT INTO snapshots VALUES (?,?,'test','t',NULL,NULL,1,?,NULL,NULL,NULL)",
            rows,
        )
    target = tmp_path / "target.json"
    target.write_text(json.dumps(TARGET | {"text": ""}))
    context = tmp_path / "context.md"
    context.write_text("Explicit context wins. " * 400)
    output = tmp_path / "request.json"
    module.prepare(
        argparse.Namespace(
            capture=False,
            target=target,
            context=[context],
            goldfish_db=database,
            query=None,
            limit=5,
            instruction="Reply",
            output=output,
        )
    )
    raw = output.read_bytes()
    assert len(raw) <= module.MAX_REQUEST
    request = json.loads(raw)
    # Explicit context is kept whole; observations fill the rest in order.
    assert request["agent_context"][0]["content"] == context.read_text()
    kept = [row["snapshot_id"] for row in request["observations"]]
    budget = request["coverage"]["budget"]
    assert kept == ["cjk0", "cjk1", "cjk2"]
    assert budget["truncated_snapshot_ids"] == ["cjk2"]
    assert budget["dropped_snapshot_ids"] == ["cjk3", "cjk4"]
    assert request["observations"][2]["content_truncated"] is True
    assert request["observations"][0]["content"] == "文" * 12000
