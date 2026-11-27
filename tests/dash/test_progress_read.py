"""mu_spec.dash.progress: answer_progress, the Ship progress read (S-330).

T-148 pins that the progress read answers running only for the current run,
ended only once the published projection includes the last recorded end,
finishing in between, refuses a malformed input and an unknown project, and
never waits on the published projection.
"""

import datetime
import json
import time


def test_progress_states():
    from mu_spec.dash.progress import answer_progress
    from mu_spec.dash.projection import (
        VIEWS,
        Part,
        Projection,
        ProjectList,
        Published,
        ViewProjection,
    )
    from mu_spec.dash.runpart import run_fields, run_part
    from mu_spec.dash.stamps import canonical, list_token

    from mu_spec.outside import FixedClock

    n = "0123456789abcdef"
    m = "fedcba9876543210"
    clock = FixedClock(datetime.datetime.fromtimestamp(1000.0, datetime.UTC))
    running = {
        "running": True,
        "admitted": 3,
        "step": "creating",
        "percent": 40,
        "total": 5,
        "processed": 2,
        "created": 2,
        "skipped": 0,
        "failed": 0,
        "time_left": 30,
        "started_at": 990.0,
    }
    done = dict(running, running=False, step="done", percent=100)

    class NeverWaits(Published):
        def wait_until(self, *args, **kwargs):
            raise AssertionError("the progress read waited on the published projection")

    def fresh():
        pub = NeverWaits()
        pub.publish_list(ProjectList(("p",), list_token(["p"]), canonical(["p"])))
        return pub

    def publish(pub, g, c):
        views = {
            v: ViewProjection("t", Part(b"{}", ()), None, frozenset(), {})
            for v in VIEWS
        }
        pub.publish(Projection("p", g, (n, c, None), views))

    pv = {"value": (None, None)}
    asked = []

    def progress_view(project):
        asked.append(project)
        return pv["value"]

    def ask(pub, run):
        t0 = time.perf_counter()
        a = answer_progress(
            pub, progress_view, n, clock, canonical({"project": "p", "run": run})
        )
        # 7. every answer at once, never a wait
        assert time.perf_counter() - t0 < 0.05
        return a, json.loads(a.body)

    ended = {"state": "ended"}

    # 1. a running view, asked with no run or by its own id: running, with the run part
    pub = fresh()
    pv["value"] = (running, None)
    want = {"state": "running", "run": json.loads(run_part(running, n)), "elapsed": 10.0}
    for run in (None, n + ":3"):
        a, j = ask(pub, run)
        assert a.status == 200
        assert j == want
        assert list(j) == ["state", "run", "elapsed"]
    assert asked and set(asked) == {"p"}

    # 2. the same view named by another nonce's id: ended, since no end is recorded
    a, j = ask(pub, m + ":3")
    assert a.status == 200
    assert j == ended

    # 3. no view and no end: ended
    pv["value"] = (None, None)
    a, j = ask(pub, None)
    assert a.status == 200
    assert j == ended

    # 4. the end at generation 5, the projection at 4: finishing with the last state;
    #    once the projection reaches the end: ended
    pub = fresh()
    pv["value"] = (done, (5, n, 4))
    publish(pub, 4, 4)
    a, j = ask(pub, n + ":3")
    assert a.status == 200
    assert j == {"state": "finishing", "run": json.loads(canonical(run_fields(done, n)))}
    assert list(j) == ["state", "run"]
    publish(pub, 5, 4)
    a, j = ask(pub, n + ":3")
    assert a.status == 200
    assert j == ended

    # 5. a new run in progress while the page names the previous one: finishing with
    #    run null until the previous end is published, then ended; the new run's id
    #    answers running
    pub = fresh()
    newer = dict(running, admitted=5)
    pv["value"] = (newer, (5, n, 4))
    publish(pub, 4, 4)
    a, j = ask(pub, n + ":3")
    assert a.status == 200
    assert j == {"state": "finishing", "run": None}
    publish(pub, 5, 4)
    a, j = ask(pub, n + ":3")
    assert a.status == 200
    assert j == ended
    a, j = ask(pub, n + ":5")
    assert a.status == 200
    assert j == {"state": "running", "run": json.loads(run_part(newer, n)), "elapsed": 10.0}
    assert list(j) == ["state", "run", "elapsed"]

    # 6. an unknown project and a malformed input are refused
    a = answer_progress(
        pub, progress_view, n, clock, canonical({"project": "x", "run": None})
    )
    assert a.status == 404
    assert json.loads(a.body) == {"state": "refused", "reason": "no project named x"}
    a = answer_progress(pub, progress_view, n, clock, b"{}")
    assert a.status == 400
    assert json.loads(a.body) == {
        "state": "refused",
        "reason": "the progress read takes an object with project and run",
    }
