"""append_log: one lock across calls, one write call per line, each record reported once (T-415).

A plain unit test: no acceptance input, no unit started, no running-unit bytes. The project record is
encoded in process by the rebuilt codec from a literal document. Modules under test are imported
inside the test, so a module not yet written fails only the tests that need it.

interception and check_coverage are also used by tests/test_store_project_logs.py.
"""

import builtins
import datetime
import io
import os
import pathlib
import threading
import time
from dataclasses import dataclass

import pytest

AT = datetime.datetime(2026, 11, 25, 12, 0, tzinfo=datetime.UTC)
PDOC = {"name": "p", "entries": [], "counters": {}}


def _key(arg):
    """The normalised path text arg names, or None when it names no path (a descriptor)."""
    if isinstance(arg, (str, bytes, os.PathLike)):
        return os.path.normcase(os.path.abspath(os.fsdecode(os.fspath(arg))))
    return None


@dataclass
class Append:
    path: str  # the normalised path it opened
    route: str  # 'open', 'os.open' or 'FileIO'
    thread: int
    writes: int = 0


class _Proxy:
    """A file opened on a watched path; each write is one counted write of its append."""

    def __init__(self, f, ic, index, fd=None):
        self._f, self._ic, self._index, self._fd = f, ic, index, fd

    def write(self, data):
        self._ic._count(self._index)
        return self._f.write(data)

    def writelines(self, lines):
        self._ic._count(self._index)
        return self._f.writelines(lines)

    def close(self):
        if self._fd is not None:
            self._ic._fds.pop(self._fd, None)
        return self._f.close()

    def __enter__(self):
        self._f.__enter__()
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            self._ic._fds.pop(self._fd, None)
        return self._f.__exit__(*exc)

    def __iter__(self):
        return iter(self._f)

    def __getattr__(self, name):
        return getattr(self._f, name)


class Interception:
    """Every open of the watched paths (an append) and every write made through it, by thread."""

    def __init__(self, paths, hook=None):
        self.targets = {_key(p) for p in paths}
        self.hook = hook  # called with the Append before each counted write; may raise
        self.appends: list[Append] = []
        self._fds: dict[int, int] = {}
        self._lock = threading.Lock()

    def _start(self, key, route):
        with self._lock:
            self.appends.append(Append(key, route, threading.get_ident()))
            return len(self.appends) - 1

    def _count(self, index):
        if self.hook is not None:
            self.hook(self.appends[index])
        with self._lock:
            self.appends[index].writes += 1

    def on(self, path):
        """The appends started on path, in order."""
        key = _key(path)
        return [a for a in self.appends if a.path == key]

    def entered(self, thread):
        """Whether thread opened a watched path or made a counted write on one."""
        return any(a.thread == thread for a in self.appends)


def interception(monkeypatch, *paths, hook=None):
    """Install an Interception of paths on every open route of the files group, plus io.FileIO."""
    ic = Interception(paths, hook)
    real_open, real_os_open, real_write, real_close = io.open, os.open, os.write, os.close
    real_fileio = io.FileIO

    def w_open(file, *args, **kwargs):
        if isinstance(file, int) and file in ic._fds:
            return _Proxy(real_open(file, *args, **kwargs), ic, ic._fds[file], file)
        if _key(file) in ic.targets:
            index = ic._start(_key(file), "open")
            return _Proxy(real_open(file, *args, **kwargs), ic, index)
        return real_open(file, *args, **kwargs)

    def w_os_open(path, *args, **kwargs):
        fd = real_os_open(path, *args, **kwargs)
        if _key(path) in ic.targets:
            ic._fds[fd] = ic._start(_key(path), "os.open")
        return fd

    def w_write(fd, data):
        if fd in ic._fds:
            ic._count(ic._fds[fd])
        return real_write(fd, data)

    def w_close(fd):
        ic._fds.pop(fd, None)
        return real_close(fd)

    class CountedFileIO(real_fileio):
        def __init__(self, file, *args, **kwargs):
            super().__init__(file, *args, **kwargs)
            self._watched = ic._start(_key(file), "FileIO") if _key(file) in ic.targets else None

        def write(self, b):
            if self._watched is not None:
                ic._count(self._watched)
            return super().write(b)

    monkeypatch.setattr(builtins, "open", w_open)
    monkeypatch.setattr(io, "open", w_open)
    monkeypatch.setattr(os, "open", w_os_open)
    monkeypatch.setattr(os, "write", w_write)
    monkeypatch.setattr(os, "close", w_close)
    monkeypatch.setattr(io, "FileIO", CountedFileIO)
    return ic


def check_coverage(ic, scratch):
    """Each open route on scratch (watched by ic) registers exactly one append and one counted write."""
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)

    def by_os_write():
        fd = os.open(scratch, flags)
        try:
            os.write(fd, b"a")
        finally:
            os.close(fd)

    def by_fdopen():
        with os.fdopen(os.open(scratch, flags), "ab") as f:
            f.write(b"a")

    def by(opener):
        def go():
            with opener() as f:
                f.write(b"a")
        return go

    # each route named exactly, since io.open and builtins.open are patched separately
    routes = {
        "pathlib.Path.open": by(lambda: pathlib.Path(scratch).open("ab")),  # noqa: SIM115
        "builtins.open": by(lambda: builtins.open(scratch, "ab")),  # noqa: SIM115
        "io.open": by(lambda: io.open(scratch, "ab")),  # noqa: SIM115, UP020
        "os.fdopen(os.open)": by_fdopen,
        "os.write(os.open)": by_os_write,
        "io.FileIO": by(lambda: io.FileIO(scratch, "ab")),
    }
    for route, go in routes.items():
        before = len(ic.on(scratch))
        writes = sum(a.writes for a in ic.on(scratch))
        go()
        made = ic.on(scratch)[before:]
        counted = sum(a.writes for a in ic.on(scratch)) - writes
        if len(made) != 1 or counted != 1:
            pytest.fail(f"the interception misses route {route}: {len(made)} appends, {counted} counted writes")


def test_appends_serialised(tmp_path, monkeypatch):
    from mu_spec.codec import (
        MUTATION_KIND,
        ProjectRecord,
        decode_log_line,
        encode_log_line,
        encode_record,
        log_path,
        new_log_record,
        record_path,
        split_log,
    )
    from mu_spec.store import SEQ_START, Store

    from mu_spec.outside import load_config

    pbytes = encode_record(ProjectRecord("p", PDOC))
    D = tmp_path / "state"
    c = load_config({"HOLONIC_STATE_DIR": str(D), "MU_SPEC_PORT": "9100"}, tmp_path)[0]
    assert c.root == D / "projects"
    record_path(c.root, "p").parent.mkdir(parents=True)
    record_path(c.root, "p").write_bytes(pbytes)
    s = Store(c)
    R = new_log_record(MUTATION_KIND, "p", [], {"n": 1}, AT)
    L = log_path(c.root)

    # part 1: B must wait while A is held inside its write
    in_write, go = threading.Event(), threading.Event()
    held = []

    def hold(append):
        if not held:
            held.append(append)
            in_write.set()
            go.wait(2)

    returned = {}

    def appender(name):
        returned[name] = s.append_log(R)

    with monkeypatch.context() as m:
        C = tmp_path / "coverage.scratch"
        ic = interception(m, L, C)
        check_coverage(ic, C)
        ic.hook = lambda a: hold(a) if a.path == _key(L) else None
        A = threading.Thread(target=appender, args=("A",), daemon=True)
        A.start()
        assert in_write.wait(5), "the first append made no counted write on the log"
        B = threading.Thread(target=appender, args=("B",), daemon=True)
        B.start()
        time.sleep(0.5)
        b_entered = ic.entered(B.ident)
        go.set()
        A.join(10)
        B.join(10)
        assert not A.is_alive() and not B.is_alive()
        on_log = ic.on(L)
    assert len(on_log) == 2, f"{len(on_log)} appends on the log, not one per append_log call"
    assert b_entered is False
    assert [a.writes for a in on_log] == [1, 1]
    lines, tail = split_log(L.read_bytes())
    assert len(lines) == 2 and tail == b""
    for line in lines:
        assert encode_log_line(decode_log_line(line)) == line + b"\n"
    assert [decode_log_line(line).seq for line in lines] == [SEQ_START, SEQ_START + 1]
    assert sorted([returned["A"].seq, returned["B"].seq]) == [SEQ_START, SEQ_START + 1]
    assert "seq" not in R.doc

    # part 2: volume
    D2 = tmp_path / "state2"
    c2 = load_config({"HOLONIC_STATE_DIR": str(D2), "MU_SPEC_PORT": "9100"}, tmp_path)[0]
    record_path(c2.root, "p").parent.mkdir(parents=True)
    record_path(c2.root, "p").write_bytes(pbytes)

    class Seqs:
        def __init__(self):
            self.seen, self.lock = [], threading.Lock()

        def installed(self, project, data, *, notify):
            pass

        def committed(self, project):
            pass

        def appended(self, record):
            with self.lock:
                self.seen.append(record.seq)

    obs2 = Seqs()
    s2 = Store(c2, observer=obs2)
    records = [new_log_record(MUTATION_KIND, "p", [], {"actor": f"w{i}-" + "x" * 5000}, AT) for i in range(10)]

    def many(i):
        for _ in range(1000):
            s2.append_log(records[i])

    threads = [threading.Thread(target=many, args=(i,), daemon=True) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines, tail = split_log(log_path(c2.root).read_bytes())
    assert len(lines) == 10000 and tail == b""
    for line in lines:
        assert encode_log_line(decode_log_line(line)) == line + b"\n"
    for i in range(10):
        assert sum(1 for line in lines if f"w{i}-".encode() in line) == 1000, i
    recs = s2.read_log(project="p")
    assert len(recs) == 10000
    assert [r.seq for r in recs] == list(range(SEQ_START, SEQ_START + 10000))
    assert len(s2.read_log(kind=MUTATION_KIND)) == 10000
    assert sorted(obs2.seen) == list(range(SEQ_START, SEQ_START + 10000))
