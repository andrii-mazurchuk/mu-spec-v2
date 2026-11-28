"""unitcheck.events: check_history over scripted units launched through the shared launch.

test_events_check_judgements drives history H (a write, the same write as a dry run, a read) and a
create-then-read history through check_history, each case under one session with a scratch start store,
and judges the verdicts and the unit runs the events module made. Each scripted unit's listener is a
ThreadingHTTPServer on the injected host and port. The modules under test are imported inside the test,
so while they are absent only this test fails.
"""

import json
import os
import re
import time

# A scripted unit, configured by CONFIG. It keeps <store root>/projects/<p>/record.json per project and
# appends its records to the followed log as JSON lines: each real write appends one event (kind 'event',
# the next seq) and one change-log line (kind 'mutation'). CONFIG["routes"]["<method> <path>"] names what
# a request is: 'create' (409 when the project folder is present), 'write' (a body with dry_run true is a
# dry run and records nothing), 'read' (the record), 'events' (the project's events after since) or 'wide'
# (every event). CONFIG["records"] adds 'nonce' (one random value per process) or 'frozen_at' ('at': 0.0)
# to every record; CONFIG["answer"] adds 'written_on' to a real write's answer; CONFIG["file"] adds
# 'updated' or 'at' to the record file, each time.time(). CONFIG["extra"]: a dry run records one event.
# CONFIG["late"]: a real write's change-log line is held and appended at the next write instead.
UNIT_MAIN = r'''
import json, os, random, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

CONFIG = %r
LOCK = threading.Lock()
NONCE = random.SystemRandom().randrange(1, 10 ** 12)
PENDING = []


def root():
    return os.path.join(os.environ[CONFIG["state"]], "projects")


def record_file(project):
    return os.path.join(root(), project, "record.json")


def append(record):
    with open(CONFIG["log"], "ab") as f:
        f.write(json.dumps(record, sort_keys=True).encode() + b"\n")


def stamped(record):
    if "nonce" in CONFIG["records"]:
        record["nonce"] = NONCE
    if "frozen_at" in CONFIG["records"]:
        record["at"] = 0.0
    return record


def logged():
    if not os.path.exists(CONFIG["log"]):
        return []
    out = []
    with open(CONFIG["log"], "rb") as f:
        for line in f.read().split(b"\n"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def events(project, since):
    return [r for r in logged() if r.get("kind") == "event" and r["seq"] > since
            and (project is None or r.get("project") == project)]


def event(project, what):
    seq = max([r["seq"] for r in logged() if r.get("kind") == "event"] or [0]) + 1
    append(stamped({"kind": "event", "seq": seq, "project": project, "type": what}))


def line(project, what):
    record = stamped({"kind": "mutation", "project": project, "what": what})
    if CONFIG["late"]:
        PENDING.append(record)
    else:
        append(record)


def write_file(project, n):
    doc = {"n": n}
    for field in CONFIG["file"]:
        doc[field] = time.time()
    with open(record_file(project), "wb") as f:
        f.write(json.dumps(doc).encode())


def answer(method, path, query, body):
    what, project = CONFIG["routes"].get(method + " " + path, (None, None))
    if what == "create":
        name = json.loads(body or b"{}")["project"]
        if os.path.exists(os.path.join(root(), name)):
            return 409, {"error": "exists"}
        os.mkdir(os.path.join(root(), name))
        write_file(name, 0)
        event(name, "created")
        line(name, "create " + name)
        return 201, {"ok": True}
    if what == "write":
        while PENDING:
            append(PENDING.pop(0))
        if json.loads(body or b"{}").get("dry_run") is True:
            if CONFIG["extra"]:
                event(project, "extra")
            return 200, {"dry_run": True}
        with open(record_file(project), "rb") as f:
            n = json.loads(f.read())["n"]
        write_file(project, n + 1)
        event(project, "written")
        line(project, "write " + project)
        out = {"ok": True}
        if "written_on" in CONFIG["answer"]:
            out["written_on"] = time.time()
        return 200, out
    if what == "read":
        if not os.path.exists(record_file(project)):
            return 404, {"error": "not found"}
        with open(record_file(project), "rb") as f:
            return 200, json.loads(f.read())
    if what in ("events", "wide"):
        since = parse_qs(query).get("since", ["-1"])[0]
        return 200, {"events": events(project, int(since) if what == "events" else -1)}
    return 200, {}


class Handler(BaseHTTPRequestHandler):
    def _answer(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        path, _, query = self.path.partition("?")
        with LOCK:
            status, doc = answer(self.command, path, query, body)
        out = json.dumps(doc, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    append({"kind": "mutation", "what": "start"})
    ThreadingHTTPServer((os.environ[CONFIG["host"]], int(os.environ[CONFIG["port"]])), Handler).serve_forever()
'''


def filled(path, project):
    return "/" + re.sub(r"\{[^}]+\}", project, path).lstrip("/")


def tool_call(name, project, body=None):
    """A 'tool' call of the named tool on project, its path the tool's pattern filled, found by find_tool."""
    from mu_spec.declarations import TOOLS, find_tool
    from unitcheck.runs import Call

    (spec,) = [t for t in TOOLS if t.name == name]
    path = filled(spec.path, project)
    hit = find_tool(spec.method, path)
    assert hit is not None and hit[0].name == name, (name, path, hit)
    return Call("tool", name, spec.method, path, body, project)


def routes(projects):
    """CONFIG["routes"] for the scripted unit over the projects named."""
    from unitcheck.runs import EVENTS_PATH, UNIT_EVENTS

    create = tool_call("create_project", "q", b"{}")
    out = {f"{create.method} {create.path}": ("create", None),
           f"{UNIT_EVENTS[0]} {UNIT_EVENTS[1].split('?')[0]}": ("wide", None)}
    for p in projects:
        write, read = tool_call("raise_issue", p, b"{}"), tool_call("get_units", p)
        out[f"{write.method} {write.path}"] = ("write", p)
        out[f"{read.method} {read.path}"] = ("read", p)
        out["GET " + EVENTS_PATH.format(project=p)] = ("events", p)
    return out


def unit(base, name, label, check_root, records=(), answer=(), file=(), extra=False, late=False):
    """A scratch install of UNIT_MAIN under base/name, at its start module, and its UnitCommand."""
    from unitcheck.launch import followed_log

    from mu_spec.outside import ENV_NAMES
    from startcheck.runner import ENTRY_FILE, UnitCommand

    config = {"host": ENV_NAMES["host"], "port": ENV_NAMES["port"], "state": ENV_NAMES["state_dir"],
              "log": str(followed_log(str(check_root))), "routes": routes(("p", "q")),
              "records": list(records), "answer": list(answer), "file": list(file), "extra": extra, "late": late}
    install = base / name
    start = install.joinpath(*ENTRY_FILE)
    start.parent.mkdir(parents=True)
    (start.parent / "__init__.py").write_text("")
    start.write_text(UNIT_MAIN % (config,))
    return UnitCommand(label=label, install_root=str(install))


def store_with(base, *projects):
    """A start store holding projects/<p>/record.json, {"n": 0}, for each project named."""
    (base / "projects").mkdir(parents=True, exist_ok=True)
    for p in projects:
        (base / "projects" / p).mkdir()
        (base / "projects" / p / "record.json").write_bytes(json.dumps({"n": 0}).encode())
    return base


def script():
    from shipcheck.standin import Script

    return Script(issues=(), links=(), next_number=1, next_id=1, user={"login": "x"}, push=True,
                  refuse={}, unanswered=frozenset(), hold=None)


def paths(v, sources):
    """The path text of each undeclared entry 'call i, source, path' whose source is in sources."""
    out = []
    for text in v.undeclared:
        _, source, path = text.split(", ", 2)
        if source.strip() in sources:
            out.append(path.strip("()[]'\" ,"))
    return out


class Recorder:
    """Stands in for any object: every method called on it is noted and does nothing."""

    def __init__(self, called):
        self._called = called

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self._called.append(name)

        return method


def test_events_check_judgements(tmp_path, monkeypatch):
    import inspect

    import unitcheck.events
    import unitcheck.runs
    from unitcheck.events import DECLARATION_GAP_S, DECLARED_SOURCES, check_history
    from unitcheck.runs import Call, ReplayHistory
    from unitcheck.session import open_session

    assert DECLARED_SOURCES == ("answer", "events", "wide", "log", "state")

    root = tmp_path / "check"
    root.mkdir()
    live = tmp_path / "live"
    store_with(live / "store", "p", "q")
    prompts = live / "prompts"
    prompts.mkdir()
    start_store = store_with(tmp_path / "start", "p")
    installs = tmp_path / "installs"
    count = [0]

    def scripted(label, **kw):
        count[0] += 1
        return unit(installs, f"u{count[0]}", label, root, **kw)

    H = ReplayHistory("h", script(), (tool_call("raise_issue", "p", b"{}"),
                                      tool_call("raise_issue", "p", b'{"dry_run": true}'),
                                      tool_call("get_units", "p")))
    own_env = dict(os.environ)

    # Not driven: a call confirm rejects; nothing launched, reset or set.
    called = []
    refused = ReplayHistory("refused", script(), (Call("tool", "no_such_tool", "GET", "/x", None, "p"),) + H.calls)
    with monkeypatch.context() as m:
        m.setattr(unitcheck.runs, "launch", lambda *a, **kw: called.append("launch"))
        v = check_history(refused, scripted("running"), scripted("rebuilt"), str(root), str(start_store),
                          str(prompts), own_env, Recorder(called), Recorder(called))
    assert v.not_driven != () and v.passed is False, v
    assert v.digests_ok is False and v.all_ended is False, v
    assert (v.mismatches, v.undeclared, v.never_varied, v.outbound) == ((), (), (), ()), v
    assert called == [], called

    real_unit_run = unitcheck.events.unit_run
    real_launch = unitcheck.runs.launch
    session = open_session(own_env)

    def judged(history, running, rebuilt, store=start_store):
        """check_history of history, with every unit run it made and the times around them."""
        made, times, current = [], [], [None]

        def recording(*args, **kwargs):
            bound = inspect.signature(real_unit_run).bind(*args, **kwargs)
            current[0] = bound.arguments["setting"].label
            out = real_unit_run(*args, **kwargs)
            times.append((current[0], "stopped", time.monotonic()))
            made.append(out)
            return out

        def launching(*args, **kwargs):
            times.append((current[0], "launch", time.monotonic()))
            return real_launch(*args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(unitcheck.events, "unit_run", recording)
            m.setattr(unitcheck.runs, "launch", launching)
            v = check_history(history, running, rebuilt, str(root), str(store), str(prompts), own_env,
                              session.standin, session.neighbour)
        assert v.not_driven == () and v.digests_ok is True and v.all_ended is True, v
        by_label = {r.setting.label: r for r in made}
        assert [r.setting.label for r in made] == ["decl1", "decl2", "healthy"], made
        assert [r.unit for r in made] == ["running", "running", "rebuilt"], made
        # Gap: decl2 launches DECLARATION_GAP_S or more after decl1 stopped.
        (stopped,) = [t for label, what, t in times if (label, what) == ("decl1", "stopped")]
        launched = [t for label, what, t in times if (label, what) == ("decl2", "launch")]
        assert launched and launched[0] - stopped >= DECLARATION_GAP_S, times
        assert by_label["decl1"].setting.keep_files is True, by_label["decl1"].setting
        assert by_label["decl2"].setting.keep_files is True, by_label["decl2"].setting
        assert by_label["healthy"].setting.keep_files is False, by_label["healthy"].setting
        return v, by_label

    try:
        # Start store: each run copies the start store given, so the create of q is admitted on all three.
        created = ReplayHistory("create", script(), (tool_call("create_project", "q", b'{"project": "q"}'),
                                                     tool_call("get_units", "q")))
        v, runs = judged(created, scripted("running"), scripted("rebuilt"))
        for label, r in runs.items():
            status = r.calls[0].status
            assert status is not None and 200 <= status < 300, (label, r.calls[0])
        assert v.passed is True, v

        # Extra: one more event on the dry run is a mismatch in the events read and the log at call 1.
        v, _ = judged(H, scripted("running"), scripted("rebuilt", extra=True))
        assert {(1, "events"), (1, "log")} <= set(v.mismatches), v.mismatches
        assert v.passed is False, v

        # Late: the right line one call late fails at both calls, though the final log is the same.
        v, _ = judged(H, scripted("running"), scripted("rebuilt", late=True))
        assert {(0, "log"), (1, "log")} <= set(v.mismatches), v.mismatches
        assert v.passed is False, v

        # Nonce: a per-process value in the records is undeclared in the records read.
        v, _ = judged(H, scripted("running", records=("nonce",)), scripted("rebuilt", records=("nonce",)))
        assert any(p.endswith("nonce") for p in paths(v, ("events", "wide", "log"))), v.undeclared
        assert v.passed is False, v

        # written_on: a time in the write's answer is undeclared in the answer.
        v, _ = judged(H, scripted("running", answer=("written_on",)), scripted("rebuilt", answer=("written_on",)))
        assert any(p.endswith("written_on") for p in paths(v, ("answer",))), v.undeclared
        assert v.passed is False, v

        # updated: a time in the stored record is undeclared in the state, at the record's relpath.
        v, runs = judged(H, scripted("running", file=("updated",)), scripted("rebuilt", file=("updated",)))
        (rel,) = [rel for rel, _ in runs["decl1"].calls[0].changed if rel.endswith("record.json")]
        assert any(p.startswith(rel) and p.endswith("updated") for p in paths(v, ("state",))), (rel, v.undeclared)
        assert v.passed is False, v

        # at: a declared time field in the stored record passes the state declaration.
        v, _ = judged(H, scripted("running", file=("at",)), scripted("rebuilt", file=("at",)))
        assert paths(v, ("state",)) == [], v.undeclared

        # Frozen at: a declared field that never varies is reported, never failed.
        v, _ = judged(H, scripted("running", records=("frozen_at",)), scripted("rebuilt", records=("frozen_at",)))
        assert "at" in v.never_varied, v
        assert v.mismatches == () and v.undeclared == (), v
        assert v.passed is True, v
    finally:
        session.neighbour.set("present")
        session.close()
