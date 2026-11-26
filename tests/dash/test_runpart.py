"""mu_spec.dash.runpart: the run part every answer and projection carries (S-241).

T-133 pins that the run part copies every field from the run registry's progress
view in RUN_PART_FIELDS order, never deriving percent or time left, is b"null"
without a running view, and raises KeyError for a missing field.
"""

import json

import pytest

NONCE = "3fa9c1d2e4b50617"

VIEW = {
    "running": True,
    "run_id": "abcdefabcdef",
    "admitted": 4,
    "step": "creating",
    "percent": 37,
    "total": 10,
    "processed": 3,
    "created": 2,
    "skipped": 1,
    "failed": 0,
    "time_left": 91,
    "started_at": 1700000000.5,
}


def test_run_part_copies_the_view():
    from mu_spec.dash.runpart import (
        RUN_ID_SEP,
        RUN_NONE,
        RUN_PART_FIELDS,
        run_fields,
        run_id,
        run_part,
    )
    from mu_spec.dash.stamps import canonical

    n = NONCE
    view = dict(VIEW)

    # 1. no view, or a view of no running run, is the one fixed null
    assert run_part(None, n) == b"null" == RUN_NONE
    assert run_part(dict(view, running=False), n) == b"null"

    # 2. every field copied in RUN_PART_FIELDS order; percent is the view's 37, not 3/10
    p = json.loads(run_part(view, n))
    assert list(p) == list(RUN_PART_FIELDS)
    assert p["id"] == "3fa9c1d2e4b50617:4"
    assert p["percent"] == 37
    assert p["time_left"] == 91
    assert p["processed"] == 3
    assert p["started_at"] == 1700000000.5
    assert "run_id" not in p
    assert "running" not in p

    # 3. the one serialisation
    assert run_part(view, n) == canonical(run_fields(view, n))

    # 4. the run id names the start through the nonce
    assert run_id("a" * 16, 4) != run_id("b" * 16, 4)
    assert run_id(n, 4) == n + RUN_ID_SEP + "4"

    # 5. a missing field is an error, never a filled-in None
    missing = dict(view)
    del missing["percent"]
    with pytest.raises(KeyError):
        run_part(missing, n)
