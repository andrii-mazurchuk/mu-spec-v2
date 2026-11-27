"""mu_spec.server: the request server and its one fixed route order (S-334).

T-295 pins route's order: both dashboard reads take the raw bytes before today's body parse, the
contract sees the stripped path, actions are tried before tools, a close is a Reply of nothing, and
an unrouted POST is logged even when the log cannot be written. T-296 pins serve: two requests on one
kept-alive connection, a screen read answered while an action on another connection still waits, and
a close that reaches the client with no status line.
Modules under test are imported inside the test, so a module not yet written fails only the tests that
need it.
"""

import http.client
import inspect
import json
import select
import socket
import threading
import time

import pytest

DOC = {"entries": {}, "slices": {}, "cuts": []}


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


def set_environment(monkeypatch, **texts: str) -> None:
    """Every configuration variable absent but those given, by field name."""
    from mu_spec.outside import ENV_NAMES

    for name in ENV_NAMES.values():
        monkeypatch.delenv(name, raising=False)
    for field, text in texts.items():
        monkeypatch.setenv(ENV_NAMES[field], text)


def make_served(tmp_path, monkeypatch):
    """A temporary state directory holding project "p", the runtime built over it, and its Served."""
    from mu_spec.__main__ import bind_counts
    from mu_spec.actions import action_handlers, bind_actions
    from mu_spec.codec import ProjectRecord
    from mu_spec.dash.assemble import build_dash_parts
    from mu_spec.dash.stamps import Stamps
    from mu_spec.registry import build_runtime
    from mu_spec.server import Served
    from mu_spec.ship.part import build_ship
    from mu_spec.store import Store

    from mu_spec.outside import ENV_NAMES, load_config

    D = tmp_path / "state"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    config = load_config({ENV_NAMES["port"]: "9100", ENV_NAMES["state_dir"]: str(D)}, cwd)[0]
    Store(config).create_project(ProjectRecord.from_doc("p", dict(DOC, name="p")))
    monkeypatch.chdir(cwd)
    set_environment(monkeypatch, port="9100", state_dir=str(D))
    stamps = Stamps()
    runtime = build_runtime(ship_builder=build_ship, observer=stamps, github=FakeGitHub())
    return Served(
        runtime,
        bind_actions(action_handlers(runtime)),
        bind_counts(runtime),
        build_dash_parts(runtime, stamps),
    )


def argument(fn, name, args, kwargs):
    """The value a call of fn with these arguments gives its parameter name."""
    return inspect.signature(fn).bind(*args, **kwargs).arguments[name]


def tool_path():
    """The S-319 path of list_projects, with its one leading '/'."""
    from mu_spec.declarations import TOOLS

    (spec,) = [t for t in TOOLS if t.name == "list_projects"]
    return "/" + spec.path.lstrip("/")


def J(r):
    return json.loads(r.body)


def test_route_order(tmp_path, monkeypatch):
    from mu_spec.codec import MUTATION_KIND
    from mu_spec.contract import INVALID_JSON, METHOD_NOT_ALLOWED, NOT_AN_OBJECT
    from mu_spec.dash.actionroute import CLOSE
    from mu_spec.dash.progress import PROGRESS_BAD_INPUT
    from mu_spec.dash.read import REASONS
    from mu_spec.server import (
        PAGE_ASSET,
        PAGE_METHOD,
        PAGE_ROUTE,
        PAGE_TYPE,
        Reply,
        route,
    )

    from mu_spec import server

    served = make_served(tmp_path, monkeypatch)
    runtime = served.runtime
    calls: list[str] = []
    answered_paths: list[str] = []
    real_answer = server.answer
    real_route_action = server.route_action
    real_find_tool = server.find_tool

    def answer(*args, **kwargs):
        calls.append("answer")
        answered_paths.append(argument(real_answer, "path", args, kwargs))
        return real_answer(*args, **kwargs)

    def route_action(*args, **kwargs):
        calls.append("route_action")
        return real_route_action(*args, **kwargs)

    def find_tool(*args, **kwargs):
        calls.append("find_tool")
        return real_find_tool(*args, **kwargs)

    monkeypatch.setattr(server, "answer", answer)
    monkeypatch.setattr(server, "route_action", route_action)
    monkeypatch.setattr(server, "find_tool", find_tool)
    tool = tool_path()

    def clear():
        calls.clear()
        answered_paths.clear()

    # 1. the screen read takes the raw bytes: its own bad_input refusal, no other step runs
    clear()
    r = route(served, "POST", "/dash/screen", b"not json", 0.0)
    assert r.status == 400
    assert J(r)["status"] == "refused"
    assert J(r)["reason"] == REASONS["bad_input"]
    assert "list_token" in J(r)
    assert calls == []

    # 2. the progress read takes the raw bytes: its own bad-input refusal, no other step runs
    clear()
    r = route(served, "POST", "/dash/progress", b"{}", 0.0)
    assert r.status == 400
    assert J(r) == {"state": "refused", "reason": PROGRESS_BAD_INPUT}
    assert calls == []

    # 3. the contract sees the path less its query string and one trailing '/', '/' itself kept
    clear()
    r = route(served, "GET", "/health/?x=1", b"", 0.0)
    assert r.status == 200
    assert answered_paths == ["/health"]
    clear()
    route(served, "GET", "/", b"", 0.0)
    assert answered_paths == ["/"]

    # 4. an unrouted POST is logged once as a mutation, and still answered when the log fails
    clear()
    n = len(list(runtime.store.read_log()))
    r = route(served, "POST", "/health", b"{}", 0.0)
    assert r.status == 405
    assert J(r) == METHOD_NOT_ALLOWED
    log = list(runtime.store.read_log())
    assert len(log) == n + 1
    assert log[-1].kind == MUTATION_KIND

    def unwritable(*args, **kwargs):
        raise OSError("log cannot be written")

    monkeypatch.setattr(runtime.store, "append_log", unwritable)
    clear()
    r = route(served, "POST", "/health", b"{}", 0.0)
    assert r.status == 405
    assert J(r) == METHOD_NOT_ALLOWED

    # 5. actions are tried before tools
    clear()
    r = route(served, "GET", tool, b"", 0.0)
    assert r.status == 200
    assert calls == ["answer", "route_action", "find_tool"]

    # 6. a CLOSE from the action route is a Reply of nothing
    monkeypatch.setattr(server, "route_action", lambda *args, **kwargs: CLOSE)
    clear()
    r = route(served, "POST", "/project/p/anything", b"{}", 0.0)
    assert isinstance(r, Reply)
    assert r.status is None
    assert r.body == b""
    monkeypatch.setattr(server, "route_action", route_action)

    # 7. today's body parse for every other request
    clear()
    r = route(served, "POST", tool, b"[1]", 0.0)
    assert r.status == 400
    assert J(r) == NOT_AN_OBJECT
    clear()
    r = route(served, "POST", tool, b"{x", 0.0)
    assert r.status == 400
    assert J(r) == INVALID_JSON

    # 8. the page file, the committed bytes as they lie
    clear()
    r = route(served, PAGE_METHOD, PAGE_ROUTE, b"", 0.0)
    assert r.status == 200
    assert r.content_type == PAGE_TYPE
    assert r.body == runtime.assets.read(PAGE_ASSET)


def readable(sock) -> bool:
    """Whether anything, data or a close, has arrived on sock."""
    ready, _, _ = select.select([sock], [], [], 0)
    return bool(ready)


def test_serve_threads_and_close(tmp_path, monkeypatch):
    from mu_spec.dash.actionroute import CLOSE, NO_MATCH
    from mu_spec.dash.stamps import canonical
    from mu_spec.server import serve
    from mu_spec.writer import Outcome

    from mu_spec import server

    served = make_served(tmp_path, monkeypatch)
    release = threading.Event()
    real_route_action = server.route_action

    def route_action(*args, **kwargs):
        path = argument(real_route_action, "path", args, kwargs)
        if path == "/project/p/hold":
            release.wait(10)
            return Outcome(200, {"ok": True})
        if path == "/project/p/close":
            return CLOSE
        return NO_MATCH

    monkeypatch.setattr(server, "route_action", route_action)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    threading.Thread(target=serve, args=(listener, served), daemon=True).start()
    open_input = canonical(
        {
            "project": None,
            "fallback": True,
            "view": "spine",
            "filter": None,
            "part": "whole",
            "held": None,
            "list_token": None,
        }
    )
    connections = []

    def connect():
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        connections.append(conn)
        return conn

    try:
        # 1. two requests on one kept-alive HTTP/1.1 connection, each with a Content-Length
        c = connect()
        sockets = []
        for _ in range(2):
            c.request("GET", "/health")
            r = c.getresponse()
            assert r.status == 200
            assert r.getheader("Content-Length") is not None
            assert r.version == 11
            r.read()
            sockets.append(c.sock)
        assert sockets[0] is not None
        assert sockets[1] is sockets[0]

        # 2. a screen read answers while an action on another connection still waits
        a = connect()
        a.request("POST", "/project/p/hold", body=b"{}")
        time.sleep(0.2)
        b = connect()
        sent = time.monotonic()
        b.request("POST", "/dash/screen", body=open_input)
        rb = b.getresponse()
        body = rb.read()
        assert time.monotonic() - sent < 2.0
        assert rb.status == 200
        assert json.loads(body)["names"] == ["p"]
        assert not readable(a.sock)
        release.set()
        ra = a.getresponse()
        assert ra.status == 200
        assert json.loads(ra.read()) == {"ok": True}

        # 3. a CLOSE reaches the client as a closed connection with no status line
        d = connect()
        d.request("POST", "/project/p/close", body=b"{}")
        with pytest.raises(http.client.RemoteDisconnected):
            d.getresponse()
    finally:
        release.set()
        for conn in connections:
            conn.close()
