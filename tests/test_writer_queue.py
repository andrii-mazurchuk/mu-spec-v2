"""One project's writer: checks see the previous commit, and only the lifecycle log is swallowed (T-420).

A plain unit test of the rebuilt package in process. Two threads submitting to one project: the
second's checks run at its turn and see the first's commit. A failing lifecycle-log append changes
neither the answer nor the record; a failing commit reaches the caller, records no log line and
leaves the idempotency key free. Modules under test are imported inside the test, so a module not
yet written fails only the tests that need it.
"""

import datetime
import os
import string
import threading
import time

import pytest

from mu_spec.outside import FixedClock

START = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
JOIN_S = 10.0
SLOW_S = 0.2
LAG_S = 0.05


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


class H:
    """Refuses once the doc holds 'taken'; build takes it for the actor, the first call slowly."""

    def __init__(self, project, refusal, slow_first=False) -> None:
        from mu_spec.refusals import refuse

        name, values = refusal

        def check(ctx):
            if "taken" in ctx.state.doc:
                refuse(name, **values)

        self.project = project
        self.check_order = (("taken", check),)
        self.slow = slow_first

    def build(self, state, call, doc, ids, at):
        from mu_spec.writer import Built

        if self.slow:
            self.slow = False
            time.sleep(SLOW_S)
        doc["taken"] = call.actor
        return Built({"ok": True}, 200)

    def log_records(self, call, built, at):
        from mu_spec.codec import MUTATION_KIND, new_log_record

        return (new_log_record(MUTATION_KIND, self.project, [], {"taken": call.actor}, at),)

    def replay_log_records(self, call, body, at):
        return ()

    def recast_dry(self, result):
        from mu_spec.writer import Built, Outcome

        return Outcome(200, {"admitted": isinstance(result, Built)})


def in_thread(fn, *args):
    """Start fn(*args) on its own thread; the returned join gives its value or raises its exception."""
    box = {}

    def target():
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller unchanged
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()

    def join():
        t.join(JOIN_S)
        if t.is_alive():
            pytest.fail(f"submit did not return within {JOIN_S} s")
        if "error" in box:
            raise box["error"]
        return box["value"]

    return join


def test_fifo_and_log_failure(tmp_path, monkeypatch):
    from mu_spec.codec import ProjectRecord, record_path
    from mu_spec.refusals import refusal_body
    from mu_spec.registry import Registry
    from mu_spec.store import Store
    from mu_spec.writer import WriteCall

    from mu_spec.outside import ENV_NAMES, load_config

    D = tmp_path / "store"
    config = load_config({ENV_NAMES["port"]: "9100", ENV_NAMES["mu_spec_root"]: str(D)}, tmp_path)[0]
    store = Store(config)
    for p in ("p", "q", "r", "s"):
        store.create_project(ProjectRecord.from_doc(p, {"name": p, "entries": [], "counters": {}}))
    registry = Registry(store, FixedClock(START))
    name, values, refusal = catalogue_refusal()

    def call(actor, key=None):
        return WriteCall("take", {"what": "it"}, actor, key, False)

    # 1. B arrives while A is being built; B's checks run at its turn and see A's commit
    w = registry.writer("p")
    h = H("p", (name, values), slow_first=True)
    join_a = in_thread(w.submit, call("a"), h)
    time.sleep(LAG_S)
    join_b = in_thread(w.submit, call("b"), h)
    a, b = join_a(), join_b()
    assert a.status == 200 and dict(a.body) == {"ok": True}, a
    assert b.status == refusal.status, b
    assert dict(b.body) == refusal_body(refusal), b
    assert store.read("p").doc["taken"] == "a"

    # 2. a failing lifecycle-log append: the same answer and record as with logging working
    def failing_log(self, record):
        raise OSError("log failed")

    with monkeypatch.context() as patched:
        patched.setattr(Store, "append_log", failing_log)
        q = registry.writer("q").submit(call("a", "k"), H("q", (name, values)))
    r = registry.writer("r").submit(call("a", "k"), H("r", (name, values)))
    assert (q.status, dict(q.body)) == (r.status, dict(r.body))
    q_bytes = record_path(D, "q").read_bytes()
    r_bytes = record_path(D, "r").read_bytes()
    assert q_bytes.replace(b'"q"', b'"r"') == r_bytes

    # 3. a failing commit reaches the caller, records nothing and leaves the key free
    def failing_replace(src, dst, *args, **kwargs):
        raise OSError("replace failed")

    ws = registry.writer("s")
    with monkeypatch.context() as patched:
        patched.setattr(os, "replace", failing_replace)
        with pytest.raises(OSError):
            ws.submit(call("a", "k2"), H("s", (name, values)))
    assert store.read_log(project="s") == []
    fresh = ws.submit(call("a", "k2"), H("s", (name, values)))
    assert fresh.status == 200 and dict(fresh.body) == {"ok": True}, fresh
    assert "replayed" not in fresh.body
