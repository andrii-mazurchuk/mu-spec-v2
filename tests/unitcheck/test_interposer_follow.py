"""unitcheck.interposer's record follower: the fault modes, and records held to their b"\\n".

Each case is a fresh child interpreter (process 0) under install, with a scratch control listener
read here; the child appends to args.log_path and reports what each of its calls gave on stdout.
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


def records(child):
    """(ordinal, outcome, kind, bytes) per record message, in stream order."""
    return [
        (m["ordinal"], m["outcome"], m["kind"], base64.b64decode(m["b64"]))
        for m in of(child.control.messages, "record")
    ]


def outcomes(child):
    found = records(child)
    ordinals = [r[0] for r in found]
    assert len(ordinals) == len(set(ordinals)), found
    return {r[0]: r[1] for r in found}


def raised_errno(result):
    assert result[0] == "raised" and "OSError" in result[1], result
    return result[2]


def R(n):
    return b'{"kind":"probe","n":%d}\n' % n


def M(n):
    return b'{"kind":"mutation","n":%d}\n' % n


def E(n):
    return b'{"kind":"event","note":"mutation","n":%d}\n' % n


RECORDS = r'''
def R(n):
    return b'{"kind":"probe","n":%d}\n' % n


def M(n):
    return b'{"kind":"mutation","n":%d}\n' % n


def E(n):
    return b'{"kind":"event","note":"mutation","n":%d}\n' % n


LOG = None


def opening(buffering=-1):
    global LOG
    LOG = None
    LOG = open(ARGS.log_path, "ab", buffering=buffering)


def put(data):
    return outcome(lambda: LOG.write(data))
'''

HELD_OPEN = r'''
emit(opened=outcome(opening))
if LOG is not None:
    emit(writes=[outcome(lambda n=n: (LOG.write(E(n)), LOG.flush())[0]) for n in (1, 2, 3)])
    outcome(LOG.close)
'''

PER_APPEND = r'''
opens = []
for n in (1, 2, 3):
    opens.append(outcome(opening))
    if LOG is not None:
        outcome(lambda: LOG.write(E(n)))
        outcome(LOG.close)
emit(opens=opens)
'''

OPEN_THEN_MODE = r'''
opening(0)
pause("opened")
emit(writes=[put(E(n)) for n in (1, 2, 3)])
outcome(LOG.close)
'''

MIXED = r'''
opening(0)
emit(writes=[put(w) for w in (M(1), E(2), M(3), E(4), M(5), E(6))])
outcome(LOG.close)
'''


def test_modes_by_kind(tmp_path):
    from unitcheck.interposer import DISK_FULL, MUTATION_KIND

    assert MUTATION_KIND == "mutation"
    assert DISK_FULL == errno.ENOSPC

    # Case 1: 'every' from install, the log held open: the open itself fails.
    log = tmp_path / "held.log"
    child = Child(document(log, mode="every"), RECORDS + HELD_OPEN).finish()
    assert raised_errno(child.value("opened")) == DISK_FULL
    assert not log.exists() or log.read_bytes() == b""

    # Case 2: 'every' from install, opened per append: each of the three opens fails.
    log = tmp_path / "per-append.log"
    child = Child(document(log, mode="every"), RECORDS + PER_APPEND).finish()
    opens = child.value("opens")
    assert len(opens) == 3
    assert [raised_errno(o) for o in opens] == [DISK_FULL] * 3
    assert not log.exists() or log.read_bytes() == b""

    # Case 3: 'every' sent after the open: the handle already open is faulted too.
    log = tmp_path / "already-open.log"
    child = Child(document(log), RECORDS + OPEN_THEN_MODE)
    child.wait_pause("opened")
    child.set_mode("every")
    child.resume()
    child.finish()
    assert [raised_errno(w) for w in child.value("writes")] == [DISK_FULL] * 3
    assert outcomes(child) == {1: "failed", 2: "failed", 3: "failed"}
    assert log.read_bytes() == b""

    written = [M(1), E(2), M(3), E(4), M(5), E(6)]

    # Case 4: 'mutation' reads the parsed kind: E(n)'s note holding "mutation" does not select it.
    log = tmp_path / "mutation.log"
    child = Child(document(log, mode="mutation"), RECORDS + MIXED).finish()
    writes = child.value("writes")
    assert [raised_errno(writes[i]) for i in (0, 2, 4)] == [DISK_FULL] * 3
    assert [writes[i] for i in (1, 3, 5)] == [["returned", len(written[i])] for i in (1, 3, 5)]
    assert outcomes(child) == {1: "failed", 2: "passed", 3: "failed", 4: "passed", 5: "failed", 6: "passed"}
    assert all(data == written[ordinal - 1] for ordinal, _, _, data in records(child))
    assert log.read_bytes() == E(2) + E(4) + E(6)

    # Case 5: 'event' fails the event records only.
    log = tmp_path / "event.log"
    child = Child(document(log, mode="event"), RECORDS + MIXED).finish()
    writes = child.value("writes")
    assert [raised_errno(writes[i]) for i in (1, 3, 5)] == [DISK_FULL] * 3
    assert [writes[i] for i in (0, 2, 4)] == [["returned", len(written[i])] for i in (0, 2, 4)]
    assert outcomes(child) == {1: "passed", 2: "failed", 3: "passed", 4: "failed", 5: "passed", 6: "failed"}
    assert all(data == written[ordinal - 1] for ordinal, _, _, data in records(child))
    assert log.read_bytes() == M(1) + M(3) + M(5)


SPLIT = r'''
pause("fresh")
opening(0)
r2 = R(2)
emit(writes=[put(w) for w in (R(1), r2[:3], r2[3:10], r2[10:]) + TAIL])
outcome(LOG.close)
'''

LATER = r'''
opening(0)
emit(before=[put(R(n)) for n in range(1, BEFORE + 1)])
pause("written")
emit(after=[put(R(n)) for n in range(BEFORE + 1, BEFORE + AFTER + 1)])
outcome(LOG.close)
'''

JOINED = r'''
pause("fresh")
opening(0)
emit(writes=[put(R(1) + R(2))])
outcome(LOG.close)
'''

ENDINGS = r'''
opening(0)
text = '{"kind":"b","t":"x\u0085y z "}'
emit(writes=[put(w) for w in (b'{"kind":"a"}\r\n', text.encode("utf-8") + b"\n")])
outcome(LOG.close)
'''


def test_record_ordinals_and_kth(tmp_path):
    from unitcheck.interposer import DISK_FULL, TERMINATOR

    assert TERMINATOR == b"\n"
    r2 = R(2)

    # Case 1, 'off': one record per ordinal whether written whole, split over three calls or joined.
    log = tmp_path / "off.log"
    child = Child(document(log), RECORDS + "TAIL = (R(3) + R(4),)\n" + SPLIT)
    child.wait_pause("fresh")
    child.resume()
    child.finish()
    assert [r[:2] for r in records(child)] == [(1, "passed"), (2, "passed"), (3, "passed"), (4, "passed")]
    assert [r[3] for r in records(child)] == [R(1), R(2), R(3), R(4)]
    assert log.read_bytes() == R(1) + R(2) + R(3) + R(4)

    # Case 2: kth 2 set before any write selects the split record whole; its fragments are held.
    log = tmp_path / "split.log"
    child = Child(document(log), RECORDS + "TAIL = (R(3),)\n" + SPLIT)
    child.wait_pause("fresh")
    child.set_mode("kth", 2)
    child.resume()
    child.finish()
    writes = child.value("writes")
    assert writes[0] == ["returned", len(R(1))]
    assert writes[1] == ["returned", len(r2[:3])]
    assert writes[2] == ["returned", len(r2[3:10])]
    assert raised_errno(writes[3]) == DISK_FULL
    assert writes[4] == ["returned", len(R(3))]
    assert outcomes(child) == {1: "passed", 2: "failed", 3: "passed"}
    assert log.read_bytes() == R(1) + R(3)

    # Cases 3 and 4: the k-th is counted from the next record once the mode is in force.
    for k, expected in ((1, {5: "failed", 6: "passed"}), (3, {5: "passed", 6: "passed", 7: "failed", 8: "passed"})):
        log = tmp_path / f"later-{k}.log"
        after = len(expected)
        child = Child(document(log), RECORDS + f"BEFORE, AFTER = 4, {after}\n" + LATER)
        child.wait_pause("written")
        child.sync(1)
        child.set_mode("kth", k)
        child.resume()
        child.finish()
        assert outcomes(child) == {1: "passed", 2: "passed", 3: "passed", 4: "passed", **expected}
        failed = [n for n, o in expected.items() if o == "failed"]
        assert raised_errno(child.value("after")[failed[0] - 5]) == DISK_FULL

    # Case 5: a selected record joined with the next fails the call; the other is lost, not written.
    log = tmp_path / "joined.log"
    child = Child(document(log), RECORDS + JOINED)
    child.wait_pause("fresh")
    child.set_mode("kth", 1)
    child.resume()
    child.finish()
    assert raised_errno(child.value("writes")[0]) == DISK_FULL
    assert outcomes(child) == {1: "failed", 2: "lost"}
    assert log.read_bytes() == b""

    # Case 6: CR LF stays in its record; U+0085, U+2028 and U+2029 are ordinary bytes.
    log = tmp_path / "endings.log"
    child = Child(document(log), RECORDS + ENDINGS).finish()
    found = records(child)
    assert [(r[0], r[2]) for r in found] == [(1, "a"), (2, "b")]
    assert found[0][3].endswith(b"\r\n")
    assert " ".encode() in found[1][3]
    assert not [b for b in of(child.control.messages, "break") if b.get("why") == "unparsed"]
