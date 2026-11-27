"""mu_spec.dash.runpart: the run part every answer and projection carries (S-417).

T-441 pins the one shape: the 13 RUN_PART_FIELDS copied in order from a progress view, the id
from the marker's nonce and the admission counter, no time left and nothing derived from the
counts, b"null" for no run, KeyError for an emission-form view. T-442 pins that on a real run
registry the run part agrees with what the registry's progress view computes.
"""

import datetime
import json

import pytest

from mu_spec.outside import FixedClock
from mu_spec.ship.runs import RunRegistry

START = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

NONCE = "3fa9c1d2e4b50617"

# a progress-view-shaped dict, in the progress view's key order, plus a time_left the run part
# must not copy
VIEW = {
    "running": True,
    "run_id": "abcdefabcdef",
    "admitted": 4,
    "phase": "running",
    "step": "wiring",
    "total": 10,
    "processed": 10,
    "created": 9,
    "skipped": 1,
    "failed": 0,
    "started_at": 990.5,
    "touched": [],
    "stage": "order",
    "stage_done": 1,
    "stage_total": 4,
    "percent": 25,
    "time_left": 7,
}

EMISSION_ONLY_ABSENT = ("admitted", "stage", "stage_done", "stage_total", "percent")


def test_run_part_shape():
    from mu_spec.dash.runpart import (
        RUN_ID_SEP,
        RUN_NONE,
        RUN_PART_FIELDS,
        VIEW_SOURCE,
        in_progress,
        run_fields,
        run_id,
        run_part,
    )

    n = NONCE
    v = dict(VIEW)
    ev = {k: val for k, val in v.items() if k not in EMISSION_ONLY_ABSENT}

    # 1. no run is the one fixed null; running must be True, not merely truthy
    assert RUN_NONE == b"null"
    assert run_part(None, n) == b"null"
    assert run_part(dict(v, running=False), n) == b"null"
    assert run_part(dict(v, running=1), n) == b"null"

    # 2.
    assert in_progress(None) is False
    assert in_progress(v) is True

    # 3. byte equality: RUN_PART_FIELDS order, compact separators, percent the view's 25 not 100
    assert run_part(v, n) == (
        b'{"id":"3fa9c1d2e4b50617:4","phase":"running","step":"wiring","stage":"order",'
        b'"stage_done":1,"stage_total":4,"percent":25,"total":10,"processed":10,"created":9,'
        b'"skipped":1,"failed":0,"started_at":990.5}'
    )

    # 4. exactly the 13 fields, each copied from its progress view field
    p = json.loads(run_part(v, n))
    assert list(p) == list(RUN_PART_FIELDS)
    assert len(RUN_PART_FIELDS) == 13
    assert "time_left" not in p
    assert "run_id" not in p
    assert "admitted" not in p
    for f in VIEW_SOURCE:
        assert p[f] == v[VIEW_SOURCE[f]]
    assert set(VIEW_SOURCE) == set(RUN_PART_FIELDS) - {"id"}

    # 5. run_fields is what run_part serialises
    assert run_fields(v, n) == json.loads(run_part(v, n))
    assert list(run_fields(v, n)) == list(RUN_PART_FIELDS)

    # 6. an emission-form view is an error, never a guessed id or a filled-in value
    with pytest.raises(KeyError):
        run_part(ev, n)
    with pytest.raises(KeyError):
        run_fields(ev, n)

    # 7. the run id: nonce, separator, admission counter
    assert run_id(n, 4) == "3fa9c1d2e4b50617:4"
    assert run_id(n, 4) != run_id("0000000000000000", 4)
    assert RUN_ID_SEP == ":"


def test_run_part_from_registry():
    from mu_spec.dash.runpart import RUN_PART_FIELDS, run_part

    reg = RunRegistry(FixedClock(START))
    nonce = reg.marker.nonce
    run = reg.admit("p", "person_ship", None, 0)
    a = reg.progress_view("p")[0]["admitted"]

    # 1. stage 'issues' with the registry's own counts and percent
    reg.report(run, step="creating", total=10, processed=3)
    pv = reg.progress_view("p")[0]
    q = json.loads(run_part(pv, nonce))
    first_id = q["id"]
    assert q["id"] == nonce + ":" + str(a)
    assert q["phase"] == "running"
    assert q["step"] == "creating"
    assert q["stage"] == "issues"
    assert q["stage_done"] == 3
    assert q["stage_total"] == 10
    assert q["percent"] == 30
    assert list(q) == list(RUN_PART_FIELDS)
    assert "time_left" not in q

    # 2. stage 'order'; reports raise the marker but the id keeps the admission counter
    reg.report(run, step="wiring", to_wire=4, wired=1)
    q = json.loads(run_part(reg.progress_view("p")[0], nonce))
    assert q["stage"] == "order"
    assert q["stage_done"] == 1
    assert q["stage_total"] == 4
    assert q["percent"] == 25
    assert q["id"] == first_id

    # 3. the emission view carries no admitted
    with pytest.raises(KeyError):
        run_part(reg.marked_view("p")[1], nonce)

    # 4. another start: same counter, different nonce, different id
    other = RunRegistry(FixedClock(START))
    other.admit("p", "person_ship", None, 0)
    other_id = json.loads(run_part(other.progress_view("p")[0], other.marker.nonce))["id"]
    assert other_id.rsplit(":", 1)[1] == str(a)
    assert other_id != first_id

    # 5. after the run's end there is no run part
    reg.finish(run, "done", None, None)
    reg.end(run)
    assert run_part(reg.progress_view("p")[0], nonce) == b"null"
