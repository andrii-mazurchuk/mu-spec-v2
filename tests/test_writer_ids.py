"""Writer identifiers: distinct per write, never reissued, kept across a restart, predicted by a dry run (T-421).

The rebuilt unit (this repository) is started as a process from its install root with the start
check's argument list and called over HTTP on 127.0.0.1; each tool's route comes from the unit's
own tool discovery. No field of the record's layout is read: every assertion is made on the ids the
unit answers with and on the record file's bytes as a whole. Modules under test are imported inside
the test, so a module not yet written fails only the tests that need it.
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

REPO_ROOT = Path(__file__).resolve().parents[1]
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
    """Project 'p' holding one intent entry I and no behaviour entry; returns (request id, I's id)."""
    status, raw, body = call(base, "post_request",
                             {"type": "initiate", "title": "ids project", "project": "p", "actor": ACTOR})
    assert 200 <= status < 300, f"post_request refused: {status} {raw!r}"
    rid = request_id(body)
    status, raw, body = call(base, "create_project", {"project": "p", "in_response_to": rid, "actor": ACTOR})
    assert 200 <= status < 300, f"create_project refused: {status} {raw!r}"
    status, raw, body = call(base, "submit_amendment", {
        "project": "p", "in_response_to": rid, "actor": ACTOR,
        "entries": [{"layer": "I", "title": "ids intent", "body": "the intent the behaviours serve"}],
    })
    assert admitted(status, body) and len(body["created"]) == 1, f"seeding intent refused: {status} {raw!r}"
    return rid, body["created"][0]


class Amend:
    """submit_amendment bodies on 'p'; every real submission gets a fresh idempotency key."""

    def __init__(self, rid: object, intent: object) -> None:
        self.rid, self.intent, self.keys = rid, intent, 0

    def body(self, title: str, supersedes: list | None = None, dry_run: bool = False) -> dict:
        entry = {"layer": "B", "title": title, "body": "a behaviour deriving from the intent",
                 "derives_from": [self.intent]}
        if supersedes is not None:
            entry["supersedes"] = supersedes
        body = {"project": "p", "in_response_to": self.rid, "actor": ACTOR, "entries": [entry]}
        if dry_run:
            body["dry_run"] = True
        else:
            self.keys += 1
            body["idempotency_key"] = f"ids-{self.keys}"
        return body

    def created(self, base: str, body: dict) -> str:
        """The one id the admitted amendment created."""
        status, raw, answer = call(base, "submit_amendment", body)
        assert admitted(status, answer), f"amendment refused: {status} {raw!r}"
        assert len(answer["created"]) == 1, raw
        return answer["created"][0]


def test_ids_and_dry_run(tmp_path):
    from mu_spec.codec import record_path
    from mu_spec.order import id_key

    from mu_spec.outside import ENV_NAMES, load_config

    state = tmp_path / "state"
    (state / "projects").mkdir(parents=True)
    config = load_config({ENV_NAMES["state_dir"]: str(state), ENV_NAMES["port"]: "9100"}, tmp_path)[0]
    record = record_path(config.root, "p")
    X = "ids behaviour"

    unit = Unit(str(REPO_ROOT), state, tmp_path)
    try:
        rid, intent = seed_project(unit.base)
        amend = Amend(rid, intent)

        # 1. a dry run predicts one id and leaves the record unchanged
        before = Path(record).read_bytes()
        status, raw, dry = call(unit.base, "submit_amendment", amend.body(X, dry_run=True))
        assert status == 200, raw
        assert isinstance(dry, dict) and dry.get("admitted") is True, raw
        assert isinstance(dry.get("created"), list) and len(dry["created"]) == 1, raw
        d1 = dry["created"][0]
        assert Path(record).read_bytes() == before

        # 2. the real call gets the predicted id
        c1 = amend.created(unit.base, amend.body(X))
        assert c1 == d1

        # 3. the next write gets the next number of the same prefix
        c2 = amend.created(unit.base, amend.body(X))
        assert c2 != c1
        assert id_key(c2)[0] == id_key(c1)[0] == "B"
        assert id_key(c2)[1] == id_key(c1)[1] + 1

        # 4. retiring c2 frees no number
        c3 = amend.created(unit.base, amend.body("ids successor", supersedes=[c2]))
        c4 = amend.created(unit.base, amend.body(X))
        assert all(id_key(c4)[1] > id_key(n)[1] for n in (c1, c2, c3)), (c1, c2, c3, c4)
    finally:
        unit.stop()

    # 5. a fresh unit over the same directory continues from the committed counter
    unit = Unit(str(REPO_ROOT), state, tmp_path)
    try:
        c5 = amend.created(unit.base, amend.body(X))
        assert id_key(c5)[1] == id_key(c4)[1] + 1, (c4, c5)
    finally:
        unit.stop()
