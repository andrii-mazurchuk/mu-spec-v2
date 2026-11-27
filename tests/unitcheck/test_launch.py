"""unitcheck.launch: the shared interposed launch, its InterposedLauncher and Control.

test_launch_tags_and_env launches a scratch mu_spec that reports what it was given and makes one lookup
and one GitHub request; test_restart_children_and_refusals covers restart, a spawned child, the loopback
refusal and the launcher's own starts through S-281. Both use a session's StandIn and Neighbour.
The modules under test are imported inside each test, so while they are absent only these tests fail.
"""

import http.client
import json
import os
import sys
import time

WAIT = 60.0


def R(n):
    return b'{"kind":"probe","n":%d}\n' % n


# The unit of test_launch_tags_and_env: reports its environment and LaunchArgs, writes R(1), serves /health;
# /w looks up nope.invalid, asks GitHub for /user through its own resolution, then writes R(2).
TAGS_MAIN = r'''
import http.client, json, os, socket, sys, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG, SEEN, ERRORS, HOST, PORT, GITHUB = %r, %r, %r, %r, %r, %r


def record(n):
    with open(LOG, "ab") as f:
        f.write(b'{"kind":"probe","n":' + str(n).encode() + b'}\n')


def note(what):
    with open(ERRORS, "a") as f:
        f.write(what + "\n")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/w":
            try:
                socket.getaddrinfo("nope.invalid", 80)
            except Exception:
                note(traceback.format_exc())
            try:
                conn = http.client.HTTPSConnection(GITHUB, 443, timeout=10)
                conn.request("GET", "/user")
                conn.getresponse().read()
                conn.close()
            except Exception:
                note(traceback.format_exc())
            record(2)
        elif self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    args = sys.modules["unitcheck.interposer"]._ARGS
    with open(SEEN, "w") as f:
        json.dump({"env": dict(os.environ), "args": repr(args)}, f)
    record(1)
    ThreadingHTTPServer((os.environ[HOST], int(os.environ[PORT])), Handler).serve_forever()
'''

# The child of the restart unit: 500 lookups of distinct names.
LOOKUPS_CHILD = r'''
import socket


def work():
    for i in range(500):
        try:
            socket.getaddrinfo("n%d.invalid" % i, 80)
        except Exception:
            pass
'''

# The unit of the restart and child cases: /w writes R(n), n counting this process's /w calls; /c starts
# a spawn-context child, joins it and answers.
RESTART_MAIN = r'''
import itertools, multiprocessing, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mu_spec.child import work

LOG, HOST, PORT = %r, %r, %r
COUNT = itertools.count(1)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/w":
            with open(LOG, "ab") as f:
                f.write(b'{"kind":"probe","n":' + str(next(COUNT)).encode() + b'}\n')
        elif self.path == "/c":
            process = multiprocessing.get_context("spawn").Process(target=work)
            process.start()
            process.join()
        elif self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((os.environ[HOST], int(os.environ[PORT])), Handler).serve_forever()
'''

# The unit that exits: listens on the healthy port for a moment, notes every request line it is sent,
# then exits 1 without answering any.
EXIT_MAIN = r'''
import json, os, socket, sys, time

HEARD, HOST, PORT = %r, %r, %r

heard = []
listener = socket.socket()
listener.bind((os.environ[HOST], PORT))
listener.listen(8)
listener.settimeout(0.1)
deadline = time.monotonic() + 1.0
while time.monotonic() < deadline:
    try:
        conn, _ = listener.accept()
    except OSError:
        continue
    conn.settimeout(1.0)
    try:
        heard.append(conn.recv(1024).split(b"\r\n")[0].decode("latin-1"))
    except OSError:
        pass
    conn.close()
listener.close()
with open(HEARD, "w") as f:
    json.dump(heard, f)
sys.exit(1)
'''


def scratch_install(base, name, main, child=None):
    package = base / name / "mu_spec"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(main)
    if child is not None:
        (package / "child.py").write_text(child)
    return package.parent


def check_root(tmp_path):
    """A check root and its pristine copies, the store holding projects/."""
    root = tmp_path / "check"
    root.mkdir()
    pristine_store = tmp_path / "pristine-store"
    (pristine_store / "projects").mkdir(parents=True)
    pristine_prompts = tmp_path / "pristine-prompts"
    pristine_prompts.mkdir()
    return root, pristine_store, pristine_prompts


def reset(session):
    from shipcheck.standin import Script

    session.standin.reset(Script(issues=(), links=(), next_number=1, next_id=1, user={"login": "x"}, push=True,
                                 refuse={}, unanswered=frozenset(), hold=None))
    session.neighbour.set("present")


def get(port, path):
    from startcheck.runner import REACH_HOST

    conn = http.client.HTTPConnection(REACH_HOST, port, timeout=WAIT)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def read(path):
    return path.read_text() if path.exists() else ""


def test_launch_tags_and_env(tmp_path):
    from mu_spec.config import ROOT_NAME, VALUE_NAMES
    from mu_spec.outside import ENV_NAMES
    from shipcheck.standin import CERT_FIXTURE, GITHUB_HOST
    from startcheck.runner import UnitCommand, restore, stop
    from startcheck.variants import PASS_THROUGH, environment
    from unitcheck.interposer import LaunchArgs
    from unitcheck.launch import START_SPAN, START_VARIANT, followed_log, launch
    from unitcheck.session import open_session

    root, pristine_store, pristine_prompts = check_root(tmp_path)
    seen_path = tmp_path / "seen.json"
    errors = tmp_path / "errors.txt"
    main = TAGS_MAIN % (str(followed_log(str(root))), str(seen_path), str(errors),
                        ENV_NAMES["host"], ENV_NAMES["port"], GITHUB_HOST)
    cmd = UnitCommand(label="running", install_root=str(scratch_install(tmp_path, "install", main)))
    own_env = dict(os.environ)

    session = open_session(own_env)
    started = []
    try:
        reset(session)
        restore(str(root), str(pristine_store), str(pristine_prompts))
        it = launch(cmd, str(root), own_env, session.standin, session.neighbour)
        started.append(it.started)
        assert get(it.started.port, "/w") == 200
        it.control.sync(3)
        problems = read(errors)

        # The unit's environment is the variant's exactly, plus SSL_CERT_FILE.
        seen = json.loads(seen_path.read_text())
        base = environment(START_VARIANT, str(root), own_env)
        assert seen["env"] == {**base, "SSL_CERT_FILE": CERT_FIXTURE}
        assert set(seen["env"]) - set(base) == {"SSL_CERT_FILE"}
        assert "SSL_CERT_FILE" not in set(VALUE_NAMES) | {ROOT_NAME} | set(PASS_THROUGH)

        # The bootstrap gave the interposer tuple addresses.
        args = eval(seen["args"], {"__builtins__": {}, "LaunchArgs": LaunchArgs})
        for field in ("control", "standin", "neighbour_tcp", "neighbour_udp"):
            assert isinstance(getattr(args, field), tuple), (field, seen["args"])

        # Start-up's record is tagged START_SPAN, the call's by its sync.
        records = it.control.records()
        assert [(r.ordinal, r.call) for r in records] == [(1, START_SPAN), (2, 3)], records
        assert [r.data for r in records] == [R(1), R(2)], records

        attempts = it.control.attempts()
        lookups = [a for a in attempts if a.op == "lookup" and a.name == "nope.invalid"]
        assert [(a.process, a.call) for a in lookups] == [(0, 3)], attempts
        assert isinstance(lookups[0].at, float), lookups

        # The GitHub connect reached the stand-in, and not the neighbour.
        assert [(r.method, r.path) for r in session.standin.log()] == [("GET", "/user")], problems
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            assert not [r for r in session.neighbour.reached() if r.transport == "tcp"], problems
            time.sleep(0.1)
    finally:
        for s in started:
            stop(s)
        session.neighbour.set("present")
        session.close()


def test_restart_children_and_refusals(tmp_path, monkeypatch):
    import unitcheck.launch
    from mu_spec.outside import ENV_NAMES
    from shipcheck.standin import CERT_FIXTURE
    from startcheck.runner import Prepared, UnitCommand, restore, start, stop
    from startcheck.variants import HEALTHY_PORT, environment
    from unitcheck.launch import (
        CHECK_PATH,
        FINAL_SPAN,
        INTERPOSED_BOOTSTRAP,
        InterposedLauncher,
        LaunchError,
        followed_log,
        launch,
        restart,
    )
    from unitcheck.session import open_session

    root, pristine_store, pristine_prompts = check_root(tmp_path)
    restart_main = RESTART_MAIN % (str(followed_log(str(root))), ENV_NAMES["host"], ENV_NAMES["port"])
    cmd = UnitCommand(label="running",
                      install_root=str(scratch_install(tmp_path, "install", restart_main, LOOKUPS_CHILD)))
    heard_path = tmp_path / "heard.json"
    exit_main = EXIT_MAIN % (str(heard_path), ENV_NAMES["host"], HEALTHY_PORT)
    exit_cmd = UnitCommand(label="running", install_root=str(scratch_install(tmp_path, "exits", exit_main)))
    own_env = dict(os.environ)

    session = open_session(own_env)
    started = []
    try:
        reset(session)
        restore(str(root), str(pristine_store), str(pristine_prompts))

        # Refusal: a host that is a name, not a numeric loopback address, starts nothing.
        calls = []
        with monkeypatch.context() as m:
            m.setattr(unitcheck.launch, "environment",
                      lambda v, r, e: {**environment(v, r, e), ENV_NAMES["host"]: "localhost"})
            m.setattr(unitcheck.launch, "start", lambda *a, **kw: calls.append((a, kw)))
            try:
                launch(cmd, str(root), own_env, session.standin, session.neighbour)
            except LaunchError:
                pass
            else:
                raise AssertionError("launch under a 'localhost' host did not raise LaunchError")
        assert calls == []

        # Never hello: ready gives False within its time and raises nothing.
        launcher = InterposedLauncher(session.standin, session.neighbour)
        lone = launcher.prepare(cmd, str(root))
        try:
            began = time.monotonic()
            assert lone.ready(1.0) is False
            assert time.monotonic() - began < 1.5
        finally:
            lone.done()

        # prepare twice: two documents, two control ports, two Controls; another launcher's is unknown.
        other = InterposedLauncher(session.standin, session.neighbour)
        p1, p2 = launcher.prepare(cmd, str(root)), launcher.prepare(cmd, str(root))
        p3 = other.prepare(cmd, str(root))
        try:
            for p in (p1, p2):
                assert p.argv[:5] == (sys.executable, "-I", "-c", INTERPOSED_BOOTSTRAP, CHECK_PATH), p.argv
                assert p.argv[6] == cmd.install_root
                assert dict(p.extra_env) == {"SSL_CERT_FILE": CERT_FIXTURE}
            port1 = json.loads(p1.argv[5])["control"][1]
            port2 = json.loads(p2.argv[5])["control"][1]
            assert port1 != port2
            assert launcher.control(p1) is not launcher.control(p2)
            try:
                launcher.control(p3)
            except KeyError:
                pass
            else:
                raise AssertionError("control() of another launcher's Prepared did not raise KeyError")
        finally:
            for p in (p1, p2, p3):
                p.done()

        # Exits: S-281 classes the start; ready said True once the hello came, and no /health was asked.
        answers = []

        class Watched:
            def __init__(self):
                self.inner = InterposedLauncher(session.standin, session.neighbour)

            def prepare(self, c, r):
                p = self.inner.prepare(c, r)

                def ready(seconds_left):
                    answers.append(p.ready(seconds_left))
                    return answers[-1]

                return Prepared(p.argv, p.extra_env, ready, p.done)

        exited = start(exit_cmd, "hhxhhh", str(root), own_env, Watched())
        assert exited.outcome == "exited", (exited.stdout, exited.stderr)
        assert exited.exit_code == 1
        assert answers == [True], answers
        assert json.loads(heard_path.read_text()) == [], heard_path.read_text()

        # Restart: the last report of the stopped process is FINAL_SPAN; the new one counts from 1.
        restore(str(root), str(pristine_store), str(pristine_prompts))
        it = launch(cmd, str(root), own_env, session.standin, session.neighbour)
        started.append(it.started)
        assert get(it.started.port, "/w") == 200
        it.control.sync(0)
        assert get(it.started.port, "/w") == 200
        it2 = restart(it, cmd, str(root), own_env, session.standin, session.neighbour)
        started.append(it2.started)
        records = it.control.records()
        assert [(r.ordinal, r.call) for r in records] == [(1, 0), (2, FINAL_SPAN)], records
        assert it2.control is not it.control

        # Child: sync waits for the spawned child's stream, and tags its lookups with the call.
        assert get(it2.started.port, "/w") == 200
        assert get(it2.started.port, "/c") == 200
        it2.control.sync(0)
        records2 = it2.control.records()
        assert [(r.ordinal, r.call) for r in records2] == [(1, 0)], records2
        child = [a for a in it2.control.attempts() if a.op == "lookup" and a.process == 1]
        assert sorted(a.name for a in child) == sorted(f"n{i}.invalid" for i in range(500)), len(child)
        assert {a.call for a in child} == {0}, {a.call for a in child}
    finally:
        for s in started:
            stop(s)
        session.neighbour.set("present")
        session.close()
