"""mu_spec.ship.part: the Ship part, its background runs, delegated Ship and its reads (T-430..T-436).

A run ends outcome commit, note, records, finish, end; a failed append ends it failed with one
ship_failed record; the person's build check, the login learning, at most one run per project,
delegated Ship's refusals and its lines-turn cut, and ship_read over the real part and over a
read-only stand-in. GitHub is a fake client recording every request; no request leaves the process.
Modules under test are imported inside the test, so a module not yet written fails only the tests
that need it.
"""

import collections.abc
import copy
import dataclasses
import datetime
import inspect
import json
import re
import threading
import time
from types import SimpleNamespace

import pytest

from mu_spec.outside import FixedClock, SystemClock

START = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
POLL_S = 0.02
RUN_WINDOW_S = 10.0     # how long a faked run may take to end
REAL_RUN_WINDOW_S = 60.0  # a real plan's run, its writes paced by the sender
HOLD_WINDOW_S = 30.0    # the longest a held fake request waits for its release
ACTOR = "a"
RUN_ID = re.compile(r"^[0-9a-f]{12}$")
DELEGATED_TEXT = ("refused, nothing was written: delegation, ready, gates, st_only, shipped, "
                  "tests_assigned did not hold. Anything outside the delegation is a person's press.")

DOC = {"project": "p", "slices": {}, "allocation": {}, "modules": {"a.py": ["S-10 (was S-01)"]},
       "repo": "o/r", "labels": [], "pickup": []}
CUT = {"seq": 1, "members": ["u1"],
       "units": {"u1": {"anchor": "S-10 (was S-01)", "entries": ["S-10 (was S-01)"], "modules": ["a.py"],
                        "slices": ["core"], "body_bytes": 10}},
       "edges": {"u1": []}}
SETTINGS = {"repo": "o/r", "labels": (), "pickup": (), "issues": {}, "orders": (), "lineage_keyed": True,
            "next_seq": 1}
SHIP_LINE = {"seq": 1, "cut_seq": 1, "repo": "o/r", "issues": {}, "wired": []}
ROLLBACK_LINE = {"kind": "rollback", "seq": 1, "cut_seq": 1, "repo": "o/r", "undone": [], "closed": {},
                 "failed": []}
CUT_LINE = {"seq": 3, "members": [], "units": {}, "edges": {}}


def wait_until(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"{what} within {timeout} s")
        time.sleep(POLL_S)


def wait_ended(part, project, timeout=RUN_WINDOW_S):
    """Poll run_view(project) until running is False; answer that view."""
    def ended():
        view = part.run_view(project)
        return view is not None and view["running"] is False

    wait_until(ended, timeout, f"the run of {project!r} did not end")
    return part.run_view(project)


def raised(name, /, **kwargs):
    """The Refused that refuse(name, **kwargs) raises."""
    from mu_spec.refusals import Refused, refuse

    with pytest.raises(Refused) as caught:
        refuse(name, **kwargs)
    return caught.value


def holds_value(value, wanted):
    """Whether wanted is value or occurs anywhere inside it, mappings and sequences walked."""
    if value == wanted:
        return True
    if isinstance(value, collections.abc.Mapping):
        return any(holds_value(v, wanted) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(holds_value(v, wanted) for v in value)
    return isinstance(value, str) and wanted in value


def busy_text(project, run_id):
    return f"an emission for '{project}' is already running (run {run_id})"


class FakeClient:
    """A GitHubClient stand-in: request() records each call and answers from respond(method, path, body).

    A (method, path) in hold is held on its first arrival until release() (paths compared without their
    leading '/'); answers are (status, body).
    """

    def __init__(self, respond=None, hold=()):
        self.respond = respond or (lambda method, path, body: (200, []))
        self.requests = []
        self._lock = threading.Lock()
        self._hold = {(m, p.lstrip("/")) for m, p in hold}
        self._held_once = set()
        self.arrived = threading.Event()
        self._release = threading.Event()

    def request(self, method, path, body=None):
        from mu_spec.github import GitHubResponse

        key = (method, path.lstrip("/"))
        with self._lock:
            self.requests.append((method, path, body))
            hold = key in self._hold and key not in self._held_once
            if hold:
                self._held_once.add(key)
        if hold:
            self.arrived.set()
            assert self._release.wait(HOLD_WINDOW_S), f"held {method} {path} was never released"
        status, answer = self.respond(method, path, body)
        return GitHubResponse(status=status, body=answer, headers={})

    def release(self, respond=None):
        if respond is not None:
            self.respond = respond
        self._release.set()

    def count(self, method, path):
        with self._lock:
            return sum(1 for m, p, _ in self.requests if (m, p.lstrip("/")) == (method, path.lstrip("/")))


def user_answer(login, kind):
    def respond(method, path, body):
        if (method, path.lstrip("/")) == ("GET", "user"):
            return 200, {"login": login, "type": kind}
        return 200, []

    return respond


class FakeIds:
    """An IdAllocator over doc answering fresh identifiers without touching doc."""

    def __init__(self, doc):
        self._doc = doc
        self.issued = 0

    @property
    def doc(self):
        return self._doc

    def next(self, prefix):
        self.issued += 1
        return f"{prefix}-{self.issued}"


class RecordingStore:
    """A Store whose append_log appends 'append_log:' + the record's kind to order."""

    def __init__(self, order):
        self.order = order
        self.records = []

    def append_log(self, record):
        self.order.append("append_log:" + record.kind)
        self.records.append(record)


class FakeWriter:
    """A ProjectWriter over one frozen state; submit builds, appends 'commit', then each record's kind.

    fail, when set, makes submit append 'submit' and raise it without building; after_records runs once
    the handler's records are given.
    """

    def __init__(self, rt, state, order, fail=None, after_records=None):
        self.rt = rt
        self.state = state
        self.order = order
        self.fail = fail
        self.after_records = after_records
        self.records = []
        self.submits = 0

    def snapshot(self):
        return self.state

    def submit(self, call, handler):
        from mu_spec.writer import Outcome

        self.submits += 1
        if self.fail is not None:
            self.order.append("submit")
            raise self.fail
        doc = copy.deepcopy(self.state.thaw())
        at = self.rt.clock.now()
        built = handler.build(self.state, call, doc, FakeIds(doc), at)
        self.order.append("commit")
        for record in handler.log_records(call, built, at):
            self.order.append("record:" + record.kind)
            self.records.append(record)
        if self.after_records is not None:
            self.after_records()
        return Outcome(built.status, built.body)


class FakeRegistry:
    def __init__(self, writers):
        self.writers = writers

    def writer(self, project):
        return self.writers.get(project)


def state_of(doc):
    from mu_spec.codec import ProjectRecord

    return ProjectRecord.from_doc(doc["project"], doc)


def fake_runtime(order, token="t", clock=None, docs=None, writer=None):
    """A runtime with config.github_token, a clock, a recording store and a registry over docs."""
    rt = SimpleNamespace(config=SimpleNamespace(github_token=token), clock=clock or SystemClock(),
                         store=RecordingStore(order), registry=None, ship=None)
    writers = {name: FakeWriter(rt, state_of(doc), order) for name, doc in (docs or {}).items()}
    if writer is not None:
        writers.update(writer(rt))
    rt.registry = FakeRegistry(writers)
    return rt


class CutFacts:
    """A ShipFacts holding the one-unit cut CUT."""

    def __init__(self, settings=None, latest_cut=CUT, serving=None):
        self.settings_answer = dict(settings or SETTINGS)
        self.cut = latest_cut
        self.serving = serving

    def latest_cut(self, state):
        return self.cut

    def unit_waves(self, cut):
        return (("u1",),)

    def drift_since(self, state, cut):
        return None

    def retired_citations(self, state, cut):
        return ()

    def dead_end_body(self, state, cut):
        return None

    def unit_cycle(self, cut):
        return None

    def serving_commit(self, runtime):
        return self.serving

    def settings(self, state):
        return dict(self.settings_answer)


def wrap_runs(monkeypatch, order):
    """RunRegistry.note_commit, finish and end append 'note', 'finish:' + phase and 'end', then run."""
    from mu_spec.ship.runs import RunRegistry

    real_note, real_finish, real_end = RunRegistry.note_commit, RunRegistry.finish, RunRegistry.end

    def note_commit(self, *args, **kwargs):
        order.append("note")
        return real_note(self, *args, **kwargs)

    def finish(self, *args, **kwargs):
        phase = inspect.signature(real_finish).bind(self, *args, **kwargs).arguments["phase"]
        order.append("finish:" + phase)
        return real_finish(self, *args, **kwargs)

    def end(self, *args, **kwargs):
        order.append("end")
        return real_end(self, *args, **kwargs)

    monkeypatch.setattr(RunRegistry, "note_commit", note_commit)
    monkeypatch.setattr(RunRegistry, "finish", finish)
    monkeypatch.setattr(RunRegistry, "end", end)


def answering(outcome):
    def plan(*args, **kwargs):
        return outcome

    return plan


def raising(error):
    def plan(*args, **kwargs):
        raise error

    return plan


def test_end_of_run_order(monkeypatch):
    from mu_spec.codec import MUTATION_KIND
    from mu_spec.ship.part import build_ship
    from mu_spec.ship.plan import PlanOutcome

    from mu_spec.ship import part as ship_part

    order = []
    wrap_runs(monkeypatch, order)

    def fresh(plan_name, plan):
        order.clear()
        rt = fake_runtime(order, clock=FixedClock(START), docs={"p": DOC})
        part = build_ship(FakeClient())
        part.facts = CutFacts()
        monkeypatch.setattr(ship_part, plan_name, plan)
        return rt, part

    # 1. lines: the outcome commit, its note and records, then finish 'done', then end
    rt, part = fresh("ship_plan", answering(PlanOutcome({"emitted": True, "project": "p", "repo": "o/r"},
                                                        (SHIP_LINE,))))
    a = part.press_ship(rt, "p", {})
    assert a.status == 202, a
    view = wait_ended(part, "p")
    assert order == ["commit", "note", "record:" + MUTATION_KIND, "record:tickets_emitted", "finish:done", "end"]
    (event,) = [r for r in rt.registry.writers["p"].records if r.kind == "tickets_emitted"]
    run_id = a.body["run_id"]
    assert holds_value(event.doc["refs"], run_id) or holds_value(event.doc["facts"], run_id)
    assert not any(e.startswith("append_log:") for e in order)
    assert view["phase"] == "done"

    # 2. no lines: no writer turn, the ending recorded through the store, then finish 'done', end
    rt, part = fresh("ship_plan", answering(PlanOutcome(
        {"emitted": False, "project": "p", "repo": "o/r", "reason": "r"}, ())))
    assert part.press_ship(rt, "p", {}).status == 202
    view = wait_ended(part, "p")
    assert order == ["append_log:" + MUTATION_KIND, "append_log:ship_failed", "finish:done", "end"]
    assert "commit" not in order
    assert view["phase"] == "done"

    # 3. the plan raises: no writer turn, the ending recorded through the store, finish 'failed', end
    rt, part = fresh("ship_plan", raising(RuntimeError("x")))
    assert part.press_ship(rt, "p", {}).status == 202
    view = wait_ended(part, "p")
    assert order == ["append_log:" + MUTATION_KIND, "append_log:ship_failed", "finish:failed", "end"]
    assert "commit" not in order
    assert view["phase"] == "failed"
    assert view["error"] == "RuntimeError: x"
    assert view["result"] is None

    # 4. a rollback with lines is logged as tickets_rolled_back
    rt, part = fresh("rollback_plan", answering(PlanOutcome(
        {"rolled_back": True, "project": "p", "repo": "o/r"}, (ROLLBACK_LINE,))))
    assert part.press_rollback(rt, "p", {}).status == 202
    wait_ended(part, "p")
    assert order == ["commit", "note", "record:" + MUTATION_KIND, "record:tickets_rolled_back", "finish:done",
                     "end"]


def test_failed_append_and_end_point(monkeypatch):
    from mu_spec.codec import MUTATION_KIND
    from mu_spec.ship.part import build_ship, outcome_records, record_ending
    from mu_spec.ship.plan import PlanOutcome

    from mu_spec.ship import part as ship_part
    from mu_spec.ship.runs import RunRegistry

    order = []
    wrap_runs(monkeypatch, order)
    monkeypatch.setattr(ship_part, "ship_plan", answering(PlanOutcome(
        {"emitted": True, "project": "p", "repo": "o/r"}, (SHIP_LINE,))))

    def fresh(gen, **writer_args):
        order.clear()
        rt = fake_runtime(order, clock=FixedClock(START), writer=lambda rt: {
            "p": FakeWriter(rt, state_of(DOC), order, **writer_args)})
        part = build_ship(FakeClient())
        part.facts = CutFacts()
        part.set_generation_source(lambda p: gen[0])
        return rt, part

    # 1. the Ship-log append raises: one ship_failed record, finish 'failed', end; the end point at end
    gen = [9]
    err = OSError("disk full")
    rt, part = fresh(gen, fail=err)
    assert part.press_ship(rt, "p", {}).status == 202
    view = wait_ended(part, "p")
    assert order == ["submit", "append_log:ship_failed", "finish:failed", "end"]
    assert "note" not in order
    assert not any(e.endswith(":" + MUTATION_KIND) for e in order)
    assert view["running"] is False
    assert view["phase"] == "failed"
    assert view["error"] == "OSError: disk full"
    assert view["result"] is None
    assert part.progress_view("p")[1][0] == 9

    # 2. a write committed after the outcome commit leaves the end point at the noted generation
    gen = [7]

    def later_write():
        gen[0] = 9

    rt, part = fresh(gen, after_records=later_write)
    assert part.press_ship(rt, "p", {}).status == 202
    wait_ended(part, "p")
    assert gen[0] == 9
    end_point = part.progress_view("p")[1]
    assert end_point[0] == 7
    assert end_point[2] == part.marker.value("p")[1]

    # 3. record_ending drops a failing record and keeps the rest, in order
    scratch = RunRegistry(FixedClock(START))
    run = scratch.admit("p", "person_ship", None, 0)
    rec1, rec2 = outcome_records(run, {"emitted": True}, START)

    class FlakyStore:
        def __init__(self, failing):
            self.failing = failing
            self.calls = 0
            self.recorded = []

        def append_log(self, record):
            self.calls += 1
            if self.calls <= self.failing:
                raise OSError("log cannot be written")
            self.recorded.append(record)

    flaky = FlakyStore(1)
    record_ending(flaky, (rec1, rec2))
    assert flaky.recorded == [rec2]
    steady = FlakyStore(0)
    record_ending(steady, (rec1, rec2))
    assert steady.recorded == [rec1, rec2]


def test_build_check(monkeypatch):
    from mu_spec.ship.part import build_ship
    from mu_spec.ship.plan import PlanOutcome

    from mu_spec.ship import part as ship_part

    order = []
    held = threading.Event()

    def plan(*args, **kwargs):
        assert held.wait(HOLD_WINDOW_S), "the held plan was never released"
        return PlanOutcome({"emitted": False, "project": "p", "repo": "o/r", "reason": "r"}, ())

    monkeypatch.setattr(ship_part, "ship_plan", plan)
    projects = ("p", "p2", "p3", "p4")
    rt = fake_runtime(order, docs={name: dict(DOC, project=name) for name in projects})
    part = build_ship(FakeClient())
    facts = CutFacts(latest_cut=None, serving="abc123")
    part.facts = facts
    try:
        # 1. the given build is stripped and compared by prefix
        assert part.press_ship(rt, "p", {"build": " abc "}).status == 202

        # 2. another build: refused in reason form, no run admitted
        o = part.press_ship(rt, "p2", {"build": "abd"})
        assert o.status == 409, o
        body = dict(o.body)
        assert body["emitted"] is False
        assert body["project"] == "p2"
        assert body["build"] == "abc123"
        assert body["reason"] == raised("SHIP_BUILD_MISMATCH", given="abd", serving="abc123").message
        assert "error" not in body
        assert part.run_view("p2") is None

        # 3. a blank build is not checked
        assert part.press_ship(rt, "p3", {"build": "   "}).status == 202

        # 4. no serving commit known: refused naming a build it cannot name
        facts.serving = None
        o = part.press_ship(rt, "p4", {"build": "abc"})
        assert o.status == 409, o
        body = dict(o.body)
        assert body["build"] is None
        assert "a build it cannot name" in body["reason"]
        assert part.run_view("p4") is None
    finally:
        held.set()
    wait_ended(part, "p")
    wait_ended(part, "p3")


def test_identity_learning():
    from mu_spec.ship.access import Identity
    from mu_spec.ship.part import build_ship

    rt = fake_runtime([])

    # 1. a fresh part knows no login and sends nothing for it
    client = FakeClient(hold=(("GET", "/user"),))
    part = build_ship(client)
    assert part.identity() is None
    assert client.count("GET", "/user") == 0

    # 2. learning returns at once and keeps one learning in flight
    seen = []
    part.marker.observe(lambda n: seen.append((n, part.identity())))
    part.learn_identity(rt)
    assert not client._release.is_set()
    wait_until(lambda: client.count("GET", "/user") == 1, 5.0, "GET /user was not sent")
    part.learn_identity(rt)
    time.sleep(0.2)
    assert client.count("GET", "/user") == 1

    # 3. the 200 login is cached before the marker learns it
    client.release(user_answer("octo", "User"))
    wait_until(lambda: part.identity() is not None, 5.0, "the login was not learned")
    assert part.identity() == Identity("octo", "User")
    assert part.marker.value("p")[2] == "octo"
    assert seen == [(None, Identity("octo", "User"))]
    part.learn_identity(rt)
    time.sleep(0.2)
    assert client.count("GET", "/user") == 1

    # 4. no token: nothing sent
    client = FakeClient(user_answer("octo", "User"))
    other = build_ship(client)
    other.learn_identity(fake_runtime([], token=None))
    time.sleep(0.2)
    assert client.count("GET", "/user") == 0

    # 5. a refused token leaves nothing known, and the next learning tries again
    client = FakeClient(lambda method, path, body: (401, {"message": "Bad credentials"}))
    other = build_ship(client)
    other.learn_identity(rt)
    wait_until(lambda: client.count("GET", "/user") == 1, 5.0, "GET /user was not sent")
    time.sleep(0.5)
    assert other.identity() is None
    assert other.marker.value("p")[2] is None
    other.learn_identity(rt)
    wait_until(lambda: client.count("GET", "/user") == 2, 5.0, "the learning was not tried again")

    # 6. check_access's identity replaces the cached one
    def access(method, path, body):
        p = path.lstrip("/")
        if (method, p) == ("GET", "user"):
            return 200, {"login": "b", "type": "Bot"}
        if (method, p) == ("GET", "repos/o/r"):
            return 200, {"permissions": {"push": True}}
        return 200, []

    other = build_ship(FakeClient(access))
    o = other.check_access(fake_runtime([], docs={"p": DOC}), {"project": "p"})
    assert o.status == 200, o
    assert other.identity() == Identity("b", "Bot")
    assert other.marker.value("p")[2] == "b"


def set_environment(monkeypatch, **texts):
    """Every configuration variable absent but those given, by field name."""
    from mu_spec.outside import ENV_NAMES

    for name in ENV_NAMES.values():
        monkeypatch.delenv(name, raising=False)
    for field, text in texts.items():
        monkeypatch.setenv(ENV_NAMES[field], text)


def real_runtime(base, monkeypatch, client, doc=None):
    """A runtime with a token built by build_runtime over a scratch state directory under base, its Ship part
    build_ship(client); with doc, the directory holds project 'p' with that manifest."""
    from mu_spec.codec import ProjectRecord
    from mu_spec.registry import build_runtime
    from mu_spec.ship.part import build_ship
    from mu_spec.store import Store

    from mu_spec.outside import ENV_NAMES, load_config

    state = base / "state"
    cwd = base / "cwd"
    cwd.mkdir(parents=True)
    if doc is not None:
        config = load_config({ENV_NAMES["port"]: "9100", ENV_NAMES["state_dir"]: str(state)}, cwd)[0]
        Store(config).create_project(ProjectRecord.from_doc("p", doc))
    monkeypatch.chdir(cwd)
    set_environment(monkeypatch, port="9100", state_dir=str(state), github_token="t")
    return build_runtime(ship_builder=build_ship, github=client)


def request_id(body):
    if isinstance(body, collections.abc.Mapping):
        for field in ("request_id", "id"):
            if field in body:
                return body[field]
        created = body.get("created")
        if isinstance(created, (list, tuple)) and len(created) == 1:
            return created[0]
    pytest.fail(f"post_request answer names no request id: {body!r}")


def dispatched(rt, tool, args):
    from mu_spec.tools.catalogue import dispatch

    outcome = dispatch(rt, tool, args)
    assert 200 <= outcome.status < 300, f"{tool} refused: {outcome!r}"
    return outcome.body


def gh_issue(number, labels=(), title="", body=""):
    return {"id": number * 10, "number": number, "url": f"https://api.github.com/repos/o/r/issues/{number}",
            "html_url": f"https://github.com/o/r/issues/{number}", "title": title, "body": body,
            "state": "open", "state_reason": None, "labels": [{"name": name} for name in labels]}


class DelegatingFacts:
    """Answers the cut questions for CUT and delegates every other question to real."""

    def __init__(self, real):
        self.real = real

    def latest_cut(self, state):
        return CUT

    def unit_waves(self, cut):
        return (("u1",),)

    def drift_since(self, state, cut):
        return None

    def retired_citations(self, state, cut):
        return ()

    def dead_end_body(self, state, cut):
        return None

    def unit_cycle(self, cut):
        return None

    def __getattr__(self, name):
        return getattr(self.real, name)


def test_busy_outcome_and_interim_write(tmp_path, monkeypatch):
    from mu_spec.actions import action_handlers
    from mu_spec.codec import record_path
    from mu_spec.ship.plan import SHIP_SEQ_START, PlanOutcome

    from mu_spec.ship import part as ship_part

    def respond(method, path, body):
        p = path.lstrip("/")
        if (method, p) == ("GET", "repos/o/r"):
            return 200, {"full_name": "o/r", "permissions": {"push": True}}
        if method == "GET" and p.startswith("repos/o/r/issues?"):
            return 200, []
        if (method, p) == ("POST", "repos/o/r/issues"):
            body = body or {}
            return 201, gh_issue(5, body.get("labels", ()), body.get("title", ""), body.get("body", ""))
        if m := re.match(r"^repos/o/r/issues/(\d+)$", p):
            return 200, gh_issue(int(m.group(1)))
        return 200, []

    client = FakeClient(respond, hold=(("POST", "/repos/o/r/issues"),))
    rt = real_runtime(tmp_path, monkeypatch, client)
    rid = request_id(dispatched(rt, "post_request", {"type": "initiate", "title": "project p", "project": "p",
                                                      "actor": ACTOR}))
    dispatched(rt, "create_project", {"project": "p", "in_response_to": rid, "actor": ACTOR})
    handlers = action_handlers(rt)
    assert handlers["set_repo"](rt, "p", {"repo": "o/r"}).status == 200
    part = rt.ship
    real = part.facts
    part.facts = DelegatingFacts(real)
    manifest = record_path(rt.config.root, "p")

    # 1. the press is admitted and its run held at the issue create
    a = part.press_ship(rt, "p", {})
    assert a.status == 202, a
    R = a.body["run_id"]
    assert dict(a.body) == {"project": "p", "run_id": R, "started": True}
    assert RUN_ID.match(R), R
    try:
        assert client.arrived.wait(REAL_RUN_WINDOW_S), "the run never sent its issue create"

        # 2. a press of either kind while it is in flight names that run
        busy = {"error": busy_text("p", R)}
        o = part.press_ship(rt, "p", {})
        assert (o.status, dict(o.body)) == (409, busy)
        o = part.press_rollback(rt, "p", {})
        assert (o.status, dict(o.body)) == (409, busy)

        # 3. a write committed mid-run
        assert handlers["set_labels"](rt, "p", {"labels": ["x"]}).status == 200
        m1 = manifest.read_bytes()
    finally:
        client.release()

    # 4. the outcome is one appended Ship line; the interim write survives and the manifest is untouched
    view = wait_ended(part, "p", REAL_RUN_WINDOW_S)
    assert view["phase"] == "done", view
    assert manifest.read_bytes() == m1
    st = real.settings(rt.registry.writer("p").snapshot())
    assert st["issues"]["u1"]["number"] == 5
    assert st["labels"] == ("x",)
    assert st["next_seq"] == SHIP_SEQ_START + 1

    # 5. the next press reads its inputs from the Ship log after the end
    seen = []

    def plan(inputs, *args, **kwargs):
        seen.append(inputs)
        return PlanOutcome({"emitted": False, "project": "p", "repo": "o/r", "reason": "r"}, ())

    monkeypatch.setattr(ship_part, "ship_plan", plan)
    assert part.press_ship(rt, "p", {}).status == 202
    wait_ended(part, "p")
    (inputs,) = seen
    assert inputs.issues["u1"]["number"] == 5
    assert tuple(inputs.labels) == ("x",)
    assert inputs.next_seq == SHIP_SEQ_START + 1

    # 6. an unknown project's rollback is admitted and ends failed, asking GitHub nothing
    sent = len(client.requests)
    o = part.press_rollback(rt, "nope", {})
    assert o.status == 202, o
    assert dict(o.body) == {"project": "nope", "run_id": o.body["run_id"], "started": True}
    view = wait_ended(part, "nope")
    assert view["phase"] == "failed"
    assert view["error"] == "UnknownProject: 'nope'"
    assert len(client.requests) == sent


class DelegatedFacts:
    """A ShipFacts with every delegated condition failing or every one holding; take_cut answers cut."""

    def __init__(self, holding, cut=None):
        self.holding = holding
        self.cut = cut
        self.take_cut_by = []

    def delegation(self, state):
        return {"enabled": True, "granted_by": "g"} if self.holding else None

    def settings(self, state):
        return dict(SETTINGS, repo="o/r" if self.holding else None)

    def gate_conditions(self, state):
        if self.holding:
            return {"ok": True, "sound": True, "modules": True, "dead_ends": [], "cycle": None, "dead_blockers": []}
        return {"ok": False, "sound": False, "modules": False, "dead_ends": [], "cycle": None, "dead_blockers": []}

    def st_only(self, state):
        return self.holding

    def shipped(self, state):
        return self.holding

    def tests_assigned(self, state):
        return self.holding

    def latest_cut(self, state):
        return {"seq": 2, "members": [], "units": {}, "edges": {}} if self.holding else None

    def unit_waves(self, cut):
        return ()

    def drift_since(self, state, cut):
        return None

    def retired_citations(self, state, cut):
        return ()

    def dead_end_body(self, state, cut):
        return None

    def unit_cycle(self, cut):
        return None

    def serving_commit(self, runtime):
        return None

    def take_cut(self, state, at, note):
        self.take_cut_by.append(self)
        if isinstance(self.cut, BaseException):
            raise self.cut
        return self.cut


def test_delegated_refusals(tmp_path, monkeypatch):
    from mu_spec.codec import CUT_LOG_NAME, record_path
    from mu_spec.ship.part import CONDITIONS, answer_body
    from mu_spec.ship.plan import PlanOutcome
    from mu_spec.writer import Built

    from mu_spec.ship import part as ship_part

    no_lines = PlanOutcome({"emitted": False, "project": "p", "repo": "o/r", "reason": "r"}, ())
    monkeypatch.setattr(ship_part, "ship_plan", answering(no_lines))
    args = {"project": "p", "note": None, "actor": ACTOR}

    def taken():
        return Built({"project": "p", "cut": True, "seq": 3}, 200, appends={CUT_LOG_NAME: (CUT_LINE,)})

    def scratch(name):
        rt = real_runtime(tmp_path / name, monkeypatch, FakeClient(), doc=DOC)
        manifest = record_path(rt.config.root, "p")
        cut_log = manifest.parent / CUT_LOG_NAME
        return rt, manifest, cut_log

    def read(path):
        return path.read_bytes() if path.exists() else b""

    def log_count(rt):
        return len(list(rt.store.read_log(project="p")))

    rt, manifest, cut_log = scratch("one")
    part = rt.ship
    m0, c0, n0 = manifest.read_bytes(), read(cut_log), log_count(rt)

    def unchanged():
        assert manifest.read_bytes() == m0
        assert read(cut_log) == c0
        assert log_count(rt) == n0

    # 1. every condition failing: all reported in CONDITIONS order, reason form, nothing written
    part.facts = DelegatedFacts(holding=False)
    no_token = dataclasses.replace(rt, config=dataclasses.replace(rt.config, github_token=None))
    o = part.auto_ship(no_token, args)
    assert o.status == 409, o
    body = dict(o.body)
    assert set(body) == {"project", "allowed", "failed", "base_cut", "conditions", "reason"}
    assert body["project"] == "p"
    assert body["allowed"] is False
    assert list(body["failed"]) == ["delegation", "ready", "gates", "st_only", "shipped", "tests_assigned"]
    assert list(body["conditions"]) == list(CONDITIONS)
    assert body["reason"] == DELEGATED_TEXT
    assert "error" not in body
    unchanged()

    # 2. every condition holding, the cut refused: the cut's own answer last, no top-level reason
    c = raised("CUT_NO_MODULES", extra={"project": "p", "cut": False, "reason": None,
                                        "unimplemented": ["S-10 (was S-01)"]})
    part.facts = DelegatedFacts(holding=True, cut=c)
    o = part.auto_ship(rt, args)
    assert o.status == 409, o
    body = dict(o.body)
    assert set(body) == {"project", "allowed", "failed", "base_cut", "conditions", "cut"}
    assert body["allowed"] is True
    assert list(body["failed"]) == []
    assert body["base_cut"] == 2
    assert list(body)[-1] == "cut"
    assert body["cut"] == answer_body(c)
    assert body["cut"]["reason"] == c.message
    assert "error" not in body["cut"]
    assert "reason" not in body
    assert "error" not in body
    unchanged()

    # 3. every condition holding: one cut-log line appended, the manifest untouched, 202 with its cut_seq
    facts = DelegatedFacts(holding=True, cut=taken())
    part.facts = facts
    o = part.auto_ship(rt, args)
    assert o.status == 202, o
    body = dict(o.body)
    assert set(body) == {"project", "run_id", "started", "cut_seq", "base_cut", "conditions"}
    assert body["cut_seq"] == 3
    assert body["base_cut"] == 2
    assert manifest.read_bytes() == m0
    after = read(cut_log)
    assert after.startswith(c0)
    added = after[len(c0):].splitlines()
    assert len(added) == 1, added
    assert json.loads(added[0]) == CUT_LINE
    wait_ended(part, "p")

    # 6. the facts object turn 1 used is part.facts
    assert facts.take_cut_by and all(used is part.facts for used in facts.take_cut_by)

    # 4. a failed cut-log append escapes unchanged: no run, no record, nothing written
    rt, manifest, cut_log = scratch("four")
    part = rt.ship
    part.facts = DelegatedFacts(holding=True, cut=taken())
    m0, c0, n0 = manifest.read_bytes(), read(cut_log), log_count(rt)
    err = OSError("disk full")

    def append_lines(*a, **k):
        raise err

    monkeypatch.setattr(rt.store, "append_lines", append_lines)
    with pytest.raises(OSError) as escaped:
        part.auto_ship(rt, args)
    assert escaped.value is err
    assert part.run_view("p") is None
    unchanged()

    # 5. a person's press admitted between the turns: turn 2 answers EMISSION_RUNNING, the cut stays
    rt, manifest, cut_log = scratch("five")
    part = rt.ship
    part.facts = DelegatedFacts(holding=True, cut=taken())
    c0 = read(cut_log)
    held = threading.Event()

    def held_plan(*a, **k):
        assert held.wait(HOLD_WINDOW_S), "the held plan was never released"
        return no_lines

    monkeypatch.setattr(ship_part, "ship_plan", held_plan)
    real_writer = rt.registry.writer
    person = []

    class PressingWriter:
        def __init__(self, inner):
            self.inner = inner

        def snapshot(self):
            return self.inner.snapshot()

        def submit(self, call, handler):
            outcome = self.inner.submit(call, handler)
            if not person:
                person.append(part.press_ship(rt, "p", {}))
            return outcome

    monkeypatch.setattr(rt.registry, "writer", lambda p: None if real_writer(p) is None
                        else PressingWriter(real_writer(p)))
    try:
        o = part.auto_ship(rt, args)
        (pressed,) = person
        assert pressed.status == 202, pressed
        R = pressed.body["run_id"]
        assert (o.status, dict(o.body)) == (409, {"error": busy_text("p", R)})
        added = read(cut_log)[len(c0):].splitlines()
        assert len(added) == 1, added
        assert json.loads(added[0]) == CUT_LINE
    finally:
        held.set()
    wait_ended(part, "p")


def test_ship_read_and_members():
    from mu_spec.codec import ProjectRecord
    from mu_spec.ship.access import Identity
    from mu_spec.ship.part import (
        DASH_MEMBERS,
        DASH_READ_MEMBERS,
        SETTINGS_KEYS,
        SHIP_READ_MEMBERS,
        ShipPart,
        ShipRead,
        build_ship,
        ship_read,
    )
    from mu_spec.ship.plan import SHIP_SEQ_START
    from mu_spec.writes import reasonform
    from mu_spec.writes import ship as writes_ship

    from mu_spec.ship import part as ship_part

    # 1. the declared members and the re-exports
    client = FakeClient(hold=(("GET", "/user"),))
    part = build_ship(client)
    assert isinstance(part, ShipPart)
    assert DASH_MEMBERS == ("marker", "progress_view", "set_generation_source", "learn_identity")
    assert DASH_READ_MEMBERS == ("identity",)
    assert SHIP_READ_MEMBERS == ("marked_view", "identity")
    for name in DASH_MEMBERS + DASH_READ_MEMBERS + SHIP_READ_MEMBERS:
        assert hasattr(part, name), name
    assert set(DASH_READ_MEMBERS) <= set(SHIP_READ_MEMBERS)
    assert set(DASH_READ_MEMBERS) & set(DASH_MEMBERS) == set()
    for name in ("CONDITIONS", "CONDITION_FIELDS", "EVENT_KINDS", "DelegatedCutHandler", "OutcomeHandler",
                 "outcome_records", "failed_records"):
        assert getattr(ship_part, name) is getattr(writes_ship, name), name
    for name in ("REASON_FORM", "reason_body", "answer_body"):
        assert getattr(ship_part, name) is getattr(reasonform, name), name

    # 2. one facts object; settings as tuples in SETTINGS_KEYS order; delegation never a non-object
    assert part.facts is part.facts
    doc = {"project": "p", "slices": {}, "allocation": {}, "modules": {}, "repo": "o/r", "labels": ["x"],
           "pickup": []}
    state = ProjectRecord.from_doc("p", doc)
    settings = part.facts.settings(state)
    assert dict(settings) == {"repo": "o/r", "labels": ("x",), "pickup": (), "issues": {}, "orders": (),
                              "lineage_keyed": True, "next_seq": SHIP_SEQ_START}
    assert list(settings) == list(SETTINGS_KEYS)
    assert part.facts.delegation(state) is None
    assert part.facts.delegation(ProjectRecord.from_doc("p", dict(doc, delegation="yes"))) is None

    # 3. the progress view on a fresh part, and the one generation source
    assert part.progress_view("p") == (None, None)
    part.set_generation_source(lambda p: 1)
    with pytest.raises(RuntimeError):
        part.set_generation_source(lambda p: 2)

    # 4. ship_read never waits on GitHub; the login shows from a later read
    rt = SimpleNamespace(config=SimpleNamespace(github_token="t"), clock=SystemClock(), ship=part)
    try:
        sr = ship_read(rt, "p")
        assert not client._release.is_set()
        assert sr.login is None
        assert sr.type is None
        assert sr.run is None
        assert sr.marker == part.marker.value("p")
        wait_until(lambda: client.count("GET", "/user") >= 1, 5.0, "ship_read started no learning")
        assert client.count("GET", "/user") == 1
    finally:
        client.release(user_answer("octo", "User"))
    wait_until(lambda: part.identity() is not None, 5.0, "the login was not learned")
    later = ship_read(rt, "p")
    assert later.login == "octo"
    assert later.type == "User"

    # 5. on a read-only stand-in: the same answer, no other member read, no learning started
    client2 = FakeClient(user_answer("octo", "User"))
    P2 = build_ship(client2)
    notified = []
    P2.marker.observe(notified.append)
    asked = []

    class Stand:
        def marked_view(self, project):
            return P2.marked_view(project)

        def identity(self):
            return P2.identity()

        def __getattr__(self, name):
            if not name.startswith("__"):
                asked.append(name)
            raise AttributeError(name)

    rt2 = SimpleNamespace(config=SimpleNamespace(github_token="t"), clock=SystemClock(), ship=Stand())
    got = ship_read(rt2, "p")
    marker, run = P2.marked_view("p")
    ident = P2.identity()
    assert ident is None
    expected = ShipRead(marker=marker, run=run, login=None, type=None)
    for field in ("marker", "run", "login", "type"):
        assert getattr(got, field) == getattr(expected, field), field
    assert asked == []
    time.sleep(0.5)
    assert client2.count("GET", "/user") == 0

    # 6. one learn_identity, as the dashboard's snapshot taking makes it, learns the login
    P2.learn_identity(SimpleNamespace(config=SimpleNamespace(github_token="t"), clock=SystemClock(), ship=P2))
    wait_until(lambda: P2.identity() is not None, 5.0, "the dashboard's learning learned no login")
    assert P2.identity() == Identity("octo", "User")
    assert notified.count(None) == 1
