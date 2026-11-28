"""unitcheck.replay: the log read into lines and classified, the start store, a cut body read, agreed and
completed, and the replay run over scripted units launched through the shared launch.

test_lines_classify_start_store, test_whole_of_and_agreement and test_complete_and_rebuild judge the module's
functions over scratch stores and built bodies; test_replay_run_scripted drives a scripted running and rebuilt
unit through run, each a ThreadingHTTPServer on the injected host and port. Every log is written through the
codec and every route is taken from the tool and action declarations. The modules under test are imported
inside each test, so while they are absent only these tests fail.
"""

import copy
import json
import os
import re
import time
from pathlib import Path

E0 = {"layer": "S", "title": "first", "derives_from": ["A-31 (was A-01)"]}
E1 = {"layer": "S", "title": "second", "derives_from": ["A-31 (was A-01)"], "depends_on": ["#0"]}
E2 = {"layer": "S", "title": "x" * 3000, "derives_from": ["A-31 (was A-01)"], "depends_on": ["#1"]}

# A scripted unit, configured by CONFIG. It keeps <state>/<projects>/<p>/scripted.json, {"writes": n}, per project
# and answers by a fixed rule from it: a request CONFIG["makes"] names by "<method> <path> <body>" creates or forks
# a project (409 when present, a fork of an absent source 404); a GET under /projects/<p> answers the project, the
# path and n; any other request under /projects/<p> counts a write and answers the new n; a project absent answers
# 404, /health 200 {}, any other path 404. With CONFIG["nonce"] every answer also holds a fresh random "nonce";
# the answer to CONFIG["tweak"] ("<method> <path>") has its next-to-last byte changed. Every request is noted in
# CONFIG["requests"], after a {"start": true} line noted once per process.
UNIT_MAIN = r'''
import json, os, random, shutil, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

CONFIG = %r
LOCK = threading.Lock()


def root():
    return os.path.join(os.environ[CONFIG["state"]], CONFIG["projects"])


def state(project):
    return os.path.join(root(), project, "scripted.json")


def writes(project):
    try:
        with open(state(project), "rb") as f:
            return json.loads(f.read())["writes"]
    except OSError:
        return 0


def note(line):
    with LOCK:
        with open(CONFIG["requests"], "a") as f:
            f.write(json.dumps(line) + "\n")


def answer(method, path, body):
    made = CONFIG["makes"].get(method + " " + path + " " + body.decode("utf-8", "replace"))
    if made is not None:
        kind, name, source = made
        if os.path.exists(os.path.join(root(), name)):
            return 409, {"error": "exists"}
        if kind == "fork":
            if not os.path.isdir(os.path.join(root(), source)):
                return 404, {"error": "no such project"}
            shutil.copytree(os.path.join(root(), source), os.path.join(root(), name))
        else:
            os.mkdir(os.path.join(root(), name))
        return 201, {"created": name}
    bare = path.split("?")[0]
    if bare == "/health":
        return 200, {}
    parts = bare.split("/")
    if len(parts) < 3 or parts[1] != "projects" or not parts[2]:
        return 404, {"error": "no such route"}
    project = unquote(parts[2])
    if not os.path.isdir(os.path.join(root(), project)):
        return 404, {"error": "no such project"}
    n = writes(project)
    if method == "GET":
        return 200, {"project": project, "read": bare, "writes": n}
    with open(state(project), "wb") as f:
        f.write(json.dumps({"writes": n + 1}).encode())
    return 200, {"project": project, "writes": n + 1}


class Handler(BaseHTTPRequestHandler):
    def _answer(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        note({"method": self.command, "path": self.path, "body": body.decode("latin-1")})
        with LOCK:
            status, doc = answer(self.command, self.path, body)
        if CONFIG["nonce"]:
            doc["nonce"] = "%%032x" %% random.getrandbits(128)
        out = json.dumps(doc).encode()
        if CONFIG["tweak"] == self.command + " " + self.path:
            out = out[:-2] + bytes([out[-2] ^ 1]) + out[-1:]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    note({"start": True})
    ThreadingHTTPServer((os.environ[CONFIG["host"]], int(os.environ[CONFIG["port"]])), Handler).serve_forever()
'''


def dumps(v, ensure_ascii=True):
    from unitcheck.replay import BODY_SEPARATORS

    return json.dumps(v, separators=BODY_SEPARATORS, ensure_ascii=ensure_ascii)


def amendment_text():
    """The whole submit_amendment body of the whole_of, complete and rebuild cases, in the mutation form."""
    return dumps({"in_response_to": "msg-1", "actor": "a", "entries": [E0, E1, E2], "idempotency_key": "k"})


def stored_amendment(**changes):
    """R: the stored amendment the cut amendment body came from, as stored_records puts it."""
    record = {"id": "am-9", "in_response_to": "msg-1", "actor": "a", "slice": "s", "at": 1764100000.5,
              "entries": [dict(E0, id="S-10", body=""), dict(E1, id="S-13 (was S-11)", depends_on=["S-10"]),
                          dict(E2, id="S-17 (was S-12)", depends_on=["S-13 (was S-11)"])]}
    record.update(copy.deepcopy(changes))
    return record


def norm(path):
    return os.path.normcase(os.path.abspath(str(path)))


def route(name, value=None):
    """(method, path) of the named tool or action, every '{x}' of its declared path filled with value."""
    from mu_spec.declarations import ACTIONS, TOOLS, find_action, find_tool

    tools = [t for t in TOOLS if t.name == name]
    (spec,) = tools or [a for a in ACTIONS if a.name == name]
    path = re.sub(r"\{[^}]+\}", value or "", spec.path)
    hit = (find_tool if tools else find_action)(spec.method, path)
    assert hit is not None and hit[0].name == name, (name, path, hit)
    return spec.method, path


def create_body(name):
    return dumps({"project": name})


def fork_body(source, target):
    """The fork_project inputs forking source to target: the source under FORK_SOURCE_INPUT, the new name under
    the one other required input of the declared shape."""
    from mu_spec.tools.schemas import BY_NAME
    from unitcheck.replay import FORK_SOURCE_INPUT, FORK_TOOL

    shape = BY_NAME[FORK_TOOL].input_schema
    named = [k for k in shape.get("required", ()) if k not in (FORK_SOURCE_INPUT, "actor", "in_response_to")]
    assert len(named) == 1, f"the fork tool's input shape names no single new-project input: {shape!r}"
    return dumps({FORK_SOURCE_INPUT: source, named[0]: target})


def mutation(method, path, body, status, project, at):
    """A mutation log record whose facts hold method, path, body and status under LINE_FIELDS."""
    from mu_spec.codec import MUTATION_KIND, new_log_record
    from unitcheck.replay import LINE_FIELDS

    facts = {LINE_FIELDS["method"]: method, LINE_FIELDS["path"]: path, LINE_FIELDS["body"]: body,
             LINE_FIELDS["status"]: status}
    return new_log_record(MUTATION_KIND, project, [], facts, at)


def write_log(projects, records):
    """The log of the projects folder: each record encoded by the codec, one per line, newline framed."""
    from mu_spec.codec import encode_log_line, log_path, with_seq

    path = Path(log_path(str(projects)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(encode_log_line(with_seq(r, i + 1)).rstrip(b"\n") + b"\n"
                              for i, r in enumerate(records)))


def hold(projects, name, data=None):
    """A record folder for name, its manifest at record_path holding data (any bytes: nothing decodes it)."""
    from mu_spec.codec import record_path

    path = Path(record_path(str(projects), name))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if data is not None else json.dumps({"held": name}).encode())


def files(root):
    """Every file under root: relative posix path -> bytes."""
    root = Path(root)
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def folder_of(store, name):
    """The record folder of name in store, relative to store, as posix."""
    from mu_spec.codec import record_path

    from mu_spec.outside import PROJECTS_SUBDIR

    projects = Path(store) / PROJECTS_SUBDIR
    return (Path(record_path(str(projects), name)).parent.relative_to(Path(store))).as_posix()


def outside(rel, folders):
    return not any(rel == f or rel.startswith(f + "/") for f in folders)


def test_lines_classify_start_store(tmp_path):
    from mu_spec.codec import (
        EVENT_KINDS,
        MUTATION_KIND,
        inbox_path,
        new_log_record,
        record_path,
    )
    from mu_spec.order import names_sorted
    from unitcheck.replay import (
        CREATE_TOOL,
        FORK_TOOL,
        REPLAY_SCRIPT,
        build_start_store,
        classify,
        read_lines,
    )
    from unitcheck.runs import ReplayHistory, confirm

    from mu_spec.outside import PROJECTS_SUBDIR

    P = tmp_path / "pristine"
    projects = P / PROJECTS_SUBDIR
    projects.mkdir(parents=True)
    event = next(k for k in sorted(EVENT_KINDS) if k != MUTATION_KIND)
    separated = dumps({"in_response_to": "msg-1", "actor": "a",
                       "entries": [{"layer": "S", "title": "one two", "derives_from": ["A-31 (was A-01)"]}]},
                      ensure_ascii=False)
    assert " " in separated
    at = 1764100000.0
    write_log(projects, [
        mutation(*route(CREATE_TOOL), create_body("a"), 201, "a", at),
        new_log_record(event, "a", [], {"note": "an event"}, at + 1),
        mutation(*route("submit_amendment", "a"), separated, 200, "a", at + 2),
        mutation(*route(FORK_TOOL, "a"), fork_body("a", "b"), 201, "b", at + 3),
        mutation(*route(FORK_TOOL, "x"), fork_body("x", "c"), 201, "c", at + 4),
        mutation(*route("raise_issue", "d"), dumps({"title": "t"}), 200, "d", at + 5),
        mutation(*route(CREATE_TOOL), create_body("e"), 409, "e", at + 6),
        mutation("POST", "/no/such/route", dumps({"a": 1}), 404, None, at + 7),
    ])
    for name in ("a", "b", "c", "d", "x"):
        hold(projects, name)
    inbox = Path(inbox_path(str(projects)))
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_bytes(b'{"held": "inbox"}')

    lines = read_lines(str(P))
    assert len(lines) == 7, lines
    assert [line.position for line in lines] == list(range(7)), lines
    text = lines[1].call.body.decode("utf-8")
    assert " " in text, text
    assert lines[1].call.name == "submit_amendment", lines[1]
    assert lines[0].call.kind == "tool" and lines[0].call.name == CREATE_TOOL, lines[0]
    assert lines[6].call.kind == "unrouted" and lines[6].call.name == "", lines[6]
    assert (lines[6].call.method, lines[6].call.path) == ("POST", "/no/such/route"), lines[6]
    assert lines[6].project is None, lines[6]
    assert confirm(ReplayHistory("unrouted", REPLAY_SCRIPT, (lines[6].call,))) == ()
    assert lines[5].admitted is False and lines[5].project == "e", lines[5]
    assert [line.admitted for line in lines[:5]] == [True] * 5, lines

    c = classify(lines, ("a", "b", "c", "d", "x"))
    assert c.replayed == ("a", "b"), c
    assert c.stored_state == tuple(names_sorted(["x", "e", "d", "c"])) == ("c", "d", "e", "x"), c
    assert dict(c.source) == {"b": "a"}, c

    out = tmp_path / "out"
    before = files(P)
    build_start_store(str(P), ("a", "b"), str(out))
    assert files(P) == before
    for name in ("a", "b"):
        assert not Path(record_path(str(out / PROJECTS_SUBDIR), name)).parent.exists(), name
    removed = [folder_of(P, "a"), folder_of(P, "b")]
    kept = {rel: data for rel, data in before.items() if outside(rel, removed)}
    assert any(rel.startswith(folder_of(P, "d") + "/") for rel in kept), kept
    assert files(out) == kept, sorted(set(files(out)) ^ set(kept))


def test_whole_of_and_agreement():
    from unitcheck.replay import CLIP_AT, agrees, same, whole_of

    t = amendment_text()
    assert len(t) > CLIP_AT
    w = whole_of(t[:CLIP_AT])
    assert list(w.inputs) == ["in_response_to", "actor"], w
    assert [w.inputs["in_response_to"], w.inputs["actor"]] == ["msg-1", "a"], w
    assert w.cut_input == "entries", w
    assert tuple(w.elements) == (E0, E1), w.elements
    assert w.end == t.index(dumps(E1)) + len(dumps(E1)), (w.end, CLIP_AT)

    t2 = dumps({"actor": "a", "in_response_to": "x" * 3000})
    w2 = whole_of(t2[:CLIP_AT])
    assert dict(w2.inputs) == {"actor": "a"}, w2
    assert w2.cut_input is None and tuple(w2.elements) == (), w2
    assert w2.end == t2.index(dumps("a")) + len(dumps("a")), w2.end

    t3 = dumps({"actor": "a", "entries": [E2]})
    w3 = whole_of(t3[:CLIP_AT])
    assert w3.cut_input == "entries" and tuple(w3.elements) == (), w3
    assert w3.end == t3.index("[") + 1, w3.end

    ids = ["S-10", "S-13 (was S-11)"]
    assert same("#0", "S-10", ids) is True
    assert same("#1", "S-10", ids) is False
    assert same("#2", "S-10", ids) is False
    assert same("#0", "#0", None) is True
    assert same("#01", "S-10", ["S-10"]) is False
    assert same(True, 1, None) is False
    assert same(1, 1.0, None) is True
    assert same("a", "a", None) is True
    assert same(None, None, None) is True
    assert same({"a": 1}, {"a": 1, "b": 2}, None) is True
    assert same({"a": 1, "c": 0}, {"a": 1}, None) is False
    assert same([1], [1, 2], None) is False

    R = stored_amendment()
    assert agrees(w, "submit_amendment", R) is True
    other = stored_amendment()
    other["entries"][1]["depends_on"] = ["S-500 (was S-09)"]
    assert agrees(w, "submit_amendment", other) is False
    assert agrees(w, "submit_amendment", stored_amendment(actor="b")) is False
    short = stored_amendment()
    short["entries"] = short["entries"][:1]
    assert agrees(w, "submit_amendment", short) is False


def test_complete_and_rebuild():
    import dataclasses

    from mu_spec.tools.schemas import BY_NAME
    from unitcheck.replay import (
        CLIP_AT,
        CLIP_MARK,
        REBUILDABLE,
        LogLine,
        complete,
        rebuild,
        whole_of,
    )
    from unitcheck.runs import Call

    assert REBUILDABLE == {"submit_amendment": "amendment", "raise_issue": "issue", "post_request": "request"}
    t = amendment_text()
    clipped = t[:CLIP_AT]
    w = whole_of(clipped)
    SHAPE = BY_NAME["submit_amendment"].input_schema
    props = SHAPE["properties"]
    DEF = props["dry_run"]["default"]
    missing = object()

    def default(k):
        return props[k].get("default", missing) if isinstance(props.get(k), dict) else missing

    items = props["entries"].get("items", {}).get("properties", {})
    item_defaults = {k: v["default"] for k, v in items.items()
                     if isinstance(v, dict) and "default" in v and k not in E2 and k != "id"}
    R = stored_amendment(dry_run=DEF, idempotency_key="k")
    R["entries"][2].update(item_defaults)

    c = complete(clipped, w, "submit_amendment", R)
    assert c.startswith(clipped[:w.end]), (w.end, c[:w.end + 40])
    d = json.loads(c)
    assert d["entries"][:2] == [E0, E1], d["entries"][:2]
    third = d["entries"][2]
    assert "id" not in third, third.keys()
    assert third["title"] == "x" * 3000
    assert third["depends_on"] == ["#1"], third["depends_on"]
    assert not set(item_defaults) & set(third), (item_defaults, third.keys())
    assert "dry_run" not in d, d.keys()
    keys = list(d)
    after = keys[keys.index("entries") + 1:]
    assert after == [k for k in props if k in R and k not in ("in_response_to", "actor", "entries")
                     and R[k] != default(k)], after
    assert "id" not in d and "at" not in d, keys
    assert set(d) <= set(props), set(d) - set(props)

    method, path = route("submit_amendment", "p")
    line = LogLine(position=2, project="p",
                   call=Call("tool", "submit_amendment", method, path, (clipped + CLIP_MARK).encode("utf-8"), "p"),
                   admitted=True, cut=True)

    claimed = set()
    got = rebuild(line, [R], claimed)
    assert got is not None, got
    assert got.body == c.encode("utf-8"), got.body[:80]
    assert got.record_id == "am-9" and got.ambiguous is False, got
    assert claimed == {"am-9"}, claimed

    R2 = dict(R, id="am-10")
    claimed = set()
    first = rebuild(line, [R, R2], claimed)
    assert first is not None and first.record_id == "am-9" and first.ambiguous is True, first
    second = rebuild(line, [R, R2], claimed)
    assert second is not None and second.record_id == "am-10" and second.ambiguous is False, second
    assert claimed == {"am-9", "am-10"}, claimed

    assert rebuild(line, [dict(R, actor="b")], set()) is None
    assert "set_labels" not in REBUILDABLE
    relabelled = dataclasses.replace(line, call=dataclasses.replace(line.call, name="set_labels"))
    assert rebuild(relabelled, [R], set()) is None


def scripted(base, name, label, makes, requests, nonce=False, tweak=None):
    """A scratch install of UNIT_MAIN under base/name, and its UnitCommand."""
    from mu_spec.outside import ENV_NAMES, PROJECTS_SUBDIR
    from startcheck.runner import UnitCommand

    config = {"host": ENV_NAMES["host"], "port": ENV_NAMES["port"], "state": ENV_NAMES["state_dir"],
              "projects": PROJECTS_SUBDIR, "makes": makes, "requests": str(requests), "nonce": nonce,
              "tweak": tweak}
    package = base / name / "mu_spec"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(UNIT_MAIN % (config,))
    return UnitCommand(label=label, install_root=str(package.parent))


def runs_of(requests):
    """The requests a scripted install noted, one list per process it ran as, in launch order."""
    out = []
    for text in requests.read_text().splitlines() if requests.exists() else ():
        line = json.loads(text)
        if line.get("start"):
            out.append([])
        elif out:
            out[-1].append((line["method"], line["path"], line["body"].encode("latin-1")))
    return out


def replay_store(tmp_path):
    """P of the scripted replay, and the scripted units' creation and fork rules for its lines."""
    from unitcheck.replay import CLIP_AT, CLIP_MARK, CREATE_TOOL, FORK_TOOL

    from mu_spec.outside import PROJECTS_SUBDIR

    P = tmp_path / "pristine"
    projects = P / PROJECTS_SUBDIR
    projects.mkdir(parents=True)
    cut = amendment_text()[:CLIP_AT] + CLIP_MARK
    refused = dumps({"in_response_to": "msg-2", "actor": "z", "entries": [E2]})[:CLIP_AT] + CLIP_MARK
    emit_method, emit_path = route("emit_tickets", "a")
    at = 1764100000.0
    write_log(projects, [
        mutation(*route(CREATE_TOOL), create_body("a"), 201, "a", at),
        mutation(*route("raise_issue", "a"), dumps({"title": "one"}), 200, "a", at + 1),
        mutation(*route("submit_amendment", "a"), cut, 200, "a", at + 2),
        mutation(*route("submit_amendment", "a"), refused, 400, "a", at + 3),
        mutation(emit_method, emit_path, dumps({}), 202, "a", at + 4),
        mutation(*route(FORK_TOOL, "a"), fork_body("a", "b"), 201, "b", at + 5),
        mutation(*route("raise_issue", "b"), dumps({"title": "two"}), 200, "b", at + 6),
        mutation(*route("raise_issue", "d"), dumps({"title": "three"}), 200, "d", at + 7),
        mutation("POST", "/no/such/route", dumps({}), 404, None, at + 8),
    ])
    hold(projects, "a")
    hold(projects, "d")
    create_method, create_path = route(CREATE_TOOL)
    fork_method, fork_path = route(FORK_TOOL, "a")
    makes = {f"{create_method} {create_path} {create_body('a')}": ["create", "a", None],
             f"{fork_method} {fork_path} {fork_body('a', 'b')}": ["fork", "b", "a"]}
    return P, makes


def drive(tmp_path, monkeypatch, tag, P, makes, records, nonce=False, tweak=None):
    """One run of the replay check over P on a fresh scripted pair; the stored amendments of 'a' are records.

    The running unit's storing rule is its own, so the records stored_records gives for 'a' are fed at that
    seam; every launch, restore and stop of run is recorded and passed on to the real one."""
    from unitcheck.runs import Call

    from unitcheck import replay

    base = tmp_path / tag
    root = base / "check"
    root.mkdir(parents=True)
    prompts = base / "prompts"
    prompts.mkdir()
    work = base / "work"
    work.mkdir()
    requests = {"running": base / "running.jsonl", "rebuilt": base / "rebuilt.jsonl"}
    running = scripted(base / "installs", "running", "running", makes, requests["running"], nonce=nonce)
    rebuilt = scripted(base / "installs", "rebuilt", "rebuilt", makes, requests["rebuilt"], tweak=tweak)
    installs = {norm(running.install_root): "running", norm(rebuilt.install_root): "rebuilt"}

    asked = []

    def stored_records(pristine_store, project, kind):
        asked.append((norm(pristine_store), project, kind))
        return tuple(copy.deepcopy(records)) if (project, kind) == ("a", "amendment") else ()

    events = []
    real_restore, real_launch, real_stop = replay.restore, replay.launch, replay.stop

    def restoring(check_root, source, pristine_prompts):
        events.append(("restore", norm(source), time.monotonic()))
        return real_restore(check_root, source, pristine_prompts)

    def launching(cmd, *args, **kwargs):
        events.append(("launch", installs.get(norm(cmd.install_root)), time.monotonic()))
        return real_launch(cmd, *args, **kwargs)

    def stopping(*args, **kwargs):
        try:
            return real_stop(*args, **kwargs)
        finally:
            events.append(("stop", None, time.monotonic()))

    def read_calls(p):
        method, path = route("get_units", p)
        return (Call("tool", "get_units", method, path, None, p),)

    with monkeypatch.context() as m:
        m.setattr(replay, "stored_records", stored_records)
        m.setattr(replay, "restore", restoring)
        m.setattr(replay, "launch", launching)
        m.setattr(replay, "stop", stopping)
        report, out = replay.run(running, rebuilt, str(root), str(P), str(prompts), dict(os.environ),
                                 read_calls=read_calls, work=str(work))
    seen = {label: runs_of(path) for label, path in requests.items()}
    return report, out, {"asked": asked, "events": events, "seen": seen, "work": work}


def names(text, word):
    return re.search(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])", text) is not None


def test_replay_run_scripted(tmp_path, monkeypatch):
    from unitcheck.replay import (
        DECLARATION_GAP_S,
        GATE_READS,
        NO_PROJECT,
        REBUILDABLE,
        REPLAY_SCRIPT,
        read_lines,
        rebuild,
    )
    from unitcheck.runs import confirm

    from mu_spec.outside import PROJECTS_SUBDIR

    P, makes = replay_store(tmp_path)
    R = stored_amendment()
    lines = read_lines(str(P))
    assert len(lines) == 9, lines
    assert [line.cut for line in lines] == [False, False, True, True] + [False] * 5, lines
    body2 = rebuild(lines[2], [R], set()).body

    report, out, seen = drive(tmp_path, monkeypatch, "main", P, makes, [R])
    assert (norm(P), "a", REBUILDABLE["submit_amendment"]) in seen["asked"], seen["asked"]
    assert report.passed, report.failures
    assert report.failures == () and report.outbound == (), report
    tally = report.replayed["a"]
    assert tally.rebuilt == 1 and tuple(tally.ambiguous) == () and tuple(tally.left_out) == (3, 4), tally
    assert report.replayed["a"].sent + report.replayed["b"].sent == 5, report.replayed
    assert dict(report.stored_state) == {"d": 1}, report.stored_state
    assert tuple(report.unrouted) == (8,), report.unrouted

    # What the units were sent: never a left-out, cut refused or stored-state line; the rebuilt body for line 2.
    running_runs, rebuilt_runs = seen["seen"]["running"], seen["seen"]["rebuilt"]
    assert len(running_runs) == 3 and len(rebuilt_runs) == 2, (len(running_runs), len(rebuilt_runs))
    every = [q for run in running_runs + rebuilt_runs for q in run]
    emit, write_d = lines[4].call, lines[7].call
    assert not [q for q in every if (q[0], q[1].split("?")[0]) == (emit.method, emit.path)], "emit_tickets sent"
    assert not [q for q in every if (q[0], q[1]) == (write_d.method, write_d.path)], "the write to 'd' sent"
    assert not [q for q in every if q[2] == lines[3].call.body], "the cut refused line sent"

    def driven(requests):
        """The requests of one run from its first line on, the launch's own health reads left out."""
        kept = [q for q in requests if (q[0], q[1].split("?")[0]) != ("GET", "/health")]
        first = next(i for i, q in enumerate(kept) if (q[0], q[1], q[2]) == (
            lines[0].call.method, lines[0].call.path, lines[0].call.body))
        return kept[first:]

    r1, r2, b = driven(running_runs[0]), driven(running_runs[1]), driven(rebuilt_runs[0])
    for label, run in (("r1", r1), ("b", b)):
        assert ("POST", "/no/such/route", lines[8].call.body) in run, label
        amended = [q for q in run if (q[0], q[1]) == (lines[2].call.method, lines[2].call.path)]
        assert [q[2] for q in amended] == [body2], (label, [q[2][:80] for q in amended])
    assert r1 == b, "the running and rebuilt units were sent different requests"

    # GATE_READS for every project the store then holds, after the last line, on r1, r2 and b.
    gates = {route(tool, p) for tool in GATE_READS for p in ("a", "b", "d")}
    for label, run in (("r1", r1), ("r2", r2), ("b", b)):
        last = max(i for i, q in enumerate(run) if (q[0], q[1]) == ("POST", "/no/such/route"))
        assert gates <= {(q[0], q[1]) for q in run[last + 1:]}, (label, gates)

    # The hand-off: the histories, a fork's prefixed by its source's, and the start store.
    assert out is not None
    assert [h.name for h in out.histories] == ["a", "b", NO_PROJECT], out.histories

    def form(call):
        return call.method, call.path, call.body

    of_a = [form(lines[0].call), form(lines[1].call), (lines[2].call.method, lines[2].call.path, body2)]
    histories = {h.name: h for h in out.histories}
    assert [form(c) for c in histories["a"].calls] == of_a, histories["a"].calls
    assert [form(c) for c in histories["b"].calls] == of_a + [form(lines[5].call), form(lines[6].call)], (
        histories["b"].calls)
    assert [form(c) for c in histories[NO_PROJECT].calls] == [form(lines[8].call)], histories[NO_PROJECT]
    for h in out.histories:
        assert h.script == REPLAY_SCRIPT, h.name
        assert confirm(h) == (), (h.name, confirm(h))
    start = Path(out.start_store)
    for name in ("a", "b"):
        assert not (start / folder_of(P, name)).exists(), name
    d_files = {rel: data for rel, data in files(P).items() if not outside(rel, [folder_of(P, "d")])}
    assert d_files and all((start / rel).read_bytes() == data for rel, data in d_files.items()), d_files.keys()

    # The declaration gap and the swap.
    events = seen["events"]
    launches = [(i, e) for i, e in enumerate(events) if e[0] == "launch"]
    stops = [e for e in events if e[0] == "stop"]
    assert len(launches) >= 5 and stops, events
    assert launches[1][1][2] - stops[0][2] >= DECLARATION_GAP_S, events

    def source_of(i):
        return next(e[1] for e in reversed(events[:i]) if e[0] == "restore")

    sourced = {(e[1], source_of(i)) for i, e in launches}
    work = seen["work"]
    assert ("running", norm(work / "b")) in sourced, sourced
    assert ("rebuilt", norm(work / "r1")) in sourced, sourced
    assert ("running", norm(Path(work) / "start")) in sourced, sourced
    assert (start / PROJECTS_SUBDIR).is_dir()

    # Two stored amendments agree with line 2: reported ambiguous, never failed.
    report, out, _ = drive(tmp_path, monkeypatch, "ambiguous", P, makes, [R, dict(R, id="am-10")])
    assert report.passed is True, report.failures
    assert tuple(report.replayed["a"].ambiguous) == (2,), report.replayed["a"]
    assert out is not None

    # None agrees with line 2: the check fails for that line and hands nothing off.
    report, out, _ = drive(tmp_path, monkeypatch, "unmatched", P, makes, [dict(R, actor="b")])
    assert report.passed is False, report
    assert any(names(f, "a") and names(f, "2") for f in report.failures), report.failures
    assert out is None

    # The rebuilt unit answers line 6 with one byte changed.
    tweak = f"{lines[6].call.method} {lines[6].call.path}"
    report, out, _ = drive(tmp_path, monkeypatch, "differs", P, makes, [R], tweak=tweak)
    assert report.passed is False, report
    assert any(names(f, "b") and names(f, "6") and (lines[6].call.name in f or lines[6].call.path in f)
               for f in report.failures), report.failures
    assert out is None

    # The running unit adds a per-run random nonce to every answer: undeclared between r1 and r2.
    report, out, _ = drive(tmp_path, monkeypatch, "nonce", P, makes, [R], nonce=True)
    assert report.passed is False, report
    assert any(re.search(r"nonce\b", f) for f in report.failures), report.failures
    assert out is None
