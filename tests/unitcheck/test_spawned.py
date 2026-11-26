"""unitcheck.spawned: the interposer carried into every child a unit starts through multiprocessing's spawn.

test_spawned_child_interposed launches a scratch mu_spec through the shared launch, whose /go starts a
spawn-context child; test_wrapper_shape calls cover in this process over a stubbed get_command_line.
The modules under test are imported inside each test, so while they are absent only these tests fail.
"""

import base64
import http.client
import json
import os
import time
from dataclasses import asdict, replace

WAIT = 60.0

R1 = b'{"kind":"probe","n":1}\n'

# The child: a lookup and a connect, each exception class name recorded, then one record to the log.
CHILD = r'''
import json, socket

LOG = %r
CAUGHT = %r


def work():
    caught = {}
    try:
        socket.getaddrinfo("nope.invalid", 80)
    except Exception as e:
        caught["lookup"] = type(e).__name__
    try:
        socket.create_connection(("10.0.0.1", 80), 1).close()
    except Exception as e:
        caught["connect"] = type(e).__name__
    with open(CAUGHT, "w") as f:
        json.dump(caught, f)
    with open(LOG, "ab") as f:
        f.write(b'{"kind":"probe","n":1}\n')
'''

# The unit: /health on its injected host and port; /go starts the child, joins it and writes out.json.
MAIN = r'''
import json, multiprocessing, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mu_spec.child import CAUGHT, work

HOST, PORT, OUT = %r, %r, %r


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/go":
            process = multiprocessing.get_context("spawn").Process(target=work)
            process.start()
            process.join()
            try:
                with open(CAUGHT) as f:
                    caught = json.load(f)
            except Exception as e:
                caught = {"read": repr(e)}
            with open(OUT, "w") as f:
                json.dump({"exitcode": process.exitcode, "caught": caught}, f)
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


def scratch_install(base, log):
    from mu_spec.outside import ENV_NAMES

    package = base / "install" / "mu_spec"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "child.py").write_text(CHILD % (str(log), str(base / "caught.json")))
    (package / "__main__.py").write_text(MAIN % (ENV_NAMES["host"], ENV_NAMES["port"], str(base / "out.json")))
    return package.parent


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


def test_spawned_child_interposed(tmp_path):
    from startcheck.runner import UnitCommand, restore, stop
    from unitcheck.launch import followed_log, launch
    from unitcheck.session import open_session

    root = tmp_path / "check"
    root.mkdir()
    pristine_store = tmp_path / "pristine-store"
    (pristine_store / "projects").mkdir(parents=True)
    pristine_prompts = tmp_path / "pristine-prompts"
    pristine_prompts.mkdir()
    install = scratch_install(tmp_path, followed_log(str(root)))
    out = tmp_path / "out.json"
    cmd = UnitCommand(label="running", install_root=str(install))
    own_env = dict(os.environ)

    session = open_session(own_env)
    started = []
    try:
        # Run 1: 'present', mode 'every' in process 0.
        session.neighbour.set("present")
        restore(str(root), str(pristine_store), str(pristine_prompts))
        it = launch(cmd, str(root), own_env, session.standin, session.neighbour, mode="every")
        started.append(it.started)
        assert get(it.started.port, "/go") == 200
        it.control.sync(0)
        seen = out.read_text()

        attempts = it.control.attempts()
        spawns = [a for a in attempts if a.process == 0 and a.op == "spawn"]
        assert [a.child for a in spawns] == [1], (attempts, seen)
        child = [a for a in attempts if a.process == 1]
        assert [a for a in child if a.op == "lookup" and a.name == "nope.invalid"], (attempts, seen)
        assert [a for a in child if a.op == "connect" and a.asked == ("10.0.0.1", 80)], (attempts, seen)

        deadline = time.monotonic() + WAIT
        while not [r for r in session.neighbour.reached() if r.transport == "tcp"] and time.monotonic() < deadline:
            time.sleep(0.1)
        assert [r for r in session.neighbour.reached() if r.transport == "tcp"], seen

        assert it.log == followed_log(str(root))
        assert it.log.read_bytes() == R1, seen
        breaks = [b for b in it.control.breaks() if b.why == "child"]
        assert [(b.process, b.data) for b in breaks] == [(1, R1)], it.control.breaks()
        assert not [r for r in it.control.records() if r.outcome == "failed"], it.control.records()
        stop(started.pop())

        # Run 2: a fresh launch under 'absent', mode 'off'.
        session.neighbour.set("absent")
        restore(str(root), str(pristine_store), str(pristine_prompts))
        out.unlink()
        it = launch(cmd, str(root), own_env, session.standin, session.neighbour, mode="off")
        started.append(it.started)
        assert get(it.started.port, "/go") == 200
        assert json.loads(out.read_text())["caught"].get("lookup") == "gaierror", out.read_text()
    finally:
        for s in started:
            stop(s)
        session.neighbour.set("present")
        session.close()


def test_wrapper_shape(monkeypatch):
    import multiprocessing.spawn

    import unitcheck.interposer
    from unitcheck.interposer import LaunchArgs, calibrate, launch_args
    from unitcheck.spawned import CHILD_PREFIX, SPAWN_PROGRAM_START, cover

    L = ["py", "-I", "-c", SPAWN_PROGRAM_START + "pipe_handle=5)", "--multiprocessing-fork"]
    calls = []
    answer = [L]

    def orig(**kwargs):
        calls.append(kwargs)
        return list(answer[0])

    reports = []
    monkeypatch.setattr(multiprocessing.spawn, "get_command_line", orig)
    monkeypatch.setattr(unitcheck.interposer, "report", reports.append)
    # numeric_errors has no default and launch_args requires every case in order: the host's own.
    args = LaunchArgs(control=("127.0.0.1", 1), standin=("127.0.0.1", 2), github_host="api.github.com",
                      neighbour_tcp=("127.0.0.1", 3), neighbour_udp=("127.0.0.1", 4), condition="present",
                      log_path="x", mode="kth", k=2, numeric_errors=tuple(tuple(e) for e in calibrate()))

    cover(args, "/chk")
    r1 = multiprocessing.spawn.get_command_line(pipe_handle=5)
    r2 = multiprocessing.spawn.get_command_line(pipe_handle=5)

    assert calls == [{"pipe_handle": 5}, {"pipe_handle": 5}]
    for n, r in ((1, r1), (2, r2)):
        assert len(r) == 7, r
        assert r[:3] == L[:3]
        assert r[3] == CHILD_PREFIX + L[3]
        assert r[4] == "/chk"
        assert r[6] == "--multiprocessing-fork"
        assert '"' not in r[3]
        assert '"' not in r[5] and " " not in r[5]
        expected = replace(args, process=n, mode="off", k=None)
        d = json.loads(base64.b64decode(r[5]))
        assert d == json.loads(json.dumps(asdict(expected)))
        assert launch_args(d) == expected

    def attempt(m):
        return tuple(m[key] for key in ("t", "op", "event", "child", "name", "dest", "asked", "numeric"))

    spawn = ("attempt", "spawn", "get_command_line")
    assert [attempt(m) for m in reports] == [spawn + (1, None, None, None, False), spawn + (2, None, None, None, False)]

    answer[0] = ["py", "x.py"]
    assert multiprocessing.spawn.get_command_line() == ["py", "x.py"]
    assert len(reports) == 3
    assert attempt(reports[2]) == spawn + (None, None, None, None, False)
