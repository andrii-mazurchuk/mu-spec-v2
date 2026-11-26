"""build_runtime hands the observer it is given to the Store, which alone reports installs to it.

Start-up installs reach it before the Ship builder runs, a write through the registry's writer
reaches installed and committed once each, a create reaches installed with notify False only, a dry
run reaches nothing, and no Runtime field is it (T-127). The running unit is the install at
MU_SPEC_REFERENCE_INSTALL; it writes the two projects. Modules under test are imported inside the
test, so a module not yet written fails only the tests that need it.
"""

import dataclasses
import http.client
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
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
# what the interpreter itself needs to run on the host
PASS_THROUGH = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
ROUTE_FIELDS = ("route", "path", "url", "endpoint")
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


def running_projects(state: pathlib.Path, cwd: pathlib.Path, names: tuple[str, ...]) -> None:
    """The running unit, over state, creates each of names; the inbox it leaves is then removed."""
    from mu_spec.codec import inbox_path, temp_path

    (state / "projects").mkdir(parents=True)
    unit = Unit(reference_install(), state, cwd)
    try:
        for name in names:
            status, raw, body = call(unit.base, "post_request",
                                     {"type": "initiate", "title": f"project {name}", "project": name,
                                      "actor": ACTOR})
            assert 200 <= status < 300, f"post_request refused: {status} {raw!r}"
            status, raw, _ = call(unit.base, "create_project",
                                  {"project": name, "in_response_to": request_id(body), "actor": ACTOR})
            assert 200 <= status < 300, f"create_project refused: {status} {raw!r}"
    finally:
        unit.stop()
    inbox = inbox_path(state / "projects")
    for path in (inbox, temp_path(inbox)):
        if path.exists():
            path.unlink()


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


class EventObserver:
    """An InstallObserver appending its installs and commits to events; appends go to a list of their own."""

    def __init__(self, events: list) -> None:
        self.events = events
        self.appends = []

    def installed(self, project, data, *, notify):
        self.events.append(("installed", project, data, notify))

    def committed(self, project):
        self.events.append(("committed", project))

    def appended(self, record):
        self.appends.append(record)


class FieldWrite:
    """A WriteHandler setting doc['fx'] = 1."""

    check_order = ()

    def build(self, state, call, doc, ids, at):
        from mu_spec.writer import Built

        doc["fx"] = 1
        return Built({"ok": True}, 200)

    def log_records(self, call, built, at):
        return ()

    def replay_log_records(self, call, body, at):
        return ()

    def recast_dry(self, result):
        from mu_spec.writer import Outcome

        return Outcome(200, {"admitted": True})


class CopyCreate:
    """A CreateHandler creating 'c' from a copy of source's doc."""

    check_order = ()

    def __init__(self, source) -> None:
        self.source = source

    def name_of(self, call):
        return "c"

    def build(self, call, at):
        from mu_spec.codec import ProjectRecord
        from mu_spec.writer import Built

        return ProjectRecord.from_doc("c", self.source.thaw()), Built({"ok": True}, 200)

    def log_records(self, call, built, at):
        return ()


def attributes(value: object) -> list:
    """The values value holds as attributes, one level down."""
    held = list(vars(value).values()) if hasattr(value, "__dict__") else []
    for cls in type(value).__mro__:
        for slot in getattr(cls, "__slots__", ()):
            if hasattr(value, slot):
                held.append(getattr(value, slot))
    return held


def test_observer_handover(tmp_path, monkeypatch):
    from mu_spec.codec import record_path
    from mu_spec.registry import build_runtime
    from mu_spec.writer import WriteCall

    D = tmp_path / "state"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    running_projects(D, cwd, ("a", "b"))
    monkeypatch.chdir(cwd)
    set_environment(monkeypatch, port="9100", state_dir=str(D))
    events = []
    obs = EventObserver(events)

    def recorder(client):
        events.append(("ship", len(events)))
        return object()

    # 1. both start-up installs, in either order, then the Ship builder; nothing else
    rt = build_runtime(ship_builder=recorder, observer=obs, github=FakeGitHub())
    root = rt.config.root
    A0 = record_path(root, "a").read_bytes()
    B0 = record_path(root, "b").read_bytes()
    assert len(events) == 3, events
    assert {events[0], events[1]} == {("installed", "a", A0, False), ("installed", "b", B0, False)}
    assert events[2] == ("ship", 2)

    # 2. no Runtime field is obs; only the parts it is handed to hold it
    for field in dataclasses.fields(rt):
        value = getattr(rt, field.name)
        assert value is not obs, field.name
        if value is not rt.store and value is not rt.registry:
            assert all(held is not obs for held in attributes(value)), field.name

    call = WriteCall("fx", {}, "x", None, False)

    # 3. a write through the registry's writer: installed with notify True, then committed, once each
    events.clear()
    outcome = rt.registry.writer("a").submit(call, FieldWrite())
    assert outcome.status == 200 and dict(outcome.body) == {"ok": True}
    N = record_path(root, "a").read_bytes()
    assert events == [("installed", "a", N, True), ("committed", "a")]

    # 4. a create through the registry: installed with notify False, and no committed
    events.clear()
    outcome = rt.registry.create(call, CopyCreate(rt.store.read("a")))
    assert outcome.status == 200 and dict(outcome.body) == {"ok": True}
    C = record_path(root, "c").read_bytes()
    assert events == [("installed", "c", C, False)]

    # 5. a dry run reports nothing
    events.clear()
    rt.registry.writer("a").submit(WriteCall("fx", {}, "x", None, True), FieldWrite())
    assert events == []
