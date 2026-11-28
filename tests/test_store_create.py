"""create_project: a project appears by its manifest, last, under the store root, with no project log (T-410).

A plain unit test: no acceptance input, no unit started, no running-unit bytes. Records are encoded
in process by the rebuilt codec from literal documents. Modules under test are imported inside the
test, so a module not yet written fails only the tests that need it.
"""

import os

import pytest

PDOC = {"name": "p", "entries": [{"id": "I-28 (was I-01)", "title": "a", "tags": ["x"]}], "counters": {"I": 1}}


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


def files(d):
    return {p for p in d.rglob("*") if p.is_file()}


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_create_project_manifest_last(tmp_path, monkeypatch):
    from mu_spec.codec import (
        CUT_LOG_NAME,
        EMPTY_LOGS,
        SHIP_LOG_NAME,
        ProjectRecord,
        decode_project_log,
        encode_project_line,
        encode_record,
        record_path,
        temp_path,
    )
    from mu_spec.store import ProjectPresent, Store

    from mu_spec.outside import load_config

    c = load_config({"HOLONIC_STATE_DIR": str(tmp_path / "state"), "MU_SPEC_PORT": "9100"}, tmp_path)[0]
    R = c.root
    R.mkdir(parents=True)
    write(record_path(R, "p"), encode_record(ProjectRecord("p", PDOC)))
    left = R / "left"
    line = encode_project_line({"seq": 1})
    for rel in ("a.txt", "sub/b.txt", CUT_LOG_NAME, SHIP_LOG_NAME):
        write(left / rel, line)
    write(temp_path(record_path(R, "left")), line)
    obs = Recorder()
    s = Store(c, observer=obs)
    obs.events.clear()
    N = ProjectRecord.from_doc("n", PDOC)
    L = ProjectRecord.from_doc("left", PDOC)

    # 1. the replace fails: absent everywhere, nothing reported
    with monkeypatch.context() as m:
        def refuse(*args, **kwargs):
            raise OSError("replace refused by the test")

        m.setattr(os, "replace", refuse)
        with pytest.raises(OSError):
            s.create_project(N)
        assert "n" not in s.project_names()
        assert Store(c).project_names() == ["p"]
    assert obs.events == []

    under_root = set(os.listdir(R))
    under_state = set(os.listdir(tmp_path / "state"))

    # 2. a new project: its manifest alone, logs EMPTY_LOGS, notify False, no committed
    s.create_project(N)
    assert "n" in s.project_names()
    assert record_path(R, "n").read_bytes() == encode_record(N)
    assert files(record_path(R, "n").parent) == {record_path(R, "n")}
    assert s.read("n").logs is EMPTY_LOGS
    assert obs.events == [("installed", "n", encode_record(N), False)]

    # 3. over a crashed attempt's leftovers, the directory kept
    ino = os.stat(left).st_ino
    s.create_project(L)
    assert files(left) == {record_path(R, "left")}
    assert os.stat(left).st_ino == ino
    assert s.read("left").logs is EMPTY_LOGS
    assert Store(c).read("left").logs is EMPTY_LOGS

    # 4. a present project
    with pytest.raises(ProjectPresent):
        s.create_project(N)

    # 5. nothing new beside the project directories, nothing under the state directory
    assert set(os.listdir(R)) - under_root <= {"n"}
    assert set(os.listdir(tmp_path / "state")) == under_state

    # 6. a record carrying logs is refused before anything is written
    M = ProjectRecord.from_doc("m", PDOC, {CUT_LOG_NAME: decode_project_log("loc", encode_project_line({"seq": 1}))})
    obs.events.clear()
    with pytest.raises(ValueError):
        s.create_project(M)
    assert not (R / "m").exists()
    assert "m" not in s.project_names()
    assert obs.events == []
