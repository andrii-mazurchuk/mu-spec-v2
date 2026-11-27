"""Run registry: RunRegistry, Busy and Stale in mu_spec.ship.runs.

Run ids are 12 random lowercase hex characters compared by shape only; one project has at most one
run of any kind in flight, and its generation moves when a run ends. The run marker moves once per
admit, report and end; the end point is recorded at end; progress_view adds the admission counter
and the stage in progress to the view. The module under test is imported inside the test, so a
module not yet written fails only the tests that need it.
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
PROGRESS_KEYS = ("admitted", "stage", "stage_done", "stage_total", "percent")
NONCE = re.compile(r"^[0-9a-f]{16}$")


def test_ids_and_view():
    from mu_spec.ship.runs import (
        COUNTER_FIELDS,
        PROGRESS_FIELDS,
        VIEW_FIELDS,
        RunRegistry,
    )

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
    assert list(view) == ["running", *VIEW_FIELDS]
    assert view["running"] is True
    assert view["run_id"] == r.run_id
    assert view["phase"] == "running"
    assert view["total"] is None
    for name in COUNTER_FIELDS:
        if name != "total":
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

    # 6. the emission read's view carries no progress field, nor a time left
    assert PROGRESS_FIELDS == PROGRESS_KEYS
    marked = reg2.marked_view("p")[1]
    for name in (*PROGRESS_KEYS, "time_left"):
        assert name not in reg2.view("p"), name
        assert name not in marked, name
    assert marked == reg2.view("p")

    # 7. finish overwrites the named counters only, and the run stays in flight
    reg2.finish(r, "done", {"counts": {"created": 38, "failed": 1}}, None)
    view = reg2.view("p")
    assert view["created"] == 38
    assert view["failed"] == 1
    assert view["processed"] == 2
    assert view["phase"] == "done"
    assert view["running"] is True
    assert view["finished_at"] == clock.now().timestamp()

    # 8. end takes the run out of flight
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


def test_marker_counter_and_observer():
    from mu_spec.ship.runs import Busy, RunRegistry, Stale

    reg = RunRegistry(FixedClock(START))
    seen = []
    reg.marker.observe(seen.append)

    # 1. a random 16-hex nonce per registry; a fresh registry reads counter 0 and no run
    nonce = reg.marker.nonce
    assert NONCE.match(nonce)
    assert RunRegistry(FixedClock(START)).marker.nonce != nonce
    assert reg.marked_view("p") == ((nonce, 0, None), None)
    assert reg.progress_view("p") == (None, None)

    # 2. one raise at admit, at each report (a no-change one included) and at end; none at finish
    r = reg.admit("p", "person_ship", None, 0)
    assert reg.marker.value("p")[1] == 1
    assert r.admitted == 1
    reg.report(r, step="creating", processed=1)
    assert reg.marker.value("p")[1] == 2
    reg.report(r)
    assert reg.marker.value("p")[1] == 3
    reg.finish(r, "done", {"counts": {}}, None)
    assert reg.marker.value("p")[1] == 3
    assert reg.marker.value("q")[1] == 0
    reg.end(r)
    assert reg.marker.value("p")[1] == 4
    assert reg.marker.value("q")[1] == 0

    # 3. a Stale or Busy admit leaves the counter where it was
    with pytest.raises(Stale):
        reg.admit("p", "person_ship", None, 0)
    assert reg.marker.value("p")[1] == 4
    r2 = reg.admit("p", "person_ship", None, 1)
    assert reg.marker.value("p")[1] == 5
    assert r2.admitted == 5
    with pytest.raises(Busy):
        reg.admit("p", "rollback", None, 1)
    assert reg.marker.value("p")[1] == 5
    assert reg.marker.value("q")[1] == 0

    # 4. the observer was told the project at each raise, and only then
    assert seen == ["p", "p", "p", "p", "p"]

    # 5. learn notifies every project (None) only when the login changes
    reg.marker.learn("octo")
    assert seen == ["p", "p", "p", "p", "p", None]
    assert reg.marker.value("p")[2] == "octo"
    reg.marker.learn("octo")
    assert seen == ["p", "p", "p", "p", "p", None]
    reg.marker.learn("other")
    assert seen == ["p", "p", "p", "p", "p", None, None]

    # 6. there is one observer only
    with pytest.raises(RuntimeError):
        reg.marker.observe(lambda name: None)

    # 7. an observer that only records does not deadlock concurrent admits and reports
    projects = [f"t{i}" for i in range(8)]
    failures = []

    def work(project):
        try:
            run = reg.admit(project, "person_ship", None, 0)
            for k in range(50):
                reg.report(run, processed=k)
            reg.end(run)
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
            failures.append(exc)

    threads = [threading.Thread(target=work, args=(project,)) for project in projects]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert failures == []
    for project in projects:
        assert reg.marker.value(project)[1] == 52, project


def test_end_point():
    from mu_spec.ship.runs import RunRegistry

    # 1. the end point takes the generation noted at the outcome commit, not the one read at end
    reg = RunRegistry(FixedClock(START))
    answers = iter([7, 9])
    reg.set_generation_source(lambda project: next(answers))
    r = reg.admit("p", "person_ship", None, 0)
    reg.report(r, step="creating")
    reg.report(r)
    reg.finish(r, "done", {"counts": {}}, None)
    reg.note_commit(r)
    reg.end(r)
    assert r.commit_generation == 7
    end_point = (7, reg.marker.nonce, 4)
    assert reg.progress_view("p")[1] == end_point

    # 2. a second end changes nothing and reads the source no more
    reg.end(r)
    assert reg.progress_view("p")[1] == end_point
    assert reg.marker.value("p")[1] == 4
    assert reg.generation("p") == 1

    # 3. with no outcome commit noted, the source is read at end
    reg2 = RunRegistry(FixedClock(START))
    reg2.set_generation_source(lambda project: 9)
    r2 = reg2.admit("p", "person_ship", None, 0)
    reg2.report(r2, step="creating")
    reg2.report(r2)
    reg2.finish(r2, "done", {"counts": {}}, None)
    reg2.end(r2)
    assert reg2.progress_view("p")[1] == (9, reg2.marker.nonce, 4)

    # 4. with no source at all, the generation is 0
    reg3 = RunRegistry(FixedClock(START))
    r3 = reg3.admit("p", "person_ship", None, 0)
    reg3.note_commit(r3)
    assert r3.commit_generation is None
    reg3.end(r3)
    assert reg3.progress_view("p")[1] == (0, reg3.marker.nonce, 2)

    # 5. the source is set once
    with pytest.raises(RuntimeError):
        reg.set_generation_source(lambda project: 1)

    # 6. a restart holds no end point
    assert RunRegistry(FixedClock(START)).progress_view("p") == (None, None)

    # 7. a run admitted and not ended leaves the previous end point in place
    reg.admit("p", "person_ship", None, 1)
    assert reg.progress_view("p")[1] == end_point


def test_stage_progress_and_progress_view():
    from mu_spec.ship.runs import PROGRESS_FIELDS, RunRegistry, stage_progress

    reg = RunRegistry(FixedClock(START))

    def fresh(kind="person_ship"):
        return reg.admit("p", kind, None, reg.generation("p"))

    # 1. nothing reported: issues, total not yet known
    r = fresh()
    assert stage_progress(r) == ("issues", 0, None, 0)
    reg.end(r)

    # 2. a total of 0 gives percent 0
    r = fresh()
    reg.report(r, total=0)
    assert stage_progress(r) == ("issues", 0, 0, 0)
    reg.end(r)

    # 3. the percent is rounded down
    r = fresh()
    reg.report(r, step="creating", total=3, processed=1)
    assert stage_progress(r) == ("issues", 1, 3, 33)
    reg.report(r, processed=2)
    assert stage_progress(r) == ("issues", 2, 3, 66)
    reg.end(r)

    # 4. 199 of 200 is 99, 5 of 5 is 100
    r = fresh()
    reg.report(r, total=200, processed=199)
    assert stage_progress(r) == ("issues", 199, 200, 99)
    reg.report(r, total=5, processed=5)
    assert stage_progress(r) == ("issues", 5, 5, 100)

    # 5. same run: wiring with orders to declare is 'order', capped, and never goes back
    reg.report(r, step="wiring", to_wire=4, wired=1)
    assert stage_progress(r) == ("order", 1, 4, 25)
    reg.report(r, wired=6)
    assert stage_progress(r) == ("order", 4, 4, 100)
    reg.report(r, step="sweeping")
    assert stage_progress(r)[0] == "order"
    reg.report(r, step="creating")
    assert stage_progress(r)[0] == "order"
    reg.end(r)

    # 6. wiring with nothing to wire stays 'issues'
    r = fresh()
    reg.report(r, step="wiring", to_wire=0, total=2, processed=1)
    assert stage_progress(r) == ("issues", 1, 2, 50)
    reg.end(r)

    # 7. a failed run keeps its progress, before and after its end
    r = fresh()
    reg.report(r, total=8, processed=3)
    reg.finish(r, "failed", None, "RuntimeError: x")
    assert stage_progress(r) == ("issues", 3, 8, 37)
    reg.end(r)
    assert stage_progress(r) == ("issues", 3, 8, 37)

    # 8. a rollback shows no progress
    r = fresh("rollback")
    reg.report(r, step="closing", total=5, processed=2)
    assert stage_progress(r) == (None, None, None, None)
    reg.end(r)
    previous_end = (0, reg.marker.nonce, reg.marker.value("p")[1])
    assert reg.progress_view("p")[1] == previous_end

    # 9. progress_view inserts admitted and the stage fields in place, and no time left
    r = fresh()
    reg.report(r, step="wiring", to_wire=4, wired=1)
    pv, ep = reg.progress_view("p")
    assert list(pv) == ["running", "run_id", "admitted", "phase", "step", "total", "processed",
                        "created", "skipped", "failed", "wired", "unwired", "edited", "withdrawn",
                        "touched", "stage", "stage_done", "stage_total", "percent", "started_at",
                        "finished_at", "error", "result", "to_wire"]
    assert pv["admitted"] == r.admitted
    assert (pv["stage"], pv["stage_done"], pv["stage_total"], pv["percent"]) == ("order", 1, 4, 25)
    assert "time_left" not in pv
    stripped = {name: value for name, value in pv.items() if name not in PROGRESS_FIELDS}
    assert list(stripped.items()) == list(reg.view("p").items())
    assert ep == previous_end
