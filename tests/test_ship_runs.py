"""Run registry: RunRegistry, Busy and Stale in mu_spec.ship.runs.

Run ids are 12 random lowercase hex characters compared by shape only; one project has at most one
run of any kind in flight, and its generation moves when a run ends. The module under test is
imported inside the test, so a module not yet written fails only the tests that need it.
"""

import datetime
import re
import threading

import pytest

from mu_spec.outside import FixedClock

START = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
RUN_ID = re.compile(r"^[0-9a-f]{12}$")
VIEW_KEYS = ["running", "run_id", "phase", "step", "total", "processed", "created", "skipped",
             "failed", "wired", "unwired", "edited", "withdrawn", "touched", "started_at",
             "finished_at", "error", "result"]
ZERO_COUNTERS = ("processed", "created", "skipped", "failed", "wired", "unwired", "edited",
                 "withdrawn", "touched")


def test_ids_and_view():
    from mu_spec.ship.runs import RunRegistry

    clock = FixedClock(START)

    # 1. 1000 admits give 1000 distinct ids, each 12 lowercase hex characters
    reg = RunRegistry(clock)
    ids = []
    for _ in range(1000):
        r = reg.admit("p", "person_ship", None, reg.generation("p"))
        ids.append(r.run_id)
        reg.finish(r, "done", None, None)
        reg.end(r)
    assert all(RUN_ID.match(run_id) for run_id in ids)
    assert len(set(ids)) == 1000

    # 2. same project, same frozen clock, two registries: the ids still differ
    first = RunRegistry(clock).admit("p", "person_ship", None, 0)
    second = RunRegistry(clock).admit("p", "person_ship", None, 0)
    assert first.run_id != second.run_id

    # 3. a fresh registry has no run to show
    reg2 = RunRegistry(clock)
    assert reg2.view("p") is None

    # 4. the view right after admission, in the running unit's key order
    r = reg2.admit("p", "person_ship", None, 0)
    view = reg2.view("p")
    assert list(view) == VIEW_KEYS
    assert view["running"] is True
    assert view["run_id"] == r.run_id
    assert view["phase"] == "running"
    assert view["total"] is None
    for name in ZERO_COUNTERS:
        assert view[name] == 0, name
    assert view["started_at"] == clock.now().timestamp()
    assert view["finished_at"] is None
    assert "to_wire" not in view

    # 5. a late counter enters the view, last, once reported
    reg2.report(r, step="wiring", processed=2, created=5, to_wire=3)
    view = reg2.view("p")
    assert view["step"] == "wiring"
    assert view["processed"] == 2
    assert view["created"] == 5
    assert list(view)[-1] == "to_wire"
    assert view["to_wire"] == 3

    # 6. finish overwrites the named counters only, and the run stays in flight
    reg2.finish(r, "done", {"counts": {"created": 38, "failed": 1}}, None)
    view = reg2.view("p")
    assert view["created"] == 38
    assert view["failed"] == 1
    assert view["processed"] == 2
    assert view["phase"] == "done"
    assert view["running"] is True
    assert view["finished_at"] == clock.now().timestamp()

    # 7. end takes the run out of flight
    reg2.end(r)
    assert reg2.view("p")["running"] is False


def test_admit_busy_stale_and_generation():
    from mu_spec.ship.runs import Busy, Run, RunRegistry, Stale

    reg = RunRegistry(FixedClock(START))

    # 1. two threads admit at once on one project: one Run, one Busy naming it
    barrier = threading.Barrier(2)
    runs = []
    busies = []
    others = []

    def press():
        barrier.wait()
        try:
            runs.append(reg.admit("p", "person_ship", None, 0))
        except Busy as exc:
            busies.append(exc)
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
            others.append(exc)

    threads = [threading.Thread(target=press) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert others == []
    assert len(runs) == 1
    assert len(busies) == 1
    r = runs[0]
    assert isinstance(r, Run)
    assert busies[0].run_id == r.run_id

    # 2. a Ship in flight blocks a rollback on the same project
    with pytest.raises(Busy) as busy:
        reg.admit("p", "rollback", None, 0)
    assert busy.value.run_id == r.run_id

    # 3. another project is free
    assert isinstance(reg.admit("q", "rollback", None, 0), Run)

    # 4. finish leaves the run in flight and the generation where it was
    reg.finish(r, "done", {"counts": {}}, None)
    assert reg.generation("p") == 0
    assert reg.in_flight("p") is r
    with pytest.raises(Busy):
        reg.admit("p", "person_ship", None, 0)

    # 5. end moves the generation: the old one is Stale, the new one admits
    reg.end(r)
    assert reg.generation("p") == 1
    assert reg.in_flight("p") is None
    with pytest.raises(Stale):
        reg.admit("p", "person_ship", None, 0)
    r2 = reg.admit("p", "person_ship", None, 1)
    assert isinstance(r2, Run)
    assert r2.run_id != r.run_id

    # 6. a run in flight and a stale generation at once: Busy is checked first
    with pytest.raises(Busy) as busy:
        reg.admit("p", "rollback", None, 0)
    assert busy.value.run_id == r2.run_id
