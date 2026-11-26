"""unitcheck.interposer's binds: a name bind is a refused lookup, a numeric bind its own op 'bind',
and an HTTPServer's own reverse lookup is answered without the resolver, naming its address.

Each case is a child interpreter under install, with a scratch control listener read here; the
real socket.socket.bind and socket.gethostbyaddr are replaced by recorders before install.
The module under test is imported inside each test, so while it is absent only these tests fail.
"""

import json
import os
import queue
import socket
import subprocess
import sys
import threading
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[2])
WAIT = 30.0

# The child's own helpers: emit() reports a result on stdout, pause() blocks until the test resumes it.
PRELUDE = r'''
import json, os, socket, subprocess, sys, time
sys.path.insert(0, %r)
DOC = json.loads(sys.argv[1])
CALLS = []


def emit(**fields):
    sys.stdout.write("@" + json.dumps(fields) + "\n")
    sys.stdout.flush()


def pause(tag):
    emit(pause=tag)
    sys.stdin.readline()


def outcome(call):
    try:
        value = call()
    except BaseException as e:
        names = [c.__name__ for c in type(e).__mro__]
        return ["raised", names, getattr(e, "errno", None), getattr(e, "strerror", None)]
    return ["returned", value]
'''

INSTALL = r'''
from unitcheck.interposer import install, launch_args
ARGS = launch_args(DOC)
install(ARGS)
emit(pid=os.getpid())
'''


class Control:
    """The check's scratch control listener: one child's connection, its messages in arrival order."""

    def __init__(self):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(WAIT)
        self.address = self.listener.getsockname()[:2]
        self.messages = []
        self.conn = None
        self.closed = False
        self.cond = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            conn, _ = self.listener.accept()
            conn.settimeout(None)
            with self.cond:
                self.conn = conn
            for line in conn.makefile("rb"):
                try:
                    message = json.loads(line)
                except ValueError:
                    message = {"t": "unreadable", "line": line.decode("utf-8", "replace")}
                with self.cond:
                    self.messages.append(message)
                    self.cond.notify_all()
        except OSError:
            pass
        finally:
            self.listener.close()
            with self.cond:
                self.closed = True
                self.cond.notify_all()

    def wait(self, ready, what):
        with self.cond:
            self.cond.wait_for(lambda: self.closed or ready(self.messages), WAIT)
            if not ready(self.messages):
                raise AssertionError(f"no {what} on the control stream: {self.messages!r}")

    def send(self, message):
        self.wait(lambda ms: any(m.get("t") == "hello" for m in ms), "hello")
        self.conn.sendall(json.dumps(message).encode() + b"\n")

    def wait_closed(self):
        with self.cond:
            assert self.cond.wait_for(lambda: self.closed, WAIT), "the control stream never closed"


class Child:
    """One interpreter under install, driven through its pauses and its control stream."""

    def __init__(self, document, body, pre=""):
        self.control = Control()
        document = dict(document, control=list(self.control.address))
        script = PRELUDE % REPO + pre + INSTALL + body
        self.proc = subprocess.Popen(
            [sys.executable, "-c", script, json.dumps(document)],
            cwd=REPO,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.lines = queue.Queue()
        self.output = []
        self.emits = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _next(self):
        line = self.lines.get(timeout=WAIT)
        if line is None:
            return None
        self.output.append(line)
        if line.startswith(b"@"):
            fields = json.loads(line[1:])
            self.emits.append(fields)
            return fields
        return {}

    def text(self):
        return b"".join(self.output).decode("utf-8", "replace")

    def finish(self):
        while self._next() is not None:
            pass
        assert self.proc.wait(WAIT) == 0, self.text()
        self.control.wait_closed()
        return self

    def value(self, key):
        values = [fields[key] for fields in self.emits if key in fields]
        assert len(values) == 1, self.text()
        return values[0]


def of(messages, t):
    return [m for m in messages if m.get("t") == t]


def document(log, **fields):
    from unitcheck.interposer import calibrate

    doc = {
        "control": None,
        "standin": ["127.0.0.1", 9],
        "github_host": "api.github.com",
        "neighbour_tcp": ["127.0.0.1", 9],
        "neighbour_udp": ["127.0.0.1", 9],
        "condition": "present",
        "log_path": os.path.normcase(os.path.realpath(log)),
        "mode": "off",
        "k": None,
        "numeric_errors": [list(e) for e in calibrate()],
        "process": 0,
    }
    doc.update(fields)
    return doc


def attempts(child):
    return of(child.control.messages, "attempt")


RECORDERS = r'''
import http.server
import socketserver

REAL_BIND = socket.socket.bind
REAL_GETHOSTBYADDR = socket.gethostbyaddr


def recording_bind(self, address):
    CALLS.append(["bind", list(address)])
    return REAL_BIND(self, address)


def recording_gethostbyaddr(address):
    CALLS.append(["gethostbyaddr", address])
    return REAL_GETHOSTBYADDR(address)


socket.socket.bind = recording_bind
socket.gethostbyaddr = recording_gethostbyaddr
'''

NAME_BIND = r'''
s = socket.socket()
emit(result=outcome(lambda: s.bind(("not a host!", 0))), calls=CALLS)
s.close()
'''

NUMERIC_BIND = r'''
s = socket.socket()
emit(result=outcome(lambda: s.bind(("127.0.0.1", 0))), calls=CALLS)
s.close()
'''

TCP_SERVER = r'''
server = socketserver.TCPServer(("127.0.0.1", 19406), socketserver.BaseRequestHandler)
server.server_close()
emit(calls=CALLS)
'''

HTTP_SERVER = r'''
server = http.server.HTTPServer(("127.0.0.1", 19406), http.server.BaseHTTPRequestHandler)
name = server.server_name
server.server_close()
emit(name=name, calls=CALLS)
'''


def test_binds_and_listener(tmp_path):
    from unitcheck.interposer import BIND_NAME_REASON

    log = tmp_path / "lifecycle.log"

    # Case 1, 'present': a bind to a name is a lookup, refused without the real bind.
    child = Child(document(log), NAME_BIND, pre=RECORDERS).finish()
    result = child.value("result")
    assert result[0] == "raised" and result[1][0] == "gaierror", result
    assert result[2:] == [socket.EAI_NONAME, BIND_NAME_REASON]
    [attempt] = attempts(child)
    assert (attempt["op"], attempt["event"], attempt["numeric"]) == ("lookup", "bind", False)
    assert child.value("calls") == []

    # Case 2, 'absent': a numeric bind is op 'bind' and proceeds.
    child = Child(document(log, condition="absent"), NUMERIC_BIND, pre=RECORDERS).finish()
    assert child.value("result") == ["returned", None]
    [attempt] = attempts(child)
    assert (attempt["op"], attempt["event"], attempt["numeric"]) == ("bind", "bind", False)
    assert attempt["asked"] == ["127.0.0.1", 0] and attempt["dest"] == ["127.0.0.1", 0]

    # Case 3, 'present': a TCPServer's bind is one attempt, the wrapper's alone.
    child = Child(document(log), TCP_SERVER, pre=RECORDERS).finish()
    assert [a["op"] for a in attempts(child)] == ["bind"]

    # Case 4: an HTTPServer gives its bind, then its own reverse lookup, answered without the resolver.
    for condition in ("present", "absent"):
        child = Child(document(log, condition=condition), HTTP_SERVER, pre=RECORDERS).finish()
        bind, lookup = attempts(child)
        assert (bind["op"], bind["asked"]) == ("bind", ["127.0.0.1", 19406]), condition
        assert (lookup["op"], lookup["event"], lookup["name"], lookup["numeric"]) == (
            "lookup",
            "gethostbyaddr",
            "127.0.0.1",
            False,
        ), condition
        assert [c for c in child.value("calls") if c[0] == "gethostbyaddr"] == [], condition
        assert child.value("name") == "127.0.0.1", condition
