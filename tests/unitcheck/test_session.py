"""unitcheck.session: the CheckSession of one check run, open_session and attempt_faults, the outbound rule.

test_attempt_faults_table judges the rule over built Attempts; test_session_lifecycle opens sessions with the
preflight stubbed and starts scratch units through their launchers; test_listener_is_not_outbound runs the
real preflight and two scratch units whose listeners are a ThreadingHTTPServer and a socketserver TCPServer.
The modules under test are imported inside each test, so while they are absent only these tests fail.
"""

import http.client
import os
import socket
import sys

import pytest

WAIT = 60.0

# A unit that listens on the injected host and port and answers every request with a fixed 200; when REACH
# is true it first connects once to 10.0.0.1:80, sends a few bytes and reads until the far end closes.
LISTEN_MAIN = r'''
import os, socket

HOST, PORT, REACH = %r, %r, %r
OK = b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"

if __name__ == "__main__":
    if REACH:
        try:
            out = socket.socket()
            out.settimeout(10)
            out.connect(("10.0.0.1", 80))
            out.sendall(b"ping")
            while out.recv(65536):
                pass
            out.close()
        except OSError:
            pass
    listener = socket.socket()
    listener.bind((os.environ[HOST], int(os.environ[PORT])))
    listener.listen(8)
    while True:
        conn, _ = listener.accept()
        try:
            conn.recv(65536)
            conn.sendall(OK)
        except OSError:
            pass
        conn.close()
'''

# A unit whose listener is http.server's ThreadingHTTPServer, answering /health and nothing else.
HTTP_MAIN = r'''
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = %r, %r


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((os.environ[HOST], int(os.environ[PORT])), Handler).serve_forever()
'''

# A unit whose listener is a socketserver TCPServer answering a fixed HTTP 200 to /health, 404 otherwise.
TCP_MAIN = r'''
import os, socketserver

HOST, PORT = %r, %r
OK = b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
MISSING = b"HTTP/1.0 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline(65536).split(b" ")
        while self.rfile.readline(65536) not in (b"\r\n", b"\n", b""):
            pass
        self.wfile.write(OK if len(line) > 1 and line[1] == b"/health" else MISSING)


if __name__ == "__main__":
    socketserver.TCPServer((os.environ[HOST], int(os.environ[PORT])), Handler).serve_forever()
'''


def scratch_install(base, name, main):
    package = base / name / "mu_spec"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(main)
    return package.parent


def check_root(tmp_path):
    """A check root and its pristine copies, the store holding projects/."""
    root = tmp_path / "check"
    root.mkdir()
    pristine_store = tmp_path / "pristine-store"
    (pristine_store / "projects").mkdir(parents=True)
    pristine_prompts = tmp_path / "pristine-prompts"
    pristine_prompts.mkdir()
    return root, pristine_store, pristine_prompts


def get(port, path):
    from startcheck.runner import REACH_HOST

    conn = http.client.HTTPConnection(REACH_HOST, port, timeout=WAIT)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def refused(port):
    from startcheck.runner import REACH_HOST

    try:
        socket.create_connection((REACH_HOST, port), timeout=10).close()
    except ConnectionRefusedError:
        return True
    except OSError:
        return False
    return False


def A(op, event, name=None, asked=None, numeric=False, process=0, child=None):
    from unitcheck.launch import Attempt

    return Attempt(op=op, event=event, name=name, dest=None, asked=asked, numeric=numeric, at=0.0,
                   process=process, child=child, call=0)


def test_attempt_faults_table():
    from shipcheck.standin import GITHUB_HOST
    from unitcheck.interposer import OPS, REVERSE_LOOKUPS
    from unitcheck.session import BOUND_REVERSE_EVENTS, JUDGED_OPS, NEVER_FAULT_OPS, attempt_faults

    SI = ("127.0.0.1", 4443)
    B = A("bind", "bind", asked=("127.0.0.1", 19406))
    L = A("lookup", "gethostbyaddr", name="127.0.0.1")

    passing = [
        [A("lookup", "getaddrinfo", name=GITHUB_HOST), A("lookup", "getaddrinfo", name="10.0.0.1", numeric=True),
         A("connect", "connect", asked=SI), A("spawn", "get_command_line", child=1)],
        [B],
        [A("bind", "socket.bind", asked=("0.0.0.0", 0))],
        [B, L],
        [B, A("lookup", "getnameinfo", asked=("127.0.0.1", 19406))],
    ]
    for seq in passing:
        assert attempt_faults(seq, SI) == (), seq

    # Each sequence with the one attempt in it that is the fault.
    faulting = [
        ([L], L),
        ([L, B], L),
        ([A("bind", "bind", asked=("127.0.0.1", 19406), process=1), L], L),
        ([B, A("lookup", "gethostbyaddr", name="10.0.0.1")], None),
        ([B, A("lookup", "socket.gethostbyaddr", name="127.0.0.1")], None),
        ([A("lookup", "bind", name="not a host!")], None),
        ([A("connect", "connect", asked=("10.0.0.1", 80))], None),
        ([A("process", "subprocess.Popen")], None),
        ([A("spawn", "get_command_line", child=None)], None),
        ([A("other", "x")], None),
    ]
    for seq, fault in faulting:
        fault = seq[-1] if fault is None else fault
        texts = attempt_faults(seq, SI)
        assert len(texts) == 1, (seq, texts)
        text = texts[0]
        assert isinstance(text, str), texts
        assert str(fault.process) in text, (fault, text)
        assert fault.op in text, (fault, text)
        assert fault.event in text, (fault, text)
        destination = fault.name if fault.name is not None else (fault.asked[0] if fault.asked else None)
        if destination is not None:
            assert destination in text, (fault, text)

    parts = (JUDGED_OPS, NEVER_FAULT_OPS, ("spawn", "process"))
    assert set(JUDGED_OPS) | set(NEVER_FAULT_OPS) | {"spawn", "process"} == set(OPS)
    assert sum(len(p) for p in parts) == 6 == len(set().union(*parts))
    assert set(BOUND_REVERSE_EVENTS) == set(REVERSE_LOOKUPS)


def test_session_lifecycle(tmp_path, monkeypatch):
    import unitcheck.session
    from mu_spec.outside import ENV_NAMES
    from shipcheck.standin import CERT_FIXTURE
    from startcheck.runner import UnitCommand, restore, start, stop
    from unitcheck.launch import START_VARIANT
    from unitcheck.preflight import PreflightError
    from unitcheck.session import open_session

    root, pristine_store, pristine_prompts = check_root(tmp_path)
    own_env = dict(os.environ)
    host, port = ENV_NAMES["host"], ENV_NAMES["port"]

    # A failing preflight: open_session raises its error and leaves the stand-in it used closed.
    seen = []
    error = PreflightError(("holding",), ("x",))

    def failing(*args, **kwargs):
        seen.append(kwargs["standin"] if "standin" in kwargs else args[2])
        raise error

    monkeypatch.setattr(unitcheck.session, "preflight", failing)
    with pytest.raises(PreflightError) as caught:
        open_session(own_env)
    assert caught.value is error
    assert len(seen) == 1, seen
    assert refused(seen[0].port), seen[0].port

    monkeypatch.setattr(unitcheck.session, "preflight", lambda *args, **kwargs: ())

    # A launcher's prepare is the shared interposed launch's.
    s = open_session(own_env)
    try:
        p = s.launcher().prepare(UnitCommand("u", str(root)), str(root))
        try:
            assert p.argv[0] == sys.executable, p.argv
            assert dict(p.extra_env) == {"SSL_CERT_FILE": CERT_FIXTURE}
        finally:
            p.done()
    finally:
        s.close()
    assert refused(s.standin.port)

    # Two starts through two launchers: two controls, in start order.
    quiet = UnitCommand("running", str(scratch_install(tmp_path, "quiet", LISTEN_MAIN % (host, port, False))))
    s = open_session(own_env)
    started = []
    try:
        s.neighbour.set("present")
        launchers = []
        for _ in range(2):
            restore(str(root), str(pristine_store), str(pristine_prompts))
            launchers.append(s.launcher())
            it = start(quiet, START_VARIANT, str(root), own_env, launchers[-1])
            started.append(it)
            assert it.outcome == "listening", (it.stdout, it.stderr)
            stop(it)
        controls = s.controls()
        assert len(controls) == 2, controls
        assert controls[0] is launchers[0].control(started[0].prepared)
        assert controls[1] is launchers[1].control(started[1].prepared)
    finally:
        for it in started:
            stop(it)
        s.close()
    assert refused(s.standin.port)

    # A unit that reaches 10.0.0.1:80 once: one fault for the attempt, one for the neighbour's Reached.
    reaching = UnitCommand("running", str(scratch_install(tmp_path, "reaching", LISTEN_MAIN % (host, port, True))))
    s = open_session(own_env)
    started = []
    try:
        s.neighbour.set("present")
        restore(str(root), str(pristine_store), str(pristine_prompts))
        it = start(reaching, START_VARIANT, str(root), own_env, s.launcher())
        started.append(it)
        assert it.outcome == "listening", (it.stdout, it.stderr)
        assert get(it.port, "/health") == 200
        s.controls()[0].sync(0)
        assert len(s.neighbour.reached()) == 1, s.neighbour.reached()
        outbound = s.outbound()
        assert len(outbound) == 2, outbound
        assert all(isinstance(text, str) for text in outbound), outbound
    finally:
        for it in started:
            stop(it)
        s.neighbour.set("present")
        s.close()
    assert refused(s.standin.port)


def test_listener_is_not_outbound(tmp_path):
    from mu_spec.outside import ENV_NAMES
    from startcheck.runner import UnitCommand, restore, start, stop
    from startcheck.variants import environment
    from unitcheck.launch import START_VARIANT
    from unitcheck.session import open_session

    root, pristine_store, pristine_prompts = check_root(tmp_path)
    own_env = dict(os.environ)
    names = (ENV_NAMES["host"], ENV_NAMES["port"])
    injected = environment(START_VARIANT, str(root), own_env)
    host, port = injected[names[0]], int(injected[names[1]])
    units = [
        UnitCommand("running", str(scratch_install(tmp_path, "http", HTTP_MAIN % names))),
        UnitCommand("running", str(scratch_install(tmp_path, "tcp", TCP_MAIN % names))),
    ]

    s = open_session(own_env)
    started = []
    try:
        s.neighbour.set("present")
        for cmd in units:
            restore(str(root), str(pristine_store), str(pristine_prompts))
            it = start(cmd, START_VARIANT, str(root), own_env, s.launcher())
            started.append(it)
            assert it.outcome == "listening", (it.stdout, it.stderr)
            assert get(it.port, "/health") == 200
            s.controls()[-1].sync(0)
            stop(it)

        assert s.outbound() == (), s.outbound()
        http_control, tcp_control = s.controls()

        attempts = [a for a in http_control.attempts() if a.process == 0]
        binds = [i for i, a in enumerate(attempts)
                 if a.op == "bind" and a.asked is not None and tuple(a.asked) == (host, port)]
        assert binds, attempts
        assert any(a.op == "lookup" and a.event == "gethostbyaddr" and a.name == host
                   for a in attempts[binds[0] + 1:]), attempts

        attempts = tcp_control.attempts()
        assert [a for a in attempts if a.op == "bind"], attempts
        assert not [a for a in attempts if a.op == "lookup"], attempts
    finally:
        for it in started:
            stop(it)
        s.neighbour.set("present")
        s.close()
