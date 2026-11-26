"""The Ship part's write handlers: DelegatedCutHandler and OutcomeHandler in mu_spec.writes.ship (T-281).

Delegated Ship's conditions-and-cut turn evaluates every condition and reports them all, takes the
cut only when every one holds, and answers a refused cut with that cut's own answer body; the
end-of-run commit notes its generation before it builds any record and changes only the outcome
keys. Modules under test are imported inside the test, so a module not yet written fails only the
tests that need it.
"""

import ast
import collections.abc
import copy
import datetime
import pathlib

import pytest

from mu_spec.outside import FixedClock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SHIP_WRITES_SOURCE = REPO_ROOT / "mu_spec" / "writes" / "ship.py"
SHIP_WRITES_PACKAGE = "mu_spec.writes"
START = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
IMPORT_MODULES = ("mu_spec.ship.runs", "mu_spec.writer", "mu_spec.refusals", "mu_spec.codec",
                  "mu_spec.writes.reasonform")
# what the module may import beyond IMPORT_MODULES
IMPORT_ALLOWED = ("__future__", "typing", "dataclasses", "datetime")
DELEGATED_TEXT = ("refused, nothing was written: delegation, ready, gates, st_only, shipped, "
                  "tests_assigned did not hold. Anything outside the delegation is a person's press.")

GRANT = {"granted": True, "granted_by": "person", "revoked": False}
GATES_HOLDING = {"sound": True, "modules": True, "dead_ends": [], "cycle": None, "dead_blockers": []}
GATES_FAILING = {"sound": False, "modules": False, "dead_ends": ["S-1"], "cycle": ["S-1", "S-2", "S-1"],
                 "dead_blockers": ["S-2"]}


def emitted_result():
    """A Ship result with emitted true, its fields in the plan's order, nothing created or wired."""
    return {
        "emitted": True, "project": "p", "repo": "o/r", "cut_seq": 1, "batch": 1, "horizon": None,
        "created": [], "failed": [], "unwired": [], "wired": [], "edited": [], "withdrawn": [],
        "touched": [], "settled": [], "follow_ups": [], "adopted": [], "swept": [], "raced": [],
        "held": [], "skipped": [], "removed": [], "dead": [], "linked": [], "unlinked": [],
        "unlabelled": [], "only": None, "merge_order": [], "dropped_orders": [],
        "unassigned_tests": [], "undeclared_test_paths": [],
        "counts": {"created": 0, "failed": 0, "unwired": 0, "wired": 0},
    }


class FakeFacts:
    """A ShipFacts whose answers are set per step; take_cut records its calls and answers cut_answer."""

    def __init__(self, holding):
        self.set(holding)
        self.cut_answer = None
        self.take_cut_calls = []

    def set(self, holding):
        self.grant = GRANT if holding else None
        self.repo = "o/r" if holding else None
        self.gates = dict(GATES_HOLDING if holding else GATES_FAILING)
        self.flags = holding

    def latest_cut(self, state):
        return {"seq": 1}

    def gate_conditions(self, state):
        return dict(self.gates)

    def st_only(self, state):
        return self.flags

    def shipped(self, state):
        return self.flags

    def tests_assigned(self, state):
        return self.flags

    def delegation(self, state):
        return self.grant

    def settings(self, state):
        return {"repo": self.repo, "labels": [], "pickup": [], "issues": {}, "orders": [],
                "lineage_keyed": False}

    def take_cut(self, state, doc, ids, at, note):
        self.take_cut_calls.append(note)
        if isinstance(self.cut_answer, BaseException):
            raise self.cut_answer
        return self.cut_answer


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


def holds_value(value, wanted):
    """Whether wanted is value or occurs anywhere inside it, mappings and sequences walked."""
    if value == wanted:
        return True
    if isinstance(value, collections.abc.Mapping):
        return any(holds_value(v, wanted) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(holds_value(v, wanted) for v in value)
    return isinstance(value, str) and wanted in value


def imported_modules(tree: ast.AST, package: str) -> list[str]:
    """Every module an Import or ImportFrom in tree names, relative ones resolved against package."""
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = parts[: len(parts) - node.level + 1]
                names.append(".".join(base + ([node.module] if node.module else [])))
            else:
                names.append(node.module or "")
    return names


def test_ship_write_handlers(monkeypatch):
    from mu_spec.codec import EVENT_KINDS as CODEC_EVENT_KINDS
    from mu_spec.codec import MUTATION_KIND, ProjectRecord
    from mu_spec.refusals import Refused, refuse
    from mu_spec.ship.runs import RunRegistry
    from mu_spec.writer import Built, WriteCall
    from mu_spec.writes import ship
    from mu_spec.writes.reasonform import answer_body
    from mu_spec.writes.ship import (
        CONDITIONS,
        EVENT_KINDS,
        OUTCOME_KEYS,
        DelegatedCutHandler,
        OutcomeHandler,
    )

    clock = FixedClock(START)
    at = clock.now()
    runs = RunRegistry(clock)
    state = ProjectRecord.from_doc("p", {"name": "p", "entries": {}, "slices": {}, "cuts": []})
    doc = state.thaw()
    doc["zz_probe"] = [1]
    ids = FakeIds(doc)
    call = WriteCall("auto_ship", {"project": "p", "note": "n"}, "a", None, False)
    facts = FakeFacts(holding=False)

    # 1. every condition failing: all are reported, in CONDITIONS order, and nothing is written
    before = copy.deepcopy(doc)
    h = DelegatedCutHandler(facts, runs, False)
    with pytest.raises(Refused) as refused:
        h.build(state, call, doc, ids, at)
    e = refused.value
    assert e.refusal.name == "DELEGATED_REFUSED"
    assert h.refused is e
    assert e.extra["failed"] == list(CONDITIONS)
    assert e.message == DELEGATED_TEXT
    assert list(e.extra["conditions"]) == list(CONDITIONS)
    assert doc == before
    assert facts.take_cut_calls == []

    # 2. every condition holding but a run in flight: ready fails on idle alone
    facts.set(holding=True)
    facts.cut_answer = Built({"cut": 2}, 200)
    busy = RunRegistry(clock)
    busy.admit("p", "person_ship", None, 0)
    h = DelegatedCutHandler(facts, busy, True)
    with pytest.raises(Refused) as refused:
        h.build(state, call, doc, ids, at)
    e = refused.value
    assert e.refusal.name == "DELEGATED_REFUSED"
    assert e.extra["failed"] == ["ready"]
    assert e.extra["conditions"]["ready"]["ok"] is False
    assert e.extra["conditions"]["ready"]["idle"] is False
    assert facts.take_cut_calls == []
    # the same with no run in flight: no refusal
    h = DelegatedCutHandler(facts, RunRegistry(clock), True)
    h.build(state, call, doc, ids, at)
    assert h.refused is None
    facts.take_cut_calls.clear()

    # 3. a refused cut is re-raised as DELEGATED_CUT_REFUSED, carrying the cut's own answer body
    c = refuse("CUT_NO_MODULES", extra={"project": "p", "cut": False, "reason": None,
                                        "unimplemented": ["S-10 (was S-01)"]})
    facts.cut_answer = c
    h = DelegatedCutHandler(facts, RunRegistry(clock), True)
    with pytest.raises(Refused) as refused:
        h.build(state, call, doc, ids, at)
    e2 = refused.value
    assert e2.refusal.name == "DELEGATED_CUT_REFUSED"
    assert h.refused is e2
    assert e2.extra["failed"] == []
    assert e2.extra["cut"] == answer_body(c)
    assert e2.extra["cut"]["reason"] == c.message
    assert "error" not in e2.extra["cut"]
    facts.take_cut_calls.clear()

    # 4. a taken cut answers take_cut's own Built
    b = Built({"cut": 2, "seq": 2}, 200)
    facts.cut_answer = b
    h = DelegatedCutHandler(facts, RunRegistry(clock), True)
    assert h.build(state, call, doc, ids, at) is b
    assert h.refused is None
    assert facts.take_cut_calls == ["n"]

    # 5. OutcomeHandler.log_records notes the commit before building any record
    order = []
    runs.set_generation_source(lambda p: (order.append("note"), 7)[1])
    real_new_log_record = ship.new_log_record

    def recording_new_log_record(kind, *args, **kwargs):
        order.append("record:" + kind)
        return real_new_log_record(kind, *args, **kwargs)

    monkeypatch.setattr(ship, "new_log_record", recording_new_log_record)
    r = runs.admit("p", "person_ship", None, 0)
    runs.finish(r, "done", emitted_result(), None)
    built = Built(r.result, 200)
    records = OutcomeHandler(r, runs).log_records(call, built, at)
    assert order == ["note", "record:" + MUTATION_KIND, "record:tickets_emitted"]
    assert r.commit_generation == 7
    assert [record.kind for record in records] == [MUTATION_KIND, "tickets_emitted"]
    assert all(record.seq is None for record in records)
    event = records[-1]
    assert holds_value(event.doc["refs"], r.run_id) or holds_value(event.doc["facts"], r.run_id)
    monkeypatch.setattr(ship, "new_log_record", real_new_log_record)

    # 6. the event kind follows the ending
    endings = [
        ("person_ship", "done", {"emitted": False, "project": "p", "repo": None,
                                 "reason": "no repo is configured for this project"}, None,
         "ship_failed"),
        ("person_ship", "failed", None, "RuntimeError: boom", "ship_failed"),
        ("rollback", "done", {"rolled_back": True, "project": "p", "repo": "o/r", "cut_seq": 1,
                              "closed": [], "failed": [], "skipped": [],
                              "counts": {"targeted": 0, "closed": 0, "failed": 0, "skipped": 0}},
         None, "tickets_rolled_back"),
    ]
    for kind, phase, result, error, event_kind in endings:
        reg = RunRegistry(clock)
        run = reg.admit("p", kind, None, 0)
        reg.finish(run, phase, result, error)
        handler = OutcomeHandler(run, reg)
        work = state.thaw()
        ending_built = handler.build(state, call, work, FakeIds(work), at)
        ending_records = handler.log_records(call, ending_built, at)
        assert [record.kind for record in ending_records] == [MUTATION_KIND, event_kind], (kind, phase)
        assert all(record.seq is None for record in ending_records)

    # 7. the outcome build changes only OUTCOME_KEYS of the doc it is given
    assert len(OUTCOME_KEYS) >= 1
    before = copy.deepcopy(doc)
    answered = OutcomeHandler(r, runs).build(state, call, doc, ids, at)
    assert answered.status == 200
    assert doc["zz_probe"] == [1]
    for key, value in before.items():
        if key not in OUTCOME_KEYS:
            assert key in doc and doc[key] == value, key

    # 8. every event is in the codec's vocabulary, and the module's imports are only those listed
    assert set(EVENT_KINDS) <= set(CODEC_EVENT_KINDS)
    tree = ast.parse(SHIP_WRITES_SOURCE.read_text(encoding="utf-8"), filename=str(SHIP_WRITES_SOURCE))
    for module in imported_modules(tree, SHIP_WRITES_PACKAGE):
        top = module.split(".")[0]
        assert module in IMPORT_MODULES or top in IMPORT_ALLOWED, (
            f"mu_spec/writes/ship.py imports {module!r}: it may import only {IMPORT_MODULES}"
        )
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("ship", "github"), (
                f"mu_spec/writes/ship.py reads an attribute named {node.attr!r} at line {node.lineno}"
            )
