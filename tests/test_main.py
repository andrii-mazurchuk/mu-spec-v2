"""mu_spec.main: the unit's start module, holding UnitComposition (S-502).

T-486 pins, with build_runtime, the bindings, build_dash_parts and the request server's serve patched
in mu_spec.main to recorders, that start_main(UnitComposition()) builds the one Stamps and runtime,
checks the tools, binds the actions and counts and builds the dashboard parts once each in that order
before the listener listens, then serves one Served of them; that main() takes nothing and runs the
start sequence once over one UnitComposition; that a refused start stops after build_runtime; and,
as processes on an empty store, that `python -s -m mu_spec.main` and the acceptance launch's runpy
start listen and answer GET /health and a whole screen read, that no mu_spec.__main__ exists, and
that importing mu_spec.main starts nothing.
A plain unit test: it reads no acceptance variable and launches no reference install.
Modules under test are imported inside the test, so a module not yet written fails only the tests
that need it.
"""

import http.client
import importlib.util
import inspect
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import types

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
WAIT_S = 20.0
CALL_TIMEOUT_S = 5.0
OPEN_INPUT = {"project": None, "fallback": True, "view": "spine", "filter": None,
              "part": "whole", "held": None, "list_token": None}


def names_in(log):
    return [item if isinstance(item, str) else item[0] for item in log]


def recorder(log, name, answer):
    """A stand-in appending (name, args, kwargs) to log and answering answer."""

    def record(*args, **kwargs):
        log.append((name, args, kwargs))
        return answer

    return record


def patch_parts(monkeypatch, m, log, runtime_or_raise):
    """Every part the composition builds, patched in m to record into log."""

    def build_runtime(*args, **kwargs):
        log.append(("build_runtime", args, kwargs))
        if isinstance(runtime_or_raise, BaseException):
            raise runtime_or_raise
        return runtime_or_raise

    sentinels = {name: object() for name in
                 ("bind_handlers", "action_handlers", "bind_actions", "bind_counts", "build_dash_parts")}
    monkeypatch.setattr(m, "build_runtime", build_runtime)
    for name, answer in sentinels.items():
        monkeypatch.setattr(m, name, recorder(log, name, answer))

    def serve(listener, served):
        log.append(("serve", listener, served))

    monkeypatch.setattr(m, "serve", serve)
    real_listen = socket.socket.listen

    def listen(self, *args):
        log.append("listen")
        return real_listen(self, *args)

    monkeypatch.setattr(socket.socket, "listen", listen)
    return sentinels


def only(log, name):
    (entry,) = [item for item in log if not isinstance(item, str) and item[0] == name]
    return entry


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def accepts(port):
    try:
        with socket.create_connection((HOST, port), timeout=0.5):
            return True
    except OSError:
        return False


def call(port, method, path, body=None):
    conn = http.client.HTTPConnection(HOST, port, timeout=CALL_TIMEOUT_S)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def unit_env(tmp_path, port):
    """A fresh environment: PATH (and SYSTEMROOT on Windows), a fixed hash seed, an empty state dir, port."""
    from mu_spec.config import ENV_NAMES

    state = tmp_path / "state"
    state.mkdir()
    env = {"PATH": os.environ["PATH"], "PYTHONHASHSEED": "0",
           ENV_NAMES["state_dir"]: str(state), ENV_NAMES["port"]: str(port)}
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def launch(argv, env, err_path):
    with open(err_path, "wb") as err:
        return subprocess.Popen(argv, env=env, cwd=str(REPO_ROOT),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)


def end(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=WAIT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=WAIT_S)


def wait_health(proc, port):
    """GET /health until it answers 200 or WAIT_S passes; the last status, or None if nothing answered."""
    deadline = time.monotonic() + WAIT_S
    status = None
    while time.monotonic() < deadline:
        try:
            status, _ = call(port, "GET", "/health")
            if status == 200:
                return status
        except OSError:
            pass
        if proc.poll() is not None:
            return status
        time.sleep(0.2)
    return status


def wait_exit_unreached(proc, port):
    """Poll port while proc runs; answers (exit code or None on timeout, whether port ever accepted)."""
    deadline = time.monotonic() + WAIT_S
    reached = False
    while time.monotonic() < deadline:
        reached = accepts(port) or reached
        code = proc.poll()
        if code is not None:
            return code, reached
        time.sleep(0.1)
    return None, reached


def test_composition_order_and_process(tmp_path, monkeypatch, capsys):
    import mu_spec.main as m
    from mu_spec.dash.stamps import Stamps
    from mu_spec.declarations import TOOLS
    from mu_spec.registry import StartRefused
    from mu_spec.server import Served

    # Arrange A: every part a recorder in m, the fake runtime's config listening wherever the OS chooses
    log: list = []
    runtime = types.SimpleNamespace(config=types.SimpleNamespace(host="127.0.0.1", port=0, root_text="r"))
    sentinels = patch_parts(monkeypatch, m, log, runtime)
    try:
        rc = m.start_main(m.UnitComposition())
    finally:
        for item in log:
            if not isinstance(item, str) and item[0] == "serve":
                item[1].close()
    capsys.readouterr()

    # 1. a healthy start; each part once, in order, the dashboard parts built before the listener listens
    assert rc != 1
    assert names_in(log) == ["build_runtime", "bind_handlers", "action_handlers", "bind_actions",
                             "bind_counts", "build_dash_parts", "listen", "serve"], names_in(log)

    # 2. build_runtime given m.build_ship and the one Stamps; build_dash_parts given that runtime and Stamps
    _, args, kwargs = only(log, "build_runtime")
    assert args == ()
    assert kwargs["ship_builder"] is m.build_ship
    observer = kwargs["observer"]
    assert isinstance(observer, Stamps)
    _, args, kwargs = only(log, "build_dash_parts")
    given = list(args) + list(kwargs.values())
    assert len(given) == 2
    assert given[0] is runtime
    assert given[1] is observer

    # 3. the 41 tool handlers checked; one Served of the parts handed to serve with the listener
    _, args, kwargs = only(log, "bind_handlers")
    (handlers,) = list(args) + list(kwargs.values())
    assert len(TOOLS) == 41
    assert len(handlers) == len(TOOLS)
    _, listener, served = only(log, "serve")
    assert isinstance(listener, socket.socket)
    assert isinstance(served, Served)
    assert served.runtime is runtime
    assert served.bound is sentinels["bind_actions"]
    assert served.counts is sentinels["bind_counts"]
    assert served.dash is sentinels["build_dash_parts"]

    # 4. main() takes nothing and answers the one start_main's status over one UnitComposition
    given_to_start: list = []

    def fake_start_main(composition):
        given_to_start.append(composition)
        return 7

    with monkeypatch.context() as patch:
        patch.setattr(m, "start_main", fake_start_main)
        assert len(inspect.signature(m.main).parameters) == 0
        assert m.main() == 7
    assert len(given_to_start) == 1
    assert isinstance(given_to_start[0], m.UnitComposition)

    # Arrange B: build_runtime refuses the start
    log.clear()
    patch_parts(monkeypatch, m, log, StartRefused("L"))

    # 5. status 1 and nothing built, bound, listened or served after it
    rc = m.start_main(m.UnitComposition())
    capsys.readouterr()
    assert rc == 1
    assert names_in(log) == ["build_runtime"]
    monkeypatch.undo()

    # Arrange C: an empty store and a free port, the two ways the unit is started, one after the other
    port = free_port()
    env = unit_env(tmp_path, port)
    root = str(REPO_ROOT)
    run_code = ("import runpy, sys; sys.path.insert(0, " + repr(root) + "); "
                "runpy.run_module('mu_spec.main', run_name='__main__', alter_sys=True)")
    starts = [[sys.executable, "-s", "-m", "mu_spec.main"], [sys.executable, "-s", "-c", run_code]]

    # 6. each start listens, answers health and the whole list of the empty store, and keeps running
    for n, argv in enumerate(starts):
        err_path = tmp_path / f"start{n}.err"
        proc = launch(argv, env, err_path)
        try:
            status = wait_health(proc, port)
            assert status == 200, (argv, status, proc.poll(), err_path.read_bytes()[-2000:])
            status, body = call(port, "POST", "/dash/screen", json.dumps(OPEN_INPUT).encode())
            assert status == 200, (argv, body)
            answer = json.loads(body)
            assert answer["status"] == "no_project", answer
            assert answer["names"] == [], answer
            assert proc.poll() is None
        finally:
            end(proc)

    # 7. no package __main__: `-m mu_spec` is not a start, exits non-zero and never listens
    assert importlib.util.find_spec("mu_spec.__main__") is None
    proc = launch([sys.executable, "-s", "-m", "mu_spec"], env, tmp_path / "package.err")
    try:
        code, reached = wait_exit_unreached(proc, port)
    finally:
        end(proc)
    assert code is not None and code != 0, code
    assert not reached

    # 8. importing mu_spec.main starts nothing: it exits 0 at once and never binds the port
    err_path = tmp_path / "import.err"
    proc = launch([sys.executable, "-s", "-c", "import mu_spec.main"], env, err_path)
    try:
        code, reached = wait_exit_unreached(proc, port)
    finally:
        end(proc)
    assert code == 0, (code, err_path.read_bytes()[-2000:])
    assert not reached
    assert not accepts(port)
