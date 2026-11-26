"""Idempotency keys: a replay survives a restart, and body variants answer as the running unit does.

The running unit is the install at MU_SPEC_REFERENCE_INSTALL; the rebuilt unit is this repository.
Each is started as a process from its own install root with the start check's argument list and
called over HTTP on 127.0.0.1; each tool's route comes from the unit's own tool discovery. Modules
under test are imported inside the test, so a module not yet written fails only the tests that
need it.
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import time
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

    def __init__(self, install_root: str, state_dir: Path, cwd: Path) -> None:
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
    """(method, path) for one tool, as the unit's tool discovery names it."""
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
    """Call one tool with args sent as a JSON object: (status, body bytes, decoded body)."""
    method, route = tool_route(base, tool)
    status, raw = http_request(base, method, route, json.dumps(args).encode("utf-8"))
    return status, raw, decode(raw)


def admitted(status: int, body: object) -> bool:
    return 200 <= status < 300 and isinstance(body, dict) and "created" in body


def request_id(body: object) -> object:
    if isinstance(body, dict):
        for field in ("request_id", "id"):
            if field in body:
                return body[field]
        created = body.get("created")
        if isinstance(created, list) and len(created) == 1:
            return created[0]
    pytest.fail(f"post_request answer names no request id: {body!r}")


def seed_project(base: str) -> tuple[object, object]:
    """Project 'p' holding one intent entry I; returns (request id, I's id)."""
    status, raw, body = call(base, "post_request",
                             {"type": "initiate", "title": "keys project", "project": "p", "actor": ACTOR})
    assert 200 <= status < 300, f"post_request refused at {base}: {status} {raw!r}"
    rid = request_id(body)
    status, raw, body = call(base, "create_project", {"project": "p", "in_response_to": rid, "actor": ACTOR})
    assert 200 <= status < 300, f"create_project refused at {base}: {status} {raw!r}"
    status, raw, body = call(base, "submit_amendment", {
        "project": "p", "in_response_to": rid, "actor": ACTOR,
        "entries": [{"layer": "I", "title": "keys intent", "body": "the intent the behaviour serves"}],
    })
    assert admitted(status, body) and body["created"], f"seeding intent refused at {base}: {status} {raw!r}"
    return rid, body["created"][0]


def amendment(rid: object, intent: object, key: str) -> dict:
    return {
        "project": "p", "in_response_to": rid, "actor": ACTOR,
        "entries": [{"layer": "B", "title": "keys behaviour", "body": "a behaviour deriving from the intent",
                     "derives_from": [intent]}],
        "idempotency_key": key,
    }


def variants(body: dict) -> list[tuple[str, dict]]:
    """The bodies sent second under A's key, each a variant of A."""
    reordered = {k: body[k] for k in reversed(list(body))}
    reordered["entries"] = [{k: e[k] for k in reversed(list(e))} for e in body["entries"]]
    trailing = dict(body, entries=[dict(e, body=e["body"] + " ") for e in body["entries"]])
    return [
        ("field order", reordered),
        ("another actor", dict(body, actor="b")),
        ("dry_run true", dict(body, dry_run=True)),
        ("dry_run false", dict(body, dry_run=False)),
        ("trailing space", trailing),
        ("same body", dict(body)),
    ]


def test_keys_after_restart_and_body_variants(tmp_path):
    from mu_spec import codec

    running_root = reference_install()
    rebuilt_root = str(REPO_ROOT)

    # 1. on the rebuilt unit, a replay survives a restart on the same directory
    state = tmp_path / "restart"
    (state / "projects").mkdir(parents=True)
    record = codec.record_path(state, "p")
    unit = Unit(rebuilt_root, state, tmp_path)
    try:
        rid, intent = seed_project(unit.base)
        body_a = amendment(rid, intent, "kr")
        status, raw, first = call(unit.base, "submit_amendment", body_a)
        assert admitted(status, first), f"A refused: {status} {raw!r}"
        assert "replayed" not in first, raw
        before = Path(record).read_bytes()
    finally:
        unit.stop()
    unit = Unit(rebuilt_root, state, tmp_path)
    try:
        status, raw, replay = call(unit.base, "submit_amendment", body_a)
        assert isinstance(replay, dict) and replay.get("replayed") is True, f"{status} {raw!r}"
        rest = {k: v for k, v in replay.items() if k != "replayed"}
        assert list(rest.items()) == list(first.items()), raw
        assert Path(record).read_bytes() == before
    finally:
        unit.stop()

    # 2. two copies of one project, one per unit; every variant answers the same on both
    units = []
    try:
        for name, root in (("running", running_root), ("rebuilt", rebuilt_root)):
            copy = tmp_path / name
            (copy / "projects").mkdir(parents=True)
            units.append(Unit(root, copy, tmp_path))
        seeded = [seed_project(u.base) for u in units]
        assert seeded[0] == seeded[1], f"seeding differs: running={seeded[0]!r} rebuilt={seeded[1]!r}"
        rid, intent = seeded[0]

        for n in range(len(variants(amendment(rid, intent, "kv")))):
            body_a = amendment(rid, intent, f"kv-{n}")
            label, body_v = variants(body_a)[n]
            answers = [(call(u.base, "submit_amendment", body_a), call(u.base, "submit_amendment", body_v))
                       for u in units]
            (run_first, run_second), (reb_first, reb_second) = answers
            seen = f"variant {label!r}: running={run_second[:2]!r} rebuilt={reb_second[:2]!r}"
            assert admitted(run_first[0], run_first[2]), f"A refused by the running unit: {run_first[:2]!r}"
            assert reb_first[:2] == run_first[:2], f"variant {label!r} first answers: {run_first[:2]!r} {reb_first[:2]!r}"
            assert reb_second[0] == run_second[0], seen
            assert reb_second[1] == run_second[1], seen
    finally:
        for u in units:
            u.stop()
