"""unitcheck.drivers: the start, cost and browser checks driven through one check session's shared launch.

test_drivers_compose runs the real session and the real start check over two scratch installs whose listener
is a ThreadingHTTPServer, and replaces the cost and browser runs with recorders; test_start_driver_end_to_end
runs the start driver over the running unit, the install at MU_SPEC_REFERENCE_INSTALL, and the rebuilt unit,
this repository. The modules under test are imported inside each test, so while they are absent only these
tests fail.
"""

import json
import os
import socket
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_VAR = "MU_SPEC_REFERENCE_INSTALL"

# A scratch install's unit, the same on every install but for REACH: exits 1 on an empty or non-integer port;
# on a host that is not a numeric address writes one bind line and exits 1, never looking the name up; else,
# when REACH is true, first connects once to 10.0.0.1:80, then serves every path from a ThreadingHTTPServer
# on the host (127.0.0.1 when missing or empty) and the port (9006 when missing). /stats and /stats/structure
# carry git_sha: HOLONIC_GIT_SHA when given, else the install's commit from its .git/HEAD.
SCRATCH_MAIN = r'''
import ipaddress, json, os, socket, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST, PORT, SHA, REACH = %r, %r, %r, %r

port_text = os.environ.get(PORT)
port = 9006
if port_text is not None:
    try:
        port = int(port_text)
    except ValueError:
        sys.stderr.write("mu-spec cannot start: %%s=%%r\n" %% (PORT, port_text))
        sys.exit(1)
host = os.environ.get(HOST) or "127.0.0.1"
try:
    ipaddress.ip_address(host)
except ValueError:
    sys.stderr.write("mu-spec cannot bind %%s:%%d: not an address\n" %% (host, port))
    sys.exit(1)


def git_sha():
    given = os.environ.get(SHA, "").strip()
    if given:
        return given
    with open(os.path.join(ROOT, ".git", "HEAD")) as f:
        return f.read().strip()


class Handler(BaseHTTPRequestHandler):
    def _answer(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        doc = {"path": self.path}
        if self.path in ("/stats", "/stats/structure"):
            doc = {"unit": "mu-spec", "metrics": {"git_sha": git_sha()}}
        body = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    if REACH:
        try:
            out = socket.socket()
            out.settimeout(10)
            out.connect(("10.0.0.1", 80))
            out.sendall(b"ping")
            while out.recv(65536):
                pass
            out.close()
        except OSError:
            pass
    ThreadingHTTPServer((host, port), Handler).serve_forever()
'''


def scratch_install(base, name, head, reach=False):
    from mu_spec.outside import ENV_NAMES

    root = base / "installs" / name
    (root / "mu_spec").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_bytes(head.encode() + b"\n")
    (root / "mu_spec" / "__init__.py").write_text("")
    main = SCRATCH_MAIN % (ENV_NAMES["host"], ENV_NAMES["port"], ENV_NAMES["git_sha"], reach)
    (root / "mu_spec" / "__main__.py").write_text(main)
    return root


def check_root(tmp_path, name="check"):
    """A fresh check root and its pristine copies, the store holding projects/."""
    root = tmp_path / name
    root.mkdir()
    pristine_store = tmp_path / f"{name}-store"
    (pristine_store / "projects").mkdir(parents=True)
    pristine_prompts = tmp_path / f"{name}-prompts"
    pristine_prompts.mkdir()
    return str(root), str(pristine_store), str(pristine_prompts)


def refused(port):
    from startcheck.runner import REACH_HOST

    try:
        socket.create_connection((REACH_HOST, port), timeout=10).close()
    except ConnectionRefusedError:
        return True
    except OSError:
        return False
    return False


class SeenLauncher:
    """The driver's launcher, passed through unchanged, recording the argv of every start it prepares."""

    def __init__(self, inner):
        self.inner = inner
        self.argvs = []

    def prepare(self, cmd, check_root):
        p = self.inner.prepare(cmd, check_root)
        self.argvs.append(tuple(p.argv))
        return p

    def __getattr__(self, name):
        return getattr(self.inner, name)


def opened_sessions(monkeypatch):
    """Every session the drivers open, through the real open_session."""
    import unitcheck.drivers

    real = unitcheck.drivers.open_session
    opened = []

    def opening(*args, **kwargs):
        session = real(*args, **kwargs)
        opened.append(session)
        return session

    monkeypatch.setattr(unitcheck.drivers, "open_session", opening)
    return opened


def test_drivers_compose(tmp_path, monkeypatch):
    import unitcheck.drivers
    from shipcheck.standin import GITHUB_HOST
    from unitcheck.drivers import (
        DRIVEN_CHECKS,
        DriverVerdict,
        run_browser_check,
        run_cost_check,
        run_start_check,
    )
    from unitcheck.launch import INTERPOSED_BOOTSTRAP
    from unitcheck.preflight import PreflightError

    from startcheck.runner import UnitCommand
    from startcheck.variants import QUICK

    assert DRIVEN_CHECKS == ("start", "cost", "browser")

    own_env = dict(os.environ)
    running = UnitCommand("running", str(scratch_install(tmp_path, "running", "a" * 40)))
    rebuilt = UnitCommand("rebuilt", str(scratch_install(tmp_path, "rebuilt", "b" * 40)))
    reaching = UnitCommand("rebuilt", str(scratch_install(tmp_path, "reaching", "b" * 40, reach=True)))
    opened = opened_sessions(monkeypatch)

    # The start driver hands S-323's run the session's launcher, by keyword, and keeps its report unchanged.
    real_start = unitcheck.drivers.start_run
    starts = []

    def start_recorder(*args, **kwargs):
        assert "launcher" in kwargs, (args, kwargs)
        seen = SeenLauncher(kwargs["launcher"])
        report = real_start(*args, **{**kwargs, "launcher": seen})
        starts.append((seen, report))
        return report

    monkeypatch.setattr(unitcheck.drivers, "start_run", start_recorder)

    v = run_start_check(QUICK, running, rebuilt, *check_root(tmp_path, "quiet"), own_env)
    assert len(opened) == 1 and len(starts) == 1, (opened, starts)
    seen, report = starts[0]
    assert isinstance(v, DriverVerdict), v
    assert v.check == "start"
    assert v.report is report
    assert tuple(r.variant for r in report.verdicts) == QUICK, report
    assert report.missing == (), report
    assert seen.argvs, "the start run prepared no start through the driver's launcher"
    for argv in seen.argvs:
        assert argv[3] == INTERPOSED_BOOTSTRAP, argv
    assert report.passed is True, report
    assert v.outbound == (), v.outbound
    assert v.passed == report.passed
    assert refused(opened[-1].standin.port)

    # The rebuilt unit reaches 10.0.0.1:80 at start: its report still passes, its verdict fails on outbound.
    v = run_start_check(QUICK, running, reaching, *check_root(tmp_path, "reaching"), own_env)
    assert len(opened) == 2 and len(starts) == 2, (opened, starts)
    seen, report = starts[1]
    assert v.check == "start"
    assert v.report is report
    assert tuple(r.variant for r in report.verdicts) == QUICK, report
    assert report.passed is True, report
    assert v.outbound != (), v
    assert all(isinstance(text, str) for text in v.outbound), v.outbound
    assert v.passed is False, v
    assert refused(opened[-1].standin.port)

    # The cost driver hands S-336's run the session's own stand-in, and a launcher that routes GitHub to it.
    costs = []
    cost_report = type("FakeCostReport", (), {"passed": True})()
    cost_root = check_root(tmp_path, "cost")

    def cost_recorder(*args, **kwargs):
        assert "launcher" in kwargs and "standin" in kwargs, (args, kwargs)
        p = kwargs["launcher"].prepare(rebuilt, cost_root[0])
        try:
            routed = json.loads(p.argv[5])
        finally:
            p.done()
        costs.append((kwargs["standin"], routed))
        return cost_report

    monkeypatch.setattr(unitcheck.drivers, "cost_run", cost_recorder)

    v = run_cost_check(rebuilt, *cost_root, own_env)
    assert len(opened) == 3 and len(costs) == 1, (opened, costs)
    session = opened[-1]
    standin, routed = costs[0]
    assert standin is session.standin
    assert routed["github_host"] == GITHUB_HOST, routed
    assert routed["standin"][1] == session.standin.port, routed
    assert v.check == "cost"
    assert v.report is cost_report
    assert refused(session.standin.port)

    # The browser driver hands S-284's run the session's launcher.
    browsers = []
    browser_report = type("FakeBrowserReport", (), {"passed": True})()

    def browser_recorder(*args, **kwargs):
        assert "launcher" in kwargs, (args, kwargs)
        browsers.append(kwargs["launcher"])
        return browser_report

    monkeypatch.setattr(unitcheck.drivers, "browser_run", browser_recorder)

    v = run_browser_check(running, rebuilt, *check_root(tmp_path, "browser"), own_env)
    assert len(opened) == 4 and len(browsers) == 1, (opened, browsers)
    assert v.check == "browser"
    assert v.report is browser_report
    assert refused(opened[-1].standin.port)

    # A failing preflight halts each driver before any check's run is called.
    error = PreflightError(("holding",), ("x",))
    runs = []

    def failing(*args, **kwargs):
        raise error

    monkeypatch.setattr(unitcheck.drivers, "open_session", failing)
    for name in ("start_run", "cost_run", "browser_run"):
        monkeypatch.setattr(unitcheck.drivers, name, lambda *a, _name=name, **kw: runs.append(_name))

    root = check_root(tmp_path, "halted")
    with pytest.raises(PreflightError) as caught:
        run_start_check(QUICK, running, rebuilt, *root, own_env)
    assert caught.value is error
    with pytest.raises(PreflightError) as caught:
        run_cost_check(rebuilt, *root, own_env)
    assert caught.value is error
    with pytest.raises(PreflightError) as caught:
        run_browser_check(running, rebuilt, *root, own_env)
    assert caught.value is error
    assert runs == []


def test_start_driver_end_to_end(tmp_path):
    from unitcheck.drivers import run_start_check

    from startcheck.runner import UnitCommand
    from startcheck.variants import QUICK

    reference = os.environ.get(REFERENCE_VAR)
    if not reference:
        pytest.fail(f"{REFERENCE_VAR} is not set: it must name the running unit's install root")
    running = UnitCommand("running", reference)
    rebuilt = UnitCommand("rebuilt", str(REPO_ROOT))
    root = tmp_path / "check"
    root.mkdir()
    pristine_store = tmp_path / "pristine_store"
    (pristine_store / "projects").mkdir(parents=True)
    pristine_prompts = REPO_ROOT / "prompts"

    v = run_start_check(QUICK, running, rebuilt, str(root), str(pristine_store), str(pristine_prompts),
                        dict(os.environ))

    print(f"stand-in requests: {v.standin_requests}")
    failing = "\n".join(repr(r) for r in v.report.verdicts if not r.passed)
    detail = f"outbound: {v.outbound}\nfailing verdicts:\n{failing}"
    assert v.outbound == (), detail
    assert v.report.passed is True, detail
    assert v.passed is True, detail
