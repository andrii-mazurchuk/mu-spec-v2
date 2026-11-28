"""unitcheck.independence: the independence check, check_history over scripted rebuilt units (T-315).

Each scripted unit's listener is a ThreadingHTTPServer on the injected host and port; it keeps
<store root>/<p>/record.json per project and notes every request it answers. One check session serves
every check_history here; unit_run, as the independence module calls it, is wrapped by a recorder that
passes every call through unchanged and keeps each returned UnitRun. The modules under test are imported
inside the test, so while they are absent only these tests fail.
"""

import json
import os
import re

# A scripted unit, configured by CONFIG. POST /projects creates <p> (409 when the folder is present), GET
# /projects/<p>/units reads its record (404 when absent), a press of CONFIG["emit"] asks GitHub for
# /repos/o/r/issues and GET of it answers {"running": false}. CONFIG["write"] answers by CONFIG["mode"]:
# 'quiet' 200; 'wait' first connects to 10.0.0.1:80 and reads until close, 10 s socket timeout, errors caught;
# 'lookup' 503 when getaddrinfo('dep.invalid', 80) fails, else 200; 'reverse' 200 with the results of
# gethostbyaddr('10.0.0.1') and getnameinfo(('10.0.0.1', 80), 0), errors caught. Anything else answers
# 200 {}. Every request is noted in CONFIG["requests"] before its answer is sent.
UNIT_MAIN = r'''
import http.client, json, os, socket, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG = %r
LOCK = threading.Lock()


def root():
    return os.path.join(os.environ[CONFIG["state"]], "projects")


def record(project):
    return os.path.join(root(), project, "record.json")


def outcome(fn):
    try:
        return repr(fn())
    except Exception as e:
        return type(e).__name__ + ": " + str(e)


def wait():
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


def press():
    try:
        conn = http.client.HTTPSConnection(CONFIG["github"], 443, timeout=10)
        conn.request("GET", "/repos/o/r/issues?state=open&per_page=100&page=1")
        conn.getresponse().read()
        conn.close()
    except Exception:
        pass


def written():
    mode = CONFIG["mode"]
    if mode == "wait":
        wait()
    if mode == "lookup":
        try:
            socket.getaddrinfo("dep.invalid", 80)
        except OSError:
            return 503, b'{"error": "dependency unavailable"}'
    if mode == "reverse":
        found = {"gethostbyaddr": outcome(lambda: socket.gethostbyaddr("10.0.0.1")),
                 "getnameinfo": outcome(lambda: socket.getnameinfo(("10.0.0.1", 80), 0))}
        return 200, json.dumps(found, sort_keys=True).encode()
    return 200, b'{"ok": true}'


def answer(method, path, body):
    parts = path.split("/")
    if method == "POST" and path == "/projects":
        name = json.loads(body or b"{}")["project"]
        if os.path.exists(os.path.join(root(), name)):
            return 409, b'{"error": "exists"}'
        os.mkdir(os.path.join(root(), name))
        with open(record(name), "wb") as f:
            f.write(b'{"n": 0}')
        return 201, b'{"ok": true}'
    if path == CONFIG["emit"]:
        if method == "GET":
            return 200, b'{"running": false}'
        press()
        return 202, b'{"started": true}'
    if method == "POST" and path == CONFIG["write"]:
        return written()
    if method == "GET" and len(parts) == 4 and parts[1] == "projects" and parts[3] == "units":
        if not os.path.exists(record(parts[2])):
            return 404, b'{"error": "not found"}'
        with open(record(parts[2]), "rb") as f:
            return 200, f.read()
    return 200, b"{}"


class Handler(BaseHTTPRequestHandler):
    def _answer(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        status, out = answer(self.command, self.path.split("?")[0], body)
        with LOCK:
            with open(CONFIG["requests"], "a") as f:
                f.write(json.dumps({"method": self.command, "path": self.path, "status": status}) + "\n")
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


def action_call(name, project, body=b"{}"):
    from mu_spec.declarations import ACTIONS, find_action
    from unitcheck.runs import Call

    (spec,) = [a for a in ACTIONS if a.name == name]
    path = filled(spec.path, project)
    hit = find_action(spec.method, path)
    assert hit is not None and hit[0].name == name, (name, path, hit)
    return Call("action", name, spec.method, path, body, project)


def script():
    from shipcheck.standin import Script

    return Script(issues=(), links=(), next_number=1, next_id=1, user={"login": "x"}, push=True,
                  refuse={}, unanswered=frozenset(), hold=None)


def unit(base, name, mode, write, emit):
    """A scratch install of UNIT_MAIN under base/name, its UnitCommand and its request log."""
    from shipcheck.standin import GITHUB_HOST

    from mu_spec.outside import ENV_NAMES
    from startcheck.runner import UnitCommand

    requests = base / f"{name}-requests.jsonl"
    config = {"host": ENV_NAMES["host"], "port": ENV_NAMES["port"], "state": ENV_NAMES["state_dir"],
              "github": GITHUB_HOST, "requests": str(requests), "mode": mode, "write": write, "emit": emit}
    package = base / name / "mu_spec"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(UNIT_MAIN % (config,))
    return UnitCommand(label="rebuilt", install_root=str(package.parent)), requests


def noted(requests):
    """The requests a scripted unit noted, in order."""
    if not requests.exists():
        return []
    return [json.loads(line) for line in requests.read_text().splitlines() if line]


def store_with(base, *projects):
    """A start store holding projects/<p>/record.json, {"n": 0}, for each project named."""
    (base / "projects").mkdir(parents=True)
    for p in projects:
        (base / "projects" / p).mkdir()
        (base / "projects" / p / "record.json").write_bytes(b'{"n": 0}')
    return base


class Recorder:
    """Stands in for any callable: every call is noted and does nothing."""

    def __init__(self, called, name):
        self._called, self._name = called, name

    def __call__(self, *args, **kwargs):
        self._called.append(self._name)


def test_independence_judgements(tmp_path, monkeypatch):
    import unitcheck.independence
    import unitcheck.runs
    from startcheck.judge import CALLS
    from unitcheck.independence import (
        CONTRACT_HISTORY,
        EXCLUDED_ACTIONS,
        check_history,
    )
    from unitcheck.runs import Call, ReplayHistory, RunSetting, confirm
    from unitcheck.session import open_session

    from unitcheck.neighbour import CONDITIONS

    assert len(CONDITIONS) == 4
    assert EXCLUDED_ACTIONS == ("emit_tickets", "rollback_tickets")
    assert len(CONTRACT_HISTORY.calls) == len(CALLS), (CONTRACT_HISTORY.calls, CALLS)
    assert all(c.kind == "contract" for c in CONTRACT_HISTORY.calls), CONTRACT_HISTORY.calls
    assert confirm(CONTRACT_HISTORY) == (), confirm(CONTRACT_HISTORY)

    # Every UnitRun the independence module makes, in order.
    runs = []
    real_unit_run = unitcheck.runs.unit_run

    def recording(*args, **kwargs):
        r = real_unit_run(*args, **kwargs)
        runs.append(r)
        return r

    for module in (unitcheck.runs, unitcheck.independence):
        if hasattr(module, "unit_run"):
            monkeypatch.setattr(module, "unit_run", recording)

    root = tmp_path / "check"
    root.mkdir()
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    installs = tmp_path / "installs"
    installs.mkdir()
    start_store = store_with(tmp_path / "start-store", "p")
    write = tool_call("raise_issue", "p", b"{}")
    emit = action_call("emit_tickets", "p")
    read = tool_call("get_units", "p")
    h = ReplayHistory("h", script(), (write, emit, read))
    own_env = dict(os.environ)
    logs = []

    def scripted(name, mode="quiet"):
        cmd, requests = unit(installs, name, mode, write.path, emit.path)
        logs.append(requests)
        return cmd

    session = open_session(own_env)
    try:
        def checked(history, cmd, store=start_store):
            before = len(runs)
            verdicts = check_history(history, cmd, str(root), str(store), str(prompts), own_env,
                                     session.standin, session.neighbour)
            assert len(verdicts) == 3, verdicts
            assert [v.condition for v in verdicts] == ["refusing", "absent", "hung"], verdicts
            assert all(v.history == history.name for v in verdicts), verdicts
            return {v.condition: v for v in verdicts}, {r.setting.label: r for r in runs[before:]}

        # Not driven: a history holding a call confirm rejects; nothing restored, launched or run.
        rejected = ReplayHistory("rejected", script(), (Call("tool", "no_such_tool", "GET", "/x", None, "p"),
                                                        write))
        assert confirm(rejected) != ()
        called = []
        with monkeypatch.context() as m:
            m.setattr(unitcheck.runs, "restore", Recorder(called, "restore"))
            m.setattr(unitcheck.runs, "launch", Recorder(called, "launch"))
            verdicts, made = checked(rejected, scripted("refused"))
        assert called == [] and made == {}, (called, made)
        for v in verdicts.values():
            assert v.not_driven != () and v.passed is False, v
            assert v.digests_ok is False, v
            assert (v.answer_faults, v.state_faults, v.late, v.outbound) == ((), (), (), ()), v

        # Start store: every condition's run copies the start_store given, which does not hold q.
        create = tool_call("create_project", "q", json.dumps({"project": "q"}).encode())
        _, made = checked(ReplayHistory("create", script(), (create, tool_call("get_units", "q"))),
                          scripted("store"))
        assert sorted(made) == sorted(CONDITIONS), made
        for label, r in made.items():
            status = r.calls[0].status
            assert status is not None and 200 <= status < 300, (label, r.calls[0])

        # U0 never reaches out: all three pass, its listener's own reverse lookup no fault.
        verdicts, _ = checked(h, scripted("u0"))
        for v in verdicts.values():
            assert v.passed is True, v

        # U1 waits on the neighbour on the write: late under 'hung'.
        verdicts, _ = checked(h, scripted("u1", "wait"))
        assert verdicts["hung"].late != (), verdicts["hung"]

        # U2 answers 503 when a lookup fails: differs under 'absent', not under 'refusing'.
        verdicts, _ = checked(h, scripted("u2", "lookup"))
        assert verdicts["absent"].answer_faults != (), verdicts["absent"]
        assert verdicts["refusing"].answer_faults == (), verdicts["refusing"]

        # U3 answers its reverse lookups of an address it never bound: the same in two present runs,
        # caught under 'absent' by both answer_faults and outbound.
        u3 = scripted("u3", "reverse")
        verdicts, made = checked(h, u3)
        again = real_unit_run(h, u3, "rebuilt", RunSetting("present", condition="present", skip=frozenset({1})),
                              str(root), str(start_store), str(prompts), own_env, session.standin,
                              session.neighbour)
        runs.append(again)
        first = made["present"].calls[0]
        second = next(c for c in again.calls if c.index == 0)
        assert first.index == 0, made["present"].calls
        assert (first.status, first.body) == (second.status, second.body), (first, second)
        assert verdicts["absent"].answer_faults != (), verdicts["absent"]
        assert verdicts["absent"].outbound != (), verdicts["absent"]
    finally:
        session.neighbour.set("present")
        session.close()

    # Every run: no emit_tickets press reached a unit, and the stand-in holds none of its GitHub requests.
    for requests in logs:
        presses = [q for q in noted(requests) if q["method"] == emit.method and q["path"].split("?")[0] == emit.path]
        assert presses == [], (requests, presses)
    for r in runs:
        issues = [q for q in r.requests if q.path.split("?")[0] == "/repos/o/r/issues"]
        assert issues == [], (r.history, r.setting.label, issues)
