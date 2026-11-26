"""unitcheck.interposer's control stream: hello, mode answered once in force, sync after every report.

Each case is a child interpreter under install, with a scratch control listener read here; the
child reports what each of its calls gave on stdout and blocks at each pause until resumed.
The module under test is imported inside each test, so while it is absent only these tests fail.
"""

import base64
import errno
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

    def set_mode(self, mode, k=None):
        before = len(of(self.control.messages, "moded"))
        self.control.send({"t": "mode", "mode": mode, "k": k})
        self.control.wait(lambda ms: len(of(ms, "moded")) > before, "moded")

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


def R(n):
    return b'{"kind":"probe","n":%d}\n' % n


PROBE = r'''
def R(n):
    return b'{"kind":"probe","n":%d}\n' % n


LOG = open(ARGS.log_path, "ab", buffering=0)
'''

WRITER = r'''
pause("go")
emit(writes=[outcome(lambda n=n: LOG.write(R(n))) for n in range(1, 201)])
pause("written")
emit(last=outcome(lambda: LOG.write(R(201))))
outcome(LOG.close)
'''

SPAWNED = r'''
pause("opened")
emit(last=outcome(lambda: LOG.write(R(202))))
outcome(LOG.close)
'''


def test_mode_and_sync(tmp_path):
    log = tmp_path / "lifecycle.log"
    child = Child(document(log), PROBE + WRITER)
    child.wait_pause("go")
    child.resume()
    child.wait_pause("written")
    assert child.value("writes") == [["returned", len(R(n))] for n in range(1, 201)]

    # Action 1: a sync is answered only after every record report made before it.
    child.sync(1)
    messages = list(child.control.messages)
    at = next(i for i, m in enumerate(messages) if m.get("t") == "synced" and m.get("n") == 1)
    before = of(messages[:at], "record")
    assert [m["ordinal"] for m in before] == list(range(1, 201))
    assert all(m["outcome"] == "passed" for m in before)
    assert messages[0]["t"] == "hello" and messages[0]["pid"] == child.value("pid")

    # Action 2: moded arrives once 'every' is in force for the writer.
    child.set_mode("every")
    child.resume()
    child.finish()
    last = child.value("last")
    assert last[0] == "raised" and "OSError" in last[1] and last[2] == errno.ENOSPC, last
    assert [m["outcome"] for m in of(child.control.messages, "record") if m["ordinal"] == 201] == ["failed"]
    assert all(m.get("process") == 0 for m in child.control.messages)

    # Action 3: a spawned child (process 1) answers mode and changes nothing: its write passes.
    spawned = Child(document(log, process=1), PROBE + SPAWNED)
    spawned.wait_pause("opened")
    spawned.set_mode("every")
    spawned.resume()
    spawned.finish()
    assert spawned.value("last") == ["returned", len(R(202))]
    breaks = of(spawned.control.messages, "break")
    assert [(b["why"], base64.b64decode(b["b64"])) for b in breaks] == [("child", R(202))]
    assert of(spawned.control.messages, "record") == []
    assert spawned.control.messages[0]["t"] == "hello" and spawned.control.messages[0]["process"] == 1
    assert log.read_bytes().endswith(R(202))
