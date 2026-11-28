"""mu_spec.github: how no answer came, timed out or not, and a reason that never holds the token (S-96).

T-341 is a plain unit test of the rebuilt client: no acceptance input, no unit launched. S1 is a
socket server on 127.0.0.1 that reads each request up to the blank line ending its headers, records
it and never answers; P2 is a port bound and closed again, so nothing listens on it. That the reason
text equals the running unit's is held by the differential test_unreachable_wording_matches_running_unit
in tests/acceptance/test_github_isolation.py. The module under test is imported inside the test, so
while it is absent only this test fails.
"""

import socket
import threading
import time

import pytest

HOST = "127.0.0.1"
TOKEN = "secret-T0K3N"


class Silent:
    """S1: accepts, reads each request's head in full, records it, never answers."""

    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind((HOST, 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.requests: list[bytes] = []
        self.conns: list[socket.socket] = []
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def _read(self, conn: socket.socket) -> None:
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        if data:
            self.requests.append(data)

    def close(self) -> None:
        self.listener.close()
        for conn in self.conns:
            conn.close()


def closed_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def silent():
    s1 = Silent()
    try:
        yield s1
    finally:
        s1.close()


def test_timeout_and_refused(silent, monkeypatch):
    import mu_spec.github
    from mu_spec.github import TIMEOUT_SECONDS, GitHubClient, GitHubUnreachable

    caught = []

    # 1. S1 never answers: timed out, after TIMEOUT_SECONDS and one request
    monkeypatch.setattr(mu_spec.github, "API_ROOT", f"http://{HOST}:{silent.port}/")
    began = time.monotonic()
    with pytest.raises(GitHubUnreachable) as info:
        GitHubClient(TOKEN).request("GET", "x")
    took = time.monotonic() - began
    assert info.value.timed_out is True
    assert TIMEOUT_SECONDS <= took < TIMEOUT_SECONDS + 2.0, took
    assert len(silent.requests) == 1, silent.requests
    caught.append(info.value)

    # 2. nothing listens on P2: not timed out, and quickly
    monkeypatch.setattr(mu_spec.github, "API_ROOT", f"http://{HOST}:{closed_port()}/")
    began = time.monotonic()
    with pytest.raises(GitHubUnreachable) as info:
        GitHubClient(TOKEN).request("GET", "x")
    took = time.monotonic() - began
    assert info.value.timed_out is False
    assert took < TIMEOUT_SECONDS / 2, took
    caught.append(info.value)

    # 3. neither holds the token; each reason is a non-empty str
    for e in caught:
        assert isinstance(e.reason, str) and e.reason, repr(e)
        for text in (e.reason, str(e), repr(e)):
            assert TOKEN not in text, text
