"""The Ship check's stand-in and launcher, shipcheck.standin and shipcheck.launch: a scripted TLS
stand-in for GitHub that records every request on arrival and indexes its faults over every request
but GET /user, and a launcher that starts an unmodified unit under the runner's seeded -s launch.

Modules under test are imported inside each test, so a module not yet written fails only the tests
that need it. The client here reaches the stand-in over TLS through socket.getaddrinfo wrapped the
way SHIP_BOOTSTRAP wraps it in a unit: GITHUB_HOST goes to the stand-in, loopback passes through,
any other name fails.
"""

import http.client
import json
import os
import socket
import ssl
import sys
import threading
import time

import pytest

WAIT = 5.0
LOOPBACK = ("localhost", "127.0.0.1", "::1")


def _issue(number, issue_id, state="open"):
    return {"number": number, "id": issue_id, "state": state, "state_reason": None,
            "title": f"issue {number}", "body": "", "labels": ()}


def _script(**changes):
    from shipcheck.standin import Script

    fields = {"issues": (), "links": (), "next_number": 1, "next_id": 1,
              "user": {"login": "octo", "type": "User"}, "push": True, "refuse": {},
              "unanswered": frozenset(), "hold": None}
    fields.update(changes)
    return Script(**fields)


@pytest.fixture
def github(monkeypatch):
    """A running StandIn, the test process's getaddrinfo wrapped to reach it, and a trusting context."""
    from shipcheck.standin import CERT_FIXTURE, GITHUB_HOST, StandIn
    from startcheck.runner import REACH_HOST

    standin = StandIn()
    real = socket.getaddrinfo

    def wrapped(host, port, *args, **kwargs):
        if host == GITHUB_HOST:
            return real(REACH_HOST, standin.port, *args, **kwargs)
        if host is None or host in LOOPBACK:
            return real(host, port, *args, **kwargs)
        raise socket.gaierror(socket.EAI_NONAME, f"foreign name {host}")

    monkeypatch.setattr(socket, "getaddrinfo", wrapped)
    context = ssl.create_default_context(cafile=CERT_FIXTURE)
    assert context.check_hostname
    try:
        yield standin, context
    finally:
        standin.close()


def _send(context, method, path, body=None, headers=None, timeout=WAIT):
    """One request on its own TLS connection to GITHUB_HOST: (status, decoded JSON body or None)."""
    from shipcheck.standin import GITHUB_HOST

    conn = http.client.HTTPSConnection(GITHUB_HOST, timeout=timeout, context=context)
    try:
        data = None if body is None else json.dumps(body).encode()
        conn.request(method, path, body=data, headers=dict(headers or {}))
        response = conn.getresponse()
        raw = response.read()
        return response.status, (json.loads(raw) if raw else None)
    finally:
        conn.close()


def _in_thread(context, method, path, body=None):
    """Send one request from its own thread; the box gets ('ok', status, body) or ('error', exc)."""
    box = []

    def run():
        try:
            box.append(("ok",) + _send(context, method, path, body, timeout=30.0))
        except Exception as exc:  # noqa: BLE001 -- the test inspects whatever happened
            box.append(("error", exc))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def _wait_until(predicate, within=WAIT):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _calls(standin):
    return [(r.method, r.path) for r in standin.log()]


def test_standin_answers_and_records(github):
    standin, context = github
    base = {"issues": (_issue(1, 11),), "links": ((1, 99),), "next_number": 2, "next_id": 12}

    # 1. Over TLS, GET /user answers Script.user and is recorded with its headers as sent.
    standin.reset(_script(**base))
    assert _send(context, "GET", "/user", headers={"x-probe": "1"}) == (200, {"login": "octo", "type": "User"})
    log = standin.log()
    assert len(log) == 1, log
    (entry,) = log
    assert (entry.method, entry.path) == ("GET", "/user")
    assert entry.body == b""
    assert ("x-probe", "1") in [(k.lower(), v) for k, v in entry.headers], entry.headers
    assert ("host", "api.github.com") in [(k.lower(), v) for k, v in entry.headers], entry.headers

    # 2. reset empties the log.
    standin.reset(_script(**base))
    assert standin.log() == ()

    # 3. The refused create answers its status and message and allocates nothing.
    standin.reset(_script(**base, refuse={2: (422, "nope")}))
    status, repo = _send(context, "GET", "/repos/o/r")
    assert status == 200
    assert repo["permissions"]["push"] is True
    new = {"title": "t", "body": "b"}
    assert _send(context, "POST", "/repos/o/r/issues", new) == (422, {"message": "nope"})
    status, created = _send(context, "POST", "/repos/o/r/issues", new)
    assert status == 201
    assert (created["number"], created["id"]) == (2, 12), created

    # 4. A link already held answers 422 'already been taken'.
    standin.reset(_script(**base))
    status, answer = _send(context, "POST", "/repos/o/r/issues/1/dependencies/blocked_by", {"issue_id": 99})
    assert status == 422
    assert "already been taken" in answer["message"], answer

    # 5. The held request is in the log while it waits, and release answers it from the state.
    standin.reset(_script(**base, hold=1))
    thread, box = _in_thread(context, "GET", "/repos/o/r/issues/1")
    assert _wait_until(standin.held), "the held request never arrived"
    time.sleep(0.5)
    assert box == [], box
    assert _calls(standin).count(("GET", "/repos/o/r/issues/1")) == 1, standin.log()
    standin.release()
    thread.join(WAIT)
    assert not thread.is_alive(), "the released request was never answered"
    assert len(box) == 1 and box[0][0] == "ok", box
    assert box[0][1] == 200 and box[0][2]["number"] == 1, box
    assert _calls(standin).count(("GET", "/repos/o/r/issues/1")) == 1, standin.log()
    assert standin.held() is False

    # 6. The silent request is recorded once, never answered, and changes nothing.
    standin.reset(_script(**base, silent=frozenset({1})))
    closing = {"state": "closed"}
    with pytest.raises(TimeoutError):
        _send(context, "PATCH", "/repos/o/r/issues/1", closing, timeout=0.5)
    patches = [r for r in standin.log() if (r.method, r.path) == ("PATCH", "/repos/o/r/issues/1")]
    assert len(patches) == 1, standin.log()
    assert patches[0].body == json.dumps(closing).encode()
    status, issue = _send(context, "GET", "/repos/o/r/issues/1")
    assert status == 200
    assert issue["state"] == "open", issue

    # 7. The unanswered request has its connection closed with no status line, and is recorded.
    standin.reset(_script(**base, unanswered=frozenset({1})))
    with pytest.raises((http.client.RemoteDisconnected, ConnectionError, ssl.SSLEOFError)):
        _send(context, "GET", "/repos/o/r")
    assert _calls(standin).count(("GET", "/repos/o/r")) == 1, standin.log()

    # 8. An unscripted path is a failure entry, and the stand-in goes on answering.
    standin.reset(_script(**base))
    try:
        _send(context, "GET", "/unscripted")
    except (OSError, http.client.HTTPException):
        pass
    assert any(getattr(e, "path", None) == "/unscripted" for e in standin.log()), standin.log()
    assert _send(context, "GET", "/user") == (200, {"login": "octo", "type": "User"})


def test_identity_request_is_not_indexed(github):
    standin, context = github
    base = {"issues": (), "next_number": 5, "next_id": 50}
    new = {"title": "t", "body": "b"}

    def run(script, sequence):
        standin.reset(script)
        answers = [_send(context, method, path, new if method == "POST" else None)
                   for method, path in sequence]
        return answers

    # 1. The second indexed request is refused wherever GET /user falls.
    refused = _script(**base, refuse={2: (500, "x")})
    for sequence in (
        [("GET", "/repos/o/r"), ("GET", "/user"), ("POST", "/repos/o/r/issues")],
        [("GET", "/user"), ("GET", "/repos/o/r"), ("POST", "/repos/o/r/issues")],
    ):
        answers = run(refused, sequence)
        assert [s for s, _ in answers] == [200, 200, 500], (sequence, answers)
        assert answers[2][1] == {"message": "x"}
        assert _calls(standin) == sequence

    # 2. With no user, GET /user answers 401 and is not the first indexed request.
    answers = run(_script(**base, user=None, refuse={1: (500, "x")}), [("GET", "/user"), ("GET", "/repos/o/r")])
    assert [s for s, _ in answers] == [401, 500], answers

    # 3. Repeated GET /user moves no index.
    answers = run(_script(**base, refuse={1: (500, "x")}), [("GET", "/user")] * 3 + [("POST", "/repos/o/r/issues")])
    assert [s for s, _ in answers] == [200, 200, 200, 500], answers

    # 4. A repeated send takes the next index.
    answers = run(_script(**base, refuse={1: (502, "x")}), [("POST", "/repos/o/r/issues")] * 2)
    assert [s for s, _ in answers] == [502, 201], answers
    assert answers[1][1]["number"] == 5, answers

    # 5. hold 1 holds the first indexed request, never GET /user.
    standin.reset(_script(**base, hold=1))
    thread, box = _in_thread(context, "GET", "/user")
    thread.join(WAIT)
    assert len(box) == 1 and box[0][:2] == ("ok", 200), box
    assert standin.held() is False
    thread, box = _in_thread(context, "GET", "/repos/o/r")
    assert _wait_until(standin.held), "GET /repos/o/r was not held"
    time.sleep(0.3)
    assert box == [], box
    standin.release()
    thread.join(WAIT)
    assert len(box) == 1 and box[0][:2] == ("ok", 200), box


# A fake unit: writes its environment, argv, working directory and how each name resolved, then
# serves GET /health on the healthy port (or never answers it).
_FAKE_MAIN = """\
import json, os, socket, sys, time
looked = {{}}
for name in {names!r}:
    try:
        socket.getaddrinfo(name, 443)
        looked[name] = 'resolved'
    except socket.gaierror:
        looked[name] = 'gaierror'
with open({dump!r}, 'w') as f:
    f.write(json.dumps({{'env': dict(os.environ), 'argv': sys.orig_argv, 'cwd': os.getcwd(),
                        'looked': looked}}))
if not {serve!r}:
    time.sleep(20)
    sys.exit(0)
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == '/health' else 404)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *args):
        pass

HTTPServer(('127.0.0.1', {port}), Handler).serve_forever()
"""


def _fake_install(root, main):
    """A fake install root holding an empty mu_spec/__init__.py and the given mu_spec/__main__.py."""
    (root / "mu_spec").mkdir(parents=True)
    (root / "mu_spec" / "__init__.py").write_text("")
    (root / "mu_spec" / "__main__.py").write_text(main)
    return root


def _env_of(env):
    """A started program's environment less Windows' per-drive '=' names."""
    return {k: v for k, v in env.items() if not k.startswith("=")}


def test_launch_environment_and_foreign_names(tmp_path, monkeypatch):
    import shipcheck.launch
    import startcheck.runner
    from mu_spec.outside import ENV_NAMES
    from shipcheck.launch import (
        LAUNCH_VARIANT,
        SHIP_BOOTSTRAP,
        TOKEN_VARIABLE,
        LaunchError,
        foreign_names,
        launch,
    )
    from shipcheck.standin import CERT_FIXTURE, GITHUB_HOST, StandIn
    from startcheck.runner import UnitCommand, start_env, stop
    from startcheck.variants import HEALTHY_PORT, WORK

    assert LAUNCH_VARIANT == "hhhhhh"
    assert TOKEN_VARIABLE == ENV_NAMES["github_token"]
    assert GITHUB_HOST == "api.github.com"

    def fake(name, names, serve=True):
        dump = tmp_path / f"{name}.json"
        main = _FAKE_MAIN.format(names=tuple(names), dump=str(dump), serve=serve, port=HEALTHY_PORT)
        return str(_fake_install(tmp_path / "fakes" / name, main)), dump

    foreign, foreign_dump = fake("foreign", ["example.com"])
    root = tmp_path / "check"
    (root / WORK).mkdir(parents=True)
    root = str(root)
    own_env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT") if k in os.environ}
    assert TOKEN_VARIABLE not in own_env
    standin = StandIn()
    started = None
    try:
        standin.reset(_script())

        # 1. -s, the seeded start_env plus SSL_CERT_FILE, the variant's token, cwd root/WORK.
        cmd = UnitCommand("rebuilt", foreign)
        expected = start_env(cmd, LAUNCH_VARIANT, root, own_env)
        started = launch(cmd, root, own_env, standin, token=True)
        first = json.loads(foreign_dump.read_text())
        env = _env_of(first["env"])
        assert first["argv"][1:] == ["-s", "-c", SHIP_BOOTSTRAP, str(standin.port), cmd.install_root], first["argv"]
        assert os.path.normcase(os.path.basename(first["argv"][0])) == \
            os.path.normcase(os.path.basename(sys.executable)), first["argv"]
        assert "-I" not in first["argv"][1:3] and "-E" not in first["argv"][1:3]
        assert first["cwd"] == os.path.join(root, WORK)
        assert env == {**expected, "SSL_CERT_FILE": CERT_FIXTURE}
        assert set(env) - set(expected) == {"SSL_CERT_FILE"}
        assert env["PYTHONHASHSEED"] == "0"
        assert [k for k in env if k.upper().startswith("PYTHON")] == ["PYTHONHASHSEED"]
        assert isinstance(env[TOKEN_VARIABLE], str) and env[TOKEN_VARIABLE]
        assert env[TOKEN_VARIABLE] == expected[TOKEN_VARIABLE]

        # 2. A command's own seed reaches PYTHONHASHSEED; nothing else moves.
        stop(started)
        started = launch(UnitCommand("rebuilt", foreign, 7), root, own_env, standin, token=True)
        seeded = json.loads(foreign_dump.read_text())
        seeded_env = _env_of(seeded["env"])
        assert seeded_env["PYTHONHASHSEED"] == "7"
        assert {k: v for k, v in seeded_env.items() if k != "PYTHONHASHSEED"} == \
            {k: v for k, v in env.items() if k != "PYTHONHASHSEED"}
        assert (seeded["argv"], seeded["cwd"]) == (first["argv"], first["cwd"])

        # 3. token=False removes the token variable and nothing else.
        stop(started)
        started = launch(cmd, root, own_env, standin, token=False)
        tokenless = _env_of(json.loads(foreign_dump.read_text())["env"])
        assert TOKEN_VARIABLE not in tokenless
        assert tokenless == {k: v for k, v in env.items() if k != TOKEN_VARIABLE}

        # 4. A foreign name fails inside the unit and is reported; GitHub's host and loopback are not.
        stop(started)
        assert json.loads(foreign_dump.read_text())["looked"] == {"example.com": "gaierror"}
        assert foreign_names(started) == ("example.com",)
        own_names, own_dump = fake("own", [GITHUB_HOST, "localhost"])
        started = launch(UnitCommand("rebuilt", own_names), root, own_env, standin, token=True)
        stop(started)
        assert json.loads(own_dump.read_text())["looked"] == {GITHUB_HOST: "resolved", "localhost": "resolved"}
        assert foreign_names(started) == ()
        started = None

        # 5. A unit that never answers /health is a LaunchError once START_ALLOWANCE_S has passed.
        monkeypatch.setattr(shipcheck.launch, "START_ALLOWANCE_S", 2.0, raising=False)
        monkeypatch.setattr(startcheck.runner, "START_ALLOWANCE_S", 2.0)
        silent, _ = fake("silent", [], serve=False)
        began = time.monotonic()
        with pytest.raises(LaunchError):
            launch(UnitCommand("rebuilt", silent), root, own_env, standin, token=True)
        took = time.monotonic() - began
        assert 2.0 <= took <= 2.0 + 10, took
    finally:
        if started is not None:
            stop(started)
        standin.close()
