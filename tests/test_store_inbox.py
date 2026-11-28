"""write_inbox: an atomic replace directly under the store root, installed frozen and reported (T-411).

A plain unit test: no acceptance input, no unit started, no running-unit bytes. The inbox documents
are literal and encoded by the rebuilt codec. Modules under test are imported inside the test, so a
module not yet written fails only the tests that need it.
"""

import os

import pytest

DOC1 = {"requests": [{"id": "msg-0001", "type": "initiate", "body": "b"}]}
DOC2 = {"requests": [{"id": "msg-0001", "type": "initiate", "body": "b"},
                     {"id": "msg-0002", "type": "initiate", "body": "c"}]}


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


def test_write_inbox(tmp_path, monkeypatch):
    from mu_spec.codec import encode_inbox, inbox_path, temp_path, thaw_value
    from mu_spec.store import Store

    from mu_spec.outside import load_config

    state = tmp_path / "state"
    c = load_config({"HOLONIC_STATE_DIR": str(state), "MU_SPEC_PORT": "9100"}, tmp_path)[0]
    R = c.root
    obs = Recorder()
    s = Store(c, observer=obs)

    # 1. no inbox yet; the root made at start-up
    assert s.read_inbox() is None
    assert obs.events == []
    assert R.is_dir()

    # 2. the first inbox
    s.write_inbox(DOC1)
    assert inbox_path(R).parent == R
    I1 = inbox_path(R).read_bytes()
    assert I1 == encode_inbox(DOC1)
    assert thaw_value(s.read_inbox()) == DOC1
    with pytest.raises((TypeError, AttributeError)):
        s.read_inbox()["x"] = 1
    assert obs.events == [("installed", None, I1, True), ("committed", None)]
    assert not temp_path(inbox_path(R)).exists()
    assert s.project_names() == []
    assert [p for p in state.rglob("*") if p != R and R not in p.parents] == []

    # 3. the replace fails: the file, the held inbox and the observer unchanged
    obs.events.clear()
    with monkeypatch.context() as m:
        def refuse(*args, **kwargs):
            raise OSError("replace refused by the test")

        m.setattr(os, "replace", refuse)
        with pytest.raises(OSError):
            s.write_inbox(DOC2)
    assert inbox_path(R).read_bytes() == I1
    assert thaw_value(s.read_inbox()) == DOC1
    assert obs.events == []

    # 4. the second inbox, seen by a fresh Store; the inbox is not a project
    s.write_inbox(DOC2)
    assert thaw_value(s.read_inbox()) == DOC2
    assert thaw_value(Store(c).read_inbox()) == DOC2
    assert Store(c).project_names() == []
