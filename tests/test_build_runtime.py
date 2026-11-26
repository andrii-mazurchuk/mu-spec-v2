"""build_runtime, the one construction site: a refused port builds nothing, then Ship gets the one client.

A port that start_refusal refuses stops the start before the Store is built, so the state directory,
a pending manifest the running unit left in it included, is untouched; otherwise the Ship builder is
called once, after the Store, with the one GitHub client, and no Runtime field holds that client
(T-123). The running unit is the install at MU_SPEC_REFERENCE_INSTALL; it makes the mid-save tree.
Modules under test are imported inside the test, so a module not yet written fails only the tests
that need it.
"""

import dataclasses
import http.client
import json
import os
import pathlib
import re
import shutil
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
KILLED_STATUS = 75  # the exit status of a running unit stopped mid-save by the prelude below


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


def mid_save_prelude(pending_name: str) -> str:
    """Code run before the bootstrap: the process exits when a pending manifest is about to be rolled
    forward over an existing manifest, so the pending manifest is written and never settled."""
    return (
        "import os as _o\n"
        "def _stop(real):\n"
        "    def wrapped(src, dst, *a, **k):\n"
        f"        if _o.path.basename(_o.fspath(src)) == {pending_name!r} and _o.path.exists(dst):\n"
        f"            _o._exit({KILLED_STATUS})\n"
        "        return real(src, dst, *a, **k)\n"
        "    return wrapped\n"
        "_o.replace = _stop(_o.replace)\n"
        "_o.rename = _stop(_o.rename)\n"
    )


class Unit:
    """One unit process serving one state directory on one port."""

    def __init__(self, install_root: str, state_dir: pathlib.Path, cwd: pathlib.Path, prelude: str = "") -> None:
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
                (sys.executable, "-I", "-c", prelude + BOOTSTRAP, install_root),
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


def mid_save_tree(work: pathlib.Path) -> pathlib.Path:
    """A state directory holding project 'p' whose directory holds a pending manifest the running unit
    wrote and was stopped before rolling forward."""
    from mu_spec.store import PENDING_NAME

    state = work / "state"
    (state / "projects").mkdir(parents=True)
    unit = Unit(reference_install(), state, work, mid_save_prelude(PENDING_NAME))
    try:
        status, raw, body = call(unit.base, "post_request",
                                 {"type": "initiate", "title": "runtime project", "project": "p", "actor": ACTOR})
        assert 200 <= status < 300, f"post_request refused: {status} {raw!r}"
        rid = request_id(body)
        status, raw, _ = call(unit.base, "create_project", {"project": "p", "in_response_to": rid, "actor": ACTOR})
        assert 200 <= status < 300, f"create_project refused: {status} {raw!r}"
        try:
            call(unit.base, "submit_amendment", {
                "project": "p", "in_response_to": rid, "actor": ACTOR,
                "entries": [{"layer": "I", "title": "runtime intent", "body": "an intent saved mid-way"}],
            })
        except (OSError, http.client.HTTPException):
            pass
        unit.proc.wait(timeout=STOP_WINDOW_S)
    finally:
        unit.stop()
    assert unit.proc.returncode == KILLED_STATUS, f"the running unit was not stopped mid-save: {unit.stderr()!r}"
    assert list(state.rglob(PENDING_NAME)), "the running unit left no pending manifest"
    return state


def snapshot(root: pathlib.Path) -> dict:
    """Every path under root, root itself included: (its bytes, None for a directory, and its mtime_ns)."""
    out = {}
    for path in [root, *sorted(root.rglob("*"))]:
        data = None if path.is_dir() else path.read_bytes()
        out[str(path.relative_to(root))] = (data, path.stat().st_mtime_ns)
    return out


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


class RecordingObserver:
    """An InstallObserver, with appeared as well, recording every call made on it."""

    def __init__(self) -> None:
        self.calls = []

    def installed(self, project, data, *, notify):
        self.calls.append(("installed", project, data, notify))

    def committed(self, project):
        self.calls.append(("committed", project))

    def appended(self, record):
        self.calls.append(("appended", record))

    def appeared(self, record, how):
        self.calls.append(("appeared", record, how))


def attributes(value: object) -> list:
    """The values value holds as attributes, one level down."""
    held = list(vars(value).values()) if hasattr(value, "__dict__") else []
    for cls in type(value).__mro__:
        for slot in getattr(cls, "__slots__", ()):
            if hasattr(value, slot):
                held.append(getattr(value, slot))
    return held


def test_build_runtime_order_and_handover(tmp_path, monkeypatch):
    from mu_spec.github import GitHubClient
    from mu_spec.registry import StartRefused, build_runtime

    from mu_spec import store as store_module
    from mu_spec.outside import load_config, read_environment, start_refusal

    work = tmp_path / "work"
    work.mkdir()
    made = mid_save_tree(work)
    D = tmp_path / "state"
    shutil.copytree(made, D)
    before = snapshot(D)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    built = [False]
    original_init = store_module.Store.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        built[0] = True

    monkeypatch.setattr(store_module.Store, "__init__", init)
    S = object()
    handed = []

    def recorder(client):
        handed.append((client, built[0]))
        return S

    fake = FakeGitHub()
    obs = RecordingObserver()

    # 1. a refused port: StartRefused with start_refusal's line, and nothing built, reported or settled
    set_environment(monkeypatch, port="x", state_dir=str(D))
    expected = start_refusal(load_config(read_environment(), pathlib.Path.cwd())[1])
    assert expected is not None
    with pytest.raises(StartRefused) as refused:
        build_runtime(ship_builder=recorder, observer=obs, github=fake)
    assert refused.value.line == expected
    assert handed == []
    assert obs.calls == []
    assert built[0] is False
    assert snapshot(D) == before

    # 2. a valid port: Ship is built once, with fake, after the Store; no Runtime field holds fake
    set_environment(monkeypatch, port="9100", state_dir=str(D))
    rt = build_runtime(ship_builder=recorder, github=fake)
    assert len(handed) == 1 and handed[0][0] is fake and handed[0][1] is True
    assert fake.requests == []
    assert rt.ship is S
    assert rt.problems == ()
    assert rt.config.port is not None
    for field in dataclasses.fields(rt):
        value = getattr(rt, field.name)
        assert value is not fake, field.name
        assert all(held is not fake for held in attributes(value)), field.name

    # 3. no github given: the client handed to Ship is a GitHubClient built from the environment
    handed.clear()
    set_environment(monkeypatch, port="9100", state_dir=str(D), github_token="t")
    build_runtime(ship_builder=recorder)
    assert len(handed) == 1
    assert isinstance(handed[0][0], GitHubClient)
    assert handed[0][0] is not fake
