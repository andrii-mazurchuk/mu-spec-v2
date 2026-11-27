"""unitcheck.preflight: the preflight every check session runs before its first unit run.

test_declared_sets counts the declared sets and the numeric comparisons; test_failures_named breaks one
behaviour at a time, through the names unitcheck.preflight imports, and expects it named;
test_preflight_passes_on_host runs it on this host as it is. Every preflight here launches through the
real shared launch and interposer, with a real StandIn and Neighbour; the stubs only remove reports,
alter a built answer, the probe's exit code or the log file, so what is judged differs from what a correct
interposer gives in exactly the one way each case names.
The modules under test are imported inside each test, so while they are absent only these tests fail.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


def R(n):
    return b'{"kind":"probe","n":%d}\n' % n


# Put in front of the probe program for the exit case: the probe runs as it is, and exits 1 once it has
# sent a datagram with sendto, after every one of its steps. A spawned child runs it unchanged.
EXITS_ONE = r'''
import socket as _socket
import sys as _sys

_EXITS_ONE_SOURCE = %r

if __name__ == "__main__":
    _sent = []
    _sendto = _socket.socket.sendto

    def _counted(self, *args):
        _sent.append(1)
        return _sendto(self, *args)

    _socket.socket.sendto = _counted
    try:
        exec(compile(_EXITS_ONE_SOURCE, __file__, "exec"), globals())
        _code = 0
    except SystemExit as e:
        _code = e.code
    _sys.exit(1 if _sent else _code)
else:
    exec(compile(_EXITS_ONE_SOURCE, __file__, "exec"), globals())
'''


class Parts:
    """A real StandIn and Neighbour for one preflight, closed after it."""

    def __init__(self):
        from shipcheck.standin import StandIn
        from unitcheck.neighbour import Neighbour

        self.standin = StandIn()
        try:
            self.neighbour = Neighbour()
        except BaseException:
            self.standin.close()
            raise

    def close(self):
        try:
            self.neighbour.close()
        finally:
            self.standin.close()


def run(scratch, parts):
    from unitcheck.preflight import preflight

    return preflight(str(scratch), dict(os.environ), parts.standin, parts.neighbour)


def snapshot(base, scratch):
    """Every path under base outside scratch, each file with its mtime."""
    seen = {}
    for folder, dirs, files in os.walk(base):
        here = Path(folder)
        if here == scratch:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if here / d != scratch]
        for d in dirs:
            seen[str((here / d).relative_to(base))] = None
        for f in files:
            seen[str((here / f).relative_to(base))] = (here / f).stat().st_mtime_ns
    return seen


def assert_named(error):
    from unitcheck.preflight import PROBES

    failed = tuple(error.failed)
    assert failed, error
    assert all(name in PROBES for name in failed), failed
    assert list(failed) == sorted(failed, key=PROBES.index), failed
    assert len(set(failed)) == len(failed), failed
    assert len(error.details) == len(failed), (failed, error.details)
    for line in error.details:
        assert "\n" not in line and "\r" not in line, error.details
    return failed


def stub_connects(monkeypatch):
    """An interposer whose control holds no report of any connect."""
    import unitcheck.preflight

    class Unseen:
        def __init__(self, inner):
            self._inner = inner

        def attempts(self):
            return tuple(a for a in self._inner.attempts() if a.op != "connect")

        def __getattr__(self, name):
            return getattr(self._inner, name)

    real = unitcheck.preflight.InterposedLauncher

    class Launcher(real):
        def control(self, prepared):
            return Unseen(super().control(prepared))

    monkeypatch.setattr(unitcheck.preflight, "InterposedLauncher", Launcher)


def stub_type_zero(monkeypatch):
    """built_forward answering every getaddrinfo sample as if it were asked type 0."""
    import unitcheck.preflight

    real = unitcheck.preflight.built_forward

    def built_forward(op, *args, **kwargs):
        if op == "getaddrinfo":
            args = list(args)
            if len(args) >= 4:
                args[3] = 0
            else:
                kwargs["type"] = 0
        return real(op, *args, **kwargs)

    monkeypatch.setattr(unitcheck.preflight, "built_forward", built_forward)


def stub_follower_leak(monkeypatch, scratch):
    """A follower that let the first call of record 2 through: the launch under ('kth', 2) leaves a fragment
    of record 2 after record 1 in the file, while its reports stay those of a correct follower."""
    import unitcheck.preflight
    from unitcheck.launch import followed_log

    real_launcher = unitcheck.preflight.InterposedLauncher
    real_start = unitcheck.preflight.start

    class Launcher(real_launcher):
        def __init__(self, standin, neighbour, mode="off", k=None):
            super().__init__(standin, neighbour, mode, k)
            self.stub_mode = (mode, k)

    def start(*args, **kwargs):
        launcher = kwargs["launcher"] if "launcher" in kwargs else args[4]
        started = real_start(*args, **kwargs)
        if getattr(launcher, "stub_mode", None) == ("kth", 2):
            log = Path(followed_log(str(scratch / "root")))
            data = log.read_bytes()
            cut = data.index(b"\n") + 1
            log.write_bytes(data[:cut] + R(2)[:8] + data[cut:])
        return started

    monkeypatch.setattr(unitcheck.preflight, "InterposedLauncher", Launcher)
    monkeypatch.setattr(unitcheck.preflight, "start", start)


def stub_send_exits_one(monkeypatch):
    """The probe program, as installed, exits 1 in the launch that sends datagrams, after sending them."""
    import unitcheck.preflight
    from unitcheck.preflight import PROBE_PACKAGE

    real_start = unitcheck.preflight.start

    def start(*args, **kwargs):
        cmd = kwargs["cmd"] if "cmd" in kwargs else args[0]
        main = Path(cmd.install_root) / PROBE_PACKAGE / "__main__.py"
        source = main.read_text()
        if "_EXITS_ONE_SOURCE" not in source:
            main.write_text(EXITS_ONE % source)
        return real_start(*args, **kwargs)

    monkeypatch.setattr(unitcheck.preflight, "start", start)


def failed_under(tmp_path, name, *stubs):
    from unitcheck.preflight import PreflightError

    scratch = tmp_path / name
    scratch.mkdir()
    parts = Parts()
    try:
        with pytest.MonkeyPatch.context() as m:
            for stub in stubs:
                stub(m, scratch)
            with pytest.raises(PreflightError) as caught:
                run(scratch, parts)
    finally:
        parts.close()
    return assert_named(caught.value)


def test_declared_sets(tmp_path, monkeypatch):
    import unitcheck.preflight
    from unitcheck.preflight import (
        NUMERIC_FAMILIES,
        NUMERIC_HOSTS,
        NUMERIC_PORTS,
        NUMERIC_REVERSE,
        NUMERIC_TYPES,
        PROBES,
        PROCESS_ROUTES,
        ROUTE_CASES,
        ProbeResult,
    )

    assert len(PROBES) == 13 == len(set(PROBES))
    assert len(ROUTE_CASES) == 14 == len(set(ROUTE_CASES))
    assert len(NUMERIC_HOSTS) * len(NUMERIC_PORTS) * len(NUMERIC_FAMILIES) * len(NUMERIC_TYPES) == 81
    assert len(NUMERIC_REVERSE) == 2
    assert ("win32" if sys.platform == "win32" else "posix") in PROCESS_ROUTES

    counts = {"getaddrinfo": 0, "gethostbyname": 0, "gethostbyname_ex": 0, "reverse": 0}
    real_forward = unitcheck.preflight.built_forward
    real_reverse = unitcheck.preflight.built_reverse

    def built_forward(op, *args, **kwargs):
        counts[op] = counts.get(op, 0) + 1
        return real_forward(op, *args, **kwargs)

    def built_reverse(*args, **kwargs):
        counts["reverse"] += 1
        return real_reverse(*args, **kwargs)

    monkeypatch.setattr(unitcheck.preflight, "built_forward", built_forward)
    monkeypatch.setattr(unitcheck.preflight, "built_reverse", built_reverse)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    parts = Parts()
    try:
        results = run(scratch, parts)
    finally:
        parts.close()

    assert len(results) == 13, results
    assert all(isinstance(r, ProbeResult) for r in results), results
    assert [r.behaviour for r in results] == list(PROBES), results
    assert all(r.ok for r in results), [r for r in results if not r.ok]
    assert counts["getaddrinfo"] == 81, counts
    assert counts["gethostbyname"] + counts["gethostbyname_ex"] == 6, counts
    assert counts["reverse"] == 2, counts
    assert sum(counts.values()) == 89, counts


def test_failures_named(tmp_path):
    from unitcheck.preflight import PROBES

    # No connect reported: the connect to a name is unseen although the neighbour holds it.
    failed = failed_under(tmp_path, "connect", lambda m, s: stub_connects(m))
    assert "observe_connect_name" in failed, failed

    # Built answers of type 0 for every sample; on win32 the system's type-0 answer is one entry, so equal.
    if sys.platform != "win32":
        failed = failed_under(tmp_path, "numeric", lambda m, s: stub_type_zero(m))
        assert "numeric_answers" in failed, failed

    # The first call of record 2 let through: the reports still say ordinal 2 'failed', the file does not.
    failed = failed_under(tmp_path, "holding", stub_follower_leak)
    assert "holding" in failed, failed

    # The probe exits 1 after sending both datagrams: its own account and the neighbour's would pass.
    failed = failed_under(tmp_path, "exits", lambda m, s: stub_send_exits_one(m))
    assert "observe_send" in failed, failed

    # All three at once: every failed behaviour is collected, none dropped at the first.
    failed = failed_under(tmp_path, "together", lambda m, s: stub_connects(m), stub_follower_leak,
                          lambda m, s: stub_send_exits_one(m))
    assert {"observe_connect_name", "holding", "observe_send"} <= set(failed), failed

    # A correct interposer: no error, and nothing beside scratch is written.
    base = tmp_path / "correct"
    base.mkdir()
    (base / "beside.txt").write_text("untouched\n")
    (base / "folder").mkdir()
    (base / "folder" / "inside.txt").write_text("untouched\n")
    scratch = base / "scratch"
    scratch.mkdir()
    before = snapshot(base, scratch)
    parts = Parts()
    try:
        results = run(scratch, parts)
    finally:
        parts.close()
    assert [r.behaviour for r in results] == list(PROBES), results
    assert all(r.ok for r in results), [r for r in results if not r.ok]
    assert snapshot(base, scratch) == before


def test_preflight_passes_on_host():
    from shipcheck.standin import IDENTITY_REQUEST
    from unitcheck.neighbour import CONDITIONS
    from unitcheck.preflight import INTERPRETERS, PROBES

    scratch = Path(tempfile.mkdtemp())
    parts = Parts()
    standin, neighbour = parts.standin, parts.neighbour
    logs = []
    conditions = []
    real_reset, real_set = standin.reset, neighbour.set

    def reset(script):
        logs.append(standin.log())
        return real_reset(script)

    def set_condition(condition):
        conditions.append(condition)
        return real_set(condition)

    standin.reset = reset
    neighbour.set = set_condition
    try:
        results = run(scratch, parts)
        logs.append(standin.log())
        after = neighbour.condition
    finally:
        parts.close()
        shutil.rmtree(scratch, ignore_errors=True)

    assert [r.behaviour for r in results] == list(PROBES) * len(INTERPRETERS), results
    assert all(r.ok for r in results), [r for r in results if not r.ok]
    for r in results:
        assert isinstance(r.detail, str) and "\n" not in r.detail and "\r" not in r.detail, r
    assert conditions and set(conditions) <= set(CONDITIONS), conditions
    assert after == conditions[-1], (after, conditions)
    assert any(IDENTITY_REQUEST in [(q.method, q.path) for q in log] for log in logs), logs
