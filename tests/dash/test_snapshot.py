"""mu_spec.dash.snapshot: coverage, the marked projects and take_snapshot (S-247).

T-178 pins that COVERAGE has exactly the VIEWS keys, which keys mark which
projects, that digests keep an empty inbox slot, and that take_snapshot reads the
stamps once, then the run views as one agreeing try (or the last of AGREE_TRIES),
then the identity once, with the progress fields only in progress.
"""

import datetime
import types

import pytest

from mu_spec.outside import FixedClock

START = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


class RecordingStamps:
    """Forwards to a real Stamps and appends "read" to log on each read."""

    def __init__(self, inner, log: list[str]) -> None:
        self.inner = inner
        self.log = log

    def read(self, keys):
        self.log.append("read")
        return self.inner.read(keys)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class FakeRegistry:
    """A run registry whose state number c starts at 1 and rises after the calls bump names.

    bump(kind, n) is asked after the n-th call (from 1) of kind "marked" or "progress".
    """

    def __init__(self, log: list[str], bump=lambda kind, n: False) -> None:
        self.log = log
        self.bump = bump
        self.c = 1
        self.counts = {"marked": 0, "progress": 0}

    def _after(self, kind: str) -> None:
        self.counts[kind] += 1
        if self.bump(kind, self.counts[kind]):
            self.c += 1

    def marked_view(self, name):
        self.log.append("marked")
        c = self.c
        answer = (("n", c, "bot"), {"running": True, "run_id": "r", "step": "s" + str(c), "processed": c})
        self._after("marked")
        return answer

    def progress_view(self, name):
        self.log.append("progress")
        c = self.c
        answer = (
            {
                "running": True,
                "run_id": "r",
                "admitted": 1,
                "step": "s" + str(c),
                "processed": c,
                "percent": 10 * c,
                "time_left": 100 - c,
            },
            None,
        )
        self._after("progress")
        return answer


def test_coverage_marks_and_snapshot():
    from mu_spec.dash.projection import VIEWS
    from mu_spec.dash.snapshot import (
        AGREE_TRIES,
        COVERAGE,
        INBOX,
        INBOX_VIEWS,
        MARKER,
        digests,
        marked_by,
        strip_progress,
        take_snapshot,
    )
    from mu_spec.dash.stamps import ALL_KEY, INBOX_KEY, Stamps, project_key
    from mu_spec.ship.access import Identity
    from mu_spec.ship.runs import PROGRESS_FIELDS

    calls: list[str] = []

    def f():
        calls.append("projects")
        return ["p", "q"]

    inner = Stamps()
    inner.installed("p", b"r", notify=False)
    inner.installed(None, b"{}", notify=False)

    def setup(bump=lambda kind, n: False):
        log: list[str] = []
        stamps = RecordingStamps(inner, log)
        registry = FakeRegistry(log, bump)

        def identity():
            log.append("identity")
            return Identity("bot", "Bot")

        return log, stamps, registry, identity

    # 1. coverage keys are the views; the inbox views and the one marker view
    assert tuple(COVERAGE) == VIEWS
    assert INBOX_VIEWS == tuple(v for v in VIEWS if INBOX in COVERAGE[v]) == ("spine", "queues")
    assert [v for v in VIEWS if MARKER in COVERAGE[v]] == ["ship"]

    # 2. the inbox and every-project keys ask projects once each; a project key and others never
    assert marked_by(INBOX_KEY, f) == ["p", "q"]
    assert marked_by(ALL_KEY, f) == ["p", "q"]
    assert calls == ["projects", "projects"]
    assert marked_by(project_key("p"), f) == ["p"]
    assert marked_by("x", f) == []
    assert len(calls) == 2

    # 3. NO_PROGRESS: stamps, one agreeing try of two marked views, then the identity
    log, stamps, registry, identity = setup()
    s = take_snapshot("p", stamps, registry.marked_view, (b"e1",), identity)
    assert log == ["read", "marked", "marked", "identity"]
    assert s.progress is None
    assert s.marker == ("n", 1, "bot")
    assert s.identity == Identity("bot", "Bot")
    assert s.events == (b"e1",)
    assert s.record.data == b"r"

    # 4. with a progress view: the progress fields stay out of run
    log, stamps, registry, identity = setup()
    s = take_snapshot("p", stamps, registry.marked_view, (b"e1",), identity, registry.progress_view)
    assert log == ["read", "marked", "progress", "marked", "identity"]
    assert s.run == strip_progress(s.progress)
    assert not any(name in s.run for name in PROGRESS_FIELDS)
    assert s.progress["percent"] == 10

    # 5. the state rises after the first progress read: the second try agrees
    log, stamps, registry, identity = setup(lambda kind, n: kind == "progress" and n == 1)
    s = take_snapshot("p", stamps, registry.marked_view, (), identity, registry.progress_view)
    assert log == ["read", "marked", "progress", "marked", "marked", "progress", "marked", "identity"]
    assert s.marker[1] == 2
    assert s.progress["step"] == "s2"
    assert s.run["step"] == "s2"

    # 6. the state rises after every read: no try agrees and the last is taken as it is
    log, stamps, registry, identity = setup(lambda kind, n: True)
    s = take_snapshot("p", stamps, registry.marked_view, (), identity, registry.progress_view)
    assert AGREE_TRIES == 3
    assert log == ["read"] + ["marked", "progress", "marked"] * 3 + ["identity"]
    assert s.marker[1] == 9
    assert s.run["processed"] == 9
    assert s.progress["processed"] == 8

    # 7. digests in coverage order, with an empty slot for a missing inbox
    assert digests("ship", s) == (s.record.digest,)
    assert digests("spine", s) == (s.record.digest, s.inbox.digest)
    bare = Stamps()
    bare.installed("p", b"r", notify=False)
    s0 = take_snapshot("p", bare, FakeRegistry([]).marked_view, ())
    assert s0.inbox is None
    assert digests("queues", s0) == (s0.record.digest, "")

    # 8. an unstamped project raises KeyError
    log, stamps, registry, identity = setup()
    with pytest.raises(KeyError):
        take_snapshot("zz", stamps, registry.marked_view, (), identity)

    # 9. a real run registry, through the members the Ship part exposes for it
    from mu_spec.ship.runs import RunRegistry

    runs = RunRegistry(FixedClock(START))
    ship = types.SimpleNamespace(
        marked_view=runs.marked_view, progress_view=runs.progress_view, identity=lambda: None
    )
    run = runs.admit("p", "person_ship", None, runs.generation("p"))
    a = take_snapshot("p", inner, ship.marked_view, (), ship.identity, ship.progress_view)
    assert a.progress["admitted"] == a.marker[1]
    runs.report(run, step="creating", processed=3)
    b = take_snapshot("p", inner, ship.marked_view, (), ship.identity, ship.progress_view)
    assert b.marker[0] == a.marker[0]
    assert b.marker[1] == b.progress["admitted"] + 1
    assert b.progress["admitted"] == a.progress["admitted"]
    assert b.progress["step"] == "creating"
    assert b.progress["processed"] == 3
    assert "percent" in b.progress
    assert "time_left" in b.progress
    assert b.progress["percent"] == ship.progress_view("p")[0]["percent"]
    assert b.run == strip_progress(b.progress)
    assert not any(name in b.run for name in ("admitted", "percent", "time_left"))
