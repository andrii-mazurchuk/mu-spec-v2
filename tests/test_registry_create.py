"""Creates are serialised in the registry: of two creates of one name, the second waits and gets the 409.

The second create arrives while the first is held inside create_project, so the overlap is forced;
a directory without a manifest counts as free; writer(p) is one object per project (T-125). The
running unit is the install at MU_SPEC_REFERENCE_INSTALL; its 409 for an existing name is captured
from it. Modules under test are imported inside the test, so a module not yet written fails only
the tests that need it.
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
HOLD_S = 5.0
OVERLAP_S = 0.3
# what the interpreter itself needs to run on the host
PASS_THROUGH = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
ROUTE_FIELDS = ("route", "path", "url", "endpoint")


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


def request_args(project: str, actor: str) -> dict:
    return {"type": "initiate", "title": f"project {project}", "project": project, "actor": actor}


def create_args(project: str, rid: object, actor: str) -> dict:
    return {"project": project, "in_response_to": rid, "actor": actor}


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


def running_exists_refusal(work: pathlib.Path, where: list) -> bytes:
    """M409: the message the running unit answers for a create_project of a name that exists."""
    state = work / "state"
    (state / "projects").mkdir(parents=True)
    unit = Unit(reference_install(), state, work)
    try:
        rids = {}
        for actor in ("a", "b"):
            status, raw, body = call(unit.base, "post_request", request_args("n", actor))
            assert admitted(status), f"post_request refused by the running unit: {status} {raw!r}"
            rids[actor] = request_id(body)
        status, raw, _ = call(unit.base, "create_project", create_args("n", rids["a"], "a"))
        assert admitted(status), f"create_project refused by the running unit: {status} {raw!r}"
        status, raw, body = call(unit.base, "create_project", create_args("n", rids["b"], "b"))
        assert status == 409, f"the running unit answered a second create {status}: {raw!r}"
        return message_bytes(body, where)
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


def posted(rt, project: str, actor: str) -> object:
    from mu_spec.tools.catalogue import dispatch

    outcome = dispatch(rt, "post_request", request_args(project, actor))
    assert admitted(outcome.status), f"post_request refused: {outcome!r}"
    return request_id(json.loads(json.dumps(outcome.body)))


def test_concurrent_create_and_leftover(tmp_path, monkeypatch):
    from mu_spec.codec import MUTATION_KIND, record_path
    from mu_spec.registry import build_runtime
    from mu_spec.store import Store
    from mu_spec.tools.catalogue import dispatch

    from mu_spec.outside import PROJECTS_SUBDIR

    where = message_location()
    running = tmp_path / "running"
    running.mkdir()
    M409 = running_exists_refusal(running, where)

    D = tmp_path / "state"
    root = D / PROJECTS_SUBDIR
    left = record_path(root, "left").parent
    left.mkdir(parents=True)
    (left / "stray.txt").write_bytes(b"left by a crashed create")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    set_environment(monkeypatch, port="9100", state_dir=str(D))
    rt = build_runtime(ship_builder=lambda client: object(), github=FakeGitHub())
    assert rt.config.root == root

    entered = threading.Event()
    release = threading.Event()
    original = Store.create_project
    calls = []

    def held(self, record):
        calls.append(record.name)
        if len(calls) == 1:
            entered.set()
            release.wait(timeout=HOLD_S)
        return original(self, record)

    monkeypatch.setattr(Store, "create_project", held)
    rids = {actor: posted(rt, "n", actor) for actor in ("a", "b")}
    answers = {}

    def create(actor):
        answers[actor] = dispatch(rt, "create_project", create_args("n", rids[actor], actor))

    # 1. A is held inside create_project; B arrives behind it
    thread_a = threading.Thread(target=create, args=("a",))
    thread_b = threading.Thread(target=create, args=("b",))
    thread_a.start()
    try:
        assert entered.wait(timeout=HOLD_S), "A never reached create_project"
        thread_b.start()
        time.sleep(OVERLAP_S)
        b_early = not thread_b.is_alive()
    finally:
        release.set()
        thread_a.join(timeout=CALL_TIMEOUT_S)
        if thread_b.ident is not None:
            thread_b.join(timeout=CALL_TIMEOUT_S)

    # 2. A admitted; B the running unit's 409, having waited behind A; the record is A's
    assert admitted(answers["a"].status), answers["a"]
    assert answers["b"].status == 409, answers["b"]
    assert message_bytes(json.loads(json.dumps(answers["b"].body)), where) == M409
    assert b_early is False
    mutations = rt.store.read_log(project="n", kind=MUTATION_KIND)
    assert mutations, "no mutation record for 'n'"
    assert mutations[0].doc["facts"]["actor"] == "a"
    for record in mutations:
        facts = record.doc["facts"]
        assert not (facts.get("actor") == "b" and admitted(int(facts.get("status", 0)))), record.doc

    # 3. a directory without a manifest counts as free; afterwards it holds the manifest only
    outcome = dispatch(rt, "create_project", create_args("left", posted(rt, "left", "a"), "a"))
    assert admitted(outcome.status), outcome
    assert sorted(p.name for p in left.iterdir()) == [record_path(root, "left").name]

    # 4. one writer per project, none for a name with no record
    assert rt.registry.writer("n") is rt.registry.writer("n")
    assert rt.registry.writer("absent") is None
