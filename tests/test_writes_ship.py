"""The Ship part's write handlers: OutcomeHandler and DelegatedCutHandler in mu_spec.writes.ship (T-425..T-427).

The outcome turn refuses an empty line set, stamps each Ship-log line with 'at' right after 'seq'
and answers them as a lines turn on the Ship log alone, leaving doc and ids untouched; its records
are given only after the append lands, its generation noted first, and the ending's records are
formed by ending_kind, outcome_records and failed_records. Through a real writer the lines land at
the end of the Ship log with the manifest bytes unchanged, and a failed append leaves nothing. The
delegated cut turn evaluates every condition and reports them all, answers a refused cut as the
conditions answer with that cut's own body last, and returns take_cut's Built unchanged. Modules
under test are imported inside the test, so a module not yet written fails only the tests that
need it.
"""

import ast
import collections.abc
import copy
import datetime
import json
import pathlib

import pytest

from mu_spec.outside import FixedClock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SHIP_WRITES_SOURCE = REPO_ROOT / "mu_spec" / "writes" / "ship.py"
SHIP_WRITES_PACKAGE = "mu_spec.writes"
AT = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
IMPORT_MODULES = ("mu_spec.ship.runs", "mu_spec.ship.plan", "mu_spec.writer", "mu_spec.refusals",
                  "mu_spec.codec", "mu_spec.writes.reasonform")
# what the module may import beyond IMPORT_MODULES
IMPORT_ALLOWED = ("__future__", "typing", "dataclasses", "datetime", "copy")
# what the module may never import, nor anything under it
IMPORT_FORBIDDEN = ("mu_spec.github", "mu_spec.ship.part")
DELEGATED_TEXT = ("refused, nothing was written: delegation, ready, gates, st_only, shipped, "
                  "tests_assigned did not hold. Anything outside the delegation is a person's press.")

D_OUT = {"project": "p", "slices": {}, "allocation": {}, "modules": {}, "repo": "o/r", "labels": [],
         "pickup": [], "zz_probe": [1]}
D_CUT = {"project": "p", "slices": {}, "allocation": {}, "modules": {"a.py": ["S-10 (was S-01)"]},
         "repo": "o/r", "labels": [], "pickup": []}
SETTINGS = {"repo": "o/r", "labels": [], "pickup": [], "issues": {}, "orders": [], "lineage_keyed": True,
            "next_seq": 1}
GRANT = {"enabled": True, "granted_by": "g"}
GATES_HOLDING = {"ok": True, "sound": True, "modules": True, "dead_ends": [], "cycle": None, "dead_blockers": []}
GATES_FAILING = {"ok": False, "sound": False, "modules": False, "dead_ends": [], "cycle": None, "dead_blockers": []}
LATEST_CUT = {"seq": 2, "members": [], "units": {}, "edges": {}}


class FakeIds:
    """An IdAllocator over doc recording every allocation without touching doc."""

    def __init__(self, doc):
        self._doc = doc
        self.allocated = []

    @property
    def doc(self):
        return self._doc

    def next(self, prefix):
        self.allocated.append(prefix)
        return f"{prefix}-{len(self.allocated)}"


class FakeFacts:
    """A ShipFacts whose answers are set per case; take_cut records its positional arguments and
    raises cut when it is an exception, answering it otherwise."""

    def __init__(self, holding):
        self.grant = copy.deepcopy(GRANT) if holding else None
        self.repo = "o/r" if holding else None
        self.gates = GATES_HOLDING if holding else GATES_FAILING
        self.flags = holding
        self.cut = None
        self.take_cut_calls = []

    def delegation(self, state):
        return copy.deepcopy(self.grant)

    def settings(self, state):
        return dict(SETTINGS, repo=self.repo)

    def gate_conditions(self, state):
        return copy.deepcopy(self.gates)

    def st_only(self, state):
        return self.flags

    def shipped(self, state):
        return self.flags

    def tests_assigned(self, state):
        return self.flags

    def latest_cut(self, state):
        return copy.deepcopy(LATEST_CUT)

    def take_cut(self, *args):
        self.take_cut_calls.append(args)
        if isinstance(self.cut, BaseException):
            raise self.cut
        return self.cut


def refused(name, **kwargs):
    """The Refused refuse(name, **kwargs) raises."""
    from mu_spec.refusals import Refused, refuse

    with pytest.raises(Refused) as caught:
        refuse(name, **kwargs)
    return caught.value


def raised_by(fn, *args):
    """The Refused fn(*args) raises; fails when it raises none."""
    from mu_spec.refusals import Refused

    with pytest.raises(Refused) as caught:
        fn(*args)
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


def imported_modules(tree: ast.AST, package: str) -> list[tuple[str, ...]]:
    """For every name an Import or ImportFrom in tree brings in, the modules it may come from.

    `import X` gives (X,); `from X import name` gives (X, X.name), since name may be a submodule;
    relative ones are resolved against package.
    """
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend((alias.name,) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = parts[: len(parts) - node.level + 1]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            names.extend((module, f"{module}.{alias.name}") for alias in node.names)
    return names


def import_permitted(candidates: tuple[str, ...]) -> bool:
    """Whether any module an imported name may come from is in IMPORT_MODULES or IMPORT_ALLOWED."""
    return any(module in IMPORT_MODULES or module.split(".")[0] in IMPORT_ALLOWED
               for module in candidates)


def import_forbidden(candidates: tuple[str, ...]) -> bool:
    """Whether a module an imported name may come from is, or lies under, an IMPORT_FORBIDDEN one."""
    return any(module == f or module.startswith(f + ".") for module in candidates for f in IMPORT_FORBIDDEN)


def test_outcome_handler(monkeypatch):
    from mu_spec.codec import EVENT_KINDS as CODEC_EVENT_KINDS
    from mu_spec.codec import MUTATION_KIND, SHIP_LOG_NAME, ProjectRecord
    from mu_spec.ship.plan import ROLLBACK_LINE_FIELDS, SHIP_LINE_FIELDS
    from mu_spec.writer import WriteCall
    from mu_spec.writes import ship
    from mu_spec.writes.ship import (
        EVENT_KINDS,
        OutcomeHandler,
        ending_kind,
        failed_records,
        outcome_records,
    )

    from mu_spec.ship.runs import RunRegistry

    clock = FixedClock(AT)
    runs = RunRegistry(clock)
    state = ProjectRecord.from_doc("p", copy.deepcopy(D_OUT))
    at = AT
    assert at.timestamp() == 1767225600.0
    call = WriteCall("emit_tickets", {}, "a", None, False)
    r = runs.admit("p", "person_ship", None, 0)
    result = {"emitted": True, "project": "p"}

    def fresh():
        doc = copy.deepcopy(D_OUT)
        return doc, copy.deepcopy(doc), FakeIds(doc)

    # 1. an empty line set is refused at construction
    with pytest.raises(ValueError):
        OutcomeHandler(r, runs, result, ())

    # 2. one Ship line: stamped with 'at' right after 'seq', a lines turn on the Ship log alone
    lines = ({"seq": 4, "cut_seq": 3, "repo": "o/r", "issues": {}, "wired": ["S-396 (was S-04)<-S-4-T"],
              "keys": "lineage"},)
    sent = copy.deepcopy(lines)
    h = OutcomeHandler(r, runs, result, lines)
    assert tuple(h.appended()) == ()
    doc, before, ids = fresh()
    built = h.build(state, call, doc, ids, at)
    assert built.status == 200
    assert dict(built.body) == result
    assert set(built.appends) == {SHIP_LOG_NAME}
    assert list(built.appends[SHIP_LOG_NAME]) == list(h.appended())
    assert doc == before
    assert ids.allocated == []
    assert list(SHIP_LINE_FIELDS) == ["seq", "at", "cut_seq", "repo", "issues", "wired", "keys"]
    (line,) = h.appended()
    assert list(line) == list(SHIP_LINE_FIELDS)
    assert line["at"] == 1767225600.0
    assert {key: value for key, value in line.items() if key != "at"} == sent[0]

    # 3. a Ship line then a rollback line: both, in that order, absent keys staying absent
    assert list(ROLLBACK_LINE_FIELDS) == ["kind", "seq", "at", "cut_seq", "repo", "undone", "closed", "failed"]
    pair = ({"seq": 4, "cut_seq": 3, "repo": "o/r", "issues": {}, "wired": []},
            {"kind": "rollback", "seq": 5, "cut_seq": 3, "repo": "o/r", "undone": [],
             "closed": {"S-410 (was S-08)": 15}, "failed": []})
    h3 = OutcomeHandler(r, runs, result, copy.deepcopy(pair))
    doc, before, ids = fresh()
    built3 = h3.build(state, call, doc, ids, at)
    first, second = h3.appended()
    assert list(first) == ["seq", "at", "cut_seq", "repo", "issues", "wired"]
    assert list(second) == list(ROLLBACK_LINE_FIELDS)
    assert {key: value for key, value in first.items() if key != "at"} == pair[0]
    assert {key: value for key, value in second.items() if key != "at"} == pair[1]
    assert list(built3.appends[SHIP_LOG_NAME]) == [first, second]
    assert doc == before

    # 4. log_records notes the commit before building any record
    order = []
    runs.set_generation_source(lambda p: (order.append("note"), 7)[1])
    real_new_log_record = ship.new_log_record

    def recording_new_log_record(*args, **kwargs):
        kind = kwargs["kind"] if "kind" in kwargs else args[0]
        order.append("record:" + kind)
        return real_new_log_record(*args, **kwargs)

    h4 = OutcomeHandler(r, runs, result, copy.deepcopy(lines))
    doc, before, ids = fresh()
    built4 = h4.build(state, call, doc, ids, at)
    with monkeypatch.context() as patch:
        patch.setattr(ship, "new_log_record", recording_new_log_record)
        records = h4.log_records(call, built4, at)
    assert order == ["note", "record:" + MUTATION_KIND, "record:tickets_emitted"]
    assert [record.kind for record in records] == [MUTATION_KIND, "tickets_emitted"]
    assert r.commit_generation == 7
    assert all(record.seq is None for record in records)
    event = records[-1]
    assert holds_value(event.doc.get("refs"), r.run_id) or holds_value(event.doc.get("facts"), r.run_id)

    # 5. the event kind follows the ending
    def run_of(kind):
        return RunRegistry(clock).admit("p", kind, None, 0)

    assert ending_kind(run_of("person_ship"), {"emitted": True}) == "tickets_emitted"
    assert ending_kind(run_of("delegated_ship"), {"emitted": True}) == "tickets_emitted"
    assert ending_kind(run_of("person_ship"), {"emitted": False}) == "ship_failed"
    assert ending_kind(run_of("person_ship"), None) == "ship_failed"
    assert ending_kind(run_of("rollback"), {"rolled_back": True}) == "tickets_rolled_back"

    # 6. a failed append records the ship_failed event alone; a plan that raised, the change line then it
    for kind in ("person_ship", "rollback"):
        failed = failed_records(run_of(kind), "OSError: disk full", at)
        assert [record.kind for record in failed] == ["ship_failed"], kind
        assert all(record.seq is None for record in failed)
    ended = outcome_records(run_of("person_ship"), None, at)
    assert [record.kind for record in ended] == [MUTATION_KIND, "ship_failed"]
    assert all(record.seq is None for record in ended)

    # 7. the events, all in the codec's vocabulary
    assert set(EVENT_KINDS) == {"tickets_emitted", "ship_failed", "tickets_rolled_back"}
    assert set(EVENT_KINDS) <= set(CODEC_EVENT_KINDS)


def set_environment(monkeypatch, **texts: str) -> None:
    """Every configuration variable absent but those given, by field name."""
    from mu_spec.outside import ENV_NAMES

    for name in ENV_NAMES.values():
        monkeypatch.delenv(name, raising=False)
    for field, text in texts.items():
        monkeypatch.setenv(ENV_NAMES[field], text)


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


def test_outcome_through_writer(tmp_path, monkeypatch):
    from mu_spec.codec import (
        MUTATION_KIND,
        SHIP_LOG_NAME,
        ProjectRecord,
        encode_record,
        record_path,
    )
    from mu_spec.registry import build_runtime
    from mu_spec.writer import WriteCall
    from mu_spec.writes.ship import OutcomeHandler

    from mu_spec.outside import PROJECTS_SUBDIR
    from mu_spec.ship.runs import RunRegistry

    D = tmp_path / "state"
    for project in ("p", "q"):
        manifest = record_path(D / PROJECTS_SUBDIR, project)
        manifest.parent.mkdir(parents=True)
        manifest.write_bytes(encode_record(ProjectRecord.from_doc(project, dict(copy.deepcopy(D_OUT),
                                                                                project=project))))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    set_environment(monkeypatch, port="9100", state_dir=str(D))
    rt = build_runtime(ship_builder=lambda client: object(), clock=FixedClock(AT), github=FakeGitHub())
    runs = RunRegistry(rt.clock)
    runs.set_generation_source(lambda p: 7)

    def read(path):
        return path.read_bytes() if path.exists() else b""

    manifests = {project: record_path(rt.config.root, project) for project in ("p", "q")}
    ship_logs = {project: manifests[project].parent / SHIP_LOG_NAME for project in ("p", "q")}
    m0 = {project: manifests[project].read_bytes() for project in ("p", "q")}
    s0 = {project: read(ship_logs[project]) for project in ("p", "q")}
    n0 = {project: len(rt.store.read_log(project=project)) for project in ("p", "q")}
    L1 = {"seq": 1, "cut_seq": 1, "repo": "o/r", "issues": {}, "wired": []}
    L2 = {"seq": 2, "cut_seq": 1, "repo": "o/r", "issues": {}, "wired": ["a<-b"]}
    result = {"emitted": True, "project": "p"}
    call = WriteCall("emit_tickets", {}, "a", None, False)

    # 1. the lines land at the end of the Ship log, the manifest bytes as they were, the records after
    r = runs.admit("p", "person_ship", None, 0)
    h = OutcomeHandler(r, runs, result, (copy.deepcopy(L1), copy.deepcopy(L2)))
    out = rt.registry.writer("p").submit(call, h)
    assert out.status == 200, out
    assert dict(out.body) == result
    assert manifests["p"].read_bytes() == m0["p"]
    s1 = read(ship_logs["p"])
    assert s1.startswith(s0["p"])
    added = s1[len(s0["p"]):].splitlines()
    assert len(added) == 2, added
    assert [json.loads(text) for text in added] == [dict(line) for line in h.appended()]
    assert all(json.loads(text)["at"] == rt.clock.now().timestamp() for text in added)
    assert r.commit_generation == 7
    new = rt.store.read_log(project="p")[n0["p"]:]
    assert [record.kind for record in new] == [MUTATION_KIND, "tickets_emitted"]

    # 2. a failed append raises out of submit with no note, no record and no line
    r2 = runs.admit("q", "person_ship", None, 0)
    err = OSError("disk full")

    def failing(*args, **kwargs):
        raise err

    monkeypatch.setattr(rt.store, "append_lines", failing)
    with pytest.raises(OSError) as raised:
        rt.registry.writer("q").submit(call, OutcomeHandler(r2, runs, result, (copy.deepcopy(L1),)))
    assert raised.value is err
    assert r2.commit_generation is None
    assert manifests["q"].read_bytes() == m0["q"]
    assert read(ship_logs["q"]) == s0["q"]
    assert len(rt.store.read_log(project="q")) == n0["q"]


def test_delegated_cut_handler():
    from mu_spec.codec import CUT_LOG_NAME, ProjectRecord
    from mu_spec.writer import Built, WriteCall
    from mu_spec.writes.reasonform import answer_body
    from mu_spec.writes.ship import CONDITION_FIELDS, CONDITIONS, DelegatedCutHandler

    from mu_spec.ship.runs import RunRegistry

    clock = FixedClock(AT)
    runs = RunRegistry(clock)
    state = ProjectRecord.from_doc("p", copy.deepcopy(D_CUT))
    at = AT
    call = WriteCall("auto_ship", {"project": "p", "note": "n"}, "a", None, False)

    def fresh():
        doc = copy.deepcopy(D_CUT)
        return doc, copy.deepcopy(doc), FakeIds(doc)

    # 1. every condition failing: all are reported, in CONDITIONS order, and nothing is written
    facts = FakeFacts(holding=False)
    doc, before, ids = fresh()
    h = DelegatedCutHandler(facts, runs, False)
    e = raised_by(h.build, state, call, doc, ids, at)
    assert e.refusal.name == "DELEGATED_REFUSED"
    assert h.refused is e
    assert e.extra["failed"] == list(CONDITIONS)
    assert list(e.extra["conditions"]) == list(CONDITIONS)
    for name in CONDITIONS:
        fields = ["ok"] + list(CONDITION_FIELDS[name])
        assert list(e.extra["conditions"][name])[:len(fields)] == fields, name
    assert e.message == DELEGATED_TEXT
    b = answer_body(e)
    assert b["project"] == "p"
    assert b["allowed"] is False
    assert b["reason"] == e.message
    assert "error" not in b
    assert doc == before
    assert ids.allocated == []
    assert facts.take_cut_calls == []

    def failed_when(facts, registry, token_held):
        doc, before, ids = fresh()
        e = raised_by(DelegatedCutHandler(facts, registry, token_held).build, state, call, doc, ids, at)
        assert e.refusal.name == "DELEGATED_REFUSED"
        assert doc == before
        assert facts.take_cut_calls == []
        return e

    # 2. delegation absent, or present with enabled false, fails delegation alone
    facts = FakeFacts(holding=True)
    facts.grant = None
    assert failed_when(facts, RunRegistry(clock), True).extra["failed"] == ["delegation"]
    facts = FakeFacts(holding=True)
    facts.grant = {"enabled": False, "granted_by": "g"}
    assert failed_when(facts, RunRegistry(clock), True).extra["failed"] == ["delegation"]
    # a run in flight fails ready on idle
    busy = RunRegistry(clock)
    busy.admit("p", "person_ship", None, 0)
    facts = FakeFacts(holding=True)
    e = failed_when(facts, busy, True)
    assert e.extra["failed"] == ["ready"]
    assert e.extra["conditions"]["ready"]["ok"] is False
    assert e.extra["conditions"]["ready"]["idle"] is False
    # on a fresh registry every condition holds
    facts = FakeFacts(holding=True)
    facts.cut = Built({"project": "p", "cut": True, "seq": 3}, 200)
    doc, before, ids = fresh()
    h = DelegatedCutHandler(facts, RunRegistry(clock), True)
    h.build(state, call, doc, ids, at)
    assert h.refused is None

    # 3. a refused cut answers as the conditions answer, allowed true, with the cut's own body last
    c = refused("CUT_NO_MODULES", extra={"project": "p", "cut": False, "reason": None,
                                         "unimplemented": ["S-10 (was S-01)"]})
    facts = FakeFacts(holding=True)
    facts.cut = c
    doc, before, ids = fresh()
    h = DelegatedCutHandler(facts, RunRegistry(clock), True)
    e2 = raised_by(h.build, state, call, doc, ids, at)
    assert e2.refusal.name == "DELEGATED_CUT_REFUSED"
    assert h.refused is e2
    b2 = answer_body(e2)
    assert b2["project"] == "p"
    assert b2["allowed"] is True
    assert b2["failed"] == []
    assert b2["base_cut"] == 2
    assert list(b2)[-1] == "cut"
    cut_body = answer_body(c)
    assert cut_body["reason"] == c.message
    assert "error" not in cut_body
    assert b2["cut"] == cut_body
    assert "reason" not in b2
    assert "error" not in b2
    assert doc == before

    # 4. a taken cut is take_cut's own Built, called with (state, at, note), doc and ids untouched
    line = {"seq": 3, "at": at.timestamp(), "note": "n", "members": [], "units": {}, "edges": {}}
    bt = Built({"project": "p", "cut": True, "seq": 3}, 200, appends={CUT_LOG_NAME: (line,)})
    facts = FakeFacts(holding=True)
    facts.cut = bt
    doc, before, ids = fresh()
    h = DelegatedCutHandler(facts, RunRegistry(clock), True)
    assert h.build(state, call, doc, ids, at) is bt
    assert h.refused is None
    assert len(facts.take_cut_calls) == 1
    (args,) = facts.take_cut_calls
    assert len(args) == 3 and args[0] is state and args[1] == at and args[2] == "n", args
    assert doc == before
    assert ids.allocated == []

    # 5. the module's imports are only those listed, and it reads no ship or github attribute
    tree = ast.parse(SHIP_WRITES_SOURCE.read_text(encoding="utf-8"), filename=str(SHIP_WRITES_SOURCE))
    for candidates in imported_modules(tree, SHIP_WRITES_PACKAGE):
        assert not import_forbidden(candidates), (
            f"mu_spec/writes/ship.py imports {candidates[-1]!r}: it may import nothing from {IMPORT_FORBIDDEN}"
        )
        assert import_permitted(candidates), (
            f"mu_spec/writes/ship.py imports {candidates[-1]!r}: it may import only {IMPORT_MODULES}"
        )
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("ship", "github"), (
                f"mu_spec/writes/ship.py reads an attribute named {node.attr!r} at line {node.lineno}"
            )
