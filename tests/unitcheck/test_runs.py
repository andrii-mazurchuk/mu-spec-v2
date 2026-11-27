"""unitcheck.runs: confirm, unit_run and outbound_faults, over scripted units launched through the shared launch.

test_outbound_faults drives scripted units, one unit_run each under a fresh session, and judges their records;
test_unit_run_settles drives a scripted unit that keeps one record file per project and serves the emission
read; test_confirm_and_not_driven judges confirm over built histories, then drives one unrouted call. Each
scripted unit's listener is a ThreadingHTTPServer on the injected host and port. The modules under test are
imported inside each test, so while they are absent only these tests fail.
"""

import json
import os
import re

# A scripted unit, configured by CONFIG: before serving it does each of CONFIG["start"]; on each request it
# first does each of CONFIG["hooks"][path], then answers. With CONFIG["store"] it keeps <store root>/<p>/record.json
# per project: POST /projects creates one (409 when the folder is present), POST /projects/<p>/issues rewrites
# it, GET /projects/<p>/units reads it, POST /projects/<p>/emit answers 202 and starts a run that asks GitHub for
# /repos/o/r/issues 1.2 s after the press and ends at 1.5 s, GET /projects/<p>/emit answers {"running": ...}.
# Any other request answers CONFIG["answers"]["<method> <path>"], else 200 {}. With CONFIG["requests"] every
# request is noted there, with its answer, before the answer is sent.
UNIT_MAIN = r'''
import http.client, json, os, socket, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG = %r
LOCK = threading.Lock()
EMISSION = {"running": False}


def act(what):
    kind, _, arg = what.partition(" ")
    try:
        if kind == "github":
            method, path = arg.split(" ", 1)
            conn = http.client.HTTPSConnection(CONFIG["github"], 443, timeout=10)
            conn.request(method, path)
            conn.getresponse().read()
            conn.close()
        elif kind == "connect":
            host, port = arg.rsplit(":", 1)
            out = socket.socket()
            out.settimeout(10)
            out.connect((host, int(port)))
            out.sendall(b"ping")
            while out.recv(65536):
                pass
            out.close()
        elif kind == "lookup":
            socket.getaddrinfo(arg, 80)
        elif kind == "subprocess":
            subprocess.run([sys.executable, "-c", "pass"])
        elif kind == "spawn":
            import multiprocessing
            from mu_spec.child import work
            child = multiprocessing.get_context("spawn").Process(target=work)
            child.start()
            child.join()
    except Exception:
        pass


def root():
    return os.path.join(os.environ[CONFIG["state"]], "projects")


def record(project):
    return os.path.join(root(), project, "record.json")


def note(what):
    with open(CONFIG["log"], "ab") as f:
        f.write(json.dumps({"kind": "mutation", "what": what}).encode() + b"\n")


def write_record(project, n):
    with open(record(project), "wb") as f:
        f.write(json.dumps({"n": n}).encode())


def emission():
    began = time.monotonic()
    time.sleep(1.2)
    act("github GET /repos/o/r/issues?state=open&per_page=100&page=1")
    time.sleep(max(0.0, began + 1.5 - time.monotonic()))
    with LOCK:
        EMISSION["running"] = False


def store_answer(method, path, body):
    parts = path.split("/")
    if method == "POST" and path == "/projects":
        name = json.loads(body or b"{}")["project"]
        if os.path.exists(os.path.join(root(), name)):
            return 409, b'{"error": "exists"}'
        os.mkdir(os.path.join(root(), name))
        write_record(name, 0)
        note("create " + name)
        return 201, b'{"ok": true}'
    if len(parts) != 4 or parts[1] != "projects":
        return None
    project, leaf = parts[2], parts[3]
    if method == "POST" and leaf == "issues":
        with open(record(project), "rb") as f:
            n = json.loads(f.read())["n"]
        write_record(project, n + 1)
        note("issue " + project)
        return 200, b'{"ok": true}'
    if method == "GET" and leaf == "units":
        if not os.path.exists(record(project)):
            return 404, b'{"error": "not found"}'
        with open(record(project), "rb") as f:
            return 200, f.read()
    if leaf == "emit" and method == "POST":
        with LOCK:
            EMISSION["running"] = True
        threading.Thread(target=emission, daemon=True).start()
        return 202, b'{"started": true}'
    if leaf == "emit" and method == "GET":
        with LOCK:
            return 200, json.dumps({"running": EMISSION["running"]}).encode()
    return None


class Handler(BaseHTTPRequestHandler):
    def _answer(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        path = self.path.split("?")[0]
        for what in CONFIG["hooks"].get(path, ()):
            act(what)
        found = store_answer(self.command, path, body) if CONFIG["store"] else None
        if found is None:
            status, text = CONFIG["answers"].get(self.command + " " + path, (200, "{}"))
            found = status, text.encode()
        status, out = found
        if CONFIG["requests"]:
            line = {"method": self.command, "path": self.path, "body": body.decode("latin-1"),
                    "status": status, "answer": out.decode("latin-1")}
            with LOCK:
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
    if CONFIG["store"]:
        note("start")
    for what in CONFIG["start"]:
        act(what)
    ThreadingHTTPServer((os.environ[CONFIG["host"]], int(os.environ[CONFIG["port"]])), Handler).serve_forever()
'''

# The spawn child of unit H: one connect to 10.0.0.1:80.
CHILD = r'''
import socket


def work():
    try:
        out = socket.socket()
        out.settimeout(10)
        out.connect(("10.0.0.1", 80))
        out.sendall(b"ping")
        while out.recv(65536):
            pass
        out.close()
    except OSError:
        pass
'''


def unit(base, name, check_root, hooks=None, start=(), answers=None, store=False, requests=None):
    """A scratch install of UNIT_MAIN under base/name, and its UnitCommand."""
    from shipcheck.standin import GITHUB_HOST
    from unitcheck.launch import followed_log

    from mu_spec.outside import ENV_NAMES
    from startcheck.runner import UnitCommand

    config = {"host": ENV_NAMES["host"], "port": ENV_NAMES["port"], "state": ENV_NAMES["state_dir"],
              "github": GITHUB_HOST, "log": str(followed_log(str(check_root))),
              "hooks": {k: list(v) for k, v in (hooks or {}).items()}, "start": list(start),
              "answers": {k: list(v) for k, v in (answers or {}).items()}, "store": store,
              "requests": None if requests is None else str(requests)}
    package = base / name / "mu_spec"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(UNIT_MAIN % (config,))
    (package / "child.py").write_text(CHILD)
    return UnitCommand(label="running", install_root=str(package.parent))


def folders(tmp_path):
    """A check root, an empty scratch start store (projects/ only) and a pristine prompts folder."""
    root = tmp_path / "check"
    root.mkdir()
    start_store = tmp_path / "start-store"
    (start_store / "projects").mkdir(parents=True)
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    return root, start_store, prompts


def store_with(base, *projects):
    """A start store holding projects/<p>/record.json, {"n": 0}, for each project named."""
    for p in projects:
        (base / "projects" / p).mkdir(parents=True)
        (base / "projects" / p / "record.json").write_bytes(json.dumps({"n": 0}).encode())
    (base / "projects").mkdir(parents=True, exist_ok=True)
    return base


def script():
    from shipcheck.standin import Script

    return Script(issues=(), links=(), next_number=1, next_id=1, user={"login": "x"}, push=True,
                  refuse={}, unanswered=frozenset(), hold=None)


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


def action_spec(name):
    from mu_spec.declarations import ACTIONS

    (spec,) = [a for a in ACTIONS if a.name == name]
    return spec


def action_call(name, project, body=b"{}"):
    from mu_spec.declarations import find_action
    from unitcheck.runs import Call

    spec = action_spec(name)
    path = filled(spec.path, project)
    hit = find_action(spec.method, path)
    assert hit is not None and hit[0].name == name, (name, path, hit)
    return Call("action", name, spec.method, path, body, project)


def session_run(history, cmd, setting, root, start_store, prompts, before=None):
    """One unit_run of history on cmd under a fresh session; before(session) runs first when given."""
    from unitcheck.runs import unit_run
    from unitcheck.session import open_session

    own_env = dict(os.environ)
    session = open_session(own_env)
    try:
        if before is not None:
            before(session)
        return unit_run(history, cmd, "running", setting, str(root), str(start_store), str(prompts), own_env,
                        session.standin, session.neighbour)
    finally:
        session.neighbour.set("present")
        session.close()


def noted(requests):
    """The requests a scripted unit noted, in order."""
    if not requests.exists():
        return []
    return [json.loads(line) for line in requests.read_text().splitlines() if line]


def test_outbound_faults(tmp_path):
    from unitcheck.runs import ReplayHistory, RunSetting, outbound_faults

    from unitcheck.neighbour import CONDITIONS

    root, start_store, prompts = folders(tmp_path)
    read, press = tool_call("get_units", "p"), tool_call("check_ship_access", "p")
    h = ReplayHistory("h", script(), (read, press))
    count = [0]

    def run(hooks=None, start=(), condition="present"):
        count[0] += 1
        cmd = unit(tmp_path / "installs", f"u{count[0]}", root, hooks=hooks, start=start)
        r = session_run(h, cmd, RunSetting(condition, condition=condition), root, start_store, prompts)
        assert r.not_driven == () and r.digest_ok is True, r
        assert len(r.calls) == 2, r.calls
        faults = outbound_faults(r)
        assert all(isinstance(text, str) for text in faults), faults
        return r, faults

    # A: a GitHub read outside any window is one fault, naming the request.
    r, faults = run(hooks={read.path: ["github GET /repos/o/r"]})
    assert [(q.method, q.path) for q in r.requests] == [("GET", "/repos/o/r")], r.requests
    assert len(faults) == 1, faults
    assert "/repos/o/r" in faults[0], faults

    # B: the same read while check_ship_access is in flight falls inside that call's window.
    r, faults = run(hooks={press.path: ["github GET /repos/o/r"]})
    assert faults == (), faults
    (j,) = [i for i, q in enumerate(r.requests) if (q.method, q.path) == ("GET", "/repos/o/r")]
    window = r.calls[1].window
    assert window is not None and window[0] <= j < window[1], (j, window)

    # C: GET /user at start-up and on a read is never a fault, and is counted.
    r, faults = run(hooks={read.path: ["github GET /user"]}, start=["github GET /user"])
    assert faults == (), faults
    assert r.login_lookups == 2, r.requests

    # D: a connect routed to the neighbour: one fault for the attempt, one for the Reached.
    r, faults = run(hooks={read.path: ["connect 10.0.0.1:80"]}, condition="present")
    assert len(r.reached) == 1, r.reached
    assert len(faults) == 2, faults

    # E: a refused connect the unit swallows is still a fault, though nothing reached the neighbour.
    r, faults = run(hooks={read.path: ["connect 127.0.0.1:1"]}, condition="refusing")
    assert r.reached == (), r.reached
    assert len(faults) == 1, faults

    # F: a failed lookup the unit swallows is a fault.
    r, faults = run(hooks={read.path: ["lookup nope.invalid"]}, condition="absent")
    assert len(faults) == 1, faults

    # G: a subprocess start is an uncovered start.
    r, faults = run(hooks={read.path: ["subprocess"]})
    assert len(faults) == 1, faults

    # H: a covered spawn is no fault; the child's connect is, and so is its Reached.
    r, faults = run(hooks={read.path: ["spawn"]}, condition="present")
    assert any(a.op == "spawn" and a.child is not None for a in r.attempts), r.attempts
    assert any(a.process != 0 and a.op == "connect" for a in r.attempts), r.attempts
    assert len(r.reached) == 1, r.reached
    assert len(faults) == 2, faults

    # I: a ThreadingHTTPServer that reaches nothing else: its listener's own reverse lookup is no fault.
    for condition in CONDITIONS:
        r, faults = run(condition=condition)
        assert faults == (), (condition, faults)


def test_unit_run_settles(tmp_path, monkeypatch):
    import unitcheck.runs
    from shipcheck.standin import script_digest
    from unitcheck.runs import EMISSION_PATH, Call, ReplayHistory, RunSetting

    root = tmp_path / "check"
    root.mkdir()
    runs = []
    count = [0]

    def scripted(requests=None):
        count[0] += 1
        return unit(tmp_path / "installs", f"s{count[0]}", root, store=True, requests=requests)

    # Start store: the live copy beside the prompts holds q; only start_store is copied.
    live = tmp_path / "live"
    store_with(live / "store", "p", "q")
    prompts = live / "prompts"
    prompts.mkdir()
    without_q = store_with(tmp_path / "start-without-q", "p")
    with_q = store_with(tmp_path / "start-with-q", "p", "q")

    real_restore = unitcheck.runs.restore
    sources = []

    def restoring(check_root, source, pristine_prompts):
        sources.append(os.path.normcase(os.path.abspath(str(source))))
        return real_restore(check_root, source, pristine_prompts)

    monkeypatch.setattr(unitcheck.runs, "restore", restoring)
    create = tool_call("create_project", "q", json.dumps({"project": "q"}).encode())
    h = ReplayHistory("create", script(), (create, tool_call("get_units", "q")))

    r = session_run(h, scripted(), RunSetting("healthy"), root, without_q, prompts)
    runs.append(r)
    assert sources == [os.path.normcase(os.path.abspath(str(without_q)))], sources
    assert r.digest_ok is True and len(r.calls) == 2, r
    assert r.calls[0].status is not None and 200 <= r.calls[0].status < 300, r.calls[0]
    r = session_run(h, scripted(), RunSetting("healthy"), root, with_q, prompts)
    runs.append(r)
    assert r.calls[0].status == 409, r.calls[0]

    # Determinism: the same history twice on S.
    h = ReplayHistory("det", script(), (tool_call("raise_issue", "p", b"{}"), tool_call("get_units", "p")))
    first = session_run(h, scripted(), RunSetting("healthy"), root, without_q, prompts)
    second = session_run(h, scripted(), RunSetting("healthy"), root, without_q, prompts)
    runs += [first, second]
    assert first.digest_ok is True and second.digest_ok is True, (first, second)
    assert first.requests == second.requests, (first.requests, second.requests)

    # Digest: a stand-in whose digest differs from the script's launches nothing.
    launched = []
    with monkeypatch.context() as m:
        m.setattr(unitcheck.runs, "launch", lambda *a, **kw: launched.append((a, kw)))
        wrong = "f" * 64
        assert wrong != script_digest(h.script)

        def mismatch(session):
            m.setattr(session.standin, "state_digest", lambda: wrong)

        r = session_run(h, scripted(), RunSetting("healthy"), root, without_q, prompts, before=mismatch)
    assert r.digest_ok is False, r
    assert r.calls == (), r.calls
    assert launched == [], launched

    # Run call: emit_tickets settles only once the emission read says it ended.
    requests = tmp_path / "emit-requests.jsonl"
    h = ReplayHistory("emit", script(), (action_call("emit_tickets", "p"),))
    r = session_run(h, scripted(requests), RunSetting("healthy"), root, without_q, prompts)
    runs.append(r)
    emit_path = EMISSION_PATH.format(project="p")
    seen = noted(requests)
    (press,) = [i for i, q in enumerate(seen) if q["method"] == "POST" and q["path"] == emit_path]
    polls = [q for q in seen[press + 1:] if q["method"] == "GET" and q["path"].split("?")[0] == emit_path]
    assert len(polls) >= 2, seen
    assert json.loads(polls[-1]["answer"])["running"] is False, polls
    (j,) = [i for i, q in enumerate(r.requests) if (q.method, q.path) == ("GET", "/repos/o/r/issues")]
    assert r.calls[0].window is not None, r.calls[0]
    lo, hi = r.calls[0].window
    assert lo <= j < hi, (j, r.calls[0].window)
    assert r.calls[0].ended is True, r.calls[0]

    # No project: a call naming none reads no events tool.
    requests = tmp_path / "health-requests.jsonl"
    h = ReplayHistory("health", script(), (Call("contract", "/health", "GET", "/health", None, None),))
    r = session_run(h, scripted(requests), RunSetting("healthy"), root, without_q, prompts)
    runs.append(r)
    events = [q for q in noted(requests) if re.match(r"^/projects/[^/]+/events$", q["path"].split("?")[0])]
    assert events == [], events
    assert r.calls[0].events == (None, b""), r.calls[0]

    # keep_files: exactly the rewritten record, with its bytes after the call.
    h = ReplayHistory("keep", script(), (tool_call("raise_issue", "p", b"{}"),))
    r = session_run(h, scripted(), RunSetting("decl1", keep_files=True), root, without_q, prompts)
    runs.append(r)
    assert len(r.calls[0].changed) == 1, r.calls[0].changed
    ((rel, data),) = r.calls[0].changed
    assert rel in ("projects/p/record.json", "p/record.json"), rel
    assert data == json.dumps({"n": 1}).encode(), data
    r = session_run(h, scripted(), RunSetting("decl1"), root, without_q, prompts)
    runs.append(r)
    assert r.calls[0].changed == (), r.calls[0].changed

    # Tags: every report of every run is tagged.
    for r in runs:
        assert r.records, r
        for report in r.attempts + r.records + r.breaks:
            assert report.call is not None, (r.history, report)


class Recorder:
    """Stands in for any object: every method called on it is noted and does nothing."""

    def __init__(self, called):
        self._called = called

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self._called.append(name)

        return method


def test_confirm_and_not_driven(tmp_path, monkeypatch):
    import unitcheck.runs
    from mu_spec.contract import ROUTES
    from mu_spec.declarations import ACTIONS, TOOLS, find_tool
    from startcheck.judge import CALLS
    from unitcheck.runs import (
        CONTRACT_CALLS,
        UNIT_EVENTS,
        UNROUTED_NAME,
        Call,
        ReplayHistory,
        RunSetting,
        confirm,
        unit_run,
    )

    A1, A2 = "ratify", "reject"
    assert {A1, A2} <= {a.name for a in ACTIONS}
    a2 = action_spec(A2)
    T1 = tool_call("get_units", "p")
    assert T1.name in {t.name for t in TOOLS}
    m1, p1 = T1.method, T1.path

    h = ReplayHistory("h", script(), (
        Call("tool", "no_such_tool", "GET", "/x", None, "p"),
        Call("action", A1, a2.method, filled(a2.path, "p"), b"{}", "p"),
        Call("other", "x", "GET", "/health", None, None),
    ))
    CH = ReplayHistory("c", script(), tuple(Call("contract", p, m, p, body, None) for (m, p, body) in CALLS))
    assert any((m, p.split("?")[0]) not in ROUTES for m, p, _ in CALLS), CALLS
    X = ("GET", "/health/extra")
    assert X not in CONTRACT_CALLS

    def one(call):
        return confirm(ReplayHistory("one", script(), (call,)))

    r = confirm(h)
    assert len(r) == 3, r
    for i, reason in enumerate(r):
        assert reason.startswith(f"call {i}: "), r

    assert len(CONTRACT_CALLS) == len(CALLS)
    assert confirm(CH) == (), confirm(CH)
    assert ("GET", "/health") in CONTRACT_CALLS
    assert len(one(Call("contract", "/stats", "GET", "/health", None, None))) == 1
    assert len(one(Call("contract", X[1], X[0], X[1], None, None))) == 1

    unrouted = Call("unrouted", UNROUTED_NAME, "POST", "/no/such/route", b'{"a":1}', None)
    assert one(unrouted) == ()
    assert len(one(Call("unrouted", UNROUTED_NAME, m1, p1, b"{}", "p"))) == 1
    assert len(one(Call("unrouted", "x", "POST", "/no/such/route", None, None))) == 1
    assert ("GET", "/health") in ROUTES and find_tool("GET", "/health?x=1") is None
    assert len(one(Call("unrouted", UNROUTED_NAME, "GET", "/health?x=1", None, None))) == 1

    queried = Call("tool", T1.name, m1, p1 + "?a=1", None, "p")
    hit = find_tool(m1, p1 + "?a=1")
    if hit is not None and hit[0].name == T1.name:
        assert one(queried) == ()
    else:
        assert len(one(queried)) == 1

    # A rejected history: nothing restored, reset or launched.
    called = []
    root, start_store, prompts = folders(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(unitcheck.runs, "restore", lambda *a, **kw: called.append("restore"))
        m.setattr(unitcheck.runs, "launch", lambda *a, **kw: called.append("launch"))
        out = unit_run(h, unit(tmp_path / "installs", "refused", root), "running", RunSetting("healthy"),
                       str(root), str(start_store), str(prompts), dict(os.environ), Recorder(called),
                       Recorder(called))
    assert out.not_driven == r, out
    assert out.calls == () and out.final is None, out
    assert called == [], called

    # The unrouted call is driven as recorded, and its answer is the unit's.
    requests = tmp_path / "unrouted-requests.jsonl"
    refusal = '{"error": "method not allowed"}'
    cmd = unit(tmp_path / "installs", "unrouted", root, answers={"POST /no/such/route": (405, refusal)},
               requests=requests)
    out = session_run(ReplayHistory("u", script(), (unrouted,)), cmd, RunSetting("healthy"), root, start_store,
                      prompts)
    checks = {("GET", "/health"), tuple(UNIT_EVENTS)}
    driven = [q for q in noted(requests) if (q["method"], q["path"].split("?")[0]) not in checks]
    assert [(q["method"], q["path"], q["body"].encode("latin-1")) for q in driven] == [
        ("POST", "/no/such/route", b'{"a":1}')], driven
    assert out.calls[0].status == 405, out.calls[0]
    assert out.calls[0].body == refusal.encode(), out.calls[0]
