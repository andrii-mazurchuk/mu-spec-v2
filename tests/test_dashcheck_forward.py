"""The forwarding stand-in between the framed page and the rebuilt unit: dashcheck.forward (S-226).

T-198 pins that a dropped action closes the page's connection without reaching the unit and is still
counted where the page sends it, that requests are classed by kind, and that reset empties the log and
returns every action to answer mode; T-199 pins commit_give_up (relayed at once, NODE_GIVE_UP no earlier
than NODE_TIMEOUT_S, the unit's answer kept) and give_up_commit_late (the action relayed only after the
answer to the first control-view screen read sent after its give-up). The unit is a fake http.server
that records every request and answers 200 {"ok":1}. Action requests go over a raw socket, so a closed
connection and a second response on one connection are both seen as such. Modules under test are
imported inside the test, so a module not yet written fails only the tests that need it.
"""

import http.client
import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UNIT_ANSWER = (200, b'{"ok":1}')
CALL_TIMEOUT_S = 10.0


class FakeUnit:
    """Records every request as (method, path, time.monotonic()) and answers 200 b'{"ok":1}'."""

    def __init__(self, host):
        self.lock = threading.Lock()
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def handle_any(self):
                with fake.lock:
                    fake.requests.append((self.command, self.path, time.monotonic()))
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                self.send_response(UNIT_ANSWER[0])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(UNIT_ANSWER[1])))
                self.end_headers()
                self.wfile.write(UNIT_ANSWER[1])

            def do_GET(self):
                self.handle_any()

            def do_POST(self):
                self.handle_any()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer((host, 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def seen(self):
        with self.lock:
            return list(self.requests)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def http_call(host, port, method, path, body=None):
    """One request through http.client; (status, body)."""
    conn = http.client.HTTPConnection(host, port, timeout=CALL_TIMEOUT_S)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        answer = conn.getresponse()
        return answer.status, answer.read()
    finally:
        conn.close()


class RawCall:
    """One request on its own socket, read as bytes: the first response, then anything after it."""

    def __init__(self, host, port, method, path, body):
        self.sock = socket.create_connection((host, port), timeout=CALL_TIMEOUT_S)
        head = (f"{method} {path} HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n").encode("ascii")
        self.sock.sendall(head + body)
        self.data = b""
        self.closed = False       # the stand-in closed the connection (EOF or reset)
        self.first_at = None      # monotonic time the first byte of an answer arrived

    def _recv(self, deadline):
        """Read once before deadline; False when nothing more can come."""
        left = deadline - time.monotonic()
        if left <= 0 or self.closed:
            return False
        self.sock.settimeout(left)
        try:
            chunk = self.sock.recv(65536)
        except TimeoutError:
            return False
        except ConnectionError:
            self.closed = True
            return False
        if not chunk:
            self.closed = True
            return False
        if self.first_at is None:
            self.first_at = time.monotonic()
        self.data += chunk
        return True

    def _response_end(self):
        """The index just past the first whole response in data, or None."""
        split = self.data.find(b"\r\n\r\n")
        if split < 0:
            return None
        m = re.search(rb"(?im)^content-length:\s*(\d+)\s*$", self.data[:split])
        if m is None:
            return len(self.data) if self.closed else None
        end = split + 4 + int(m.group(1))
        return end if len(self.data) >= end else None

    def read_response(self, deadline):
        while self._response_end() is None and self._recv(deadline):
            pass

    def drain(self, until):
        while self._recv(until):
            pass

    def response(self):
        """(status, body) of the first response, or None when none was read."""
        end = self._response_end()
        if end is None:
            return None
        split = self.data.find(b"\r\n\r\n")
        status = int(self.data.split(b" ", 2)[1])
        return status, self.data[split + 4:end]

    def after_response(self):
        end = self._response_end()
        return self.data[end:] if end is not None else self.data

    def close(self):
        self.sock.close()


def arrange():
    """(fake unit, stand-in, ACT, action method, SCR, PRG, unit path of ACT) as the drop test sets them up."""
    from dashcheck.browser import PREFIX
    from dashcheck.forward import ForwardingStandIn
    from mu_spec.dash.progress import PROGRESS_PATH
    from mu_spec.dash.read import SCREEN_PATH
    from mu_spec.declarations import ACTIONS, find_action

    from startcheck.runner import REACH_HOST

    names = [spec.name for spec in ACTIONS if spec.name != "rollback_tickets"]
    assert len(names) == 7, names
    control_views = {a: ["ship"] for a in names}
    control_views["cut_units"] = ["order", "ship"]
    control_views["ratify"] = ["queues"]
    control_views["reject"] = ["queues"]

    spec = next(s for s in ACTIONS if s.name == "set_repo")
    act_rel = re.sub(r"\{[^}]+\}", "p", spec.path).lstrip("/")
    found = find_action(spec.method, act_rel)
    assert found is not None and found[0].name == "set_repo", (spec.path, act_rel, found)

    fake = FakeUnit(REACH_HOST)
    try:
        f = ForwardingStandIn(fake.port, control_views)
    except BaseException:
        fake.close()
        raise

    def scr(view):
        body = {"project": "p", "fallback": False, "view": view, "filter": None, "part": "whole",
                "held": None, "list_token": None}
        return PREFIX + SCREEN_PATH.lstrip("/"), json.dumps(body).encode("utf-8")

    prg = (PREFIX + PROGRESS_PATH.lstrip("/"), json.dumps({"project": "p", "run": None}).encode("utf-8"))
    return fake, f, PREFIX + act_rel, spec.method, scr, prg, "/" + act_rel


def wait_for(cond, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_drop_counts_and_kinds():
    from dashcheck.browser import PREFIX

    from startcheck.runner import REACH_HOST

    fake, f, act, method, scr, prg, unit_act = arrange()
    repo = json.dumps({"repo": "a/b"}).encode("utf-8")

    def unit_actions():
        return [r for r in fake.seen() if r[1].split("?", 1)[0] == unit_act]

    try:
        # 1. drop: the connection closes with no status line, counted at the stand-in, never relayed
        f.set_mode("set_repo", "drop")
        call = RawCall(REACH_HOST, f.port, method, act, repo)
        try:
            call.read_response(time.monotonic() + CALL_TIMEOUT_S)
            assert call.closed, ("the dropped action's connection was held open", call.data)
            assert call.data == b"", ("a dropped action got an answer", call.data)
        finally:
            call.close()
        time.sleep(0.3)
        assert f.received("set_repo") == 1
        log = f.log()
        assert len(log) == 1, log
        entry = log[0]
        assert entry.n == 1
        assert entry.kind == "action" and entry.action == "set_repo", entry
        assert entry.method == method and entry.path == act[len(PREFIX):], entry
        assert entry.relayed_at is None and entry.answered is None, entry
        assert fake.seen() == [], fake.seen()

        # 2. page, screen and progress requests: classed by kind, relayed and answered unchanged
        page = http_call(REACH_HOST, f.port, "GET", PREFIX)
        screen = http_call(REACH_HOST, f.port, "POST", *scr("ship"))
        progress = http_call(REACH_HOST, f.port, "POST", *prg)
        assert page == screen == progress == UNIT_ANSWER, (page, screen, progress)
        log = f.log()
        assert [e.kind for e in log] == ["action", "page", "screen", "progress"], log
        assert [e.n for e in log] == [1, 2, 3, 4], log
        assert [e.at for e in log] == sorted(e.at for e in log), log
        for e in log[1:]:
            assert e.relayed_at is not None and e.answered == UNIT_ANSWER, e
            assert e.action is None, e
        assert log[2].view == "ship", log[2]
        assert log[1].view is None and log[3].view is None, log

        # 3. a second dropped press: counted again, the unit still never sees set_repo
        call = RawCall(REACH_HOST, f.port, method, act, repo)
        try:
            call.read_response(time.monotonic() + CALL_TIMEOUT_S)
            assert call.closed and call.data == b"", call.data
        finally:
            call.close()
        time.sleep(0.3)
        assert f.received("set_repo") == 2
        assert unit_actions() == [], fake.seen()

        # 4. reset: the log empty, the count zero, set_repo back in answer mode
        f.reset()
        assert f.log() == ()
        assert f.received("set_repo") == 0
        call = RawCall(REACH_HOST, f.port, method, act, repo)
        try:
            call.read_response(time.monotonic() + CALL_TIMEOUT_S)
            assert call.response() == UNIT_ANSWER, ("set_repo still dropped after reset", call.data)
        finally:
            call.close()
        assert len(unit_actions()) == 1, fake.seen()
        assert f.received("set_repo") == 1
    finally:
        f.close()
        fake.close()


def test_give_up_modes():
    from dashcheck.browser import PREFIX
    from dashcheck.forward import NODE_GIVE_UP
    from mu_spec.dash.settle import NODE_TIMEOUT_S

    from startcheck.runner import REACH_HOST

    fake, f, act, method, scr, _, unit_act = arrange()
    repo = json.dumps({"repo": "a/b"}).encode("utf-8")
    unit_screen = "/" + scr("ship")[0][len(PREFIX):]

    def unit_actions():
        return [r for r in fake.seen() if r[1].split("?", 1)[0] == unit_act]

    try:
        # Case A: commit_give_up relays at once and answers NODE_GIVE_UP at NODE_TIMEOUT_S
        f.set_mode("set_repo", "commit_give_up")
        t = time.monotonic()
        call = RawCall(REACH_HOST, f.port, method, act, repo)
        try:
            call.read_response(t + NODE_TIMEOUT_S + CALL_TIMEOUT_S)
            answer = call.response()
            answered_at = call.first_at
        finally:
            call.close()
        relayed = unit_actions()
        assert len(relayed) == 1, fake.seen()
        assert relayed[0][2] < t + 0.5, relayed[0][2] - t
        assert answer == tuple(NODE_GIVE_UP), (answer, NODE_GIVE_UP)
        assert answered_at >= t + NODE_TIMEOUT_S, answered_at - t
        actions = [e for e in f.log() if e.kind == "action"]
        assert len(actions) == 1, f.log()
        assert actions[0].unit_answer == UNIT_ANSWER, actions[0]
        assert actions[0].answered == tuple(NODE_GIVE_UP), actions[0]

        # Case B: give_up_commit_late relays the action only after the first control-view read after the give-up
        f.reset()
        with fake.lock:
            fake.requests.clear()
        f.set_mode("set_repo", "give_up_commit_late")
        done = threading.Event()
        page = {}

        def press():
            c = RawCall(REACH_HOST, f.port, method, act, repo)
            try:
                c.read_response(page["t"] + NODE_TIMEOUT_S + CALL_TIMEOUT_S)
                page["answer"] = c.response()
                page["answered_at"] = c.first_at
                while not done.is_set() and not c.closed:
                    c.drain(time.monotonic() + 0.1)
                c.drain(time.monotonic() + 0.5)
                page["after"] = c.after_response()
            finally:
                c.close()

        page["t"] = t = time.monotonic()
        presser = threading.Thread(target=press, daemon=True)
        presser.start()
        reads = []
        for at, view in ((t + 1, "ship"), (t + NODE_TIMEOUT_S + 1, "spine"), (t + NODE_TIMEOUT_S + 2, "ship")):
            time.sleep(max(0.0, at - time.monotonic()))
            reads.append(http_call(REACH_HOST, f.port, "POST", *scr(view)))
        assert reads == [UNIT_ANSWER] * 3, reads
        assert wait_for(lambda: unit_actions() != [], 5.0), ("set_repo never reached the unit", fake.seen())
        time.sleep(1.0)
        done.set()
        presser.join(CALL_TIMEOUT_S)
        assert not presser.is_alive()

        assert page["answer"] == tuple(NODE_GIVE_UP), (page["answer"], NODE_GIVE_UP)
        assert page["answered_at"] >= t + NODE_TIMEOUT_S, page["answered_at"] - t
        assert page["after"] == b"", ("the action's connection got more than one response", page["after"])

        log = f.log()
        screens = [e for e in log if e.kind == "screen"]
        assert [e.view for e in screens] == ["ship", "spine", "ship"], log
        for e in screens:
            assert e.relayed_at is not None and e.relayed_at - e.at <= 0.5, e
            assert e.answered == UNIT_ANSWER, e
        unit_paths = [r[1].split("?", 1)[0] for r in fake.seen()]
        assert unit_paths == [unit_screen] * 3 + [unit_act], fake.seen()
        actions = [e for e in log if e.kind == "action"]
        assert len(actions) == 1, log
        assert actions[0].relayed_at is not None and actions[0].relayed_at > screens[2].answered_at, \
            (actions[0], screens[2])
        assert actions[0].unit_answer == UNIT_ANSWER, actions[0]
        assert actions[0].answered == tuple(NODE_GIVE_UP), actions[0]
        assert f.received("set_repo") == 1
    finally:
        f.close()
        fake.close()
