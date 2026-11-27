"""mu_spec.dash.settle: the point an admitted action takes and the wait before its answer (S-136).

T-149 pins that take_point gives a Point only for an admitted waiting action on a
stamped project, read from the stamp and, for Ship, the run marker's counter; and
that settle answers at once for no point, at once when the point is published,
False at the deadline received_at + NODE_TIMEOUT_S, and wakes on a publish.
"""

import datetime
import threading
import time

from mu_spec.outside import FixedClock

START = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


def projection(generation):
    from mu_spec.dash.projection import VIEWS, Part, Projection, ViewProjection

    return Projection(
        "p",
        generation,
        ("m", 0, None),
        {v: ViewProjection("t", Part(b"{}", ()), None, frozenset(), {}) for v in VIEWS},
    )


class SpyPublished:
    """Forwards wait_until to a real Published and lists every call it receives."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls: list[tuple] = []

    def wait_until(self, project, generation, counter, deadline, monotonic):
        self.calls.append((project, generation, counter, deadline))
        return self.inner.wait_until(project, generation, counter, deadline, monotonic)


def test_point_and_settle():
    from mu_spec.dash.projection import Published
    from mu_spec.dash.settle import (
        MARKER_ACTIONS,
        NODE_TIMEOUT_S,
        WAITING_ACTIONS,
        Point,
        settle,
        take_point,
    )
    from mu_spec.dash.stamps import Stamps
    from mu_spec.declarations import ACTIONS
    from mu_spec.ship.runs import RunRegistry
    from mu_spec.writer import Outcome

    stamps = Stamps()
    stamps.installed("p", b"a", notify=False)
    for _ in range(4):
        stamps.installed("p", b"a", notify=True)
    assert stamps.generation("p") == 4
    registry = RunRegistry(FixedClock(START))
    marker = registry.marker
    nonce = marker.nonce
    ok = Outcome(200, {"ok": True})
    refused = Outcome(409, {"error": "x"})

    # 1. an admitted waiting action takes the stamp as read after its turn
    assert take_point("set_repo", "p", ok, stamps, marker) == Point("p", 4, None)

    # 2. a refusal, a non-waiting action and an unstamped project never wait
    assert take_point("set_repo", "p", refused, stamps, marker) is None
    assert take_point("rollback_tickets", "p", ok, stamps, marker) is None
    assert take_point("set_repo", "zz", ok, stamps, marker) is None

    # 3. Ship's point carries the marker counter its admission raised
    registry.admit("p", "person_ship", None, registry.generation("p"))
    assert take_point("emit_tickets", "p", ok, stamps, marker) == Point("p", 4, (nonce, 1))

    # 4. the hand list against the declared actions
    names = {spec.name for spec in ACTIONS}
    assert set(WAITING_ACTIONS) == names - {"rollback_tickets"}
    assert len(WAITING_ACTIONS) == 7
    assert set(MARKER_ACTIONS) <= set(WAITING_ACTIONS)

    # 5. no point answers at once
    assert settle(None, Published(), 0.0, time.monotonic) is True

    # 6. a point already published answers without blocking
    held = Published()
    held.publish(projection(4))
    spy = SpyPublished(held)
    assert settle(Point("p", 4, None), spy, 0.0, lambda: 0.0) is True
    assert len(spy.calls) <= 1

    # 7. nothing published and the clock past the deadline: False, deadline from receipt
    answers = iter([0.0])

    def fake():
        return next(answers, 6.0)

    empty = SpyPublished(Published())
    assert settle(Point("p", 4, None), empty, 0.0, fake) is False
    assert NODE_TIMEOUT_S == 5.0
    assert [call[3] for call in empty.calls] == [5.0]

    # 8. a publish from another thread wakes a waiting settle
    live = Published()
    t0 = time.monotonic()

    def later():
        time.sleep(0.1)
        live.publish(projection(4))

    thread = threading.Thread(target=later)
    thread.start()
    try:
        assert settle(Point("p", 4, None), live, time.monotonic(), time.monotonic) is True
        assert time.monotonic() - t0 < 2.0
    finally:
        thread.join()
