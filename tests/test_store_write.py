"""write_record: the manifest replaced atomically under the store root, reported under the map lock (T-412).

A plain unit test: no acceptance input, no unit started, no running-unit bytes. The record is encoded
in process by the rebuilt codec from a literal document. Modules under test are imported inside the
test, so a module not yet written fails only the tests that need it.
"""

import builtins
import io
import os
import threading

import pytest

PDOC = {"name": "p", "title": "old", "entries": [{"id": "I-28 (was I-01)", "title": "a", "tags": ["x"]}],
        "counters": {"I": 1}}
T0 = 10**18  # nanoseconds, a fixed past instant


def same(arg, *paths):
    if isinstance(arg, (str, bytes, os.PathLike)):
        text = os.path.normcase(os.path.abspath(os.fsdecode(os.fspath(arg))))
        return any(text == os.path.normcase(os.path.abspath(str(p))) for p in paths)
    return False


class Probing:
    """An InstallObserver that checks a reader is held off while installed runs."""

    def __init__(self):
        self.events = []
        self.store = None
        self.readers = []

    def installed(self, project, data, *, notify):
        self.events.append(("installed", project, data, notify))
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("read", self.store.read(project)), daemon=True)
        t.start()
        t.join(0.2)
        self.events.append(("blocked", t.is_alive()))
        self.readers.append((t, result))

    def committed(self, project):
        self.events.append(("committed", project, self.store.read(project)))

    def appended(self, record):
        pass


@pytest.mark.parametrize("observed", [False, True], ids=["observer None", "observer recording"])
def test_write_record_atomic(tmp_path, monkeypatch, observed):
    from mu_spec.codec import (
        CUT_LOG_NAME,
        EMPTY_LOGS,
        SHIP_LOG_NAME,
        ProjectRecord,
        encode_project_line,
        encode_record,
        record_path,
        temp_path,
    )
    from mu_spec.store import ProjectAbsent, Store

    from mu_spec.outside import load_config

    D = tmp_path / "state"
    c = load_config({"HOLONIC_STATE_DIR": str(D), "MU_SPEC_PORT": "9100"}, tmp_path)[0]
    R = c.root
    assert R == D / "projects"
    final = record_path(R, "p")
    final.parent.mkdir(parents=True)
    B0 = encode_record(ProjectRecord("p", PDOC))
    final.write_bytes(B0)
    dp = final.parent
    CL, SL = dp / CUT_LOG_NAME, dp / SHIP_LOG_NAME
    CB = encode_project_line({"seq": 1, "units": ["u"]})
    SB = encode_project_line({"seq": 1, "kind": "ship"})
    CL.write_bytes(CB)
    SL.write_bytes(SB)
    os.utime(CL, ns=(T0, T0))
    os.utime(SL, ns=(T0, T0))
    tmp = temp_path(final)
    tmp.write_bytes(b"junk")
    obs = Probing() if observed else None
    s = Store(c, observer=obs) if observed else Store(c)
    if obs is not None:
        obs.store = s
    old = s.read("p")
    assert list(old.logs) == [CUT_LOG_NAME, SHIP_LOG_NAME]
    newdoc = old.thaw()
    newdoc["title"] = "new"
    new = ProjectRecord.from_doc("p", newdoc)
    assert new.logs is EMPTY_LOGS
    N = encode_record(new)
    assert N != B0

    # 1. the replace fails: old bytes, old object, nothing reported
    with monkeypatch.context() as m:
        def refuse(*args, **kwargs):
            raise OSError("replace refused by the test")

        m.setattr(os, "replace", refuse)
        with pytest.raises(OSError):
            s.write_record(new)
    assert final.read_bytes() == B0
    assert s.read("p") is old
    if obs is not None:
        assert obs.events == []

    # 2. the save, with the project logs closed to every open and fsync and replace recorded
    order = []
    temp_fds = set()
    real_open, real_os_open = builtins.open, os.open
    real_fsync, real_replace = os.fsync, os.replace

    def guarded_open(file, *args, **kwargs):
        if same(file, CL, SL):
            raise AssertionError(f"a project log was opened: {file!r}")
        f = real_open(file, *args, **kwargs)
        if same(file, tmp):
            temp_fds.add(f.fileno())
        return f

    def guarded_os_open(path, *args, **kwargs):
        if same(path, CL, SL):
            raise AssertionError(f"a project log was opened: {path!r}")
        fd = real_os_open(path, *args, **kwargs)
        if same(path, tmp):
            temp_fds.add(fd)
        return fd

    def fsync(fd):
        order.append(("fsync", fd))
        return real_fsync(fd)

    def replace(src, dst, *args, **kwargs):
        order.append(("replace", src))
        return real_replace(src, dst, *args, **kwargs)

    outcome = {}

    def worker():
        try:
            s.write_record(new)
            outcome["ok"] = True
        except BaseException as exc:  # noqa: BLE001 -- reported by the main thread
            outcome["raised"] = exc

    with monkeypatch.context() as m:
        m.setattr(builtins, "open", guarded_open)
        m.setattr(io, "open", guarded_open)
        m.setattr(os, "open", guarded_os_open)
        m.setattr(os, "fsync", fsync)
        m.setattr(os, "replace", replace)
        w = threading.Thread(target=worker, daemon=True)
        w.start()
        w.join(5)
        assert not w.is_alive(), "write_record did not return within 5 s"
    assert outcome == {"ok": True}, outcome
    assert final.read_bytes() == N
    got = s.read("p")
    assert got is not old
    assert encode_record(got) == N
    assert list(got.logs) == list(old.logs)
    assert dict(got.logs) == dict(old.logs)
    fresh = Store(c).read("p")
    assert encode_record(fresh) == N
    assert dict(fresh.logs) == dict(old.logs)
    assert not tmp.exists()
    assert CL.read_bytes() == CB and SL.read_bytes() == SB
    assert os.stat(CL).st_mtime_ns == T0 and os.stat(SL).st_mtime_ns == T0

    # 3. reported under the map lock, committed after it is released
    if obs is not None:
        X = s.read("p")
        assert obs.events[:2] == [("installed", "p", N, True), ("blocked", True)]
        assert len(obs.events) == 3
        assert obs.events[2][:2] == ("committed", "p") and obs.events[2][2] is X
        t, result = obs.readers[0]
        t.join(5)
        assert not t.is_alive()
        assert encode_record(result["read"]) == N

    # 4. an absent project
    seen = list(obs.events) if obs is not None else None
    with pytest.raises(ProjectAbsent):
        s.write_record(ProjectRecord.from_doc("nope", {}))
    if obs is not None:
        assert obs.events == seen

    # 5. the temp file was fsync'd before the replace
    first_replace = next(i for i, e in enumerate(order) if e[0] == "replace")
    assert any(e == ("fsync", fd) for e in order[:first_replace] for fd in temp_fds), order
