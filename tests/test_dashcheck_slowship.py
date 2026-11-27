"""The action page check's long Ship run: dashcheck.slowship (S-223).

T-193 pins slow_script: the base script with hold = RUN_HOLD_INDEX and every other field as it was,
base itself unchanged. T-194 pins the Releaser against a fake stand-in: one release, RUN_HOLD_S after
the hold arrives rather than after start(); a stop before the hold releases nothing; a stop during it
releases once, within stop(), and nothing follows. Modules under test are imported inside the test, so
a module not yet written fails only the tests that need it.
"""

import dataclasses
import threading
import time


def empty_repository_script():
    # the empty-repository script the action check resets the stand-in with (S-234): no issues,
    # no links, numbering from 1, a login and push, no fault of any kind
    from shipcheck.standin import Script

    return Script(issues=(), links=(), next_number=1, next_id=1, user={"login": "x"}, push=True,
                  refuse={}, unanswered=frozenset(), hold=None)


def test_slow_script_changes_hold_only():
    from dashcheck.cost import RUN_HOLD_INDEX
    from dashcheck.slowship import slow_script
    from shipcheck.standin import Script

    base = dataclasses.replace(empty_repository_script(), user="octo-x")
    assert base.hold != RUN_HOLD_INDEX, base.hold
    before = base.hold

    s = slow_script(base)
    assert s.hold == RUN_HOLD_INDEX, s.hold
    assert s == dataclasses.replace(base, hold=RUN_HOLD_INDEX), s
    for f in dataclasses.fields(Script):
        if f.name != "hold":
            assert getattr(s, f.name) == getattr(base, f.name), f.name
    assert base.hold == before, base.hold


class FakeStandIn:
    """held() is True from held_from (a time.monotonic() instant, None for never) until release()."""

    def __init__(self) -> None:
        self.held_from: float | None = None
        self.releases: list[float] = []
        self._lock = threading.Lock()

    def held(self) -> bool:
        with self._lock:
            return (not self.releases and self.held_from is not None
                    and time.monotonic() >= self.held_from)

    def release(self) -> None:
        with self._lock:
            self.releases.append(time.monotonic())

    def count(self) -> int:
        with self._lock:
            return len(self.releases)


def wait_until(instant: float) -> None:
    delay = instant - time.monotonic()
    if delay > 0:
        time.sleep(delay)


def test_releaser_times_from_the_hold():
    from dashcheck.cost import RUN_HOLD_S
    from dashcheck.slowship import Releaser

    # 1. held from t0 + 1.0: the release is timed from the hold's arrival, not from start()
    fake = FakeStandIn()
    r = Releaser(fake)
    t0 = time.monotonic()
    fake.held_from = t0 + 1.0
    r.start()
    try:
        wait_until(t0 + RUN_HOLD_S + 2.5)
        assert fake.count() == 1, fake.releases
        assert fake.releases[0] >= t0 + 1.0 + RUN_HOLD_S, (fake.releases[0] - t0, RUN_HOLD_S)
    finally:
        r.stop()

    # 2. the hold never arrives: stop() before it releases nothing, then or later
    fake = FakeStandIn()
    r = Releaser(fake)
    t0 = time.monotonic()
    r.start()
    try:
        wait_until(t0 + 0.3)
    finally:
        r.stop()
    time.sleep(1.0)
    assert fake.count() == 0, fake.releases

    # 3. held from t0, stop() at t0 + 0.5: one release made within stop(), and none follows it
    fake = FakeStandIn()
    r = Releaser(fake)
    t0 = time.monotonic()
    fake.held_from = t0
    r.start()
    try:
        wait_until(t0 + 0.5)
        assert fake.count() == 0, fake.releases
    finally:
        entered = time.monotonic()
        r.stop()
        left = time.monotonic()
    assert fake.count() == 1, fake.releases
    assert entered <= fake.releases[0] <= left, (fake.releases[0] - t0, entered - t0, left - t0)
    wait_until(t0 + RUN_HOLD_S + 1.0)
    assert fake.count() == 1, fake.releases
