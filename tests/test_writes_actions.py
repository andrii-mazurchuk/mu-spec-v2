"""The six writer person-only actions' handlers in mu_spec.writes.actions (T-428, T-429).

Their checks before the project lookup are held apart as PRE_ORDERS; cut units judges soundness by
the one gate report, then modules absent or empty, and returns take_cut's Built unchanged as a lines
turn; the manifest actions change only their keys, in order, over literal manifests in today's
layout. Through a real writer a taken cut appends one cut-log line and leaves the manifest bytes as
they were, and a failed append raises out of submit with nothing appended or recorded. Modules under
test are imported inside the test, so a module not yet written fails only the tests that need it.
"""

import ast
import copy
import datetime
import json
import pathlib

import pytest

from mu_spec.outside import FixedClock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTIONS_SOURCE = REPO_ROOT / "mu_spec" / "writes" / "actions.py"
ACTIONS_PACKAGE = "mu_spec.writes"
AT = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
IMPORT_MODULES = ("mu_spec.writer", "mu_spec.refusals", "mu_spec.codec", "mu_spec.rules")
# what the module may import beyond IMPORT_MODULES
IMPORT_ALLOWED = ("__future__", "typing", "dataclasses", "datetime", "copy")

D_MOD = {"project": "p", "slices": {}, "allocation": {"S": 2}, "modules": {"a.py": ["S-10 (was S-01)"]},
         "repo": None, "labels": [], "pickup": []}
D_EMPTY = dict(D_MOD, modules={})
D_ABSENT = {key: value for key, value in D_MOD.items() if key != "modules"}
D_LAB = dict(D_MOD, labels=["old"], pickup=["claimed"])

SOUND = {"sound": True, "complete": True, "clean": True, "findings": [], "cosmetic": [], "slice_findings": []}
UNSOUND = dict(SOUND, sound=False, findings=[{"kind": "orphan", "id": "B-53 (was B-09)"}])
CONDITIONS = {"ok": False, "sound": True, "modules": True, "dead_ends": ["B-90 (was B-03)"],
              "cycle": ["a", "b", "a"], "dead_blockers": ["S-369 (was S-09)"]}
SPEC_IDS = ("S-10 (was S-01)", "S-17 (was S-02)")
UNSOUND_TEXT = "graph is unsound -- it contains orphaned entries"


class FakeFacts:
    """A ShipFacts recording every call by name with its positional arguments.

    gate_report answers report; take_cut raises cut when it is an exception and answers it otherwise.
    """

    def __init__(self, report, cut=None):
        self.report = report
        self.cut = cut
        self.calls = []

    def gate_report(self, state):
        self.calls.append(("gate_report", (state,)))
        return copy.deepcopy(self.report)

    def gate_conditions(self, state):
        self.calls.append(("gate_conditions", (state,)))
        return copy.deepcopy(CONDITIONS)

    def spec_ids(self, state):
        self.calls.append(("spec_ids", (state,)))
        return SPEC_IDS

    def take_cut(self, *args):
        self.calls.append(("take_cut", args))
        if isinstance(self.cut, BaseException):
            raise self.cut
        if callable(self.cut):
            return self.cut(*args)
        return self.cut

    def called(self, name):
        return [args for called, args in self.calls if called == name]


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


def cut_line(at, note):
    """The cut-log line the fake take_cut forms for (at, note)."""
    return {"seq": 1, "at": at.timestamp(), "note": note, "members": [], "units": {}, "edges": {},
            "unimplemented": [], "stale_modules": [], "untested": [], "unimplemented_tests": [],
            "unfollowed_tests": [], "dangling": []}


def raised_by(fn, *args):
    """The Refused fn(*args) raises; fails when it raises none."""
    from mu_spec.refusals import Refused

    with pytest.raises(Refused) as caught:
        fn(*args)
    return caught.value


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
    """Whether any module an imported name may come from is IMPORT_MODULES' or IMPORT_ALLOWED's."""
    return any(module in IMPORT_MODULES or module.startswith("mu_spec.rules.")
               or module.split(".")[0] in IMPORT_ALLOWED
               for module in candidates)


def test_writer_action_handlers():
    from mu_spec.codec import CUT_LOG_NAME, MUTATION_KIND, ProjectRecord
    from mu_spec.declarations import ACTIONS
    from mu_spec.refusals import CheckContext, Refused, refuse, run_checks
    from mu_spec.writer import Built, WriteCall
    from mu_spec.writes.actions import (
        LABEL_MAX,
        LINES_ACTIONS,
        MANIFEST_KEYS,
        PRE_ORDERS,
        WRITE_HANDLERS,
        WRITER_ACTIONS,
        CutUnitsHandler,
        SetDelegationHandler,
        SetLabelsHandler,
        SetRepoHandler,
    )
    from mu_spec.writes.reasonform import answer_body

    def state_of(d):
        return ProjectRecord.from_doc("p", copy.deepcopy(d))

    # 1. the declarations
    assert WRITER_ACTIONS == ("cut_units", "ratify", "reject", "set_repo", "set_labels", "set_delegation")
    assert set(WRITE_HANDLERS) == set(PRE_ORDERS) == set(MANIFEST_KEYS) == set(WRITER_ACTIONS)
    assert set(WRITER_ACTIONS) <= {spec.name for spec in ACTIONS}
    assert LINES_ACTIONS == ("cut_units",)
    assert set(LINES_ACTIONS) <= set(WRITER_ACTIONS)
    assert MANIFEST_KEYS["cut_units"] == ()
    assert MANIFEST_KEYS["set_labels"] == ("labels", "pickup")
    assert LABEL_MAX == 50

    # 2. the checks before the project lookup, run with no state
    def pre(action, body):
        run_checks(PRE_ORDERS[action], CheckContext("nope", None, body))

    assert raised_by(pre, "set_repo", {}).refusal.name == "REPO_REQUIRED"
    e = raised_by(pre, "set_repo", {"repo": 5})
    assert e.refusal.name == "REPO_NOT_STRING"
    assert e.status == 400
    assert e.message == "'repo' must be a string, or null to clear it"
    pre("set_repo", {"repo": "not a repo"})
    assert raised_by(pre, "set_labels", {}).refusal.name == "LABELS_REQUIRED"
    pre("set_labels", {"labels": [""]})
    e = raised_by(pre, "set_delegation", {"enabled": "yes"})
    assert e.refusal.name == "DELEGATION_NOT_BOOL"
    assert e.message == "'enabled' must be true or false"
    pre("set_delegation", {"enabled": True})
    for action in ("cut_units", "ratify", "reject"):
        assert tuple(PRE_ORDERS[action]) == (), action

    # 3. cut units
    cut_call = WriteCall("cut_units", {"note": "n"}, "a", None, False)

    def cut(d, facts):
        state = state_of(d)
        doc = copy.deepcopy(d)
        before = copy.deepcopy(doc)
        ids = FakeIds(doc)
        h = CutUnitsHandler(facts)
        return h, state, doc, before, ids

    # 3a. an unsound graph refuses, whatever the manifest holds
    facts = FakeFacts(UNSOUND)
    h, state, doc, before, ids = cut(D_MOD, facts)
    e = raised_by(h.build, state, cut_call, doc, ids, AT)
    assert e.refusal.name == "CUT_UNSOUND"
    assert e.status == 409
    assert h.refused is e
    b = answer_body(e)
    assert list(b)[:3] == ["project", "cut", "reason"]
    assert b["project"] == "p"
    assert b["cut"] is False
    assert b["reason"] == UNSOUND_TEXT
    assert b["gates"] == UNSOUND
    assert "error" not in b
    assert doc == before
    assert facts.called("take_cut") == []

    # 3b. a sound graph with modules empty, and with modules absent, refuses with no gates
    for d in (D_EMPTY, D_ABSENT):
        facts = FakeFacts(SOUND)
        h, state, doc, before, ids = cut(d, facts)
        e = raised_by(h.build, state, cut_call, doc, ids, AT)
        assert e.refusal.name == "CUT_NO_MODULES", d
        assert e.status == 409
        assert h.refused is e
        b = answer_body(e)
        assert b["unimplemented"] == list(SPEC_IDS)
        assert "gates" not in b
        assert "error" not in b
        assert doc == before
        assert facts.called("take_cut") == [], d

    # 3c. soundness is checked before modules
    facts = FakeFacts(UNSOUND)
    h, state, doc, before, ids = cut(D_EMPTY, facts)
    assert raised_by(h.build, state, cut_call, doc, ids, AT).refusal.name == "CUT_UNSOUND"

    # 3d. take_cut's refusal is kept and re-raised unchanged
    with pytest.raises(Refused) as caught:
        refuse("CUT_NO_MODULES", extra={"project": "p", "cut": False, "reason": None, "unimplemented": []})
    c = caught.value
    facts = FakeFacts(SOUND, cut=c)
    h, state, doc, before, ids = cut(D_MOD, facts)
    assert raised_by(h.build, state, cut_call, doc, ids, AT) is c
    assert h.refused is c
    assert facts.called("take_cut") == [(state, AT, "n")]

    # 3e. dead ends, a cycle and dead blockers never refuse a cut; take_cut's Built is returned unchanged
    bt = Built({"project": "p", "cut": True, "seq": 1}, 200, appends={CUT_LOG_NAME: (cut_line(AT, "n"),)})
    facts = FakeFacts(SOUND, cut=bt)
    h, state, doc, before, ids = cut(D_MOD, facts)
    assert h.build(state, cut_call, doc, ids, AT) is bt
    assert h.refused is None
    assert doc == before
    assert ids.allocated == []
    assert facts.called("take_cut") == [(state, AT, "n")]

    # 4. set labels: labels checked before pickup, stored trimmed with first occurrences kept
    def labels(body):
        state = state_of(D_LAB)
        h = SetLabelsHandler()
        run_checks(h.check_order, CheckContext("p", state, body))
        return h, state

    body = {"labels": [" a ", "a", "b"]}
    h, state = labels(body)
    doc = copy.deepcopy(D_LAB)
    before = copy.deepcopy(doc)
    built = h.build(state, WriteCall("set_labels", body, "a", None, False), doc, FakeIds(doc), AT)
    assert built.status == 200
    assert dict(built.body) == {"project": "p", "labels": ["a", "b"], "pickup": ["claimed"]}
    assert doc["labels"] == ["a", "b"]
    assert doc["pickup"] == ["claimed"]
    assert list(doc) == list(D_LAB)
    for key, value in before.items():
        if key != "labels":
            assert doc[key] == value, key

    e = raised_by(labels, {"labels": ["x" * 51]})
    assert e.refusal.name == "LABEL_TOO_LONG"
    assert e.message == "'" + "x" * 51 + "' is longer than 50 characters"
    labels({"labels": ["x" * 50]})
    assert raised_by(labels, {"labels": ["a"], "pickup": [""]}).refusal.name == "LABEL_EMPTY"
    assert raised_by(labels, {"labels": ["a"], "pickup": "c"}).refusal.name == "LABELS_NOT_LIST"
    assert raised_by(labels, {"labels": [""], "pickup": "c"}).refusal.name == "LABEL_EMPTY"

    # 5. set repo changes only repo, in place, and its records start with the change line
    repo_call = WriteCall("set_repo", {"repo": "o/r"}, "a", None, False)
    h = SetRepoHandler()
    doc = copy.deepcopy(D_MOD)
    before = copy.deepcopy(doc)
    built = h.build(state_of(D_MOD), repo_call, doc, FakeIds(doc), AT)
    assert built.status == 200
    assert dict(built.body) == {"project": "p", "repo": "o/r"}
    assert doc["repo"] == "o/r"
    assert list(doc) == list(D_MOD)
    for key, value in before.items():
        if key != "repo":
            assert doc[key] == value, key
    assert not built.appends
    records = h.log_records(repo_call, built, AT)
    assert records and records[0].kind == MUTATION_KIND
    assert all(record.seq is None for record in records)

    # 6. set delegation adds delegation last, its at a number
    h = SetDelegationHandler()
    doc = copy.deepcopy(D_MOD)
    h.build(state_of(D_MOD), WriteCall("set_delegation", {"enabled": True, "note": "go"}, "a", None, False),
            doc, FakeIds(doc), AT)
    assert list(doc["delegation"]) == ["enabled", "note", "actor", "at"]
    assert doc["delegation"]["enabled"] is True
    assert doc["delegation"]["note"] == "go"
    assert doc["delegation"]["at"] == 1767225600.0
    assert isinstance(doc["delegation"]["at"], (int, float))
    assert list(doc)[:-1] == list(D_MOD)
    assert list(doc)[-1] == "delegation"

    # 7. the module's imports are only those listed, and it reads no ship or github attribute
    tree = ast.parse(ACTIONS_SOURCE.read_text(encoding="utf-8"), filename=str(ACTIONS_SOURCE))
    for candidates in imported_modules(tree, ACTIONS_PACKAGE):
        assert import_permitted(candidates), (
            f"mu_spec/writes/actions.py imports {candidates[-1]!r}: it may import only {IMPORT_MODULES}"
        )
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("ship", "github"), (
                f"mu_spec/writes/actions.py reads an attribute named {node.attr!r} at line {node.lineno}"
            )


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


def test_cut_through_writer(tmp_path, monkeypatch):
    from mu_spec.codec import (
        CUT_LOG_NAME,
        MUTATION_KIND,
        ProjectRecord,
        encode_record,
        record_path,
    )
    from mu_spec.registry import build_runtime
    from mu_spec.writer import Built, WriteCall
    from mu_spec.writes.actions import CutUnitsHandler

    from mu_spec.outside import PROJECTS_SUBDIR

    D = tmp_path / "state"
    manifest = record_path(D / PROJECTS_SUBDIR, "p")
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(encode_record(ProjectRecord.from_doc("p", copy.deepcopy(D_MOD))))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    set_environment(monkeypatch, port="9100", state_dir=str(D))
    rt = build_runtime(ship_builder=lambda client: object(), clock=FixedClock(AT), github=FakeGitHub())
    manifest = record_path(rt.config.root, "p")
    cut_log = manifest.parent / CUT_LOG_NAME
    w = rt.registry.writer("p")

    def taken(state, at, note):
        return Built({"project": "p", "cut": True, "seq": 1}, 200, appends={CUT_LOG_NAME: (cut_line(at, note),)})

    facts = FakeFacts(SOUND, cut=taken)
    m0 = manifest.read_bytes()
    c0 = cut_log.read_bytes() if cut_log.exists() else b""
    log0 = rt.store.read_log(project="p")
    n0 = len(log0)

    # 1. a taken cut appends one cut-log line, leaves the manifest bytes, and records after it
    out = w.submit(WriteCall("cut_units", {"note": "n"}, "a", None, False), CutUnitsHandler(facts))
    assert out.status == 200, out
    assert manifest.read_bytes() == m0
    c1 = cut_log.read_bytes()
    assert c1.startswith(c0)
    added = c1[len(c0):].splitlines()
    assert len(added) == 1, added
    assert json.loads(added[0]) == cut_line(AT, "n")
    log1 = rt.store.read_log(project="p")
    new = log1[n0:]
    assert new, "a taken cut recorded nothing"
    assert new[0].kind == MUTATION_KIND
    old_seqs = [record.seq for record in log0]
    new_seqs = [record.seq for record in new]
    assert all(isinstance(seq, int) for seq in new_seqs), new_seqs
    assert new_seqs == sorted(set(new_seqs)), new_seqs
    assert not old_seqs or min(new_seqs) > max(old_seqs), (old_seqs, new_seqs)

    # 2. a failed append raises out of submit with nothing appended or recorded
    n1 = len(log1)
    err = OSError("disk full")

    def failing(*args, **kwargs):
        raise err

    monkeypatch.setattr(rt.store, "append_lines", failing)
    h = CutUnitsHandler(facts)
    with pytest.raises(OSError) as raised:
        w.submit(WriteCall("cut_units", {"note": "n2"}, "a", None, False), h)
    assert raised.value is err
    assert h.refused is None
    assert cut_log.read_bytes() == c1
    assert manifest.read_bytes() == m0
    assert len(rt.store.read_log(project="p")) == n1
