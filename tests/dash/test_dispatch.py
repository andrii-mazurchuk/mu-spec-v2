"""mu_spec.dash.dispatch: Dispatcher, the one writer of Published (S-355).

T-299 pins that a project created empty is published, then listed, before its creation answers and
is never marked, that an inbox write racing the creation leaves it marked, and that a fork is listed
and marked. T-300 pins that the worker is started once with the given config and clock, the list is
published before any build, marks build in first-marked order, coalesce, and survive their own
build, the inbox and a None marker mark every project, a lost worker is restarted and the identity
and progress view are handed on. T-301 pins that learn is called once per snapshot with no identity,
after it and outside the dispatcher's lock, never with a cached identity, that a raising learn is
kept in failures, and that the learned login rebuilds every project. Modules under test are imported
inside the test, so a module not yet written fails only the tests that need it.
"""

import datetime
import itertools
import queue
import threading
import time
from collections.abc import Mapping

import pytest

from mu_spec.outside import FixedClock

START = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
DOC = {"name": "p", "entries": {}, "slices": {}, "cuts": []}
ACTOR = "a"
CALL_INPUTS = ("actor", "in_response_to")
CONFIG = object()  # what a fake worker factory is handed; the dispatcher only passes it on
GATE_S = 10.0  # the longest a gated fake build waits for the test
WITHIN_S = 5.0
QUIET_S = 0.5  # no build entered for this long: the dispatcher is idle
IDLE_S = 0.3
POLL_S = 0.02


class FakeWorker:
    """A Worker stand-in: build records the snapshot, optionally waits on a per-call gate, then
    raises the error set for its project or answers a Projection at snap.record.generation."""

    def __init__(self, gated: bool = False, log: list | None = None) -> None:
        self.gated = gated
        self.log = log
        self.built: list[str] = []
        self.snaps: list = []
        self.entered: queue.Queue = queue.Queue()
        self.holds: dict[str, threading.Event] = {}
        self.fail: dict[str, Exception] = {}
        self.restarts = 0
        self.busy = False
        self.last = time.monotonic()

    def build(self, snap):
        self.busy = True
        try:
            if self.log is not None:
                self.log.append("build " + snap.project)
            self.built.append(snap.project)
            self.snaps.append(snap)
            if self.gated:
                gate = threading.Event()
                self.entered.put((snap.project, gate))
                gate.wait(timeout=GATE_S)
            hold = self.holds.get(snap.project)
            if hold is not None:
                hold.wait(timeout=GATE_S)
            error = self.fail.pop(snap.project, None)
            if error is not None:
                raise error
            return projection_of(snap)
        finally:
            self.last = time.monotonic()
            self.busy = False

    def restart(self) -> None:
        self.restarts += 1


class Factory:
    """A WorkerFactory recording each (config, clock) it is called with; answers its one worker."""

    def __init__(self, worker: FakeWorker) -> None:
        self.worker = worker
        self.calls: list[tuple] = []

    def __call__(self, config, clock):
        self.calls.append((config, clock))
        return self.worker


class FakeMarker:
    """A RunMarker stand-in whose observe keeps the callback."""

    def __init__(self) -> None:
        self.callback = None

    def observe(self, callback) -> None:
        self.callback = callback


def projection_of(snap):
    from mu_spec.dash.projection import VIEWS, Part, Projection, ViewProjection

    views = {v: ViewProjection("t", Part(b"{}", ()), None, frozenset(), {}) for v in VIEWS}
    return Projection(snap.project, snap.record.generation, snap.marker, views)


def spy_published(monkeypatch, spy: list, log: list | None = None) -> None:
    """mu_spec.dash.dispatch.Published replaced by a subclass listing each publish before making it."""
    from mu_spec.dash import dispatch
    from mu_spec.dash.projection import Published

    class SpyPublished(Published):
        def publish(self, p):
            spy.append(("publish", p.project))
            if log is not None:
                log.append("publish " + p.project)
            return super().publish(p)

        def publish_list(self, listed):
            spy.append(("publish_list", tuple(listed.names)))
            return super().publish_list(listed)

    monkeypatch.setattr(dispatch, "Published", SpyPublished)


def stamps_of(*names: str):
    from mu_spec.dash.stamps import Stamps

    stamps = Stamps()
    for name in names:
        stamps.installed(name, ('{"name":"' + name + '"}').encode(), notify=False)
    return stamps


def release_next(worker: FakeWorker, timeout: float = WITHIN_S) -> str | None:
    """Opens the gate of the next build entered within timeout; its project, or None."""
    try:
        project, gate = worker.entered.get(timeout=timeout)
    except queue.Empty:
        return None
    gate.set()
    return project


def drain(worker: FakeWorker) -> list[str]:
    """Opens every gate until no build is entered for QUIET_S; the projects released, in order."""
    released = []
    while (project := release_next(worker, QUIET_S)) is not None:
        released.append(project)
    return released


def settle(worker: FakeWorker) -> None:
    """Waits until the worker has been idle IDLE_S, at most WITHIN_S."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < WITHIN_S:
        if not worker.busy and time.monotonic() - max(worker.last, t0) >= IDLE_S:
            return
        time.sleep(POLL_S)


def within(condition, seconds: float = WITHIN_S) -> bool:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(POLL_S)
    return True


def ship_failed(project: str):
    from mu_spec.codec import new_log_record, with_seq

    return with_seq(new_log_record("ship_failed", project, None, {"reason": "boom"}, START), 1)


def plain(value: object) -> object:
    """A frozen doc as plain dicts and lists."""
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


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


def runtime_over_p(tmp_path, monkeypatch):
    """A runtime over a temporary store holding "p", whose observer is a real Stamps."""
    from mu_spec.codec import ProjectRecord
    from mu_spec.dash.stamps import Stamps
    from mu_spec.registry import build_runtime
    from mu_spec.store import Store

    from mu_spec.outside import ENV_NAMES, load_config

    D = tmp_path / "state"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    config = load_config({ENV_NAMES["port"]: "9100", ENV_NAMES["state_dir"]: str(D)}, cwd)[0]
    Store(config).create_project(ProjectRecord.from_doc("p", DOC))
    monkeypatch.chdir(cwd)
    set_environment(monkeypatch, port="9100", state_dir=str(D))
    stamps = Stamps()
    runtime = build_runtime(ship_builder=lambda client: object(), observer=stamps, github=FakeGitHub())
    return runtime, stamps


class ReadInbox:
    """Wraps the store's read_inbox; armed with a write, runs it once in another thread first."""

    def __init__(self, store) -> None:
        self.store = store
        self.armed = None
        self.calls = 0

    def __call__(self):
        self.calls += 1
        write, self.armed = self.armed, None
        if write is not None:
            thread = threading.Thread(target=write)
            thread.start()
            thread.join(timeout=GATE_S)
        return self.store.read_inbox()


def request_id(body: object) -> object:
    if isinstance(body, dict):
        for field in ("request_id", "id"):
            if field in body:
                return body[field]
        created = body.get("created")
        if isinstance(created, list) and len(created) == 1:
            return created[0]
    pytest.fail(f"post_request answer names no request id: {body!r}")


def dispatched(rt, tool: str, args: dict):
    from mu_spec.tools.catalogue import dispatch

    outcome = dispatch(rt, tool, args)
    assert 200 <= outcome.status < 300, f"{tool} refused: {outcome!r}"
    return plain(outcome.body)


def posted(rt, project: str) -> object:
    return request_id(dispatched(rt, "post_request", {
        "type": "initiate", "title": f"project {project}", "project": project, "actor": ACTOR}))


def create(rt, project: str, rid: object) -> None:
    dispatched(rt, "create_project", {"project": project, "in_response_to": rid, "actor": ACTOR})


def fork(rt, source: str, target: str, rid: object) -> None:
    """fork_project, its inputs named as its input shape names them."""
    from mu_spec.tools.schemas import BY_NAME
    from unitcheck.replay import FORK_SOURCE_INPUT

    shape = BY_NAME["fork_project"].input_schema
    properties = shape.get("properties", {})
    named = [k for k in shape.get("required", ()) if k != FORK_SOURCE_INPUT and k not in CALL_INPUTS]
    assert len(named) == 1, f"fork_project's input shape names no single new-project input: {shape!r}"
    args = {FORK_SOURCE_INPUT: source, named[0]: target}
    if "in_response_to" in properties:
        args["in_response_to"] = rid
    if "actor" in properties:
        args["actor"] = ACTOR
    dispatched(rt, "fork_project", args)


def dispatcher_over_runtime(runtime, stamps, worker, **kwargs):
    """A started Dispatcher over a runtime, as the dashboard's assembly builds it, with a fake worker."""
    from mu_spec.dash.dispatch import Dispatcher
    from mu_spec.ship.runs import RunRegistry

    runs = RunRegistry(runtime.clock)
    read_inbox = ReadInbox(runtime.store)
    d = Dispatcher(stamps, runs.marked_view, FakeMarker(), list(runtime.registry.names()), runtime.config,
                   runtime.clock, read_inbox, worker=Factory(worker), **kwargs)
    d.start(runtime.store.read_log())
    return d, read_inbox


def test_appearances(tmp_path, monkeypatch):
    from mu_spec.dash import readsource

    spy: list = []
    spy_published(monkeypatch, spy)
    runtime, stamps = runtime_over_p(tmp_path, monkeypatch)
    worker = FakeWorker()
    _, read_inbox = dispatcher_over_runtime(runtime, stamps, worker)
    assert within(lambda: ("publish", "p") in spy), "p's start-up build was never published"
    rids = {name: posted(runtime, name) for name in ("n", "m", "f")}
    settle(worker)

    # 1. created empty: published, then listed, before the creation returns; never marked
    def refuse(*args, **kwargs):
        raise AssertionError("a creation decoded through the read source")

    with monkeypatch.context() as patched:
        for name in ("decode_record", "decode_inbox", "decode_log_line"):
            patched.setattr(readsource, name, refuse)
        create(runtime, "n", rids["n"])
    assert len(spy) >= 2
    assert spy[-2] == ("publish", "n"), spy
    assert spy[-1][0] == "publish_list" and "n" in spy[-1][1], spy
    assert "n" not in worker.built
    time.sleep(1.0)
    assert "n" not in worker.built

    # 2. an inbox write racing the creation of "m" leaves "m" marked: the worker builds it once
    read_inbox.armed = lambda: runtime.store.write_inbox(plain(runtime.store.read_inbox()))
    create(runtime, "m", rids["m"])
    assert read_inbox.armed is None, "the creation of m never read the inbox"
    assert within(lambda: "m" in worker.built), "m was never built after the racing inbox write"
    settle(worker)
    assert worker.built.count("m") == 1

    # 3. a fork is listed, not published, before its answer; then built and published
    worker.holds["f"] = threading.Event()
    try:
        fork(runtime, "p", "f", rids["f"])
        at_answer = list(spy)
    finally:
        worker.holds["f"].set()
    lists = [i for i, call in enumerate(at_answer) if call[0] == "publish_list" and "f" in call[1]]
    assert lists, at_answer
    assert ("publish", "f") not in at_answer
    assert within(lambda: "f" in worker.built and ("publish", "f") in spy), "f was never built and published"
    assert spy.index(("publish", "f")) > lists[0]

    # 4. the published list only moves forward
    names = [set(call[1]) for call in spy if call[0] == "publish_list"]
    for before, after in itertools.pairwise(names):
        assert before <= after, (before, after)


def test_marks_order_and_recovery(monkeypatch):
    from mu_spec.codec import encode_log_line
    from mu_spec.dash.dispatch import Dispatcher
    from mu_spec.dash.snapshot import strip_progress
    from mu_spec.dash.stamps import INBOX_KEY, project_key
    from mu_spec.dash.worker import BuildFailed, WorkerLost
    from mu_spec.ship.access import Identity
    from mu_spec.ship.runs import RunRegistry

    clock = FixedClock(START)
    spy: list = []
    spy_published(monkeypatch, spy)
    worker = FakeWorker(gated=True, log=spy)  # each build entered is listed in spy too
    factory = Factory(worker)
    marker = FakeMarker()
    runs = RunRegistry(clock)
    d = Dispatcher(stamps_of("p", "q", "r"), runs.marked_view, marker, ["p", "q", "r", "u"], CONFIG, clock,
                   lambda: None, worker=factory)

    # 1. one worker with config and the very clock; the list first; start-up builds in order; u never
    d.start([])
    assert len(factory.calls) == 1
    assert factory.calls[0][0] is CONFIG
    assert factory.calls[0][1] is clock
    assert spy[0] == ("publish_list", ("p", "q", "r", "u"))
    assert [release_next(worker) for _ in range(3)] == ["p", "q", "r"]
    assert drain(worker) == []
    assert "u" not in worker.built
    with pytest.raises(RuntimeError):
        d.start([])

    # 2. first-marked order, coalesced marks, and a mark during the project's own build
    d.changed(project_key("r"))
    project, gate = worker.entered.get(timeout=WITHIN_S)
    assert project == "r"
    d.changed(project_key("q"))
    for _ in range(10):
        d.changed(project_key("p"))
    d.changed(project_key("q"))
    d.changed(project_key("r"))
    gate.set()
    drain(worker)
    assert worker.built[-4:] == ["r", "q", "p", "r"]

    # 3. the inbox and a None marker call each mark every project; u stays unbuilt
    count = len(worker.built)
    d.changed(INBOX_KEY)
    assert sorted(drain(worker)) == ["p", "q", "r"]
    assert len(worker.built) == count + 3
    marker.callback(None)
    assert sorted(drain(worker)) == ["p", "q", "r"]
    assert len(worker.built) == count + 6
    assert "u" not in worker.built

    # 4. a lost worker: restarted once, p rebuilt behind every project already waiting
    d.changed(project_key("p"))
    project, gate = worker.entered.get(timeout=WITHIN_S)
    assert project == "p"
    d.changed(project_key("q"))
    d.changed(project_key("r"))
    worker.fail["p"] = WorkerLost()
    gate.set()
    assert drain(worker) == ["q", "r", "p"]
    assert worker.built[-4:] == ["p", "q", "r", "p"]
    assert worker.restarts == 1
    assert len(factory.calls) == 1

    # 5. a failed build: its reason kept, not rebuilt, the earlier projection still published
    earlier = d.published.projection("q")
    assert earlier is not None
    worker.fail["q"] = BuildFailed("boom")
    count = len(worker.built)
    d.changed(project_key("q"))
    assert drain(worker) == ["q"]
    assert len(worker.built) == count + 1
    assert d.failures()["q"] == "boom"
    assert d.published.projection("q") is earlier

    # 6. an appended Ship event: one line in p's next snapshot, and no build
    record = ship_failed("p")
    count = len(worker.built)
    d.appended(record)
    assert drain(worker) == []
    assert len(worker.built) == count
    d.changed(project_key("p"))
    assert drain(worker) == ["p"]
    assert worker.snaps[-1].project == "p"
    assert worker.snaps[-1].events == (encode_log_line(record),)

    # 7. the cached identity and the progress view reach the snapshots
    octo = Identity("octo", "User")
    w7 = FakeWorker()
    d7 = Dispatcher(stamps_of("p", "q"), RunRegistry(clock).marked_view, FakeMarker(), ["p", "q"], CONFIG,
                    clock, lambda: None, worker=Factory(w7), identity=lambda: octo)
    d7.start([])
    assert within(lambda: {"p", "q"} <= set(w7.built))
    settle(w7)
    assert w7.snaps
    assert all(s.identity == octo for s in w7.snaps)

    running = RunRegistry(clock)
    running.admit("p", "person_ship", None, running.generation("p"))
    pv = running.progress_view("p")[0]
    assert pv is not None and pv["running"] is True
    assert running.marked_view("p")[1] == strip_progress(pv)
    w8 = FakeWorker()
    d8 = Dispatcher(stamps_of("p"), running.marked_view, FakeMarker(), ["p"], CONFIG, clock, lambda: None,
                    worker=Factory(w8), progress_view=running.progress_view)
    d8.start([])
    assert within(lambda: "p" in w8.built)
    settle(w8)
    assert [s.progress for s in w8.snaps if s.project == "p"] == [pv] * w8.built.count("p")


def test_learn_identity(tmp_path, monkeypatch):
    from mu_spec.dash.dispatch import LEARN_FAILURE, Dispatcher
    from mu_spec.dash.stamps import project_key
    from mu_spec.ship.access import Identity
    from mu_spec.ship.runs import RunRegistry

    clock = FixedClock(START)
    octo = Identity("octo", "User")
    log: list[str] = []
    holder = [None]
    spy: list = []
    spy_published(monkeypatch, spy, log)

    def identity():
        log.append("identity")
        return holder[0]

    def dispatcher(learn):
        worker = FakeWorker(log=log)
        marker = FakeMarker()
        d = Dispatcher(stamps_of("p", "q"), RunRegistry(clock).marked_view, marker, ["p", "q"], CONFIG, clock,
                       lambda: None, worker=Factory(worker), identity=identity, learn=learn)
        return d, worker, marker

    # 1. after each snapshot, before its build, with the dispatcher's lock free
    alive = []
    held = {}

    def learn():
        log.append("learn")
        helper = threading.Thread(target=held["d"].appended, args=(ship_failed("p"),))
        helper.start()
        helper.join(timeout=2.0)
        alive.append(helper.is_alive())

    d, worker, _ = dispatcher(learn)
    held["d"] = d
    d.start([])
    settle(worker)
    assert [e for e in log if e.startswith(("identity", "learn", "build"))] == [
        "identity", "learn", "build p", "identity", "learn", "build q"]
    assert alive and not any(alive)

    # 2. created empty: one learn after the creation's snapshot, before its in-process publish
    calls = []

    def recording_learn():
        calls.append(1)
        log.append("learn")

    runtime, stamps = runtime_over_p(tmp_path, monkeypatch)
    w2 = FakeWorker(log=log)
    dispatcher_over_runtime(runtime, stamps, w2, identity=identity, learn=recording_learn)
    assert within(lambda: "p" in w2.built)
    rid = posted(runtime, "n")
    settle(w2)
    mark = len(log)
    before = len(calls)
    create(runtime, "n", rid)
    tail = log[mark:]
    assert [e for e in tail if e in ("identity", "learn", "publish n", "build n")] == [
        "identity", "learn", "publish n"], tail
    assert len(calls) - before == 1

    # 3. a cached identity: learn never called, every snapshot holds it
    holder[0] = octo
    calls.clear()
    d, worker, _ = dispatcher(recording_learn)
    d.start([])
    settle(worker)
    d.changed(project_key("p"))
    settle(worker)
    assert calls == []
    assert worker.snaps
    assert all(s.identity == octo for s in worker.snaps)

    # 4. a raising learn: kept in failures under LEARN_FAILURE, and every build still published
    holder[0] = None
    e = RuntimeError("x")

    def raising_learn():
        raise e

    spy.clear()
    d, worker, _ = dispatcher(raising_learn)
    d.start([])
    settle(worker)
    assert ("publish", "p") in spy and ("publish", "q") in spy
    assert d.failures()[LEARN_FAILURE] == repr(e)
    count = spy.count(("publish", "p"))
    d.changed(project_key("p"))
    assert within(lambda: spy.count(("publish", "p")) > count), "p was not built and published again"

    # 5. converging: once the login is cached and marked, each project is rebuilt with it; learn stops
    calls.clear()
    d, worker, marker = dispatcher(recording_learn)
    d.start([])
    settle(worker)
    assert len(calls) == 2
    count = len(worker.snaps)
    holder[0] = octo
    marker.callback(None)
    settle(worker)
    after = worker.snaps[count:]
    assert sorted(s.project for s in after) == ["p", "q"]
    assert all(s.identity == octo for s in after)
    assert len(calls) == 2
