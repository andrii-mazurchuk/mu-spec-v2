"""unitcheck.interposer's lookups: numeric ones answered here, their errors as calibrated in the
check's process; reverse ones reported numeric false with their address, answered without the resolver.

Each child is an interpreter under install, with a scratch control listener read here; it pauses
after each step and the test syncs, so the attempts between two synced messages are that step's.
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

    def wait_pause(self, tag):
        while True:
            fields = self._next()
            if fields is None:
                raise AssertionError(f"the child exited before pause {tag!r}:\n{self.text()}")
            if "pause" in fields:
                assert fields["pause"] == tag, self.text()
                return

    def resume(self):
        self.proc.stdin.write(b"\n")
        self.proc.stdin.flush()

    def sync(self, n):
        self.control.send({"t": "sync", "n": n})
        self.control.wait(lambda ms: any(m.get("t") == "synced" and m.get("n") == n for m in ms), f"synced {n}")

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


def stepwise(child, tags):
    """Sync after each step; return each step's emitted fields and the attempts made during it."""
    for n, tag in enumerate(tags, 1):
        child.wait_pause(tag)
        child.sync(n)
        child.resume()
    child.finish()
    made, current, done = {}, [], 0
    for m in child.control.messages:
        if m.get("t") == "synced":
            made[tags[done]] = current
            current, done = [], done + 1
        elif m.get("t") == "attempt":
            current.append(m)
    results = {fields["tag"]: fields for fields in child.emits if "tag" in fields}
    return results, made


RECORDERS = r'''
def recorder(name):
    real = getattr(socket, name)

    def recorded(*args, **kwargs):
        CALLS.append(name)
        return real(*args, **kwargs)

    return recorded


for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
    setattr(socket, name, recorder(name))
'''

STEPS = r'''
def step(tag, call):
    del CALLS[:]
    start = time.monotonic()
    result = outcome(call)
    emit(tag=tag, result=result, elapsed=time.monotonic() - start, calls=list(CALLS))
    pause(tag)


NUMERIC = socket.NI_NUMERICHOST | socket.NI_NUMERICSERV
step("v4", lambda: socket.getaddrinfo("10.0.0.1", 80))
step("v6_by_name", lambda: socket.gethostbyname("::1"))
step("v4_as_v6", lambda: socket.getaddrinfo("10.0.0.1", 80, socket.AF_INET6))
step("v6_as_v4", lambda: socket.getaddrinfo("::1", 80, socket.AF_INET))
step("numeric_nameinfo", lambda: socket.getnameinfo(("10.0.0.1", 80), NUMERIC))
step("hostbyaddr", lambda: socket.gethostbyaddr("10.0.0.1"))
step("nameinfo", lambda: socket.getnameinfo(("10.0.0.1", 80), 0))
step("name_ex", lambda: socket.gethostbyname_ex("x.invalid"))
step("name_addr", lambda: socket.gethostbyaddr("x.invalid"))
step("name_info", lambda: socket.getnameinfo(("x.invalid", 80), 0))
'''

TAGS = (
    "v4",
    "v6_by_name",
    "v4_as_v6",
    "v6_as_v4",
    "numeric_nameinfo",
    "hostbyaddr",
    "nameinfo",
    "name_ex",
    "name_addr",
    "name_info",
)

INJECTED = r'''
emit(result=outcome(lambda: socket.getaddrinfo("10.0.0.1", 80, socket.AF_INET6)))
'''

COUNTED = r'''
import json, socket, sys
sys.path.insert(0, %r)
COUNT = [0]


def counted(real):
    def call(*args, **kwargs):
        COUNT[0] += 1
        return real(*args, **kwargs)

    return call


for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
    setattr(socket, name, counted(getattr(socket, name)))

from unitcheck.interposer import calibrate

first = calibrate()
second = calibrate()
print(json.dumps({"count": COUNT[0], "same": first == second}))
'''


def first_class(names):
    from unitcheck.interposer import ERROR_CLASSES

    return next((c for c in ERROR_CLASSES if c in names), None)


def calibrated(result):
    """(class name, errno, text) of a child's raised result, the class as calibrate names it."""
    assert result[0] == "raised", result
    return (first_class(result[1]), result[2], result[3])


def raised(call, *args):
    """(class name, errno) of what call(*args) raises, or ('answered',)."""
    try:
        call(*args)
    except OSError as e:
        return (first_class([c.__name__ for c in type(e).__mro__]), getattr(e, "errno", None))
    return ("answered",)


def test_numeric_and_reverse_lookups(tmp_path):
    from unitcheck.interposer import (
        CALIBRATION,
        INJECTED_ERRNO,
        NUMERIC_ERROR_CASES,
        built_forward,
        built_reverse,
        calibrate,
        launch_args,
        local_lookup,
    )

    CAL = calibrate()
    CAL_BY = {case: (cls, code, text) for case, cls, code, text in CAL}
    IE = INJECTED_ERRNO
    assert IE == 424242
    log = tmp_path / "lifecycle.log"

    # The child under 'hung', the real lookups replaced by recorders before install.
    child = Child(document(log, condition="hung"), STEPS, pre=RECORDERS)
    results, made = stepwise(child, TAGS)

    for tag in ("v4", "v6_by_name", "numeric_nameinfo"):
        [attempt] = made[tag]
        assert (attempt["op"], attempt["numeric"]) == ("lookup", True), tag
    for tag in TAGS:
        assert results[tag]["calls"] == [], tag
    assert calibrated(results["v6_by_name"]["result"]) == CAL_BY["v6_by_name"]
    assert calibrated(results["v4_as_v6"]["result"]) == CAL_BY["v4_as_v6"]
    assert calibrated(results["v6_as_v4"]["result"]) == CAL_BY["v6_as_v4"]

    [attempt] = made["hostbyaddr"]
    assert (attempt["event"], attempt["name"], attempt["asked"], attempt["numeric"]) == (
        "gethostbyaddr",
        "10.0.0.1",
        None,
        False,
    )
    assert results["hostbyaddr"]["result"] == ["returned", ["10.0.0.1", [], ["10.0.0.1"]]]
    assert results["hostbyaddr"]["elapsed"] < 1.0

    [attempt] = made["nameinfo"]
    assert (attempt["event"], attempt["name"], attempt["asked"], attempt["numeric"]) == (
        "getnameinfo",
        None,
        ["10.0.0.1", 80],
        False,
    )
    assert results["nameinfo"]["result"] == ["returned", ["10.0.0.1", "80"]]

    for tag in ("name_ex", "name_addr", "name_info"):
        assert results[tag]["result"][0] == "returned", (tag, results[tag])
        assert results[tag]["elapsed"] < 1.0, tag
        assert len(made[tag]) == 1, (tag, made[tag])

    # A second child: the 'v4_as_v6' error comes from the launch document, not from the host.
    rows = [["v4_as_v6", "gaierror", IE, "x"]] + [list(e) for e in CAL if e[0] != "v4_as_v6"]
    child = Child(document(log, condition="hung", numeric_errors=rows), INJECTED).finish()
    result = child.value("result")
    assert result[0] == "raised" and result[1][0] == "gaierror", result
    assert result[2:] == [424242, "x"]

    # In the test's process: each error case built raises what the system raises.
    for case in NUMERIC_ERROR_CASES:
        op, literal, family = CALIBRATION[case]
        if op == "getaddrinfo":
            built = raised(built_forward, op, literal, None, family, 0)
            real = raised(socket.getaddrinfo, literal, None, family, 0)
        else:
            built = raised(built_forward, op, literal)
            real = raised(getattr(socket, op), literal)
        assert built == real, case

    # calibrate caches: twice in a fresh process is one system call per case.
    counted = subprocess.run(
        [sys.executable, "-c", COUNTED % REPO], check=False, cwd=REPO, capture_output=True, timeout=WAIT
    )
    assert counted.returncode == 0, counted.stdout + counted.stderr
    assert json.loads(counted.stdout.splitlines()[-1]) == {"count": 4, "same": True}

    # launch_args refuses numeric_errors out of order or incomplete.
    complete = document(log)
    with pytest.raises(ValueError):
        launch_args(dict(complete, numeric_errors=complete["numeric_errors"][::-1]))
    with pytest.raises(ValueError):
        launch_args(dict(complete, numeric_errors=complete["numeric_errors"][:3]))

    # Built answers equal the host's own numeric answers.
    proto = 0 if sys.platform == "win32" else socket.IPPROTO_TCP
    stream = built_forward("getaddrinfo", "10.0.0.1", 80, 0, socket.SOCK_STREAM)
    assert stream == [(socket.AF_INET, socket.SOCK_STREAM, proto, "", ("10.0.0.1", 80))]
    assert stream == socket.getaddrinfo("10.0.0.1", 80, 0, socket.SOCK_STREAM, 0, socket.AI_NUMERICHOST)
    scoped = built_forward("getaddrinfo", "fe80::1%3", None, socket.AF_INET6, socket.SOCK_DGRAM)
    assert scoped[0][4] == ("fe80::1", 0, 0, 3)
    numeric = socket.NI_NUMERICHOST | socket.NI_NUMERICSERV
    assert built_reverse(("::1", 80, 0, 0), numeric) == ("::1", "80")
    assert built_reverse(("::1", 80, 0, 0), numeric) == socket.getnameinfo(("::1", 80, 0, 0), numeric)

    assert local_lookup("gethostbyaddr", "127.0.0.1") is False
    assert local_lookup("getaddrinfo", "127.0.0.1") is True
