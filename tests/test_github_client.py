"""mu_spec.github: GitHubClient sends the declared headers and one attempt per request (S-96).

T-60 drives the client at a local http.server stand-in on 127.0.0.1 that records each request's
method, path and headers, with mu_spec.github.API_ROOT redirected to it by monkeypatch. A plain
in-process test: no acceptance input, no unit launched. The module under test is imported inside
the test, so while it is absent only this test fails.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HOST = "127.0.0.1"
QUERY_PATH = "repos/o/r/issues?state=all&per_page=100"


class Recorder(BaseHTTPRequestHandler):
    """Answers by path: 'ok' 200 {"a": 1}, 'busy' 503 with Retry-After and no body; records each request."""

    seen: list[tuple[str, str, dict[str, str]]]

    def _answer(self) -> None:
        self.server.seen.append((self.command, self.path, dict(self.headers.items())))
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path.endswith("/busy"):
            self.send_response(503)
            self.send_header("Retry-After", "2")
            self.send_header("X-RateLimit-Remaining", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps({"a": 1}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _answer

    def log_message(self, *args):
        pass


@pytest.fixture
def standin(monkeypatch):
    """A running recording stand-in, API_ROOT redirected to it; yields its list of seen requests."""
    import mu_spec.github

    server = ThreadingHTTPServer((HOST, 0), Recorder)
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(mu_spec.github, "API_ROOT", f"http://{HOST}:{server.server_address[1]}/")
    try:
        yield server.seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def header(headers: dict[str, str], name: str) -> str | None:
    """The recorded value of a header, its name matched without regard to case."""
    return next((v for k, v in headers.items() if k.lower() == name.lower()), None)


def test_headers_and_one_attempt(standin):
    from mu_spec.github import REQUEST_HEADERS, GitHubClient

    # 1. no token: 200 with its decoded body; every declared header exactly; no Authorization
    answer = GitHubClient(None).request("GET", "ok")
    assert answer.status == 200
    assert answer.body == {"a": 1}
    _, _, headers = standin[-1]
    for name, value in REQUEST_HEADERS:
        assert header(headers, name) == value, (name, headers)
    assert header(headers, "Authorization") is None, headers

    # 2. a token: Bearer
    GitHubClient("t").request("GET", "ok")
    _, _, headers = standin[-1]
    assert header(headers, "Authorization") == "Bearer t", headers

    # 3. a 503 is returned, not raised or retried; header names lower-cased, values unchanged
    began = time.monotonic()
    busy = GitHubClient("t").request("GET", "busy")
    took = time.monotonic() - began
    assert busy.status == 503
    assert busy.body is None
    assert busy.headers["retry-after"] == "2"
    assert busy.headers["x-ratelimit-remaining"] == "0"
    assert [p for _, p, _ in standin if p.endswith("/busy")] == ["/busy"], standin
    assert took < 2.0, took

    # 4. the path's query arrives as given
    GitHubClient("t").request("GET", QUERY_PATH)
    method, path, _ = standin[-1]
    assert method == "GET"
    assert path.endswith(QUERY_PATH), path
