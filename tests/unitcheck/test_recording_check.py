"""unitcheck.recording: the recording-failure check, its schedules and its judgements over scripted units.

test_build_schedule builds each schedule from hand-made healthy runs whose record reports carry the
given call tags; test_recording_judgements drives check_history over scripted units launched through
the shared launch, one session for every case, each case from a scratch start store of its own. Each
scripted unit's listener is a ThreadingHTTPServer on the injected host and port. The modules under test
are imported inside each test, so while they are absent only these tests fail.
"""

import json
import os
import re

# A scripted unit, configured by CONFIG. It keeps <store root>/<p>/record.json per project and appends its
# records, one JSON line each, to CONFIG["log"], opened per append. CONFIG["routes"] maps "<method> <path>"
# to what the request does: 'create' makes the project its body names (409 when the folder is present),
# 'write' rewrites the named project's record, 'press' answers 202 and starts a run that records an end
# event with a fresh run id 0.3 s later, 'status' answers {"running": ...}; each admitted 'create', 'write'
# and 'press' appends CONFIG["records"][route] in order. GET /projects/<p>/events answers the project's
# records after since and the count of every record it holds for the project; CONFIG["wide"] answers
# every record. A failed append leaves a gap in the sequence numbers unless CONFIG["gap"] is false; the
# request then answers as healthy, unless CONFIG["on_fail"] is 'status500' (answers 500) or 'sleep'
# (answers after 6 s). With CONFIG["pending"], each failed line is kept there and written to the log by
# the next process that finds the log non-empty at start.
UNIT_MAIN = r'''
import json, os, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG = %r
LOCK = threading.Lock()
EMISSION = {"running": False}
SEQ = [0]
LOCAL = threading.local()


def root():
    return os.path.join(os.environ[CONFIG["state"]], "projects")


def record_file(project):
    return os.path.join(root(), project, "record.json")


def held():
    """Every whole record in the log, in order."""
    if not os.path.exists(CONFIG["log"]):
        return []
    with open(CONFIG["log"], "rb") as f:
        data = f.read()
    out = []
    for line in data.split(b"\n"):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def append(project, kind, facts):
    with LOCK:
        seq = SEQ[0] + 1
        line = json.dumps({"seq": seq, "at": time.time(), "project": project, "kind": kind, "refs": [],
                           "facts": facts}).encode() + b"\n"
        try:
            with open(CONFIG["log"], "ab", buffering=0) as f:
                f.write(line)
        except OSError:
            LOCAL.failed = True
            if CONFIG["gap"]:
                SEQ[0] = seq
            if CONFIG["pending"]:
                with open(CONFIG["pending"], "ab") as f:
                    f.write(line)
            return
        SEQ[0] = seq


def recorded(route, project):
    for kind, tag in CONFIG["records"].get(route, ()):
        append(project, kind, {"tag": tag})


def emission(project):
    time.sleep(0.3)
    append(project, "tickets_emitted", {"run_id": os.urandom(6).hex()})
    with LOCK:
        EMISSION["running"] = False


def answer(method, path, query, body):
    route = method + " " + path
    what = CONFIG["routes"].get(route)
    parts = path.split("/")
    if what == "create":
        name = json.loads(body or b"{}")["project"]
        if os.path.exists(os.path.join(root(), name)):
            return 409, {"error": "exists"}
        os.mkdir(os.path.join(root(), name))
        with open(record_file(name), "wb") as f:
            f.write(json.dumps({"n": 0}).encode())
        recorded(route, name)
        return 201, {"ok": True}
    if what == "write":
        project = parts[2]
        with open(record_file(project), "rb") as f:
            n = json.loads(f.read())["n"]
        with open(record_file(project), "wb") as f:
            f.write(json.dumps({"n": n + 1}).encode())
        recorded(route, project)
        return 200, {"ok": True}
    if what == "press":
        with LOCK:
            EMISSION["running"] = True
        recorded(route, parts[2])
        threading.Thread(target=emission, args=(parts[2],), daemon=True).start()
        return 202, {"started": True}
    if what == "status":
        with LOCK:
            return 200, {"running": EMISSION["running"]}
    if method == "GET" and len(parts) == 4 and parts[1] == "projects" and parts[3] == "events":
        since = 0
        for pair in query.split("&"):
            key, _, value = pair.partition("=")
            if key == "since" and value:
                since = int(value)
        mine = [r for r in held() if r.get("project") == parts[2]]
        return 200, {"count": len(mine), "events": [r for r in mine if r["seq"] > since]}
    if [method, path] == CONFIG["wide"]:
        return 200, {"events": held()}
    if path == "/health":
        return 200, {}
    return 404, {"error": "not found"}


class Handler(BaseHTTPRequestHandler):
    def _answer(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        path, _, query = self.path.partition("?")
        LOCAL.failed = False
        status, doc = answer(self.command, path, query, body)
        if LOCAL.failed and CONFIG["on_fail"] == "status500":
            status, doc = 500, {"error": "log"}
        if LOCAL.failed and CONFIG["on_fail"] == "sleep":
            time.sleep(6)
        out = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    found = held()
    SEQ[0] = max([r.get("seq", 0) for r in found] or [0])
    if CONFIG["pending"] and os.path.exists(CONFIG["pending"]):
        with open(CONFIG["pending"], "rb") as f:
            lines = f.read()
        os.remove(CONFIG["pending"])
        if found:
            with open(CONFIG["log"], "ab", buffering=0) as f:
                f.write(lines)
    ThreadingHTTPServer((os.environ[CONFIG["host"]], int(os.environ[CONFIG["port"]])), Handler).serve_forever()
'''


def unit(base, name, label, check_root, calls, records, on_fail="carry", gap=True, rewrite=False):
    """A scratch install of UNIT_MAIN under base/name serving calls, and its UnitCommand.

    records maps a call's route kind ('create', 'write', 'press') to the (kind, tag) records it appends.
    """
    from unitcheck.launch import followed_log
    from unitcheck.runs import EMISSION_PATH, UNIT_EVENTS

    from mu_spec.outside import ENV_NAMES
    from startcheck.runner import UnitCommand

    routes, by_route = {}, {}
    for c, what in calls:
        route = c.method + " " + c.path.split("?")[0]
        routes[route] = what
        by_route[route] = [list(r) for r in records.get(what, ())]
        if what == "press":
            routes["GET " + EMISSION_PATH.format(project=c.project)] = "status"
    install = base / name
    config = {"host": ENV_NAMES["host"], "port": ENV_NAMES["port"], "state": ENV_NAMES["state_dir"],
              "log": str(followed_log(str(check_root))), "routes": routes, "records": by_route,
              "wide": list(UNIT_EVENTS), "on_fail": on_fail, "gap": gap,
              "pending": str(install / "pending.jsonl") if rewrite else None}
    package = install / "mu_spec"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "main.py").write_text(UNIT_MAIN % (config,))
    return UnitCommand(label=label, install_root=str(install))


def store_with(base, *projects):
    """A scratch start store holding projects/<p>/record.json, {"n": 0}, for each project named."""
    (base / "projects").mkdir(parents=True)
    for p in projects:
        (base / "projects" / p).mkdir()
        (base / "projects" / p / "record.json").write_bytes(json.dumps({"n": 0}).encode())
    return base


def script():
    from shipcheck.standin import Script

    return Script(issues=(), links=(), next_number=1, next_id=1, user={"login": "x"}, push=True,
                  refuse={}, unanswered=frozenset(), hold=None)


def filled(path, project):
    return "/" + re.sub(r"\{[^}]+\}", project, path).lstrip("/")


def tool_call(name, project, body=None):
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


def healthy(tags):
    """A running unit's healthy UnitRun whose records are one RecordReport per tag, tagged with it."""
    from unitcheck.launch import RecordReport
    from unitcheck.runs import RunSetting, UnitRun

    records = tuple(RecordReport(ordinal=i + 1, kind="mutation", outcome="passed",
                                 data=b'{"kind":"mutation","n":%d}\n' % i, call=t) for i, t in enumerate(tags))
    return UnitRun(history="h", unit="running", setting=RunSetting("healthy"), not_driven=(), digest_ok=True,
                   calls=(), final=None, attempts=(), records=records, breaks=(), requests=(), reached=(),
                   login_lookups=0)


def test_build_schedule():
    from unitcheck.launch import FINAL_SPAN, START_SPAN
    from unitcheck.recording import SCHEDULES, build_schedule

    assert len(SCHEDULES) == 7

    # Start-up reports belong to no call: n_0 is 3, not 5.
    run = healthy([START_SPAN, START_SPAN, 0, 0, 0])
    assert build_schedule("kth_last", run).faults == ((0, "kth", 3), (1, "off", None))

    # Calls 1 and 3 tie at 5 records: the lowest index; the middle of 5 is 3.
    run = healthy([1] * 5 + [3] * 5 + [2] * 2)
    assert build_schedule("kth_middle", run).faults == ((1, "kth", 3), (2, "off", None))
    assert build_schedule("kth_first", run).faults == ((1, "kth", 1), (2, "off", None))

    # End reports belong to no call: call 4 alone records, once; (1 + 1) // 2 = 1.
    run = healthy([4, FINAL_SPAN, FINAL_SPAN])
    assert build_schedule("kth_middle", run).faults == ((4, "kth", 1), (5, "off", None))

    # Fewer recording calls than STRETCH_MIN_WRITES: the reason the stretch is skipped.
    assert isinstance(build_schedule("stretch", healthy([0, 1])), str)

    # Nine recording calls W: the toggles are W[9 // 3] and W[18 // 3], indexes into W, not calls.
    W = (0, 2, 3, 5, 6, 8, 9, 11, 12)
    stretch = build_schedule("stretch", healthy(list(W)))
    assert stretch.faults == ((5, "every", None), (9, "off", None)), stretch
    assert stretch.restart_at_end is True

    run = healthy([0])
    assert build_schedule("all_records", run).initial == ("every", None)
    assert build_schedule("mutation_only", run).initial == ("mutation", None)
    assert build_schedule("event_only", run).initial == ("event", None)


def test_recording_judgements(tmp_path, monkeypatch):
    import unitcheck.recording
    import unitcheck.runs
    from mu_spec.dash.settle import NODE_TIMEOUT_S
    from unitcheck.recording import SCHEDULES, STRETCH_MIN_WRITES, check_history
    from unitcheck.runs import Call, ReplayHistory
    from unitcheck.session import open_session

    assert STRETCH_MIN_WRITES == 3
    assert NODE_TIMEOUT_S < 6

    root = tmp_path / "check"
    root.mkdir()
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    own_env = dict(os.environ)

    launches = []
    real_launch = unitcheck.runs.launch

    def launching(*args, **kwargs):
        launches.append(args)
        return real_launch(*args, **kwargs)

    runs = {}
    real_unit_run = unitcheck.recording.unit_run

    def recording_run(*args, **kwargs):
        r = real_unit_run(*args, **kwargs)
        runs[(r.history, r.unit, r.setting.label)] = r
        return r

    monkeypatch.setattr(unitcheck.runs, "launch", launching)
    monkeypatch.setattr(unitcheck.recording, "unit_run", recording_run)

    count = [0]
    one = (("mutation", "m"),)

    def case(name, calls, start_store, records=None, **rebuilt_variant):
        """check_history of the history of calls on R0 (running) and its variant (rebuilt); its verdicts by schedule."""
        records = records or {"create": one, "write": one, "press": one}
        count[0] += 1
        base = tmp_path / f"installs-{count[0]}"
        running = unit(base, "running", "running", root, calls, records)
        rebuilt = unit(base, "rebuilt", "rebuilt", root, calls, records, **rebuilt_variant)
        h = ReplayHistory(name, script(), tuple(c for c, _ in calls))
        del launches[:]
        verdicts = check_history(h, running, rebuilt, str(root), str(start_store), str(prompts), own_env,
                                 session.standin, session.neighbour)
        assert [v.schedule for v in verdicts] == list(SCHEDULES), verdicts
        assert all(v.history == name for v in verdicts), verdicts
        return {v.schedule: v for v in verdicts}

    session = open_session(own_env)
    try:
        # Not driven: seven verdicts, none passing, no process started.
        bad = ((Call("tool", "no_such_tool", "GET", "/x", None, "p"), "write"),)
        verdicts = case("not-driven", bad, store_with(tmp_path / "s-not-driven", "p"))
        assert launches == [], launches
        for v in verdicts.values():
            assert v.not_driven != () and v.passed is False, v

        # Start store: q is absent from it, so its creation is admitted on every twin and schedule; two
        # recording calls skip the stretch, so 2 healthy and 12 faulted launches.
        create = tool_call("create_project", "q", json.dumps({"project": "q"}).encode())
        calls = ((create, "create"), (tool_call("raise_issue", "q", b"{}"), "write"))
        verdicts = case("start", calls, store_with(tmp_path / "s-start"))
        assert len(launches) == 14, len(launches)
        start_runs = [r for key, r in runs.items() if key[0] == "start"]
        assert len(start_runs) == 14, sorted(k for k in runs if k[0] == "start")
        for r in start_runs:
            status = r.calls[0].status
            assert status is not None and 200 <= status < 300, (r.unit, r.setting.label, r.calls[0])
        assert isinstance(verdicts["stretch"].skipped, str), verdicts["stretch"]
        assert all(verdicts[s].skipped is None for s in SCHEDULES if s != "stretch"), verdicts

        write = (tool_call("raise_issue", "p", b"{}"), "write")

        # A rebuilt unit answering 500 when its log write fails: its answer differs from its healthy twin's.
        verdicts = case("status500", (write,), store_with(tmp_path / "s-500", "p"), on_fail="status500")
        assert verdicts["all_records"].answer_faults != (), verdicts["all_records"]

        # One answering after 6 s when its log write fails, past NODE_TIMEOUT_S.
        verdicts = case("sleep", (write,), store_with(tmp_path / "s-sleep", "p"), on_fail="sleep")
        assert verdicts["all_records"].late != (), verdicts["all_records"]

        # One writing the dropped line after the restart: the stretch's final reads hold a failed record.
        verdicts = case("rewrite", (write,) * STRETCH_MIN_WRITES, store_with(tmp_path / "s-rewrite", "p"),
                        rewrite=True)
        assert verdicts["stretch"].skipped is None, verdicts["stretch"]
        assert verdicts["stretch"].drop_faults != (), verdicts["stretch"]

        # One numbering the record after the dropped one with no gap, where R0 leaves one.
        three = {"write": (("mutation", "a"), ("issue_raised", "b"), ("issue_raised", "c"))}
        verdicts = case("nogap", (write,), store_with(tmp_path / "s-nogap", "p"), records=three, gap=False)
        assert verdicts["kth_middle"].truth_faults != (), verdicts["kth_middle"]

        # A Ship press whose end event's run id is drawn afresh in every run, never failed: call 0 makes
        # the most records, so kth_first fails its first and the press's records are all kept.
        press = (action_call("emit_tickets", "p"), "press")
        verdicts = case("ship", (write, press), store_with(tmp_path / "s-ship", "p"), records=three)
        assert verdicts["kth_first"].drop_faults == (), verdicts["kth_first"]

        # R0 on both units: call 0 makes a, b and c in that order; call 2 asks the events count.
        count_call = (tool_call("list_events", "p"), "count")
        verdicts = case("abc", (write, write, count_call), store_with(tmp_path / "s-abc", "p"), records=three)
        for schedule, tag in (("kth_first", "a"), ("kth_last", "c")):
            faulted = runs[("abc", "rebuilt", schedule)]
            failed = [r for r in faulted.records if r.outcome == "failed"]
            assert [json.loads(r.data)["facts"]["tag"] for r in failed] == [tag], (schedule, failed)
            assert [r.call for r in failed] == [0], (schedule, failed)
        v = verdicts["all_records"]
        assert 2 in v.reporting, v
        assert v.answer_faults == (), v
        assert v.passed is True, v
    finally:
        session.neighbour.set("present")
        session.close()
