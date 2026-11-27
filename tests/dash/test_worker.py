"""mu_spec.dash.worker: the projector's own process (S-331).

T-290 pins that a Worker built with the runtime's clock answers what an in-process build answers,
survives a failing build, reports a child killed between or during a build or stalled as
WorkerLost, and builds again after restart. T-292 pins that the child holds an unpickled copy of
the runtime's clock and constructs none, that a failed send is WorkerLost, and that the process
sweep passes on the worker module as written. Modules under test are imported inside the test, so
a module not yet written fails only the tests that need it.
"""

import ast
import dataclasses
import datetime
import multiprocessing
import os
import pickle
import shutil
import signal
import threading
import time
from pathlib import Path

import pytest

from mu_spec.outside import SystemClock

MU_SPEC = Path(__file__).resolve().parents[2] / "mu_spec"
WORKER = "mu_spec.dash.worker"
DOC = {"name": "p", "entries": {}, "slices": {}, "cuts": []}
LOG_VAR = "WORKER_CLOCK_LOG"
ANSWER_S = 30.0
CLOSE_S = 5.0
TEST_STALL_S = 2.0
KILL_DELAY_S = 0.3


class RecordingClock(SystemClock):
    """A SystemClock appending each construction and each now() to the file named in WORKER_CLOCK_LOG."""

    def __init__(self) -> None:
        note(f"init {os.getpid()}")
        super().__init__()

    def now(self) -> datetime.datetime:
        result = super().now()
        note(f"now {os.getpid()} {result.tzinfo is not None}")
        return result


def note(line: str) -> None:
    with open(os.environ[LOG_VAR], "a", encoding="utf-8") as log:
        log.write(line + "\n")


def logged(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


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


class FakeConnection:
    """A pipe end: send raises the given error or succeeds; poll answers polled; recv gives answer."""

    def __init__(self, send_error=None, polled=True, answer=None) -> None:
        self.send_error = send_error
        self.polled = polled
        self.answer = answer

    def send(self, obj) -> None:
        if self.send_error is not None:
            raise self.send_error

    def poll(self, timeout=None) -> bool:
        return self.polled

    def recv(self):
        return self.answer

    def close(self) -> None:
        pass


def set_environment(monkeypatch, **texts: str) -> None:
    """Every configuration variable absent but those given, by field name."""
    from mu_spec.outside import ENV_NAMES

    for name in ENV_NAMES.values():
        monkeypatch.delenv(name, raising=False)
    for field, text in texts.items():
        monkeypatch.setenv(ENV_NAMES[field], text)


def runtime_over_p(tmp_path, monkeypatch):
    """A runtime over a temporary state directory holding one project "p", and stamps holding its record."""
    from mu_spec.codec import ProjectRecord, record_path
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
    runtime = build_runtime(ship_builder=lambda client: object(), github=FakeGitHub())
    stamps = Stamps()
    stamps.installed("p", record_path(runtime.config.root, "p").read_bytes(), notify=False)
    return runtime, stamps


def snapshot_of_p(runtime, stamps):
    from mu_spec.dash.snapshot import take_snapshot
    from mu_spec.ship.runs import RunRegistry

    return take_snapshot("p", stamps, RunRegistry(runtime.clock).marked_view, ())


def assert_same(got, expected) -> None:
    """Equal view by view on head, rest, selections and token, and on generation, marker and run."""
    assert got.generation == expected.generation
    assert got.marker == expected.marker
    assert got.run == expected.run
    assert set(got.views) == set(expected.views)
    for view, want in expected.views.items():
        have = got.views[view]
        assert have.head == want.head, view
        assert have.rest == want.rest, view
        assert dict(have.selections) == dict(want.selections), view
        assert have.token == want.token, view


def child_of(others: set):
    """The one live child not in others: the one the worker under test started."""
    started = [p for p in multiprocessing.active_children() if p not in others]
    assert len(started) == 1, started
    return started[0]


def gone_within(process, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while process.is_alive():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def about(failure, module: str) -> bool:
    values = dataclasses.astuple(failure) if dataclasses.is_dataclass(failure) else tuple(failure)
    return module in values


def test_worker_life(tmp_path, monkeypatch):
    from mu_spec.dash import worker
    from mu_spec.dash.projector import build
    from mu_spec.dash.stamps import Stamp, record_digest
    from mu_spec.dash.worker import START_METHOD, BuildFailed, Worker, WorkerLost

    runtime, stamps = runtime_over_p(tmp_path, monkeypatch)
    config = runtime.config
    clock = runtime.clock
    good = snapshot_of_p(runtime, stamps)
    bad = dataclasses.replace(good, record=Stamp(b"not json", record_digest(b"not json"), 0))
    expected = build(good, config, clock)
    others = set(multiprocessing.active_children())
    w = Worker(config, clock)
    children = [child_of(others)]
    try:
        # 1. the child's build equals the in-process build
        assert_same(w.build(good), expected)

        # 2. a failing build is BuildFailed with its reason, and the child goes on building
        with pytest.raises(BuildFailed) as failed:
            w.build(bad)
        assert isinstance(failed.value.reason, str) and failed.value.reason != ""
        t0 = time.monotonic()
        assert_same(w.build(good), expected)
        assert time.monotonic() - t0 < ANSWER_S

        monkeypatch.setattr(worker, "STALL_S", TEST_STALL_S)

        # 3. killed between builds: WorkerLost, never another type, then restart builds again
        child = children[-1]
        child.kill()
        child.join()
        t0 = time.monotonic()
        with pytest.raises(WorkerLost) as lost:
            w.build(good)
        assert time.monotonic() - t0 < 6.0
        assert not isinstance(lost.value, (OSError, EOFError))
        w.restart()
        children.append(child_of(others | set(children)))
        assert_same(w.build(good), expected)

        # 4. killed during a build: the build in flight is WorkerLost
        if hasattr(signal, "SIGSTOP") and hasattr(signal, "SIGKILL"):
            child = children[-1]
            os.kill(child.pid, signal.SIGSTOP)
            outcome = {}

            def in_flight():
                try:
                    outcome["answer"] = w.build(good)
                except Exception as error:  # noqa: BLE001 - the type is what is asserted
                    outcome["error"] = error
                outcome["at"] = time.monotonic()

            thread = threading.Thread(target=in_flight)
            thread.start()
            time.sleep(KILL_DELAY_S)
            os.kill(child.pid, signal.SIGKILL)
            killed = time.monotonic()
            thread.join(timeout=ANSWER_S)
            assert not thread.is_alive(), "the build in flight never returned"
            assert isinstance(outcome.get("error"), WorkerLost), outcome
            assert outcome["at"] - killed < 6.0
            child.join()
            w.restart()
            children.append(child_of(others | set(children)))
            assert_same(w.build(good), expected)

        # 5. a stalled child: WorkerLost at about STALL_S
        if hasattr(signal, "SIGSTOP"):
            child = children[-1]
            os.kill(child.pid, signal.SIGSTOP)
            t0 = time.monotonic()
            with pytest.raises(WorkerLost):
                w.build(good)
            assert time.monotonic() - t0 < 5.0
            w.restart()
            children.append(child_of(others | set(children)))
            assert_same(w.build(good), expected)

        # 6. spawn, and close leaves no child alive
        assert START_METHOD == "spawn"
    finally:
        w.close()
    for child in children:
        assert gone_within(child, CLOSE_S), child


def test_worker_clock_and_send_failure(tmp_path, monkeypatch):
    from mu_spec.dash.projector import build
    from mu_spec.dash.worker import BuildFailed, Worker, WorkerLost

    import mu_spec.outside as clocks

    path = tmp_path / "clock.log"
    monkeypatch.setenv(LOG_VAR, str(path))
    clock = RecordingClock()
    runtime, stamps = runtime_over_p(tmp_path, monkeypatch)
    snap = snapshot_of_p(runtime, stamps)
    me = str(os.getpid())

    # 1. the clock survives pickling with no constructor call
    c2 = pickle.loads(pickle.dumps(clock))
    assert c2.now().tzinfo is not None
    assert c2.now().utcoffset() == datetime.timedelta(0)
    a = c2.monotonic()
    time.sleep(0.01)
    assert c2.monotonic() > a
    assert [line for line in logged(path) if line.startswith("init ")] == [f"init {me}"]

    # 2. the child uses the copy it was sent and constructs no clock
    expected = build(snap, runtime.config, clock)
    w = Worker(runtime.config, clock)
    try:
        assert_same(w.build(snap), expected)
    finally:
        w.close()
    lines = logged(path)
    assert [line for line in lines if line.startswith("init ")] == [f"init {me}"]
    for line in lines:
        fields = line.split()
        if fields[0] == "now" and fields[1] != me:
            assert fields[2] == "True", line

    # 3. a failed send or a silent child is WorkerLost; a failed build is BuildFailed with its reason
    w = Worker(runtime.config, clock)
    held = {name: value for name, value in vars(w).items()
            if all(callable(getattr(value, m, None)) for m in ("send", "recv", "poll"))}
    assert held, "the worker holds no connection"
    try:
        for fake, raised in (
            (FakeConnection(send_error=BrokenPipeError()), WorkerLost),
            (FakeConnection(send_error=ConnectionResetError()), WorkerLost),
            (FakeConnection(polled=False), WorkerLost),
            (FakeConnection(answer=("failed", "ValueError('x')")), BuildFailed),
        ):
            for name in held:
                setattr(w, name, fake)
            with pytest.raises(raised) as caught:
                w.build(snap)
            if raised is BuildFailed:
                assert caught.value.reason == "ValueError('x')"
    finally:
        for name, value in held.items():
            setattr(w, name, value)
        w.close()

    # 4. the process sweep passes on the worker as written, and reports a get_context(START_METHOD)
    from sweeps.source import sweep

    assert not [f for f in sweep(MU_SPEC.parent) if about(f, WORKER)]
    copy = tmp_path / "copy"
    shutil.copytree(MU_SPEC, copy / "mu_spec", ignore=shutil.ignore_patterns("__pycache__"))
    module = copy / "mu_spec" / "dash" / "worker.py"
    text = module.read_text(encoding="utf-8")
    changed = (text.replace("get_context('spawn')", "get_context(START_METHOD)")
               .replace('get_context("spawn")', "get_context(START_METHOD)"))
    assert changed != text, "worker.py holds no multiprocessing.get_context('spawn')"
    module.write_text(changed, encoding="utf-8", newline="")
    assert [f for f in sweep(copy) if about(f, WORKER)]

    # 5. worker.py calls no Clock constructor
    clock_classes = {
        name for name, value in vars(clocks).items()
        if isinstance(value, type) and callable(getattr(value, "now", None))
        and callable(getattr(value, "monotonic", None))
    }
    assert {"SystemClock", "FixedClock"} <= clock_classes
    tree = ast.parse((MU_SPEC / "dash" / "worker.py").read_text(encoding="utf-8"))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
    assert not called & clock_classes, called & clock_classes
