"""Dispatch order: a call breaking two rules gets the running unit's refusal; an unknown tool too (T-42).

For each tool taking a project, a call names a project absent from the store and gives one other
field a value of the wrong JSON type; one more call names the tool 'no_such_tool'. Each is sent to
the running unit and to the rebuilt unit over copies of the same store, and the two answers must be
equal in status and body bytes. The running unit is the install at MU_SPEC_REFERENCE_INSTALL; the
rebuilt unit is this repository. Each is started as a process from its own install root with the
start check's argument list and called over HTTP on 127.0.0.1; each tool's route comes from the
unit's own tool discovery. Modules under test are imported inside the test, so a module not yet
written fails only the tests that need it.
"""

import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_VAR = "MU_SPEC_REFERENCE_INSTALL"
HOST = "127.0.0.1"
DEFAULT_PORT = 9006
START_WINDOW_S = 20.0
STOP_WINDOW_S = 15.0
CALL_TIMEOUT_S = 30.0
POLL_S = 0.1
# what the interpreter itself needs to run on the host; the same for both units
PASS_THROUGH = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
ROUTE_FIELDS = ("route", "path", "url", "endpoint")
SCHEMA_FIELDS = ("input_schema", "inputSchema", "schema")
ACTOR = "a"
ABSENT = "absent"  # a project the store does not hold
FILLER = "p"  # every path parameter but the project
UNKNOWN_TOOL = "no_such_tool"
# a value of another JSON type than the one a field declares
WRONG = {"string": 7, "integer": "x", "number": "x", "boolean": "x", "array": "x", "object": "x", "null": "x"}


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

    def __init__(self, install_root: str, state_dir: Path, cwd: Path) -> None:
        from mu_spec.outside import ENV_NAMES
        from startcheck.runner import BOOTSTRAP

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


def discovery(base: str) -> dict[str, dict]:
    """The unit's tool discovery, as {tool name: its entry}."""
    status, raw = http_request(base, "GET", "/tools")
    listing = decode(raw)
    assert status == 200, f"GET /tools answered {status}: {raw!r}"
    if isinstance(listing, dict) and isinstance(listing.get("tools"), (list, dict)):
        listing = listing["tools"]
    if isinstance(listing, dict):
        return {name: spec if isinstance(spec, dict) else {} for name, spec in listing.items()}
    return {t["name"]: t for t in listing or () if isinstance(t, dict) and isinstance(t.get("name"), str)}


def route_of(spec: dict | None, tool: str) -> tuple[str, str]:
    """(method, path pattern) of one discovery entry; an unlisted tool has the default route."""
    spec = spec or {}
    method = str(spec.get("method", "POST")).upper()
    route = next((spec[f] for f in ROUTE_FIELDS if isinstance(spec.get(f), str)), f"/tools/{tool}")
    return method, route


def properties_of(spec: dict) -> dict[str, dict]:
    schema = next((spec[f] for f in SCHEMA_FIELDS if isinstance(spec.get(f), dict)), {})
    props = schema.get("properties")
    return {k: v for k, v in props.items() if isinstance(v, dict)} if isinstance(props, dict) else {}


def path_params(route: str) -> list[str]:
    return re.findall(r"\{(\w+)\}", route)


def takes_project(spec: dict) -> bool:
    return "project" in properties_of(spec) or "project" in path_params(route_of(spec, "")[1])


def json_type(field: dict) -> str | None:
    declared = field.get("type")
    if isinstance(declared, list):
        declared = next((t for t in declared if t != "null"), None)
    return declared if declared in WRONG else None


def breaking_args(spec: dict) -> dict:
    """An absent project and the first other body field given a value of the wrong JSON type."""
    args: dict = {"project": ABSENT}
    in_path = set(path_params(route_of(spec, "")[1]))
    for name, field in properties_of(spec).items():
        kind = json_type(field)
        if name != "project" and name not in in_path and kind is not None:
            args[name] = WRONG[kind]
            break
    return args


def call(base: str, spec: dict | None, tool: str, args: dict) -> tuple[int, bytes]:
    """Send one call: path parameters from args (FILLER where args has none), args as the JSON body."""
    method, route = route_of(spec, tool)
    path = re.sub(r"\{(\w+)\}",
                  lambda m: urllib.parse.quote(str(args.get(m.group(1), FILLER)), safe=""), route)
    return http_request(base, method, path, json.dumps(args).encode("utf-8"))


def seed_store(base: str) -> None:
    """The store holds project 'p', so ABSENT is absent from a store that is not empty."""
    tools = discovery(base)
    status, raw = call(base, tools["post_request"], "post_request",
                       {"type": "initiate", "title": "order project", "project": "p", "actor": ACTOR})
    assert 200 <= status < 300, f"post_request refused: {status} {raw!r}"
    body = decode(raw)
    rid = next((body[f] for f in ("request_id", "id") if isinstance(body, dict) and f in body), None)
    if rid is None and isinstance(body, dict) and isinstance(body.get("created"), list):
        rid = body["created"][0]
    assert rid is not None, f"post_request answer names no request id: {raw!r}"
    status, raw = call(base, tools["create_project"], "create_project",
                       {"project": "p", "in_response_to": rid, "actor": ACTOR})
    assert 200 <= status < 300, f"create_project refused: {status} {raw!r}"


def test_two_breaches_and_unknown_tool(tmp_path):
    running_root = reference_install()
    rebuilt_root = str(REPO_ROOT)

    # one store, seeded by the running unit, then one copy per unit
    origin = tmp_path / "origin"
    (origin / "projects").mkdir(parents=True)
    unit = Unit(running_root, origin, tmp_path)
    try:
        seed_store(unit.base)
    finally:
        unit.stop()

    units = []
    try:
        for name, root in (("running", running_root), ("rebuilt", rebuilt_root)):
            copy = tmp_path / name
            shutil.copytree(origin, copy)
            units.append(Unit(root, copy, tmp_path))
        running, rebuilt = units
        run_tools, reb_tools = discovery(running.base), discovery(rebuilt.base)

        calls = [(tool, breaking_args(spec)) for tool, spec in run_tools.items() if takes_project(spec)]
        assert calls, "the running unit's discovery lists no tool taking a project"
        calls.append((UNKNOWN_TOOL, {}))

        answers = [(tool, args, call(running.base, run_tools.get(tool), tool, args),
                    call(rebuilt.base, reb_tools.get(tool), tool, args)) for tool, args in calls]

        # the precondition: on at least one tool the running unit checks the project first
        refusal = f"unknown project '{ABSENT}'".encode()
        unknown_project = [tool for tool, _, (status, raw), _ in answers
                           if tool != UNKNOWN_TOOL and status == 404 and refusal in raw]
        assert unknown_project, f"the running unit answers no call 404 {refusal!r}: {answers!r}"

        for tool, args, run_answer, reb_answer in answers:
            seen = f"{tool} {args!r}: running={run_answer!r} rebuilt={reb_answer!r}"
            assert reb_answer[0] == run_answer[0], seen
            assert reb_answer[1] == run_answer[1], seen
    finally:
        for u in units:
            u.stop()
