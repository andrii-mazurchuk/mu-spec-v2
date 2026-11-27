"""mu_spec.__main__: UnitComposition, the one Composition the start sequence runs over (S-335).

T-297 pins, with build_runtime, the bindings, build_dash_parts and the request server's serve patched
to recorders, that main(UnitComposition()) builds the one Stamps and runtime, checks the tools, binds
the actions and counts and builds the dashboard parts once each in that order before the listener
listens, then serves one Served of them; that a refused start stops after build_runtime; and that
`python -m mu_spec`, started through the shared interposed launch on a restored copy of a store,
answers GET /health and a whole screen read naming the store's every project.
Modules under test are imported inside the test, so a module not yet written fails only the tests
that need it.
"""

import http.client
import json
import os
import pathlib
import socket
import types

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DOC = {"entries": {}, "slices": {}, "cuts": []}
NAMES = ("q", "m", "p")
CALL_TIMEOUT_S = 30.0
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


def patch_parts(monkeypatch, log, runtime_or_raise):
    """Every part main's composition builds, patched in mu_spec.__main__ to record into log."""
    import mu_spec.__main__ as composition

    def build_runtime(*args, **kwargs):
        log.append(("build_runtime", args, kwargs))
        if isinstance(runtime_or_raise, BaseException):
            raise runtime_or_raise
        return runtime_or_raise

    sentinels = {name: object() for name in
                 ("bind_handlers", "action_handlers", "bind_actions", "bind_counts", "build_dash_parts")}
    monkeypatch.setattr(composition, "build_runtime", build_runtime)
    for name, answer in sentinels.items():
        monkeypatch.setattr(composition, name, recorder(log, name, answer))

    def serve(listener, served):
        log.append(("serve", listener, served))

    monkeypatch.setattr(composition, "serve", serve)
    real_listen = socket.socket.listen

    def listen(self, *args):
        log.append("listen")
        return real_listen(self, *args)

    monkeypatch.setattr(socket.socket, "listen", listen)
    return sentinels


def only(log, name):
    (entry,) = [item for item in log if not isinstance(item, str) and item[0] == name]
    return entry


def pristine_store(base):
    """A store holding every one of NAMES, built through the Store itself, as the copy to restore."""
    from mu_spec.codec import ProjectRecord
    from mu_spec.store import Store

    from mu_spec.outside import ENV_NAMES, load_config

    store = base / "pristine_store"
    (store / "projects").mkdir(parents=True)
    cwd = base / "pristine_cwd"
    cwd.mkdir()
    config = load_config({ENV_NAMES["port"]: "9100", ENV_NAMES["state_dir"]: str(store)}, cwd)[0]
    writer = Store(config)
    for name in NAMES:
        writer.create_project(ProjectRecord.from_doc(name, dict(DOC, name=name)))
    return store


def call(host, port, method, path, body=None):
    conn = http.client.HTTPConnection(host, port, timeout=CALL_TIMEOUT_S)
    try:
        conn.request(method, path, body=body)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_composition_order_and_process(tmp_path, monkeypatch, capsys):
    import mu_spec.__main__ as composition
    from mu_spec.dash.stamps import Stamps
    from mu_spec.order import names_sorted
    from mu_spec.registry import StartRefused
    from mu_spec.server import Served
    from mu_spec.ship.part import build_ship

    from startcheck.runner import REACH_HOST, UnitCommand, restore, start, stop
    from startcheck.variants import HEALTHY_PORT

    # Arrange A: every part a recorder, the fake runtime's config listening wherever the OS chooses
    log: list = []
    runtime = types.SimpleNamespace(config=types.SimpleNamespace(host="127.0.0.1", port=0, root_text="r"))
    sentinels = patch_parts(monkeypatch, log, runtime)
    try:
        composition.main(composition.UnitComposition())
    finally:
        for item in log:
            if not isinstance(item, str) and item[0] == "serve":
                item[1].close()
    capsys.readouterr()

    # 1. each part once, in order, the dashboard parts built before the listener listens
    assert names_in(log) == ["build_runtime", "bind_handlers", "action_handlers", "bind_actions",
                             "bind_counts", "build_dash_parts", "listen", "serve"], names_in(log)

    # 2. build_runtime given build_ship and the one Stamps; build_dash_parts given that runtime and Stamps
    _, args, kwargs = only(log, "build_runtime")
    assert args == ()
    assert kwargs["ship_builder"] is build_ship
    observer = kwargs["observer"]
    assert isinstance(observer, Stamps)
    _, args, kwargs = only(log, "build_dash_parts")
    assert len(args) + len(kwargs) == 2
    given = list(args) + list(kwargs.values())
    assert given[0] is runtime
    assert given[1] is observer

    # the bindings each given what the contract names
    _, args, _ = only(log, "action_handlers")
    assert args[0] is runtime
    _, args, _ = only(log, "bind_actions")
    assert args[0] is sentinels["action_handlers"]
    _, args, _ = only(log, "bind_counts")
    assert args[0] is runtime

    # 3. the 41 tool handlers checked; one Served of the parts handed to serve with the listener
    _, args, kwargs = only(log, "bind_handlers")
    (handlers,) = list(args) + list(kwargs.values())
    assert len(handlers) == 41
    assert len(handlers) == len(composition.TOOLS)
    _, listener, served = only(log, "serve")
    assert isinstance(listener, socket.socket)
    assert isinstance(served, Served)
    assert served.runtime is runtime
    assert served.bound is sentinels["bind_actions"]
    assert served.counts is sentinels["bind_counts"]
    assert served.dash is sentinels["build_dash_parts"]

    # Arrange B: build_runtime refuses the start
    log.clear()
    patch_parts(monkeypatch, log, StartRefused("L"))

    # 4. status 1 and nothing built, bound, listened or served after it
    rc = composition.main(composition.UnitComposition())
    capsys.readouterr()
    assert rc == 1
    assert names_in(log) == ["build_runtime"]
    monkeypatch.undo()

    # Arrange C: the acceptance copy restored, the unit started through the shared interposed launch
    from unitcheck.session import open_session

    store = pristine_store(tmp_path)
    prompts = tmp_path / "pristine_prompts"
    prompts.mkdir()
    (prompts / "default.md").write_bytes(b"d")
    root = tmp_path / "check"
    root.mkdir()
    restore(str(root), str(store), str(prompts))
    own_env = dict(os.environ)
    session = open_session(own_env)
    started = None
    try:
        started = start(UnitCommand("rebuilt", str(REPO_ROOT)), "hhhhhh", str(root), own_env, session.launcher())

        # 5. listening; health answers; the whole screen read names every stored project, sorted
        assert started.outcome == "listening", (started.outcome, started.stderr)
        status, _ = call(REACH_HOST, HEALTHY_PORT, "GET", "/health")
        assert status == 200
        status, body = call(REACH_HOST, HEALTHY_PORT, "POST", "/dash/screen", json.dumps(OPEN_INPUT).encode())
        assert status == 200, body
        assert json.loads(body)["names"] == list(names_sorted(list(NAMES)))
    finally:
        try:
            if started is not None:
                stop(started)
        finally:
            session.close()
