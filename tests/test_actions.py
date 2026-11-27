"""mu_spec.actions: the eight person-only action handlers and the one action dispatch (T-437..T-440).

bind_actions takes exactly the ACTIONS names and match_action reads an action route as (name,
project); no tool module reaches the actions module and the start never reads runtime.ship; the
Ship presses are the Ship part's own and handle_action answers only once its handler has returned.
Through handle_action each writer action refuses in the running unit's check order with nothing
written, set_labels stores as the running unit does, the cut's reason-form refusals come from the
Ship part's facts, and a failed store write escapes unchanged. Modules under test are imported
inside the test, so a module not yet written fails only the tests that need it.
"""

import ast
import collections.abc
import copy
import datetime
import json
import pathlib
import threading
import time

import pytest

from mu_spec.outside import FixedClock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTIONS_SOURCE = REPO_ROOT / "mu_spec" / "actions.py"
TOOLS_DIR = REPO_ROOT / "mu_spec" / "tools"
START_SOURCE = REPO_ROOT / "mu_spec" / "start.py"
AT = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
ACTOR = "a"
BLOCK_S = 0.3
RETURN_S = 2.0
HOLD_S = 10.0

D_MOD = {"project": "p", "slices": {}, "allocation": {"S": 2}, "modules": {"a.py": ["S-10 (was S-01)"]},
         "repo": None, "labels": [], "pickup": []}
SOUND = {"sound": True, "complete": True, "clean": True, "findings": [], "cosmetic": [], "slice_findings": []}
UNSOUND = dict(SOUND, sound=False, findings=[{"kind": "orphan", "id": "B-53 (was B-09)"}])
SPEC_IDS = ("S-10 (was S-01)", "S-17 (was S-02)")
UNKNOWN = {"error": "unknown project 'nope'"}
UNSOUND_TEXT = "graph is unsound -- it contains orphaned entries"
NO_MODULES_TEXT = ("no modules are declared, so there is nothing to cut. A work unit is a set of files and the "
                   "entries they implement; with no module map every entry is unimplemented and no unit has "
                   "anything to write")


class FakeFacts:
    """A ShipFacts recording every call by name with its positional arguments.

    gate_report answers report(state); take_cut answers cut(*args) when cut is callable, else cut.
    """

    def __init__(self, report=lambda state: SOUND, cut=None):
        self.report = report
        self.cut = cut
        self.calls = []

    def gate_report(self, state):
        self.calls.append(("gate_report", (state,)))
        return copy.deepcopy(self.report(state))

    def spec_ids(self, state):
        self.calls.append(("spec_ids", (state,)))
        return SPEC_IDS

    def take_cut(self, *args):
        self.calls.append(("take_cut", args))
        return self.cut(*args) if callable(self.cut) else self.cut

    def called(self, name):
        return [args for called, args in self.calls if called == name]


class FakeShip:
    """A Ship part recording every press as (project, body); its facts is the given fake."""

    def __init__(self, facts):
        self.facts = facts
        self.ships = []
        self.rollbacks = []

    def press_ship(self, runtime, project, body):
        from mu_spec.writer import Outcome

        self.ships.append((project, copy.deepcopy(dict(body))))
        return Outcome(202, {"project": project, "run_id": "r", "started": True})

    def press_rollback(self, runtime, project, body):
        from mu_spec.writer import Outcome

        self.rollbacks.append((project, copy.deepcopy(dict(body))))
        return Outcome(202, {"project": project, "run_id": "r", "started": True})


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


def runtime_over(base, monkeypatch, ship, doc=None):
    """build_runtime over a scratch state directory under base, its Ship part ship; with doc, the
    directory holds project 'p' with that manifest, written at record_path."""
    from mu_spec.codec import ProjectRecord, encode_record, record_path
    from mu_spec.registry import build_runtime

    from mu_spec.outside import PROJECTS_SUBDIR

    state = base / "state"
    cwd = base / "cwd"
    cwd.mkdir(parents=True)
    if doc is not None:
        manifest = record_path(state / PROJECTS_SUBDIR, "p")
        manifest.parent.mkdir(parents=True)
        manifest.write_bytes(encode_record(ProjectRecord.from_doc("p", copy.deepcopy(doc))))
    monkeypatch.chdir(cwd)
    set_environment(monkeypatch, port="9100", state_dir=str(state))
    return build_runtime(ship_builder=lambda client: ship, clock=FixedClock(AT), github=FakeGitHub())


def request_id(body):
    if isinstance(body, collections.abc.Mapping):
        for field in ("request_id", "id"):
            if field in body:
                return body[field]
        created = body.get("created")
        if isinstance(created, (list, tuple)) and len(created) == 1:
            return created[0]
    pytest.fail(f"post_request answer names no request id: {body!r}")


def create(rt, project):
    """Project created through rt.registry by the create_project tool, nothing else written to it."""
    from mu_spec.tools.catalogue import dispatch

    o = dispatch(rt, "post_request", {"type": "initiate", "title": f"project {project}", "project": project,
                                      "actor": ACTOR})
    assert 200 <= o.status < 300, f"post_request refused: {o!r}"
    o = dispatch(rt, "create_project", {"project": project, "in_response_to": request_id(plain(o.body)),
                                        "actor": ACTOR})
    assert 200 <= o.status < 300, f"create_project refused: {o!r}"


def plain(body):
    """body as JSON would carry it: mappings as dicts, sequences as lists, key order kept."""
    def default(value):
        if isinstance(value, collections.abc.Mapping):
            return dict(value)
        return list(value)

    return json.loads(json.dumps(body, default=default))


def route_of(name, project):
    """(method, path) of the action name's declared route for project."""
    from mu_spec.declarations import ACTIONS

    (spec,) = [s for s in ACTIONS if s.name == name]
    return spec.method, spec.path.replace("{project}", project)


def module_name(path: pathlib.Path) -> str:
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imported_modules(tree: ast.AST, module: str, is_package: bool) -> list[str]:
    """Every module an Import or ImportFrom in tree may bring in, relative ones resolved.

    `import X` gives X; `from X import name` gives X and X.name, since name may be a submodule.
    """
    package = module if is_package else module.rpartition(".")[0]
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = parts[: len(parts) - node.level + 1]
                source = ".".join(base + ([node.module] if node.module else []))
            else:
                source = node.module or ""
            names.append(source)
            names.extend(f"{source}.{alias.name}" for alias in node.names)
    return names


def names_module(names: list[str], target: str) -> bool:
    return any(name == target or name.startswith(target + ".") for name in names)


def test_binding_and_isolation(tmp_path, monkeypatch):
    from mu_spec.actions import (
        BOUND_HOLDERS,
        SHIP_READER,
        action_handlers,
        bind_actions,
        handle_action,
        match_action,
    )
    from mu_spec.declarations import ACTIONS, RegistryError
    from mu_spec.writer import Outcome
    from sweeps.source import GITHUB_HOLDERS

    names = [spec.name for spec in ACTIONS]

    # 1. exactly the ACTIONS names bind; seven, a ninth, or one renamed raise
    full = {n: (lambda rt, p, b: None) for n in names}
    assert set(bind_actions(full)) == set(names)
    with pytest.raises(RegistryError):
        bind_actions({n: f for n, f in full.items() if n != names[-1]})
    with pytest.raises(RegistryError):
        bind_actions({**full, "extra": lambda rt, p, b: None})
    renamed = {("emit_ticket" if n == "emit_tickets" else n): f for n, f in full.items()}
    assert len(renamed) == 8
    with pytest.raises(RegistryError):
        bind_actions(renamed)

    # 2. every action route reads as (name, project); a tool path and an unknown path match nothing
    for spec in ACTIONS:
        assert match_action(spec.method, spec.path.replace("{project}", "p")) == (spec.name, "p"), spec
    assert match_action("GET", "/projects/p/emit") is None
    assert match_action("POST", "/nowhere") is None

    # 3. no tool module imports mu_spec.actions
    for path in sorted(TOOLS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = imported_modules(tree, module_name(path), path.name == "__init__.py")
        assert not names_module(imported, "mu_spec.actions"), f"{path} imports mu_spec.actions"

    # 4. mu_spec.actions imports nothing from mu_spec.github and declares no check order
    tree = ast.parse(ACTIONS_SOURCE.read_text(encoding="utf-8"), filename=str(ACTIONS_SOURCE))
    assert not names_module(imported_modules(tree, "mu_spec.actions", False), "mu_spec.github")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for inner in ast.walk(node):
                targets = []
                if isinstance(inner, ast.Assign):
                    targets = inner.targets
                elif isinstance(inner, (ast.AnnAssign, ast.AugAssign)):
                    targets = [inner.target]
                for target in targets:
                    named = (target.id if isinstance(target, ast.Name)
                             else target.attr if isinstance(target, ast.Attribute) else None)
                    assert named != "check_order", f"class {node.name} defines check_order"
    assert SHIP_READER == "mu_spec.actions"
    assert SHIP_READER in GITHUB_HOLDERS
    assert len(BOUND_HOLDERS) == 4

    # 5. the start never reads an attribute named ship
    tree = ast.parse(START_SOURCE.read_text(encoding="utf-8"), filename=str(START_SOURCE))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "ship", f"mu_spec/start.py reads .ship at line {node.lineno}"

    # 6. emit_tickets is the Ship part's press_ship
    ship = FakeShip(FakeFacts())
    rt = runtime_over(tmp_path, monkeypatch, ship)
    h = action_handlers(rt)
    assert set(h) == set(names)
    h["emit_tickets"](rt, "p", {"waves": 1})
    assert ship.ships == [("p", {"waves": 1})]
    assert ship.rollbacks == []

    # 7. a tool path and an unknown path are no action
    bound = bind_actions(h)
    assert handle_action(rt, bound, "GET", "/projects/p/emit", {}) is None
    assert handle_action(rt, bound, "POST", "/nowhere", {}) is None

    # 8. handle_action returns only once its handler has
    ev = threading.Event()
    answer = Outcome(200, {"ok": True})

    def set_repo(runtime, project, body):
        ev.wait(HOLD_S)
        return answer

    waiting = bind_actions({**full, "set_repo": set_repo})
    method, path = route_of("set_repo", "p")
    returned = []
    t = threading.Thread(target=lambda: returned.append(handle_action(rt, waiting, method, path, {})))
    t.start()
    try:
        time.sleep(BLOCK_S)
        assert t.is_alive() and returned == [], "handle_action returned before its handler did"
    finally:
        ev.set()
        t.join(RETURN_S)
    assert not t.is_alive(), "handle_action did not return once its handler had"
    assert len(returned) == 1 and returned[0] is answer


def test_check_orders(tmp_path, monkeypatch):
    from mu_spec.actions import action_handlers, bind_actions, handle_action
    from mu_spec.codec import record_path

    rt = runtime_over(tmp_path, monkeypatch, FakeShip(FakeFacts()))
    create(rt, "p")
    bound = bind_actions(action_handlers(rt))
    manifest = record_path(rt.config.root, "p")
    m0 = manifest.read_bytes()
    n0 = len(list(rt.store.read_log()))

    def act(name, project, body):
        method, path = route_of(name, project)
        o = handle_action(rt, bound, method, path, body)
        assert o is not None, (name, project)
        return o.status, plain(o.body)

    # the checks before the project lookup, then the unknown project, then the rest
    assert act("set_repo", "nope", {}) == (400, {"error": "'repo' is required: 'owner/name', a github.com URL "
                                                          "to take one from, or null to clear it"})
    assert act("set_repo", "nope", {"repo": 5}) == (400, {"error": "'repo' must be a string, or null to clear it"})
    assert act("set_repo", "nope", {"repo": "not a repo"}) == (404, UNKNOWN)
    assert act("set_labels", "nope", {}) == (400, {"error": "'labels' is required: a list of label names, "
                                                            "or [] to clear them"})
    assert act("set_labels", "nope", {"labels": [""]}) == (404, UNKNOWN)
    assert act("set_delegation", "nope", {"enabled": "yes"}) == (400, {"error": "'enabled' must be true or false"})
    assert act("set_delegation", "nope", {"enabled": True}) == (404, UNKNOWN)
    for name in ("reject", "ratify", "cut_units"):
        assert act(name, "nope", {}) == (404, UNKNOWN), name

    # on a known project the checks after the lookup answer
    waiting = (400, {"error": "no proposal is waiting for ratification"})
    assert act("ratify", "p", {}) == waiting
    assert act("reject", "p", {}) == waiting
    assert act("set_repo", "p", {"repo": "not a repo"}) == (
        400, {"error": "'not a repo' is not a repository: expected 'owner/name', or a github.com URL to take one from"})
    assert act("set_labels", "p", {"labels": [""]}) == (400, {"error": "a label cannot be empty"})

    # nothing written or recorded
    assert manifest.read_bytes() == m0
    assert len(list(rt.store.read_log())) == n0


def test_labels_and_cut_refusals(tmp_path, monkeypatch):
    from mu_spec.actions import action_handlers, bind_actions, handle_action

    facts = FakeFacts(report=lambda state: UNSOUND if state.name == "u" else SOUND)
    ship = FakeShip(facts)
    rt = runtime_over(tmp_path, monkeypatch, ship)
    for project in ("p", "u", "m"):
        create(rt, project)
    bound = bind_actions(action_handlers(rt))

    def act(name, project, body):
        method, path = route_of(name, project)
        o = handle_action(rt, bound, method, path, body)
        assert o is not None, (name, project)
        return o.status, plain(o.body)

    def stored(project):
        return plain(rt.registry.writer(project).snapshot().doc)

    # 0. labels and pickup set
    status, _ = act("set_labels", "p", {"labels": ["old"], "pickup": ["claimed"]})
    assert status == 200
    assert stored("p")["labels"] == ["old"]
    assert stored("p")["pickup"] == ["claimed"]

    # 1. trimmed, then first occurrences kept; an absent pickup unchanged
    assert act("set_labels", "p", {"labels": [" a ", "a", "b"]}) == (
        200, {"project": "p", "labels": ["a", "b"], "pickup": ["claimed"]})
    assert stored("p")["labels"] == ["a", "b"]
    assert stored("p")["pickup"] == ["claimed"]

    # 2. 51 characters refuse with nothing stored
    s = copy.deepcopy(stored("p"))
    assert act("set_labels", "p", {"labels": ["x" * 51]}) == (
        400, {"error": "'" + "x" * 51 + "' is longer than 50 characters"})
    assert stored("p") == s

    # 3. 50 characters store
    status, _ = act("set_labels", "p", {"labels": ["x" * 50]})
    assert status == 200
    assert stored("p")["labels"] == ["x" * 50]

    # 4. pickup is held to the label rules
    assert act("set_labels", "p", {"labels": ["a"], "pickup": [""]}) == (400, {"error": "a label cannot be empty"})
    assert act("set_labels", "p", {"labels": ["a"], "pickup": "c"}) == (
        400, {"error": "'labels' must be a list of strings"})
    assert stored("p")["labels"] == ["x" * 50]

    # 5. a clear holds no label; pickup unchanged
    status, body = act("set_labels", "p", {"labels": None})
    assert status == 200
    assert body["labels"] == stored("p")["labels"]
    assert not stored("p")["labels"]
    assert stored("p")["pickup"] == ["claimed"]

    # 6. an unsound graph refuses in reason form from the Ship part's facts
    s = copy.deepcopy(stored("u"))
    status, body = act("cut_units", "u", {})
    assert status == 409, body
    assert list(body)[:3] == ["project", "cut", "reason"]
    assert body["project"] == "u"
    assert body["cut"] is False
    assert body["reason"] == UNSOUND_TEXT
    assert body["gates"] == UNSOUND
    assert "error" not in body
    assert stored("u") == s
    assert facts.called("take_cut") == []

    # 7. a sound graph with no modules declared refuses with the spec ids unimplemented
    status, body = act("cut_units", "m", {})
    assert status == 409, body
    assert body["reason"] == NO_MODULES_TEXT
    assert body["unimplemented"] == list(SPEC_IDS)
    assert "gates" not in body
    assert "error" not in body
    assert facts.called("take_cut") == []
    assert ship.ships == [] and ship.rollbacks == []


def cut_line(at, note):
    """The cut-log line the fake take_cut forms for (at, note)."""
    return {"seq": 1, "at": at.timestamp(), "note": note, "members": [], "units": {}, "edges": {},
            "unimplemented": [], "stale_modules": [], "untested": [], "unimplemented_tests": [],
            "unfollowed_tests": [], "dangling": []}


def test_failed_store_write_escapes(tmp_path, monkeypatch):
    from mu_spec.actions import action_handlers, bind_actions, handle_action
    from mu_spec.codec import CUT_LOG_NAME, record_path
    from mu_spec.writer import Built

    def taken(state, at, note):
        return Built({"project": "p", "cut": True, "seq": 1}, 200, appends={CUT_LOG_NAME: (cut_line(at, note),)})

    rt = runtime_over(tmp_path, monkeypatch, FakeShip(FakeFacts(cut=taken)), doc=D_MOD)
    bound = bind_actions(action_handlers(rt))
    manifest = record_path(rt.config.root, "p")
    cut_log = manifest.parent / CUT_LOG_NAME

    def read(path):
        return path.read_bytes() if path.exists() else b""

    def records():
        return len(rt.store.read_log(project="p"))

    m0, c0, n0 = manifest.read_bytes(), read(cut_log), records()

    def act(name, body):
        method, path = route_of(name, "p")
        return handle_action(rt, bound, method, path, body)

    def raising(err):
        def fail(*args, **kwargs):
            raise err

        return fail

    # 1. a failed cut-log append escapes unchanged with nothing written or recorded
    err = OSError("disk full")
    with monkeypatch.context() as patch:
        patch.setattr(rt.store, "append_lines", raising(err))
        with pytest.raises(OSError) as escaped:
            act("cut_units", {"note": "n"})
        assert escaped.value is err
    assert manifest.read_bytes() == m0
    assert read(cut_log) == c0
    assert records() == n0

    # 2. a failed manifest save escapes unchanged with nothing stored or recorded
    err2 = OSError("disk full")
    with monkeypatch.context() as patch:
        patch.setattr(rt.store, "write_record", raising(err2))
        with pytest.raises(OSError) as escaped:
            act("set_repo", {"repo": "o/r"})
        assert escaped.value is err2
    assert manifest.read_bytes() == m0
    assert rt.registry.writer("p").snapshot().doc["repo"] is None
    assert records() == n0

    # 3. with the store working: the cut appends one line and leaves the manifest, the repo is stored
    o = act("cut_units", {"note": "n"})
    assert o is not None and o.status == 200, o
    assert manifest.read_bytes() == m0
    after = read(cut_log)
    assert after.startswith(c0)
    assert len(after[len(c0):].splitlines()) == 1
    o = act("set_repo", {"repo": "o/r"})
    assert o is not None and (o.status, plain(o.body)) == (200, {"project": "p", "repo": "o/r"})
    assert records() > n0
