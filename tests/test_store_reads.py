"""After start-up, reads come from the held map alone and hand out one frozen object (T-413).

A plain unit test: no acceptance input, no unit started, no running-unit bytes. Records, project log
lines and the inbox are encoded in process by the rebuilt codec from literal documents. Modules under
test are imported inside the test, so a module not yet written fails only the tests that need it.
"""

import builtins
import io
import os

import pytest


def doc(n):
    return {"name": n, "entries": [{"id": "I-28 (was I-01)", "title": "a", "tags": ["x"], "meta": {"k": "v"}}],
            "counters": {"I": 1}}


def refuse(*args, **kwargs):
    raise AssertionError(f"a file was touched after start-up: {args!r}")


def test_reads_from_map_only(tmp_path, monkeypatch):
    from mu_spec.codec import (
        CUT_LOG_NAME,
        ProjectRecord,
        encode_inbox,
        encode_project_line,
        encode_record,
        inbox_path,
        record_path,
    )
    from mu_spec.store import Store

    from mu_spec.outside import load_config

    D = tmp_path / "state"
    c = load_config({"HOLONIC_STATE_DIR": str(D), "MU_SPEC_PORT": "9100"}, tmp_path)[0]
    R = c.root
    assert R == D / "projects"
    for n in ("a", "b"):
        record_path(R, n).parent.mkdir(parents=True)
        record_path(R, n).write_bytes(encode_record(ProjectRecord(n, doc(n))))
    cut_log = record_path(R, "a").parent / CUT_LOG_NAME
    cut_log.write_bytes(encode_project_line({"seq": 1, "units": [{"key": "u"}]}))
    inbox_path(R).write_bytes(encode_inbox({"requests": [{"id": "msg-0001", "type": "initiate", "body": "b"}]}))
    s = Store(c)
    assert s.read_inbox() is not None
    assert list(s.read("a").logs) == [CUT_LOG_NAME]
    assert s.read("a").logs[CUT_LOG_NAME].lines[0].doc is not None

    # 1. no file is touched by a read
    with monkeypatch.context() as m:
        for target, name in ((builtins, "open"), (io, "open"), (os, "open"), (os, "stat"),
                             (os, "listdir"), (os, "scandir")):
            m.setattr(target, name, refuse)
        assert s.project_names() == ["a", "b"]
        assert s.projects_unreadable() is False
        r1 = s.read("a")
        r2 = s.read("a")
        assert r1 is r2
        assert s.read("zzz") is None
        i1 = s.read_inbox()
        i2 = s.read_inbox()
        assert i1 is i2

    # 2. the held copy is frozen at every depth, project log lines included
    def change(f):
        with pytest.raises((TypeError, AttributeError)):
            f()

    change(lambda: r1.doc.__setitem__("x", 1))
    change(lambda: r1.doc["entries"][0]["meta"].__setitem__("k", "w"))
    change(lambda: r1.doc["entries"][0]["tags"].append("y"))
    ld = r1.logs[CUT_LOG_NAME].lines[0].doc
    change(lambda: ld.__setitem__("x", 1))
    change(lambda: ld["units"][0].__setitem__("key", "z"))
    change(lambda: i1.__setitem__("x", 1))
    change(lambda: i1["requests"][0].__setitem__("body", "z"))

    # 3. later reads equal a fresh Store's and the files
    stored = record_path(R, "a").read_bytes()
    fresh = Store(c)
    assert encode_record(s.read("a")) == encode_record(fresh.read("a")) == stored
    assert encode_record(ProjectRecord.from_doc("a", s.read("a").thaw())) == stored
    assert s.read("a").logs == fresh.read("a").logs
    assert encode_project_line(s.read("a").logs[CUT_LOG_NAME].lines[0].doc) == cut_log.read_bytes()
    assert encode_inbox(s.read_inbox()) == inbox_path(R).read_bytes()

    # 4. an outside change is seen at the next start only
    record_path(R, "a").unlink()
    assert s.read("a") is r1
    assert "a" in s.project_names()
