"""unitcheck.interposer's routing of connects, sends, lookups of a name and process starts, and
launch_args, the one place a launch document becomes LaunchArgs.

Each case is a child interpreter under install, with a scratch control listener read here, a
neighbour simulator and a listener standing in for the stand-in. The module under test is
imported inside each test, so while it is absent only these tests fail.
"""

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

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

    def wait_closed(self):
        with self.cond:
            assert self.cond.wait_for(lambda: self.closed, WAIT), "the control stream never closed"


class Child:
    """One interpreter under install, run to its end; its control stream and its stdout emits."""

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


class Listener:
    """A TCP listener that accepts and holds every connection, recording each peer."""

    def __init__(self):
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.address = self.sock.getsockname()[:2]
        self.accepted = []
        self.held = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, peer = self.sock.accept()
            except OSError:
                return
            self.held.append(conn)
            self.accepted.append(peer)

    def close(self):
        self.sock.close()
        for conn in self.held:
            conn.close()


class Neighbour:
    """The neighbour simulator: everything routed to it, by TCP connection or by datagram."""

    def __init__(self):
        self.tcp = Listener()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp_address = self.udp.getsockname()[:2]
        self.datagrams = []
        threading.Thread(target=self._receive, daemon=True).start()

    def _receive(self):
        while True:
            try:
                data, _ = self.udp.recvfrom(65536)
            except OSError:
                return
            self.datagrams.append(data)

    def reached(self):
        return tuple([("tcp", peer) for peer in self.tcp.accepted] + [("udp", data) for data in self.datagrams])

    def close(self):
        self.tcp.close()
        self.udp.close()


def settled(read, count):
    """read() once it holds count entries, or after a short grace when count is 0."""
    deadline = time.monotonic() + (5.0 if count else 0.3)
    while time.monotonic() < deadline and len(read()) < max(count, 1):
        time.sleep(0.02)
    return read()


def unused_port():
    with socket.create_server(("127.0.0.1", 0)) as s:
        return s.getsockname()[1]


def routed(tmp_path, condition, body):
    n = Neighbour()
    sl = Listener()
    doc = document(
        tmp_path / "lifecycle.log",
        condition=condition,
        standin=list(sl.address),
        neighbour_tcp=list(n.tcp.address),
        neighbour_udp=list(n.udp_address),
    )
    return Child(doc, body).finish(), n, sl


RAW_CONNECT = r'''
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
emit(result=outcome(lambda: s.connect(ADDRESS)))
s.close()
'''

CREATE_CONNECTION = r'''
emit(result=outcome(lambda: socket.create_connection(ADDRESS).close()))
'''

GITHUB = r'''
answer = socket.getaddrinfo(ARGS.github_host, 443)
emit(sockaddr=list(answer[0][4]), equal=answer[0][4] == ARGS.standin, standin=list(ARGS.standin))
'''

SENDTO = r'''
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
emit(result=outcome(lambda: u.sendto(b"p", ("10.0.0.1", 9))))
u.close()
'''

ABSENT_LOOKUP = r'''
try:
    socket.getaddrinfo("nope.invalid", 80)
except socket.gaierror as e:
    emit(now=time.time(), result=["raised", [c.__name__ for c in type(e).__mro__], e.errno, e.strerror])
else:
    emit(now=None, result=["returned", None])
'''

PROCESS = r'''
emit(result=outcome(lambda: subprocess.run([sys.executable, "-c", "pass"]).returncode))
'''


def test_connect_routing(tmp_path):
    from unitcheck.interposer import launch_args

    port = unused_port()
    to_port = f"ADDRESS = ('127.0.0.1', {port})\n"

    # 'refusing': a raw connect elsewhere is refused here, with nothing at the neighbour.
    child, n, sl = routed(tmp_path, "refusing", to_port + RAW_CONNECT)
    result = child.value("result")
    assert result[0] == "raised" and "ConnectionRefusedError" in result[1], result
    [attempt] = attempts(child)
    assert (attempt["op"], attempt["asked"], attempt["dest"]) == ("connect", ["127.0.0.1", port], None)
    assert settled(n.reached, 0) == ()

    # 'refusing': create_connection's numeric lookups precede the one refused connect.
    child, n, sl = routed(tmp_path, "refusing", to_port + CREATE_CONNECTION)
    result = child.value("result")
    assert result[0] == "raised" and "ConnectionRefusedError" in result[1], result
    *lookups, last = attempts(child)
    assert (last["op"], last["dest"]) == ("connect", None)
    for lookup in lookups:
        assert lookup["op"] == "lookup" and lookup["numeric"] is True, lookup
        assert lookup["name"] == "127.0.0.1" or (lookup["asked"] or [None])[0] == "127.0.0.1", lookup
    assert settled(n.reached, 0) == ()

    # 'refusing': the stand-in's address, as launch_args turned it from JSON, proceeds.
    child, n, sl = routed(tmp_path, "refusing", "ADDRESS = ARGS.standin\n" + RAW_CONNECT)
    assert child.value("result") == ["returned", None]
    assert len(settled(lambda: sl.accepted, 1)) == 1
    assert [a["op"] for a in attempts(child)] == ["connect"]
    assert settled(n.reached, 0) == ()

    # 'present': a name handed straight to connect reaches the neighbour.
    child, n, sl = routed(tmp_path, "present", "ADDRESS = ('example.invalid', 80)\n" + RAW_CONNECT)
    assert child.value("result") == ["returned", None]
    assert len([e for e in settled(n.reached, 1) if e[0] == "tcp"]) == 1

    # 'present': GitHub's host resolves to the stand-in.
    child, n, sl = routed(tmp_path, "present", GITHUB)
    assert child.value("equal") is True
    assert child.value("sockaddr") == child.value("standin") == list(sl.address)

    # 'present': a datagram is one 'send' and reaches the neighbour's UDP side.
    child, n, sl = routed(tmp_path, "present", SENDTO)
    assert child.value("result") == ["returned", 1]
    assert [a["op"] for a in attempts(child)] == ["send"]
    assert [e for e in settled(n.reached, 1) if e[0] == "udp"] == [("udp", b"p")]

    # 'absent': a name lookup is reported once, before its gaierror.
    child, n, sl = routed(tmp_path, "absent", ABSENT_LOOKUP)
    result = child.value("result")
    assert result[0] == "raised" and result[1][0] == "gaierror", result
    [attempt] = attempts(child)
    assert (attempt["op"], attempt["dest"]) == ("lookup", None)
    assert attempt["at"] <= child.value("now")

    # 'present': a subprocess start is one attempt of op 'process'.
    child, n, sl = routed(tmp_path, "present", PROCESS)
    assert child.value("result") == ["returned", 0]
    assert [a["op"] for a in attempts(child)].count("process") == 1

    # In the test's process: launch_args makes tuples and refuses an incomplete document.
    complete = document(tmp_path / "lifecycle.log", control=["127.0.0.1", 5])
    assert launch_args(complete).control == ("127.0.0.1", 5)
    for field in ("condition", "numeric_errors"):
        less = {key: value for key, value in complete.items() if key != field}
        with pytest.raises(ValueError):
            launch_args(less)
    with pytest.raises(ValueError):
        launch_args(dict(complete, standin=["h"]))
    assert launch_args({key: value for key, value in complete.items() if key != "process"}).process == 0
