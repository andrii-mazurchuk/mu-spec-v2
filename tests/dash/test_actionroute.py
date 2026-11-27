"""mu_spec.dash.actionroute: the one path a person-only action request takes (S-137).

T-150 pins that route_action answers the handler's own Outcome object once the
published projection holds the action's generation, closes without an answer at
the deadline, answers a refusal without waiting, passes an unmatched path on
without calling a handler, and takes its point only after the handler returned.
"""

import datetime

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


def path_of(name):
    from mu_spec.declarations import ACTIONS

    (spec,) = [s for s in ACTIONS if s.name == name]
    return spec.path.replace("{project}", "p")


class SpyPublished:
    """Forwards wait_until to a real Published and lists every call it receives."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls: list[tuple] = []

    def wait_until(self, project, generation, counter, deadline, monotonic):
        self.calls.append((project, generation, counter, deadline))
        return self.inner.wait_until(project, generation, counter, deadline, monotonic)


class World:
    """Stamps with "p" at generation 2 and the eight actions bound to fake handlers."""

    def __init__(self) -> None:
        from mu_spec.actions import bind_actions
        from mu_spec.dash.stamps import Stamps
        from mu_spec.declarations import ACTIONS
        from mu_spec.ship.runs import RunRegistry
        from mu_spec.writer import Outcome

        self.runtime = object()
        self.stamps = Stamps()
        self.stamps.installed("p", b"a", notify=False)
        self.stamps.installed("p", b"a", notify=True)
        self.stamps.installed("p", b"a", notify=True)
        assert self.stamps.generation("p") == 2
        self.marker = RunRegistry(FixedClock(START)).marker
        self.o200 = Outcome(200, {"labels": ["a"]})
        self.o409 = Outcome(409, {"error": "slicing already ratified"})
        self.calls: list[str] = []
        self.record: list[str] = []

        def set_labels(runtime, project, body):
            self.calls.append("set_labels")
            assert runtime is self.runtime
            assert project == "p"
            # the action's one write_record of "p" raises its generation to 3
            self.stamps.installed("p", b'{"labels":["a"]}', notify=True)
            self.record.append("handler_end")
            return self.o200

        def ratify(runtime, project, body):
            self.calls.append("ratify")
            return self.o409

        def other(name):
            def handler(runtime, project, body):
                self.calls.append(name)
                return Outcome(200, {})

            return handler

        handlers = {spec.name: other(spec.name) for spec in ACTIONS}
        handlers["set_labels"] = set_labels
        handlers["ratify"] = ratify
        self.bound = bind_actions(handlers)

    def route(self, published, method, path, body, monotonic):
        from mu_spec.dash.actionroute import route_action

        return route_action(
            self.runtime,
            self.bound,
            self.stamps,
            published,
            self.marker,
            method,
            path,
            body,
            received_at=0.0,
            monotonic=monotonic,
        )


def test_route_action_paths(monkeypatch):
    from mu_spec.dash import actionroute
    from mu_spec.dash.actionroute import CLOSE, NO_MATCH
    from mu_spec.dash.projection import Published

    # 1. published at the action's generation: the handler's own Outcome, the same object
    w = World()
    held = Published()
    held.publish(projection(3))
    answer = w.route(held, "POST", path_of("set_labels"), {"labels": ["a"]}, lambda: 0.0)
    assert answer is w.o200
    assert w.calls == ["set_labels"]
    assert w.stamps.generation("p") == 3

    # 2. nothing published at generation 3 and the clock past the deadline: CLOSE
    w = World()
    empty = SpyPublished(Published())
    answer = w.route(empty, "POST", path_of("set_labels"), {"labels": ["a"]}, lambda: 6.0)
    assert answer is CLOSE
    assert w.calls == ["set_labels"]

    # 3. a refusal answers at once and never waits
    w = World()
    spy = SpyPublished(Published())
    answer = w.route(spy, "POST", path_of("ratify"), {}, lambda: 0.0)
    assert answer is w.o409
    assert spy.calls == []

    # 4. a path no action matches passes on without calling a handler
    w = World()
    spy = SpyPublished(Published())
    answer = w.route(spy, "POST", "/project/p/nonexistent-action", {}, lambda: 0.0)
    assert answer is NO_MATCH
    assert w.calls == []
    assert spy.calls == []

    # 5. the point is taken only after the handler has returned, from the match and its Outcome
    w = World()
    real_take_point = actionroute.take_point
    taken: list[tuple] = []

    def take_point(name, project, outcome, stamps, marker):
        w.record.append("point")
        taken.append((name, project, outcome, stamps, marker))
        return real_take_point(name, project, outcome, stamps, marker)

    monkeypatch.setattr(actionroute, "take_point", take_point)
    held = Published()
    held.publish(projection(3))
    answer = w.route(held, "POST", path_of("set_labels"), {"labels": ["a"]}, lambda: 0.0)
    assert answer is w.o200
    assert w.record == ["handler_end", "point"]
    assert len(taken) == 1
    name, project, outcome, stamps, marker = taken[0]
    assert (name, project) == ("set_labels", "p")
    assert outcome is w.o200
    assert stamps is w.stamps
    assert marker is w.marker
