"""Project logs held beside the manifest, and append_lines as one durable, single-write append (T-418).

A plain unit test: no acceptance input, no unit started, no running-unit bytes. Manifests and log
lines are encoded in process by the rebuilt codec; lines are asserted by their raw bytes and docs,
never by the value of n. The write interception is tests/test_store_log.py's. Modules under test are
imported inside the test, so a module not yet written fails only the tests that need it.
"""

import builtins
import collections
import io
import os

import pytest

T0 = 10**18
X = {"seq": 2, "units": ["a"]}
Y = {"seq": 3, "units": ["b"]}


def pdoc(n):
    return {"name": n, "entries": [], "counters": {}}


class Recorder:
    """An InstallObserver recording its installs and commits."""

    def __init__(self):
        self.events = []

    def installed(self, project, data, *, notify):
        self.events.append(("installed", project, data, notify))

    def committed(self, project):
        self.events.append(("committed", project))

    def appended(self, record):
        pass


def test_project_logs_held_and_appended(tmp_path, monkeypatch):
    from mu_spec.codec import (
        CUT_LOG_NAME,
        EMPTY_LOGS,
        PROJECT_LOGS,
        SHIP_LOG_NAME,
        ProjectRecord,
        decode_project_log,
        encode_project_line,
        encode_record,
        record_path,
        thaw_value,
    )
    from mu_spec.store import MANIFEST_NAME, ProjectAbsent, Store

    # the module pytest itself loaded from tests/ (no package files there), so one copy of the helper
    from test_store_log import check_coverage, interception

    from mu_spec.outside import load_config

    D = tmp_path / "state"
    c = load_config({"HOLONIC_STATE_DIR": str(D), "MU_SPEC_PORT": "9100"}, tmp_path)[0]
    R = c.root
    assert PROJECT_LOGS == (CUT_LOG_NAME, SHIP_LOG_NAME)
    assert MANIFEST_NAME not in PROJECT_LOGS
    MP = encode_record(ProjectRecord("p", pdoc("p")))
    record_path(R, "p").parent.mkdir(parents=True)
    record_path(R, "p").write_bytes(MP)
    dp = record_path(R, "p").parent
    c1 = encode_project_line({"seq": 1, "units": ["u x"]})
    CL, SL = dp / CUT_LOG_NAME, dp / SHIP_LOG_NAME
    CB = c1 + b"{not json\n"
    SB = encode_project_line({"seq": 1, "kind": "ship"})
    CL.write_bytes(CB)
    SL.write_bytes(SB)
    MQ = encode_record(ProjectRecord("q", pdoc("q")))
    record_path(R, "q").parent.mkdir(parents=True)
    record_path(R, "q").write_bytes(MQ)
    dq = record_path(R, "q").parent
    os.utime(record_path(R, "p"), ns=(T0, T0))
    os.utime(SL, ns=(T0, T0))
    obs = Recorder()

    def snapR():
        return {p.relative_to(R).as_posix(): p.read_bytes() for p in R.rglob("*") if p.is_file()}

    # 1. start-up holds both logs, split on b'\n' only, the bad line with doc None
    s = Store(c, observer=obs)
    hp = s.read("p")
    assert list(hp.logs) == [CUT_LOG_NAME, SHIP_LOG_NAME]
    cut = hp.logs[CUT_LOG_NAME]
    assert [ln.raw for ln in cut.lines] == [c1[:-1], b"{not json"]
    assert thaw_value(cut.lines[0].doc) == {"seq": 1, "units": ["u x"]}
    assert cut.lines[0].detail is None
    assert cut.lines[1].doc is None
    assert isinstance(cut.lines[1].detail, str) and cut.lines[1].detail
    assert cut.lines[0].n < cut.lines[1].n
    assert cut == decode_project_log(cut.location, CB)
    assert [ln.raw for ln in hp.logs[SHIP_LOG_NAME].lines] == [SB[:-1]]
    assert s.read("q").logs is EMPTY_LOGS
    assert CL.read_bytes() == CB
    assert collections.Counter(obs.events) == collections.Counter(
        [("installed", "p", MP, False), ("installed", "q", MQ, False)])
    assert len(obs.events) == 2

    # 2. append_lines: one append, one write, manifest and Ship log untouched
    obs.events.clear()
    with monkeypatch.context() as m:
        C = tmp_path / "coverage.scratch"
        ic = interception(m, CL, C)
        check_coverage(ic, C)
        s.append_lines("p", CUT_LOG_NAME, [X, Y])
        on_cut = ic.on(CL)
    assert CL.read_bytes() == CB + encode_project_line(X) + encode_project_line(Y)
    assert len(on_cut) == 1 and on_cut[0].writes == 1, on_cut
    h2 = s.read("p")
    held_cut = h2.logs[CUT_LOG_NAME]
    assert held_cut == decode_project_log(held_cut.location, CL.read_bytes())
    assert held_cut == Store(c).read("p").logs[CUT_LOG_NAME]
    assert h2.doc is hp.doc
    assert h2.logs[SHIP_LOG_NAME] == hp.logs[SHIP_LOG_NAME]
    assert len(hp.logs[CUT_LOG_NAME].lines) == 2
    assert record_path(R, "p").read_bytes() == MP
    assert os.stat(record_path(R, "p")).st_mtime_ns == T0
    assert SL.read_bytes() == SB
    assert os.stat(SL).st_mtime_ns == T0
    assert obs.events == [("installed", "p", MP, True), ("committed", "p")]

    # 3. a project with no Ship log gets one holding only the new lines
    obs.events.clear()
    s.append_lines("q", SHIP_LOG_NAME, [X])
    assert (dq / SHIP_LOG_NAME).read_bytes() == encode_project_line(X)
    assert not (dq / CUT_LOG_NAME).exists()
    assert list(Store(c).read("q").logs) == [SHIP_LOG_NAME]
    assert obs.events == [("installed", "q", MQ, True), ("committed", "q")]

    # 4. refused before anything is opened
    root_key = os.path.normcase(os.path.abspath(str(R)))

    def under_root(arg):
        if not isinstance(arg, (str, bytes, os.PathLike)):
            return False
        text = os.path.normcase(os.path.abspath(os.fsdecode(os.fspath(arg))))
        return text == root_key or text.startswith(root_key + os.sep)

    real_open, real_os_open = builtins.open, os.open

    def closed_open(file, *args, **kwargs):
        if under_root(file):
            raise AssertionError(f"opened before the call was refused: {file!r}")
        return real_open(file, *args, **kwargs)

    def closed_os_open(path, *args, **kwargs):
        if under_root(path):
            raise AssertionError(f"opened before the call was refused: {path!r}")
        return real_os_open(path, *args, **kwargs)

    for args, error in ((("p", "other.jsonl", [X]), ValueError),
                        (("p", CUT_LOG_NAME, []), ValueError),
                        (("zzz", CUT_LOG_NAME, [X]), ProjectAbsent)):
        obs.events.clear()
        before = snapR()
        held = s.read("p")
        with monkeypatch.context() as m:
            m.setattr(builtins, "open", closed_open)
            m.setattr(io, "open", closed_open)
            m.setattr(os, "open", closed_os_open)
            with pytest.raises(error):
                s.append_lines(*args)
        assert snapR() == before, args
        assert s.read("p") is held, args
        assert obs.events == [], args

    # 5a. a failed fsync is raised unchanged, nothing installed or reported
    obs.events.clear()
    held = s.read("p")
    e = OSError("fsync")
    with monkeypatch.context() as m:
        def failing_fsync(fd):
            raise e

        m.setattr(os, "fsync", failing_fsync)
        with pytest.raises(OSError) as caught:
            s.append_lines("p", CUT_LOG_NAME, [X])
    assert caught.value is e
    assert s.read("p") is held
    assert obs.events == []

    # 5b. a failed write is raised unchanged, the file as it was
    held = s.read("p")
    bytes0 = CL.read_bytes()
    e2 = OSError("write")

    def failing_write(append):
        raise e2

    with monkeypatch.context() as m:
        interception(m, CL, hook=failing_write)
        with pytest.raises(OSError) as caught:
            s.append_lines("p", CUT_LOG_NAME, [X])
    assert caught.value is e2
    assert CL.read_bytes() == bytes0
    assert s.read("p") is held
    assert obs.events == []
