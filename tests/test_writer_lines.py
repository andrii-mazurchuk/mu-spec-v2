"""The writer's two kinds of turn: a lines turn appends without a manifest save, never both (T-424).

A plain unit test of the rebuilt package in process: no acceptance input, no unit launched. A
handler that names cut-log lines and leaves the document unchanged gets them appended by
append_lines, seen by the next job; a dry run, a refusal and a keyed replay append nothing; a failed
append reaches the caller with no lifecycle record; a Built mixing both kinds raises BothKinds with
nothing written. Modules under test are imported inside the test, so a module not yet written fails
only the tests that need it.
"""

import datetime
import os
import string
import threading

import pytest

from mu_spec.outside import FixedClock

AT = datetime.datetime(2026, 11, 25, 12, 0, tzinfo=datetime.UTC)
T0 = 10**18  # nanoseconds, a fixed past instant
JOIN_S = 10.0
L1 = {"seq": 1, "units": ["a"]}
L2 = {"seq": 2, "units": ["b"]}


def joined(fn, *args):
    """fn(*args) on its own thread, joined within JOIN_S; its exception re-raised here."""
    box = {}

    def target():
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller unchanged
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(JOIN_S)
    if t.is_alive():
        pytest.fail(f"submit did not return within {JOIN_S} s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def catalogue_refusal():
    """A catalogue refusal and the values it is raised with: the member with the fewest fields."""
    from mu_spec.refusals import CATALOGUE, Refused, refuse

    def fields(name):
        return {f for _, f, _, _ in string.Formatter().parse(CATALOGUE[name].template) if f is not None}

    name = min(CATALOGUE, key=lambda n: len(fields(n)))
    values = {f: f"<{f}>" for f in fields(name)}
    try:
        refuse(name, **values)
    except Refused as probe:
        return name, values, probe
    pytest.fail(f"refuse({name!r}) raised nothing")


class Fixture:
    """The fixture handlers' shared parts: no checks, no replay records, the dry-run recast."""

    check_order = ()

    def log_records(self, call, built, at):
        return ()

    def replay_log_records(self, call, body, at):
        return ()

    def recast_dry(self, result):
        from mu_spec.writer import Built, Outcome

        return Outcome(200, {"admitted": isinstance(result, Built)})


class HL(Fixture):
    """A lines turn: the doc as given, two cut-log lines; log_records a spy."""

    def __init__(self) -> None:
        self.spied = []

    def build(self, state, call, doc, ids, at):
        from mu_spec.codec import CUT_LOG_NAME
        from mu_spec.writer import Built

        return Built({"ok": True}, 200, appends={CUT_LOG_NAME: (L1, L2)})

    def log_records(self, call, built, at):
        from mu_spec.codec import MUTATION_KIND, new_log_record

        self.spied.append(call.tool)
        return (new_log_record(MUTATION_KIND, "p", [], {"t": "cut"}, AT),)


class HS(Fixture):
    """A manifest turn recording how many cut-log lines its state holds."""

    def __init__(self) -> None:
        self.seen = []

    def build(self, state, call, doc, ids, at):
        from mu_spec.codec import CUT_LOG_NAME
        from mu_spec.writer import Built

        self.seen.append(len(state.logs[CUT_LOG_NAME].lines) if CUT_LOG_NAME in state.logs else 0)
        doc["seen"] = 1
        return Built({"ok": True}, 200)


class HR(HL):
    """HL behind one check that refuses with a catalogue refusal."""

    def __init__(self, name, values) -> None:
        super().__init__()
        from mu_spec.refusals import refuse

        def check(ctx):
            refuse(name, **values)

        self.check_order = (("refuse", check),)


class HM(Fixture):
    """A manifest turn with no appends argument."""

    def build(self, state, call, doc, ids, at):
        from mu_spec.writer import Built

        doc["m"] = 1
        return Built({"ok": True}, 200)


class HB1(Fixture):
    """A manifest change and cut-log lines in one Built."""

    def build(self, state, call, doc, ids, at):
        from mu_spec.codec import CUT_LOG_NAME
        from mu_spec.writer import Built

        doc["fx"] = 1
        return Built({"ok": True}, 200, appends={CUT_LOG_NAME: (L1,)})


class HB2(Fixture):
    """Lines for both project logs in one Built."""

    def build(self, state, call, doc, ids, at):
        from mu_spec.codec import CUT_LOG_NAME, SHIP_LOG_NAME
        from mu_spec.writer import Built

        return Built({"ok": True}, 200, appends={CUT_LOG_NAME: (L1,), SHIP_LOG_NAME: (L2,)})


def test_lines_turn(tmp_path, monkeypatch):
    from mu_spec.codec import (
        CUT_LOG_NAME,
        SHIP_LOG_NAME,
        ProjectRecord,
        encode_project_line,
        encode_record,
        record_path,
        thaw_value,
    )
    from mu_spec.refusals import refusal_body
    from mu_spec.store import Store
    from mu_spec.writer import (
        NO_APPENDS,
        BothKinds,
        Built,
        Outcome,
        ProjectWriter,
        WriteCall,
    )

    from mu_spec.outside import load_config

    D = tmp_path / "state"
    c = load_config({"HOLONIC_STATE_DIR": str(D), "MU_SPEC_PORT": "9100"}, tmp_path)[0]
    R = c.root
    R.mkdir(parents=True)
    ML = record_path(R, "p")
    ML.parent.mkdir(parents=True, exist_ok=True)
    ML.write_bytes(encode_record(ProjectRecord("p", {"name": "p", "entries": [], "counters": {}})))
    CL = ML.parent / CUT_LOG_NAME

    counts = {"write_record": 0, "append_lines": 0}
    original_write, original_append = Store.write_record, Store.append_lines

    def write_record(self, record):
        counts["write_record"] += 1
        return original_write(self, record)

    def append_lines(self, project, log, docs):
        counts["append_lines"] += 1
        return original_append(self, project, log, docs)

    monkeypatch.setattr(Store, "write_record", write_record)
    monkeypatch.setattr(Store, "append_lines", append_lines)

    store = Store(c)
    w = ProjectWriter("p", store, FixedClock(AT))
    name, values, refusal = catalogue_refusal()
    hl, hs, hr, hm = HL(), HS(), HR(name, values), HM()

    def call(tool, key=None, dry=False):
        return WriteCall(tool, {"a": 1}, "x", key, dry)

    def submit(c_, h):
        return joined(w.submit, c_, h)

    def n_log():
        return len(store.read_log(project="p"))

    def snapR():
        return {str(f.relative_to(R)): f.read_bytes() for f in R.rglob("*") if f.is_file()}

    # 1. a Built with body and status only is a manifest turn: write_record, no project log file
    assert Built({"ok": True}, 200).appends is NO_APPENDS
    assert submit(call("m"), hm) == Outcome(200, {"ok": True})
    assert counts == {"write_record": 1, "append_lines": 0}
    assert not (ML.parent / CUT_LOG_NAME).exists()
    assert not (ML.parent / SHIP_LOG_NAME).exists()
    os.utime(ML, ns=(T0, T0))
    counts.update(write_record=0, append_lines=0)

    # 2. a lines turn appends without a manifest save, and the next job's state holds the lines
    b0, k0 = ML.read_bytes(), n_log()
    assert submit(call("cut"), hl) == Outcome(200, {"ok": True})
    assert CL.read_bytes() == encode_project_line(L1) + encode_project_line(L2)
    assert counts == {"write_record": 0, "append_lines": 1}
    assert ML.read_bytes() == b0
    assert os.stat(ML).st_mtime_ns == T0
    assert [thaw_value(line.doc) for line in store.read("p").logs[CUT_LOG_NAME].lines] == [L1, L2]
    assert n_log() == k0 + 1
    submit(call("seen"), hs)
    assert hs.seen == [2]

    # 3. a dry run and a refusal append nothing and record nothing
    cb, k1 = CL.read_bytes(), n_log()
    assert submit(call("cut", dry=True), hl) == Outcome(200, {"admitted": True})
    assert CL.read_bytes() == cb and n_log() == k1
    refused = submit(call("cut"), hr)
    assert refused.status == refusal.status
    assert dict(refused.body) == refusal_body(refusal)
    assert CL.read_bytes() == cb and n_log() == k1

    # 4. a keyed replay runs no build and appends nothing
    first = submit(call("k", key="K"), hm)
    assert first.status == 200 and dict(first.body) == {"ok": True}, first
    a = counts["append_lines"]
    replay = submit(call("k", key="K"), hl)
    assert replay.body.get("replayed") is True, replay
    assert CL.read_bytes() == cb
    assert counts["append_lines"] == a

    # 5. a failed append reaches the caller as the same object, with nothing recorded
    e = OSError("disk")

    def failing_append(self, project, log, docs):
        raise e

    with monkeypatch.context() as patched:
        patched.setattr(Store, "append_lines", failing_append)
        hl.spied.clear()
        k2, s0 = n_log(), snapR()
        with pytest.raises(OSError) as raised:
            submit(call("cut"), hl)
        assert raised.value is e
        assert hl.spied == []
        assert n_log() == k2
        assert snapR() == s0

    # 6. a Built mixing both kinds, naming two logs, or naming lines on a keyed call: BothKinds
    for c_, h in ((call("b1"), HB1()), (call("b2"), HB2()), (call("b3", key="K3"), hl)):
        s0, k3 = snapR(), n_log()
        with pytest.raises(BothKinds) as raised:
            submit(c_, h)
        assert raised.value.tool == c_.tool
        assert snapR() == s0
        assert n_log() == k3

    # 7. the writer still serves after the exceptions
    assert submit(call("m2"), hm) == Outcome(200, {"ok": True})
