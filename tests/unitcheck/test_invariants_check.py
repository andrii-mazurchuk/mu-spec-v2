"""unitcheck.invariants: the invariants check's builders and tables, and its judgements over scripted units.

test_build_and_tables judges build, unformed_of, membership and the declared tables over hand-made Facts;
test_invariants_judgements drives scripted running and rebuilt units, through the shared launch, from scratch
pristine stores. Each scripted unit keeps one manifest per project, answers the FACT_READS tools in the form
FACT_PATHS declares (built from FACT_PATHS itself, never retyped), refuses each ATTEMPTS attempt with 400 and a
message holding that attempt's marker read from ATTEMPTS, and admits the repeated member. The modules under
test are imported inside each test, so while they are absent only these tests fail.
"""

import json
import os
import re
from pathlib import Path

# A scripted unit, configured by CONFIG. It keeps <state>/projects/<p>/<CONFIG["record"]> per project, a JSON
# doc {"entries": [...], "slices": {name: members}, "issues": [...], "requests": [...]}, re-read on every
# request. A GET matching a CONFIG["reads"] tool answers the facts of that tool laid out as FACT_PATHS says;
# a POST matching a CONFIG["tools"] tool is classed as one attempt and answered: CONFIG["override"][attempt]
# when given; the repeated member collapsed, stored twice or refused as CONFIG["member"] says, with one
# change-log line when CONFIG["log_member"]; any other attempt refused with 400 and its marker. An attempt in
# CONFIG["rewrite"] first rewrites its project's manifest with the same content in other bytes. Any other
# request answers 200 {}. Every request is noted in CONFIG["requests"], with its answer.
UNIT_MAIN = r'''
import json, os, re, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG = %r
LOCK = threading.Lock()


def record(project):
    return os.path.join(os.environ[CONFIG["state"]], "projects", project, *CONFIG["record"])


def load(project):
    with open(record(project), "rb") as f:
        return json.loads(f.read())


def save(project, doc, indent=None):
    with open(record(project), "wb") as f:
        f.write(json.dumps(doc, indent=indent).encode())


def note(what):
    with open(CONFIG["log"], "ab") as f:
        f.write(json.dumps({"kind": "mutation", "what": what}).encode() + b"\n")


def rows(doc, fact):
    if fact == "slices":
        return [{"name": k, "members": list(v)} for k, v in doc["slices"].items()]
    return doc[fact]


def laid_out(doc, fact, keys):
    names = CONFIG["fields"][fact]
    if all(n in keys for n in names):
        return [{keys[n]: r.get(n) for n in names} for r in rows(doc, fact)]
    return {r[names[0]]: r[names[1]] for r in rows(doc, fact)}


def read_answer(tool, project):
    doc = load(project)
    out = {}
    for fact, path, keys in CONFIG["reads"][tool]["layout"]:
        value = laid_out(doc, fact, keys)
        if not path:
            out = value
            continue
        at = out
        for step in path[:-1]:
            at = at.setdefault(step, {})
        at[path[-1]] = value
    return 200, json.dumps(out).encode()


def walk(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from walk(v)


def attempt_of(tool, body):
    if tool == "submit_amendment":
        found = list(walk(body))
        if any("id" in d for d in found):
            return "entry_id"
        if any(CONFIG["slice_key"] in d for d in found):
            return "entry_slice_key"
        return "second_successor"
    if tool == "split_slice":
        return "split_foreign_member" if body.get("into") == CONFIG["new_slice"] else "split_into_existing"
    proposal = body.get("proposal")
    values = list(proposal.values()) if isinstance(proposal, dict) else []
    if any(not isinstance(v, list) for v in values):
        return "proposal_not_list"
    if any(len(v) != len(set(v)) for v in values):
        return "repeated_member"
    return "proposed_twice"


def write_answer(tool, project, body):
    attempt = attempt_of(tool, body)
    if attempt in CONFIG["rewrite"]:
        save(project, load(project), indent=1)
    if attempt in CONFIG["override"]:
        status, text = CONFIG["override"][attempt]
        return status, text.encode()
    if attempt == "repeated_member":
        if CONFIG["member"] == "refuse":
            return 400, json.dumps({"error": "slice member repeated"}).encode()
        doc = load(project)
        for name, members in body["proposal"].items():
            doc["slices"][name] = list(dict.fromkeys(members)) if CONFIG["member"] == "collapse" else list(members)
        save(project, doc)
        if CONFIG["log_member"]:
            note("submit_proposal " + project)
        return 200, json.dumps({"ok": True}).encode()
    return 400, json.dumps({"error": "refused " + attempt + ": " + CONFIG["markers"][attempt]}).encode()


def answer(method, path, raw):
    for tool, spec in CONFIG["reads"].items():
        m = re.fullmatch(spec["regex"], path)
        if method == "GET" and m:
            return read_answer(tool, m.group(1))
    for tool, tool_method, regex in CONFIG["tools"]:
        m = re.fullmatch(regex, path)
        if method == tool_method and m:
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return 400, b'{"error": "invalid JSON"}'
            return write_answer(tool, m.group(1), body)
    return 200, b"{}"


class Handler(BaseHTTPRequestHandler):
    def _answer(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with LOCK:
            status, out = answer(self.command, self.path.split("?")[0], raw)
            line = {"method": self.command, "path": self.path, "body": raw.decode("latin-1"),
                    "status": status, "answer": out.decode("latin-1")}
            with open(CONFIG["requests"], "a") as f:
                f.write(json.dumps(line) + "\n")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((os.environ[CONFIG["host"]], int(os.environ[CONFIG["port"]])), Handler).serve_forever()
'''

ATTEMPT_TOOLS = ("submit_amendment", "split_slice", "submit_proposal")


def route(name):
    """The registered tool's method and its path pattern as a regex whose first group is the project."""
    from mu_spec.declarations import TOOLS

    (spec,) = [t for t in TOOLS if t.name == name]
    pattern = "/" + spec.path.lstrip("/")
    params = re.findall(r"\{[^}]+\}", pattern)
    assert params and params[0] == "{project}", pattern
    return spec.method, "([^/]+)".join(re.escape(part) for part in re.split(r"\{[^}]+\}", pattern))


def unit(base, label, check_root, requests, override=None, rewrite=(), member="collapse", log_member=True):
    """A scratch install of UNIT_MAIN under base/label, and its UnitCommand."""
    from mu_spec.codec import record_path
    from unitcheck.invariants import (
        ATTEMPTS,
        ELEMENT_FIELDS,
        ENTRY_SLICE_KEY,
        FACT_PATHS,
        NEW_SLICE,
    )
    from unitcheck.launch import followed_log

    from mu_spec.outside import ENV_NAMES
    from startcheck.runner import UnitCommand

    reads = {}
    for fact, (tool, path, keys) in FACT_PATHS.items():
        reads.setdefault(tool, {"regex": route(tool)[1], "layout": []})
        reads[tool]["layout"].append([fact, list(path), dict(keys)])
    config = {"host": ENV_NAMES["host"], "port": ENV_NAMES["port"], "state": ENV_NAMES["state_dir"],
              "log": str(followed_log(str(check_root))), "requests": str(requests),
              "record": list(Path(record_path(Path("r"), "p")).relative_to(Path("r") / "p").parts),
              "fields": {k: list(v) for k, v in ELEMENT_FIELDS.items()}, "reads": reads,
              "tools": [[t, *route(t)] for t in ATTEMPT_TOOLS],
              "markers": {a: marker for _, a, _, marker in ATTEMPTS}, "slice_key": ENTRY_SLICE_KEY,
              "new_slice": NEW_SLICE, "override": {k: list(v) for k, v in (override or {}).items()},
              "rewrite": list(rewrite), "member": member, "log_member": log_member}
    package = base / label / "mu_spec"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "main.py").write_text(UNIT_MAIN % (config,))
    (package / "__main__.py").write_text(UNIT_MAIN % (config,))
    return UnitCommand(label=label, install_root=str(package.parent))


def entry(eid, slice_name, superseded_by=None):
    return {"id": eid, "layer": "S", "title": "entry " + eid, "derives_from": [], "depends_on": [],
            "superseded_by": superseded_by, "slice": slice_name}


def two_slices():
    """p's manifest doc: two slices, a superseded entry and an open issue, so all eight attempts form."""
    from unitcheck.invariants import OPEN_VALUES

    closed = next(v for v in (False, "closed", None, 0, "resolved") if v not in OPEN_VALUES)
    return {"entries": [entry("S-1", "alpha"), entry("S-2", "alpha", "S-3"), entry("S-3", "alpha"),
                        entry("S-4", "beta")],
            "slices": {"alpha": ["S-2", "S-1", "S-3"], "beta": ["S-4"]},
            "issues": [{"id": "iss-0", "open": closed}, {"id": "iss-1", "open": OPEN_VALUES[0]}],
            "requests": [{"id": "msg-1"}]}


def one_slice():
    """p's manifest doc: one slice, no superseded entry, an open issue."""
    from unitcheck.invariants import OPEN_VALUES

    return {"entries": [entry("S-1", "alpha"), entry("S-3", "alpha")], "slices": {"alpha": ["S-1", "S-3"]},
            "issues": [{"id": "iss-1", "open": OPEN_VALUES[0]}], "requests": []}


def store(base, doc):
    """A pristine store holding the one project p, its manifest doc written at record_path."""
    from mu_spec.codec import record_path

    from mu_spec.outside import PROJECTS_SUBDIR

    manifest = Path(record_path(base / PROJECTS_SUBDIR, "p"))
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(json.dumps(doc).encode())
    return base


def noted(requests):
    """The requests a scripted unit noted, in order."""
    if not requests.exists():
        return []
    return [json.loads(line) for line in requests.read_text().splitlines() if line]


def attempts_sent(requests):
    """(method, path, body bytes) of every request a scripted unit noted to an attempt tool, in order."""
    regexes = [route(t) for t in ATTEMPT_TOOLS]
    return [(q["method"], q["path"], q["body"].encode("latin-1")) for q in noted(requests)
            if any(q["method"] == m and re.fullmatch(r, q["path"].split("?")[0]) for m, r in regexes)]


def verdict_of(report, attempt):
    (v,) = [v for v in report.verdicts if v.attempt == attempt]
    return v


def test_invariants_judgements(tmp_path, monkeypatch):
    import unitcheck.runs
    from unitcheck.invariants import NO_ATTEMPT_RULES, SET_RULE, run

    real_restore = unitcheck.runs.restore
    sources = []

    def restoring(check_root, source, pristine_prompts):
        sources.append(os.path.normcase(os.path.abspath(str(source))))
        return real_restore(check_root, source, pristine_prompts)

    monkeypatch.setattr(unitcheck.runs, "restore", restoring)
    pristine = store(tmp_path / "pristine", two_slices())
    single = store(tmp_path / "single", one_slice())
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    count = [0]

    def case(running=None, rebuilt=None, pristine_store=pristine):
        """One run of the check; running and rebuilt are unit() options, R0 when None."""
        count[0] += 1
        base = tmp_path / f"case{count[0]}"
        root = base / "check"
        root.mkdir(parents=True)
        req = {"running": base / "running.jsonl", "rebuilt": base / "rebuilt.jsonl"}
        r = unit(base / "installs", "running", root, req["running"], **(running or {}))
        b = unit(base / "installs", "rebuilt", root, req["rebuilt"], **(rebuilt or {}))
        del sources[:]
        report = run(r, b, str(root), str(pristine_store), str(prompts), dict(os.environ))
        assert report.no_attempt == NO_ATTEMPT_RULES, report.no_attempt
        return report, req

    def faulted(report, attempt):
        v = verdict_of(report, attempt)
        assert v.faults, v
        assert report.passed is False, report
        return v

    # rebuilt == R0: the check passes, from pristine_store, the same bytes to both, the repeated member last.
    report, req = case()
    assert report.passed is True, report
    assert report.unformed == (), report.unformed
    assert report.outbound == (), report.outbound
    assert all(v.faults == () for v in report.verdicts), report.verdicts
    assert all(v.after_landed is False for v in report.verdicts), report.verdicts
    assert len(sources) >= 3 and set(sources) == {os.path.normcase(os.path.abspath(str(pristine)))}, sources
    formed = [a for a in report.attempts if a.call is not None]
    assert len(formed) == 8, report.attempts
    sent = {u: attempts_sent(req[u]) for u in req}
    assert sent["running"] == sent["rebuilt"], sent
    assert sorted(body for _, _, body in sent["running"]) == sorted(a.call.body for a in formed), sent
    (member,) = [a for a in formed if a.attempt == SET_RULE[1]]
    for u in req:
        bodies = [body for _, _, body in sent[u]]
        assert bodies[-1] == member.call.body and bodies.count(member.call.body) == 1, (u, bodies)

    # (a) the running unit admits split_into_existing: not a refusal of the rule.
    admits = {"override": {"split_into_existing": (200, '{"ok": true}')}}
    report, _ = case(admits, admits)
    v = faulted(report, "split_into_existing")
    assert any("wrong attempt" in f for f in v.faults), v.faults

    # (a) the running unit refuses entry_id for another reason: a 4xx is not the rule's refusal.
    other = {"override": {"entry_id": (400, '{"error": "invalid JSON"}')}}
    report, _ = case(other, other)
    v = faulted(report, "entry_id")
    assert v.running[0] == 400 and v.running == v.rebuilt, v
    assert any("wrong attempt" in f for f in v.faults), v.faults

    # (b) the rebuilt merge refusal differs from R0's by one byte.
    report, _ = case()
    r0 = verdict_of(report, "split_into_existing").running
    one_byte = r0[1].replace(b"refused", b"Refused", 1)
    assert len(one_byte) == len(r0[1]) and sum(x != y for x, y in zip(one_byte, r0[1])) == 1, (r0, one_byte)
    report, _ = case(rebuilt={"override": {"split_into_existing": (r0[0], one_byte.decode())}})
    v = faulted(report, "split_into_existing")
    assert v.rebuilt == (r0[0], one_byte), v

    # The rebuilt entry_id rewrites p's manifest (same content, other bytes): changed, and later runs landed.
    report, _ = case(rebuilt={"rewrite": ("entry_id",)})
    v = faulted(report, "entry_id")
    assert v.rebuilt == v.running, v
    at = next(i for i, x in enumerate(report.verdicts) if x.attempt == "entry_id")
    later = [x for x in report.verdicts[at + 1:] if x.project == v.project]
    assert later and all(x.after_landed for x in later), report.verdicts

    # (c) both units store the repeated member twice: agreeing is not enough.
    twice = {"member": "twice"}
    report, _ = case(twice, twice)
    v = faulted(report, SET_RULE[1])
    assert v.running == v.rebuilt, v
    (member,) = [a for a in report.attempts if a.attempt == SET_RULE[1]]
    assert member.repeated_all, member
    for label in ("running", "rebuilt"):
        for slice_name, entry_id in member.repeated_all:
            assert any("membership not a set" in f and label in f and slice_name in f and entry_id in f
                       and "2" in f for f in v.faults), (label, slice_name, entry_id, v.faults)

    # The rebuilt unit collapses the member but records no change-log line: the records judgement.
    report, _ = case(rebuilt={"log_member": False})
    v = faulted(report, SET_RULE[1])
    assert v.running == v.rebuilt, v
    assert not any("membership not a set" in f for f in v.faults), v.faults

    # The rebuilt unit refuses the repeated member the running unit admits: the answer judgement.
    report, _ = case(rebuilt={"member": "refuse"})
    v = faulted(report, SET_RULE[1])
    assert v.rebuilt[0] == 400 and v.running[0] is not None and 200 <= v.running[0] < 300, v

    # (e) a store whose only project has one slice and no superseded entry: nothing to try is a failure.
    report, _ = case(pristine_store=single)
    assert report.unformed == ("second_successor", "split_into_existing", "proposed_twice",
                               "split_foreign_member"), report.unformed
    assert report.passed is False, report
    assert report.verdicts and all(v.faults == () for v in report.verdicts), report.verdicts


def test_build_and_tables():
    from mu_spec.declarations import TOOLS
    from unitcheck.invariants import (
        ATTEMPTS,
        ENTRY_SLICE_KEY,
        FORMED_SOMEWHERE,
        HELD_BY_TOOL_LIST,
        NEW_SLICE,
        NO_ATTEMPT_RULES,
        REFUSED_RULES,
        SET_RULE,
        Facts,
        build,
        membership,
        unformed_of,
    )

    def e(eid, slice_name, superseded_by=None):
        return {"id": eid, "layer": "S", "title": "t " + eid, "derives_from": (), "depends_on": (),
                "superseded_by": superseded_by, "slice": slice_name}

    f2_entries = (e("S-10", "a"), e("S-17", "a", "S-320"), e("S-320", "a"), e("S-496", "b"))
    members2 = {"a": ("S-17", "S-10", "S-320"), "b": ("S-496",)}
    F2 = Facts("p", f2_entries, ("a", "b"), members2, "iss-1")
    F2x = Facts("p", (e("S-10", "a", "S-496"),) + f2_entries[1:], ("a", "b"), members2, "iss-1")
    F1 = Facts("p", (e("S-10", "a"), e("S-320", "a")), ("a",), {"a": ("S-10", "S-320")}, "iss-1")
    F0 = Facts("p", f2_entries, ("a", "b"), members2, None)

    def body(a):
        doc = json.loads(a.call.body)
        assert isinstance(doc, dict), a
        return doc

    def the_entry(a):
        entries = body(a)["entries"]
        assert isinstance(entries, list) and len(entries) == 1, a
        return entries[0]

    def by_name(attempts):
        return {a.attempt: a for a in attempts}

    # Every attempt, in ATTEMPTS order then the repeated member, each a POST of its tool's route.
    b2 = build(F2)
    assert [a.attempt for a in b2] == [x[1] for x in ATTEMPTS] + [SET_RULE[1]], b2
    tools = {t.name: t for t in TOOLS}
    for a, tool in zip(b2, [x[2] for x in ATTEMPTS] + [SET_RULE[2]]):
        assert a.call is not None and a.project == "p", a
        assert a.call.method == "POST" == tools[tool].method and a.call.name == tool, a
        doc = body(a)
        assert doc.get("actor") == "invariants-check" and doc.get("in_response_to") == "iss-1", doc
        assert doc.get("dry_run") is not True, doc
    n2 = by_name(b2)

    # (d) entry_id from the first live entry.
    assert the_entry(n2["entry_id"])["id"] == "S-10", n2["entry_id"]
    assert the_entry(by_name(build(F2x))["entry_id"])["id"] == "S-320", build(F2x)

    split = n2["split_foreign_member"]
    assert "a" in split.call.path.split("?")[0].split("/"), split.call.path
    assert body(split)["into"] == NEW_SLICE and body(split)["members"] == ["S-496"], body(split)
    twice = body(n2["proposed_twice"])["proposal"]
    assert twice["a"] == twice["b"] and len(twice["a"]) == 1 and twice["a"][0] in members2["a"], twice

    # (a) the repeated member: the first entry in identifier order, repeated at the end, per slice.
    repeated = n2["repeated_member"]
    assert repeated.repeated_all == (("a", "S-10"), ("b", "S-496")), repeated
    proposal = body(repeated)["proposal"]
    assert proposal["a"][-1] == "S-10" and proposal["a"].count("S-10") == 2, proposal
    assert proposal["b"] == ["S-496", "S-496"], proposal

    # (b) one slice and no superseded entry: only the attempts that need neither.
    b1 = build(F1)
    assert {a.attempt for a in b1 if a.call is not None} == {
        "entry_id", "entry_slice_key", "proposal_not_list", "repeated_member"}, b1
    assert the_entry(by_name(b1)["entry_slice_key"]).get(ENTRY_SLICE_KEY) == [], by_name(b1)["entry_slice_key"]
    for a in b1:
        if a.call is None:
            assert a.why_not, a

    # (c) no cite: nothing formed.
    assert all(a.call is None for a in build(F0)), build(F0)

    # (e) unformed_of names the repeated member too.
    assert unformed_of(b1) == ("second_successor", "split_into_existing", "proposed_twice",
                               "split_foreign_member"), unformed_of(b1)
    assert unformed_of(b1 + b2) == (), unformed_of(b1 + b2)
    assert unformed_of([a for a in b2 if a.attempt != "repeated_member"]) == ("repeated_member",)

    counted = Facts("p", (), ("a",), {"a": ("S-10", "S-10", "S-17")}, "iss-1")
    assert membership(counted, "a", "S-10") == 2
    assert membership(counted, "a", "S-17") == 1
    assert membership(counted, "c", "S-10") == 0

    assert len(FORMED_SOMEWHERE) == 8 == len(set(FORMED_SOMEWHERE)), FORMED_SOMEWHERE
    assert FORMED_SOMEWHERE[-1] == "repeated_member", FORMED_SOMEWHERE
    assert NO_ATTEMPT_RULES == (("renumbering", HELD_BY_TOOL_LIST), ("amendment_removal", HELD_BY_TOOL_LIST))
    assert {r for r, _, _, _ in ATTEMPTS} == set(REFUSED_RULES) and len(REFUSED_RULES) == 5, REFUSED_RULES
