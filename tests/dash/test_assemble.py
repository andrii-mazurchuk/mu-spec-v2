"""mu_spec.dash.assemble: build_dash_parts, the one construction site of the dashboard's server parts (S-356).

T-144 pins that, over a store holding two projects and a runtime built with build_ship and a real
Stamps, the published list names exactly the registry's projects, the parts hand over the Ship part's
one marker, its nonce and the very Stamps, and the Stamps' one observer is taken; and that, with a fake
Ship part and a fake Dispatcher, the generation source is set to stamps.generation before the
dispatcher is constructed and started, the dispatcher is given the store's read_inbox and the Ship
part's identity as bound methods and not their results, and start is given the store's whole log (two
records of different kinds and projects, appended before the runtime is built), in full and in order.
Modules under test are imported inside the test, so a module not yet written fails only the tests that
need it.
"""

import inspect

import pytest

DOC = {"entries": {}, "slices": {}, "cuts": []}
NAMES = ("q", "p")


class FakeGitHub:
    """A GitHubClient stand-in: every method call on it is recorded as a request, none is sent."""

    def __init__(self) -> None:
        self.requests = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def request(*args, **kwargs):
            self.requests.append((name, args, kwargs))

        return request


def set_environment(monkeypatch, **texts: str) -> None:
    """Every configuration variable absent but those given, by field name."""
    from mu_spec.outside import ENV_NAMES

    for name in ENV_NAMES.values():
        monkeypatch.delenv(name, raising=False)
    for field, text in texts.items():
        monkeypatch.setenv(ENV_NAMES[field], text)


def store_copy(tmp_path, monkeypatch, directory: str):
    """A temporary store holding every one of NAMES, the environment naming it; answers the Store."""
    from mu_spec.codec import ProjectRecord
    from mu_spec.store import Store

    from mu_spec.outside import ENV_NAMES, load_config

    D = tmp_path / directory
    cwd = tmp_path / (directory + "-cwd")
    cwd.mkdir()
    config = load_config({ENV_NAMES["port"]: "9100", ENV_NAMES["state_dir"]: str(D)}, cwd)[0]
    store = Store(config)
    for name in NAMES:
        store.create_project(ProjectRecord.from_doc(name, dict(DOC, name=name)))
    monkeypatch.chdir(cwd)
    set_environment(monkeypatch, port="9100", state_dir=str(D))
    return store


def append_two_records(store) -> None:
    """Two log records a filter by project or by kind would tell apart: a mutation of a project, then
    a record of another kind with no project."""
    import datetime

    from mu_spec.codec import EVENT_KINDS, MUTATION_KIND, new_log_record

    at = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    other = min(EVENT_KINDS - {MUTATION_KIND})
    store.append_log(new_log_record(MUTATION_KIND, NAMES[0], [], {"note": "first"}, at))
    store.append_log(new_log_record(other, None, [], {"note": "second"}, at))


class FakeMarker:
    """A RunMarker stand-in carrying a nonce."""

    nonce = "fake-nonce"

    def observe(self, callback) -> None:
        pass


class FakeShip:
    """A Ship part stand-in listing set_generation_source in the shared order list."""

    def __init__(self, order: list) -> None:
        self.order = order
        self.marker = FakeMarker()
        self.sources: list = []
        self.learned: list = []

    def set_generation_source(self, fn) -> None:
        self.order.append("set_generation_source")
        self.sources.append(fn)

    def marked_view(self, project):
        return (self.marker.nonce, 0, None), None

    def progress_view(self, project):
        return None, None

    def identity(self):
        return None

    def learn_identity(self, runtime) -> None:
        self.learned.append(runtime)


def dispatcher_arguments(stamps, marked_view, marker, names, config, clock, read_inbox,
                         worker=None, identity=None, progress_view=None, learn=None):
    """The Dispatcher's parameters as S-355 names them; binds a call's arguments to their names."""


def fake_dispatcher_class(order: list, made: list):
    """A Dispatcher stand-in listing its construction and start(log) in order; each one made in made."""

    class FakeDispatcher:
        def __init__(self, *args, **kwargs) -> None:
            order.append("Dispatcher")
            bound = inspect.signature(dispatcher_arguments).bind(*args, **kwargs)
            self.given = dict(bound.arguments)
            self.published = object()
            self.logs: list = []
            made.append(self)

        def start(self, log) -> None:
            order.append("start")
            self.logs.append(log)

    return FakeDispatcher


def test_build_dash_parts_order(tmp_path, monkeypatch):
    from mu_spec.dash import assemble
    from mu_spec.dash.assemble import build_dash_parts
    from mu_spec.dash.stamps import Stamps
    from mu_spec.order import names_sorted
    from mu_spec.registry import build_runtime
    from mu_spec.ship.part import build_ship

    # Arrange A: a store copy holding two projects, the runtime built with build_ship and a real Stamps
    store_copy(tmp_path, monkeypatch, "a")
    stamps = Stamps()
    runtime = build_runtime(ship_builder=build_ship, observer=stamps, github=FakeGitHub())
    assert len(tuple(runtime.registry.names())) >= 2

    # 1. the published list names exactly the registry's projects, sorted
    parts = build_dash_parts(runtime, stamps)
    assert parts.published.project_list().names == tuple(names_sorted(runtime.registry.names()))
    assert set(parts.published.project_list().names) == set(NAMES)

    # 2. the one marker, its nonce, the very Stamps, the dispatcher's own Published
    assert parts.marker is runtime.ship.marker
    assert parts.nonce == runtime.ship.marker.nonce
    assert parts.stamps is stamps
    assert parts.published is parts.dispatcher.published

    # 3. the Stamps' one observer is taken
    with pytest.raises(RuntimeError):
        stamps.observe(object())

    # Arrange B: a fake Ship part and a fake Dispatcher, both listing their calls in one shared list
    order: list = []
    made: list = []
    append_two_records(store_copy(tmp_path, monkeypatch, "b"))
    fake_ship = FakeShip(order)
    stamps = Stamps()
    runtime = build_runtime(ship_builder=lambda client: fake_ship, observer=stamps, github=FakeGitHub())
    monkeypatch.setattr(assemble, "Dispatcher", fake_dispatcher_class(order, made))
    parts = build_dash_parts(runtime, stamps)

    # 4. the generation source first, once, and it is this Stamps' bound generation
    assert order == ["set_generation_source", "Dispatcher", "start"]
    assert len(fake_ship.sources) == 1
    source = fake_ship.sources[0]
    assert source == stamps.generation
    assert inspect.ismethod(source) and source.__self__ is stamps

    # 5. read_inbox and identity as bound methods, not their results; start given the whole log
    assert len(made) == 1
    given = made[0].given
    assert given["read_inbox"] == runtime.store.read_inbox
    assert inspect.ismethod(given["read_inbox"]) and given["read_inbox"].__self__ is runtime.store
    assert given["identity"] == fake_ship.identity
    assert inspect.ismethod(given["identity"]) and given["identity"].__self__ is fake_ship
    assert len(made[0].logs) == 1
    whole = list(runtime.store.read_log())
    assert len(whole) == 2
    assert {record.project for record in whole} == {NAMES[0], None}
    assert len({record.kind for record in whole}) == 2
    given_log = list(made[0].logs[0])
    assert [record.doc for record in given_log] == [record.doc for record in whole]

    # the rest of what the dispatcher is handed, as S-356 names it, and no worker factory
    assert given["stamps"] is stamps
    assert given["marked_view"] == fake_ship.marked_view
    assert given["marker"] is fake_ship.marker
    assert sorted(given["names"]) == sorted(runtime.registry.names())
    assert given["config"] is runtime.config
    assert given["clock"] is runtime.clock
    assert given["progress_view"] == fake_ship.progress_view
    assert "worker" not in given

    # learn is learn_identity bound to this runtime: never called here, once per call after
    assert fake_ship.learned == []
    given["learn"]()
    assert len(fake_ship.learned) == 1 and fake_ship.learned[0] is runtime

    # the parts answered over the fake Ship part
    assert parts.stamps is stamps
    assert parts.dispatcher is made[0]
    assert parts.published is made[0].published
    assert parts.marker is fake_ship.marker
    assert parts.nonce == fake_ship.marker.nonce
    assert parts.progress_view == fake_ship.progress_view
