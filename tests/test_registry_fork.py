"""A fork reads its source through the source's own writer, so it holds every write queued before it.

A write to the source is held inside its build when the fork arrives; it is released only once the
fork has reached the source's writer or returned, and the forked record holds that write; a fork of
a missing source gets the running unit's refusal (T-126). The running unit is the install at
MU_SPEC_REFERENCE_INSTALL; the fork's inputs come from the tool declarations copied from it, and its
refusal is captured from it. Modules under test are imported inside the test, so a module not yet
written fails only the tests that need it.
"""

import http.client
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

import pytest

REFERENCE_VAR = "MU_SPEC_REFERENCE_INSTALL"
HOST = "127.0.0.1"
DEFAULT_PORT = 9006
START_WINDOW_S = 20.0
STOP_WINDOW_S = 15.0
CALL_TIMEOUT_S = 30.0
POLL_S = 0.1
HOLD_S = 10.0
REACH_S = 5.0
REACH_POLL_S = 0.01
# what the interpreter itself needs to run on the host
PASS_THROUGH = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
ROUTE_FIELDS = ("route", "path", "url", "endpoint")
# inputs every write call carries besides the ones naming projects
CALL_INPUTS = ("actor", "in_response_to")
ACTOR = "a"


def reference_install() -> str:
    root = os.environ.get(REFERENCE_VAR)
    if not root:
        pytest.fail(f"{REFERENCE_VAR} is not set: it must name the running unit's install root")
    return root


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
        except OSError:
            return False
    return True


def free_high_port() -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((HOST, 0))
            port = s.getsockname()[1]
        if 1024 < port < 65536 and port != DEFAULT_PORT:
            return port


def listens(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=POLL_S * 5):
            return True
    except OSError:
        return False


class Unit:
    """One unit process serving one state directory on one port."""

    def __init__(self, install_root: str, state_dir: pathlib.Path, cwd: pathlib.Path) -> None:
        from startcheck.runner import BOOTSTRAP

        from mu_spec.outside import ENV_NAMES

        self.port = free_high_port()
        self.base = f"{HOST}:{self.port}"
        env = {name: os.environ[name] for name in PASS_THROUGH if name in os.environ}
        env.update({ENV_NAMES["state_dir"]: str(state_dir), ENV_NAMES["host"]: HOST,
                    ENV_NAMES["port"]: str(self.port)})
        self.err_path = cwd / f"unit-{self.port}.err"
        with open(self.err_path, "wb") as err:
            self.proc = subprocess.Popen(
                (sys.executable, "-I", "-c", BOOTSTRAP, install_root),
                cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
            )
        deadline = time.monotonic() + START_WINDOW_S
        while not listens(self.port):
            if self.proc.poll() is not None or time.monotonic() > deadline:
                self.stop()
                pytest.fail(f"unit at {install_root} did not listen on {self.port}: {self.stderr()!r}")
            time.sleep(POLL_S)

    def stderr(self) -> str:
        try:
            return self.err_path.read_text("utf-8", "replace")
        except OSError:
            return ""

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=STOP_WINDOW_S)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        deadline = time.monotonic() + STOP_WINDOW_S
        while not port_is_free(self.port):
            if time.monotonic() > deadline:
                pytest.fail(f"port {self.port} is still taken after the unit stopped")
            time.sleep(POLL_S)


def http_request(base: str, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
    host, port = base.rsplit(":", 1)
    conn = http.client.HTTPConnection(host, int(port), timeout=CALL_TIMEOUT_S)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        answer = conn.getresponse()
        return answer.status, answer.read()
    finally:
        conn.close()


def decode(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def tool_route(base: str, tool: str) -> tuple[str, str]:
    """(method, path pattern) for one tool, as the unit's tool discovery names it."""
    status, raw = http_request(base, "GET", "/tools")
    listing = decode(raw)
    assert status == 200, f"GET /tools answered {status}: {raw!r}"
    if isinstance(listing, dict) and isinstance(listing.get("tools"), (list, dict)):
        listing = listing["tools"]
    if isinstance(listing, dict):
        spec = listing.get(tool)
    else:
        spec = next((t for t in listing or () if isinstance(t, dict) and t.get("name") == tool), None)
    assert spec is not None, f"GET /tools names no tool {tool!r}: {raw!r}"
    method = str(spec.get("method", "POST")).upper() if isinstance(spec, dict) else "POST"
    route = next((spec[f] for f in ROUTE_FIELDS if isinstance(spec, dict) and isinstance(spec.get(f), str)),
                 f"/tools/{tool}")
    return method, route


def call(base: str, tool: str, args: dict) -> tuple[int, bytes, object]:
    """Call one tool with args sent as a JSON object, path parameters filled from args."""
    method, route = tool_route(base, tool)
    path = re.sub(r"\{(\w+)\}", lambda m: urllib.parse.quote(str(args[m.group(1)]), safe=""), route)
    status, raw = http_request(base, method, path, json.dumps(args).encode("utf-8"))
    return status, raw, decode(raw)


def request_id(body: object) -> object:
    if isinstance(body, dict):
        for field in ("request_id", "id"):
            if field in body:
                return body[field]
        created = body.get("created")
        if isinstance(created, list) and len(created) == 1:
            return created[0]
    pytest.fail(f"post_request answer names no request id: {body!r}")


def request_args(project: str) -> dict:
    return {"type": "initiate", "title": f"project {project}", "project": project, "actor": ACTOR}


def fork_args(source: str, target: str, rid: object) -> dict:
    """The fork_project inputs forking source to target, named as the running unit's input shape names them:
    the source under FORK_SOURCE_INPUT, the new name under the one other required input."""
    from mu_spec.tools.schemas import BY_NAME
    from unitcheck.replay import FORK_SOURCE_INPUT

    shape = BY_NAME["fork_project"].input_schema
    properties = shape.get("properties", {})
    named = [k for k in shape.get("required", ()) if k != FORK_SOURCE_INPUT and k not in CALL_INPUTS]
    assert len(named) == 1, f"fork_project's input shape names no single new-project input: {shape!r}"
    args = {FORK_SOURCE_INPUT: source, named[0]: target}
    if "in_response_to" in properties:
        args["in_response_to"] = rid
    if "actor" in properties:
        args["actor"] = ACTOR
    return args


def admitted(status: int) -> bool:
    return 200 <= status < 300


def message_path(body: object, message: str) -> list | None:
    """Where in an answer body the message sits, as a list of keys."""
    if body == message:
        return []
    if isinstance(body, dict):
        for k, v in body.items():
            found = message_path(v, message)
            if found is not None:
                return [k, *found]
    return None


def message_location() -> list:
    """Where refusal_body puts a refusal's message."""
    from mu_spec.refusals import Refused, refusal_body, refuse

    try:
        refuse("UNKNOWN_PROJECT", project="<probe>")
    except Refused as probe:
        where = message_path(json.loads(json.dumps(refusal_body(probe))), probe.message)
    assert where is not None, "refusal_body does not carry the refusal's message"
    return where


def message_bytes(body: object, where: list) -> bytes:
    message = body
    for k in where:
        assert isinstance(message, dict) and k in message, body
        message = message[k]
    assert isinstance(message, str), body
    return message.encode("utf-8")


def running_missing_source(work: pathlib.Path, where: list) -> tuple[int, bytes]:
    """The running unit's status and message bytes for a fork of 'missing' to 'x'."""
    state = work / "state"
    (state / "projects").mkdir(parents=True)
    unit = Unit(reference_install(), state, work)
    try:
        status, raw, body = call(unit.base, "post_request", request_args("x"))
        assert admitted(status), f"post_request refused by the running unit: {status} {raw!r}"
        status, raw, body = call(unit.base, "fork_project", fork_args("missing", "x", request_id(body)))
        assert not admitted(status), f"the running unit admitted a fork of a missing source: {raw!r}"
        return status, message_bytes(body, where)
    finally:
        unit.stop()


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


def plain(value: object) -> object:
    return json.loads(json.dumps(value))


def dispatched(rt, tool: str, args: dict):
    from mu_spec.tools.catalogue import dispatch

    outcome = dispatch(rt, tool, args)
    assert admitted(outcome.status), f"{tool} refused: {outcome!r}"
    return plain(outcome.body)


def entry_of(doc: dict, eid: str) -> object:
    """The entry with id eid in doc's entries, keyed by id or listed with its id; None when absent."""
    entries = doc["entries"]
    if isinstance(entries, dict):
        return entries.get(eid)
    return next((e for e in entries if isinstance(e, dict) and e.get("id") == eid), None)


class HeldBehaviour:
    """A WriteHandler adding one behaviour entry, then held until release is set."""

    check_order = ()

    def __init__(self, intent: str) -> None:
        self.intent = intent
        self.started = threading.Event()
        self.release = threading.Event()
        self.eid = None

    def build(self, state, call, doc, ids, at):
        from mu_spec.writer import Built

        self.eid = ids.next("B")
        entry = {"layer": "B", "title": "fork behaviour", "body": "a behaviour the fork must hold",
                 "derives_from": [self.intent]}
        if isinstance(doc["entries"], dict):
            doc["entries"][self.eid] = entry
        else:
            doc["entries"].append({"id": self.eid, **entry})
        self.started.set()
        self.release.wait(timeout=HOLD_S)
        return Built({"created": [self.eid]}, 200)

    def log_records(self, call, built, at):
        return ()

    def replay_log_records(self, call, body, at):
        return ()

    def recast_dry(self, result):
        from mu_spec.writer import Outcome

        return Outcome(200, {"admitted": True})


def test_fork_sees_queued_write(tmp_path, monkeypatch):
    from mu_spec.registry import build_runtime
    from mu_spec.tools.catalogue import dispatch
    from mu_spec.writer import ProjectWriter, WriteCall

    where = message_location()
    running = tmp_path / "running"
    running.mkdir()
    missing_answer = running_missing_source(running, where)

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    set_environment(monkeypatch, port="9100", state_dir=str(tmp_path / "state"))
    rt = build_runtime(ship_builder=lambda client: object(), github=FakeGitHub())
    rid = request_id(dispatched(rt, "post_request", request_args("src")))
    dispatched(rt, "create_project", {"project": "src", "in_response_to": rid, "actor": ACTOR})
    seeded = dispatched(rt, "submit_amendment", {
        "project": "src", "in_response_to": rid, "actor": ACTOR,
        "entries": [{"layer": "I", "title": "fork intent", "body": "the intent the behaviour serves"}],
    })
    intent = seeded["created"][0]
    dst_rid = request_id(dispatched(rt, "post_request", request_args("dst")))

    snap_called = threading.Event()
    original_snapshot = ProjectWriter.snapshot

    def snapshot(self):
        snap_called.set()
        return original_snapshot(self)

    monkeypatch.setattr(ProjectWriter, "snapshot", snapshot)
    W = HeldBehaviour(intent)
    answers = {}

    def write():
        answers["write"] = rt.registry.writer("src").submit(WriteCall("fx", {}, ACTOR, None, False), W)

    def fork():
        answers["fork"] = dispatch(rt, "fork_project", fork_args("src", "dst", dst_rid))

    # 1. W is held inside its build; 2. the fork arrives; 3. W is released once the fork reached
    # the source's writer or returned
    thread_1 = threading.Thread(target=write)
    thread_2 = threading.Thread(target=fork)
    thread_1.start()
    try:
        assert W.started.wait(timeout=HOLD_S), "W never reached its build"
        thread_2.start()
        deadline = time.monotonic() + REACH_S
        while not snap_called.is_set() and thread_2.is_alive() and time.monotonic() < deadline:
            time.sleep(REACH_POLL_S)
    finally:
        W.release.set()
        thread_1.join(timeout=CALL_TIMEOUT_S)
        if thread_2.ident is not None:
            thread_2.join(timeout=CALL_TIMEOUT_S)

    # 1. the fork is admitted and holds W's entry, under the same id as in 'src'
    assert admitted(answers["write"].status), answers["write"]
    assert admitted(answers["fork"].status), answers["fork"]
    src_entry = entry_of(plain(rt.store.read("src").thaw()), W.eid)
    dst_entry = entry_of(plain(rt.store.read("dst").thaw()), W.eid)
    assert src_entry is not None
    assert dst_entry == src_entry

    # 2. the fork read its source through the source's writer
    assert snap_called.is_set()

    # 3. a fork of a missing source answers the running unit's refusal and creates nothing
    x_rid = request_id(dispatched(rt, "post_request", request_args("x")))
    outcome = dispatch(rt, "fork_project", fork_args("missing", "x", x_rid))
    assert (outcome.status, message_bytes(plain(outcome.body), where)) == missing_answer
    assert "x" not in rt.registry.names()
