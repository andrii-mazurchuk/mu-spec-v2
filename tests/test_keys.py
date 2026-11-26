"""Idempotency keys on the rebuilt unit: replay on the same body, 409 on another, a refused first call
leaves its key free.

The rebuilt unit (this repository) is started as a process from its install root with the start
check's argument list and called over HTTP on 127.0.0.1; each tool's route comes from the unit's
own tool discovery. Modules under test are imported inside the test, so a module not yet written
fails only the tests that need it.
"""

import http.client
import json
import os
import re
import socket
import string
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
    assert 200 <= status < 300, f"post_request refused: {status} {raw!r}"
    rid = request_id(body)
    status, raw, body = call(base, "create_project", {"project": "p", "in_response_to": rid, "actor": ACTOR})
    assert 200 <= status < 300, f"create_project refused: {status} {raw!r}"
    status, raw, body = call(base, "submit_amendment", {
        "project": "p", "in_response_to": rid, "actor": ACTOR,
        "entries": [{"layer": "I", "title": "keys intent", "body": "the intent the behaviour serves"}],
    })
    assert admitted(status, body) and body["created"], f"seeding intent refused: {status} {raw!r}"
    return rid, body["created"][0]


def amendment(rid: object, title: str, derives_from: list, key: str) -> dict:
    return {
        "project": "p", "in_response_to": rid, "actor": ACTOR,
        "entries": [{"layer": "B", "title": title, "body": "a behaviour deriving from the intent",
                     "derives_from": derives_from}],
        "idempotency_key": key,
    }


def message_matches(template: str, message: str, values: tuple[str, ...] | None) -> bool:
    """message is template with every literal part as it is and each field one of values (any text
    when values is None)."""
    pattern = ""
    for literal, field, spec, conversion in string.Formatter().parse(template):
        pattern += re.escape(literal)
        if field is not None and values is None:
            pattern += ".*?"
        elif field is not None:
            convert = {"r": repr, "s": str, "a": ascii}.get(conversion or "", lambda v: v)
            pattern += "(?:" + "|".join(re.escape(format(convert(v), spec or "")) for v in values) + ")"
    return re.fullmatch(pattern, message, re.DOTALL) is not None


def message_at(answer: object, where: list, raw: bytes) -> str:
    """The refusal message at the path refusal_body puts it."""
    message = answer
    for k in where:
        assert isinstance(message, dict) and k in message, raw
        message = message[k]
    assert isinstance(message, str), raw
    return message


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


def test_replay_conflict_and_refused_key_stays_free(tmp_path):
    from mu_spec.refusals import CATALOGUE, Refused, refusal_body, refuse

    from mu_spec import codec

    state = tmp_path / "state"
    (state / "projects").mkdir(parents=True)
    record = codec.record_path(state, "p")

    unit = Unit(str(REPO_ROOT), state, tmp_path)
    try:
        rid, intent = seed_project(unit.base)
        body_a = amendment(rid, "keys behaviour", [intent], "k1")
        body_a2 = amendment(rid, "keys behaviour, another title", [intent], "k1")

        # 1. the first keyed call is admitted
        status, raw, a1 = call(unit.base, "submit_amendment", body_a)
        assert admitted(status, a1), f"A refused: {status} {raw!r}"
        assert "replayed" not in a1, raw
        status1 = status
        b1 = Path(record).read_bytes()

        # 2. the same body replays the stored answer and writes nothing
        status, raw, replay = call(unit.base, "submit_amendment", body_a)
        assert status == status1, raw
        assert isinstance(replay, dict) and replay.get("replayed") is True, raw
        rest = {k: v for k, v in replay.items() if k != "replayed"}
        assert list(rest.items()) == list(a1.items()), raw
        assert replay["created"] == a1["created"], raw
        assert Path(record).read_bytes() == b1

        # 3. another body under the same key is the 409 conflict
        conflict = CATALOGUE["IDEMPOTENCY_CONFLICT"]
        fields = {field for _, field, _, _ in string.Formatter().parse(conflict.template) if field is not None}
        where = None
        try:
            refuse("IDEMPOTENCY_CONFLICT", **{f: f"<{f}>" for f in fields})
        except Refused as probe:
            where = message_path(refusal_body(probe), probe.message)
        assert where is not None, "refusal_body does not carry the refusal's message"
        status, raw, answer = call(unit.base, "submit_amendment", body_a2)
        assert status == conflict.status == 409, raw
        message = message_at(answer, where, raw)
        assert message_matches(conflict.template, message, ("k1", "submit_amendment", "p")), (
            f"{message!r} is not {conflict.template!r}")
        assert Path(record).read_bytes() == b1

        # 4. a call refused by a graph rule is a catalogued refusal and stores nothing under its key
        body_r = amendment(rid, "keys behaviour, unknown parent", ["I-999"], "k2")
        status, raw, answer = call(unit.base, "submit_amendment", body_r)
        assert 400 <= status < 500, raw
        assert not (isinstance(answer, dict) and "created" in answer), raw
        message = message_at(answer, where, raw)
        assert any(member.status == status and message_matches(member.template, message, None)
                   for member in CATALOGUE.values()), (
            f"{status} {message!r} is no catalogued refusal")
        assert Path(record).read_bytes() == b1

        # 5. the refused call's key is free: a corrected body is a fresh write
        body_a2_k2 = dict(body_a2, idempotency_key="k2")
        status, raw, fresh = call(unit.base, "submit_amendment", body_a2_k2)
        assert admitted(status, fresh), f"A2 under k2 refused: {status} {raw!r}"
        assert "replayed" not in fresh, raw
        assert len(fresh["created"]) == 1 and fresh["created"][0] not in a1["created"], raw
        assert Path(record).read_bytes() != b1
    finally:
        unit.stop()
