"""CLI JSON protocol, private credentials, redirect refusal and SSH argument boundaries."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from shadow_mdc.skill_cli import main


class Server(BaseHTTPRequestHandler):
    authorization: str | None = None
    requests: ClassVar[list[tuple[str, str]]] = []

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        type(self).authorization = self.headers.get("Authorization")
        type(self).requests.append(("GET", self.path))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"capabilities":[{"name":"works.query","effect":"read"}]}')

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(("POST", self.path))
        self.send_response(200)
        self.end_headers()
        result = {"ok": body["name"] != "missing", "result": body["arguments"]}
        self.wfile.write(json.dumps(result).encode())


def config(tmp_path: Path, base_url: str) -> Path:
    token = tmp_path / "token"
    token.write_text("REPLACE_PRIVATE_TEST")
    token.chmod(0o600)
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"base_url": base_url, "token_file": str(token), "owner": "test-owner"}))
    path.chmod(0o600)
    return path


def test_cli_http_catalog_call_errors_and_private_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        path = config(tmp_path, f"http://127.0.0.1:{server.server_port}")
        assert main(["--config", str(path), "catalog"]) == 0
        assert "REPLACE_PRIVATE_TEST" not in capsys.readouterr().out
        assert Server.authorization == "Bearer REPLACE_PRIVATE_TEST"
        assert main(["--config", str(path), "describe", "works.query"]) == 0
        assert json.loads(capsys.readouterr().out)["effect"] == "read"
        data = tmp_path / "input.json"
        data.write_text('{"q":"A & B"}')
        assert main(["--config", str(path), "call", "works.query", str(data)]) == 0
        assert json.loads(capsys.readouterr().out)["result"] == {"q": "A & B"}
        assert main(["--config", str(path), "call", "missing", str(data)]) == 1
        assert json.loads(capsys.readouterr().out)["ok"] is False
        path.chmod(0o644)
        assert main(["--config", str(path), "catalog"]) == 1
        output = capsys.readouterr().out
        assert "REPLACE_PRIVATE_TEST" not in output and str(path) not in output
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_cli_refuses_redirects_with_credentials(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class Redirect(Server):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", "https://example.com/should-not-receive-token")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        path = config(tmp_path, f"http://127.0.0.1:{server.server_port}")
        assert main(["--config", str(path), "catalog"]) == 1
        assert json.loads(capsys.readouterr().out)["error"]["status"] == 302
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_cli_ssh_keeps_json_in_stdin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "ssh.json"
    path.write_text(
        json.dumps(
            {
                "transport": "ssh",
                "ssh_host": "fixture-host",
                "remote_launcher": "/fixture root/scripts/mdc",
                "remote_config": "/private/config.json",
            }
        )
    )
    path.chmod(0o600)
    data = tmp_path / "input.json"
    data.write_text('{"q":"$(echo do-not-execute)"}')
    calls: list[tuple[list[str], bytes]] = []

    def run(
        args: list[str],
        *,
        input: bytes,
        capture_output: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((args, input))
        return subprocess.CompletedProcess(args, 0, stdout=b'{"ok":true,"result":{}}', stderr=b"")

    monkeypatch.setattr("shadow_mdc.skill_cli.subprocess.run", run)
    assert main(["--config", str(path), "call", "works.query", str(data)]) == 0
    assert "do-not-execute" not in calls[0][0][-1]
    assert json.loads(calls[0][1])["q"] == "$(echo do-not-execute)"
    assert "fixture root/scripts/mdc'" in calls[0][0][-1]
    assert json.loads(capsys.readouterr().out)["ok"]


def test_cli_batch_executes_over_http_and_resumes_without_duplicate_calls(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        path = config(tmp_path, f"http://127.0.0.1:{server.server_port}")
        manifest = tmp_path / "manifest.json"
        manifest.write_text('{"calls":[{"arguments":{"code":"A-001"}},{"arguments":{"code":"B-002"}}]}')
        args = [
            "--config",
            str(path),
            "batch",
            str(manifest),
            "--receipt",
            str(tmp_path / "receipt.jsonl"),
            "--pace-seconds",
            "0",
        ]
        before = len(Server.requests)
        assert main(args) == 0
        assert json.loads(capsys.readouterr().out)["counts"] == {"completed": 2}
        assert main(args) == 0
        assert len(Server.requests) - before == 2
        assert "REPLACE_PRIVATE_TEST" not in capsys.readouterr().out
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
