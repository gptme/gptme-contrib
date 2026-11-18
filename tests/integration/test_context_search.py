"""Exercise the standalone Context.dev CLI over local HTTPS, without API credits."""

import json
import os
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/context-search.py"
KEY = "context-e2e-synthetic-credential"
RESULT: dict[str, Any] = {
    "url": "https://example.com/source",
    "title": "Public source",
    "description": "Discovery snippet",
    "highlights": {"code": "SUCCESS", "highlights": ["Relevant passage"]},
}


def test_context_search_cli(tmp_path: Path) -> None:
    """Run the public uv entrypoint; only its DNS destination is redirected."""
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "UV_PYTHON": str(Path(sys.executable).resolve()),
        "UV_NO_CONFIG": "1",
        "NO_COLOR": "1",
    }
    # Install the script's declared dependencies before disabling network access.
    warmup = subprocess.run(
        ["uv", "run", "--script", str(SCRIPT), "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert warmup.returncode == 0, warmup.stderr
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=api.context.dev",
            "-addext",
            "subjectAltName=DNS:api.context.dev",
        ],
        capture_output=True,
        timeout=30,
        check=True,
    )
    response_body: object = {}
    response_status = 200
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_POST(self) -> None:
            requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": json.loads(
                        self.rfile.read(int(self.headers["Content-Length"]))
                    ),
                }
            )
            self.send_response(response_status)
            self.send_header("Content-Type", "application/json")
            if response_status == 302:
                self.send_header("Location", "https://example.com/credential-target")
            self.end_headers()
            self.wfile.write(
                response_body
                if isinstance(response_body, bytes)
                else json.dumps(response_body).encode()
            )

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        shim = tmp_path / "shim"
        shim.mkdir()
        (shim / "sitecustomize.py").write_text(
            "import socket\n"
            "_original = socket.getaddrinfo\n"
            "def getaddrinfo(host, port, *args, **kwargs):\n"
            f"    if host == 'api.context.dev': host, port = '127.0.0.1', {server.server_port}\n"
            "    elif host not in ('127.0.0.1', 'localhost'): raise OSError('Offline fixture only')\n"
            "    return _original(host, port, *args, **kwargs)\n"
            "socket.getaddrinfo = getaddrinfo\n"
        )
        env.update(
            CONTEXT_DEV_API_KEY=KEY,
            SSL_CERT_FILE=str(cert),
            PYTHONPATH=str(shim),
            UV_OFFLINE="1",
        )
        cases = []

        def run(
            name: str,
            options: list[str],
            payload: object = None,
            status: int = 200,
            expected_code: int = 0,
            text: str | None = None,
        ) -> subprocess.CompletedProcess[str]:
            nonlocal response_body, response_status
            response_body = payload if payload is not None else {"results": [RESULT]}
            response_status = status
            requests.clear()
            proc = subprocess.run(
                ["uv", "run", "--script", str(SCRIPT), "test", *options],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert proc.returncode == expected_code, (name, proc.stdout, proc.stderr)
            assert KEY not in proc.stdout + proc.stderr
            assert "Traceback" not in proc.stderr
            assert len(requests) == 1
            assert requests[0]["path"] == "/v1/web/search"
            assert requests[0]["authorization"] == "Bearer " + KEY
            if text:
                assert text in proc.stdout + proc.stderr
            cases.append({"name": name, "exit_code": proc.returncode})
            return proc

        try:
            proc = run("success_json", ["--json"])
            assert json.loads(proc.stdout) == {
                "query": "test",
                "results": [RESULT],
                "partial": False,
            }
            assert requests[0]["body"] == {
                "query": "test",
                "numResults": 10,
                "highlightsOptions": {"enabled": True},
            }
            proc = run("success_text", [], text="Highlights (search evidence):")
            assert RESULT["url"] in proc.stdout
            assert "Relevant passage" in proc.stdout
            partial = {
                "results": [RESULT],
                "partial": True,
                "key_metadata": {"credits_remaining": 12345},
            }
            proc = run("partial_json", ["--json"], partial)
            assert json.loads(proc.stdout)["partial"] is True
            assert "key_metadata" not in proc.stdout
            run("partial_text", [], partial, text="Partial results:")
            for name, metadata, code in [
                ("missing", None, "NOT_RETURNED"),
                ("null", None, "NOT_RETURNED"),
                ("empty", {}, "NOT_RETURNED"),
                (
                    "failed",
                    {"code": "TIMEOUT", "highlights": ["Unusable passage"]},
                    "TIMEOUT",
                ),
                (
                    "unknown",
                    {"code": "FUTURE_CODE", "highlights": ["Unusable passage"]},
                    "FUTURE_CODE",
                ),
            ]:
                result = {k: v for k, v in RESULT.items() if k != "highlights"}
                if name != "missing":
                    result["highlights"] = metadata
                proc = run(name + "_highlights", ["--json"], {"results": [result]})
                assert json.loads(proc.stdout)["results"][0] == {
                    **RESULT,
                    "highlights": {"code": code, "highlights": None},
                }
            empty = {**RESULT, "highlights": {"code": "SUCCESS", "highlights": []}}
            run(
                "success_empty_highlights",
                [],
                {"results": [empty]},
                text="Discovery snippet",
            )
            disabled = {
                **RESULT,
                "highlights": {"code": "NOT_REQUESTED", "highlights": None},
            }
            proc = run(
                "no_highlights", ["--json", "--no-highlights"], {"results": [disabled]}
            )
            assert requests[0]["body"]["highlightsOptions"] == {"enabled": False}
            assert json.loads(proc.stdout)["results"] == [disabled]
            for name, payload in [
                ("non_json", b"not json"),
                ("missing_results", {}),
                ("bad_url", {"results": [{**RESULT, "url": None}]}),
                ("bad_partial", {"results": [], "partial": "false"}),
                (
                    "bad_highlights",
                    {
                        "results": [
                            {
                                **RESULT,
                                "highlights": {"code": "SUCCESS", "highlights": [5]},
                            }
                        ]
                    },
                ),
            ]:
                run(name, [], payload, expected_code=1, text="response")
            for status in [302, 401, 500]:
                run("http_" + str(status), [], {"error": KEY}, status, 1, str(status))
        finally:
            server.shutdown()
            thread.join(timeout=5)
    artifact = tmp_path / "context-search-results.json"
    artifact.write_text(
        json.dumps({"passed": len(cases), "cases": cases}, indent=2) + "\n"
    )
    print(f"Context CLI HTTPS evidence: {artifact}")
