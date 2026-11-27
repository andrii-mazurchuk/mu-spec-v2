"""dashboard/src/page.ts: the page's wiring of a Ship run in a framed page, in headless Chromium (T-175, S-256).

The rebuilt unit (this repository) is started through the Ship check's launch on a restored copy and
seeded through its own tools and action routes: project "p" with one cut and repository "a/b", so a Ship
press is admitted, and project "q" with entries alone. The check serves a host page framing the unit's
page through a path-prefix proxy, as dashcheck.browser's (S-284 PREFIX: the proxy forwards PREFIX + p to
the unit's /p). The proxy records every request and answer with its monotonic time, path and body, and,
for each request, whether the frame closed its connection before the proxy wrote the answer; it can
delay its answers to the progress read. The stand-in holds the Ship run's first indexed request until
the test releases it. Modules under test are imported inside the test, so a module not yet written
fails only the tests that need it.

The pre-Ship token check control is found on a throwaway page first: each control of p's Ship screen is
clicked in turn, with the proxy refusing every person-only action without forwarding it, until one
click sends a request that is neither a dashboard read nor an action.
"""

import http.client
import itertools
import json
import os
import re
import select
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DASHBOARD = REPO / "dashboard"
SRC = DASHBOARD / "src"
CALL_TIMEOUT_S = 30.0
FORWARD_TIMEOUT_S = 180.0
ROUTE_FIELDS = ("route", "path", "url", "endpoint")
ACTOR = "a"
VIEWPORT = {"width": 1600, "height": 1000}
PREFIX = "/unit/"          # dashcheck.browser PREFIX (S-284)
PROGRESS_DELAY_S = 2.0     # step 3: how long the proxy holds its answers to the progress read
SAMPLE_S = 0.25            # how often the Ship control's state is read off the frame
BUN = shutil.which("bun") or "bun"

CLICKABLE = "button, [role=button], input[type=button], input[type=submit]"

# The view list: the deepest element whose children hold, in order, one item per view whose text
# begins with that view's name (as tests/dash/test_views_draw.py finds it).
VIEW_ITEMS = """(names) => {
  let best = null, depth = -1;
  for (const e of document.body.querySelectorAll("*")) {
    const kids = [...e.children];
    const items = [];
    let at = 0;
    for (const n of names) {
      while (at < kids.length && !kids[at].textContent.trim().toLowerCase().startsWith(n)) at++;
      if (at === kids.length) break;
      items.push(kids[at++]);
    }
    if (items.length !== names.length) continue;
    let d = 0;
    for (let p = e; p; p = p.parentElement) d++;
    if (d > depth) { best = items; depth = d; }
  }
  return best;
}"""

# The project picker: a select holding an option of that name, else the first control whose whole text
# is the name. The target is marked so Playwright can drive it; answers "select", "click" or null.
PICK = """(name) => {
  for (const e of document.querySelectorAll("[data-check-pick]")) e.removeAttribute("data-check-pick");
  for (const s of document.querySelectorAll("select")) {
    if ([...s.options].some(o => o.value === name || o.text.trim() === name)) {
      s.setAttribute("data-check-pick", "1");
      return "select";
    }
  }
  const sel = "button, [role=button], [role=option], [role=tab], [role=menuitem], li, a";
  const c = [...document.querySelectorAll(sel)].find(e => e.textContent.trim() === name);
  if (!c) return null;
  c.setAttribute("data-check-pick", "1");
  return "click";
}"""

# The Ship control's state as drawn: 'waiting' (never the 'waiting for the unit' mark) and 'done'.
SHIP_STATE = """() => {
  const t = document.body.innerText;
  return { waiting: /\\bwaiting\\b(?! for the unit)/i.test(t), done: /\\bdone\\b/i.test(t),
           ship: t.includes("Ship to GitHub") };
}"""


# ---- the unit: tools and action routes, as tests/test_keys.py calls them


def http_request(base, method, path, body=None, headers=None, timeout=CALL_TIMEOUT_S):
    host, port = base.rsplit(":", 1)
    conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
    try:
        sent = {"Content-Type": "application/json"} if body is not None else {}
        sent.update(headers or {})
        conn.request(method, path, body=body, headers=sent)
        answer = conn.getresponse()
        return answer.status, answer.read(), answer.getheader("Content-Type")
    finally:
        conn.close()


def decode(raw):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def tool_route(base, tool):
    """(method, path) for one tool, as the unit's tool discovery names it."""
    status, raw, _ = http_request(base, "GET", "/tools")
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


def call(base, tool, args):
    """Call one tool with args sent as a JSON object; every seeding call must answer 2xx."""
    method, route = tool_route(base, tool)
    status, raw, _ = http_request(base, method, route, json.dumps(args).encode("utf-8"))
    assert 200 <= status < 300, f"{tool} {args!r} answered {status}: {raw!r}"
    return decode(raw)


def act(base, path, body):
    """POST one person-only action to its relative route; every seeding press must answer 2xx."""
    status, raw, _ = http_request(base, "POST", "/" + path, json.dumps(body).encode("utf-8"))
    assert 200 <= status < 300, f"POST {path} {body!r} answered {status}: {raw!r}"
    return decode(raw)


def request_id(body):
    if isinstance(body, dict):
        for field in ("request_id", "id"):
            if field in body:
                return body[field]
        created = body.get("created")
        if isinstance(created, list) and len(created) == 1:
            return created[0]
    raise AssertionError(f"post_request answer names no request id: {body!r}")


def created_id(body):
    assert isinstance(body, dict) and isinstance(body.get("created"), list) and body["created"], body
    return body["created"][0]


def seed_entries(base, project):
    """project initiated, created and holding I and B (B deriving from I); returns (request id, B)."""
    rid = request_id(call(base, "post_request",
                          {"type": "initiate", "project": project, "title": f"{project} project", "actor": ACTOR}))
    call(base, "create_project", {"project": project, "in_response_to": rid, "actor": ACTOR})
    ids = []
    for layer in ("I", "B"):
        entry = {"layer": layer, "title": f"{project} {layer}", "body": f"the {layer} entry of {project}",
                 "derives_from": ids[-1:]}
        ids.append(created_id(call(base, "submit_amendment", {
            "project": project, "in_response_to": rid, "actor": ACTOR, "entries": [entry]})))
    return rid, ids[1]


def seed_p(base, routes):
    """p: I, B, A, S, a module implementing S, a ratified proposal, its cut and repository a/b."""
    rid, parent = seed_entries(base, "p")
    for layer in ("A", "S"):
        entry = {"layer": layer, "title": f"p {layer}", "body": f"the {layer} entry of p", "derives_from": [parent]}
        parent = created_id(call(base, "submit_amendment", {
            "project": "p", "in_response_to": rid, "actor": ACTOR, "entries": [entry]}))
    call(base, "declare_module", {"project": "p", "path": "src/core.py", "implements": [parent], "actor": ACTOR})
    call(base, "submit_proposal", {"project": "p", "proposal": {"core": [parent]}, "note": "first", "actor": ACTOR})
    act(base, routes["ratify"]["p"], {})
    act(base, routes["cut_units"]["p"], {})
    act(base, routes["set_repo"]["p"], {"repo": "a/b"})


# ---- the dashboard modules, read through bun


def bun_value(module, expr):
    script = f"const m = await import({json.dumps(module.as_uri())}); console.log(JSON.stringify({expr}));"
    done = subprocess.run([BUN, "-e", script], check=False, cwd=DASHBOARD, capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def action_routes(projects):
    """{action: {project: relative path}} from dashboard/src/actions.ts ACTION_ROUTES."""
    expr = ("Object.fromEntries(Object.entries(m.ACTION_ROUTES).map(([a, r]) => "
            f"[a, Object.fromEntries({json.dumps(list(projects))}.map(p => [p, r.path(p)]))]))")
    return bun_value(SRC / "actions.ts", expr)


# ---- the proxy and host page


class Proxy:
    """Serves the host page and forwards PREFIX + p to the unit's /p, recording every request.

    Each record: t (monotonic, when the request's first line was read), method, path (relative, PREFIX
    removed), body (JSON or None), status, answer (JSON or None), answered_at (monotonic, when the answer
    was written, None for none), closed (the frame closed its connection before the answer was written).
    """

    def __init__(self, unit_base, frame_path):
        self.lock = threading.Lock()
        self.records = []
        self.delay_progress = False
        self.refuse = frozenset()     # relative paths answered 503 without forwarding
        self.progress_path = None
        host = (f"<!doctype html><html><head><meta charset='utf-8'></head><body>"
                f"<iframe id='f' src='{frame_path}' style='width:1560px;height:960px'></iframe>"
                f"</body></html>").encode()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def parse_request(self):
                self._t = time.monotonic()
                return super().parse_request()

            def write(self, rec, status, raw, kind):
                rec["closed"] = rec["closed"] or proxy.peer_closed(self.connection)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", kind or "application/octet-stream")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    self.wfile.flush()
                except OSError:
                    pass
                rec["status"] = status
                rec["answer"] = decode(raw)
                rec["answered_at"] = time.monotonic()

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                path = self.path.split("?", 1)[0]
                if not path.startswith(PREFIX):
                    if path == "/" and self.command == "GET":
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", str(len(host)))
                        self.end_headers()
                        self.wfile.write(host)
                    else:
                        self.send_error(404)
                    return
                rel = self.path[len(PREFIX):]
                rec = {"t": self._t, "method": self.command, "path": rel.split("?", 1)[0], "body": decode(body),
                       "status": None, "answer": None, "answered_at": None, "closed": False,
                       "delayed": proxy.delay_progress and rel.split("?", 1)[0] == proxy.progress_path}
                with proxy.lock:
                    proxy.records.append(rec)
                if rec["path"] in proxy.refuse:
                    self.write(rec, 503, b"{}", "application/json")
                    return
                kept = {k: v for k, v in self.headers.items() if k.lower() == "content-type"}
                try:
                    status, raw, kind = http_request(unit_base, self.command, "/" + rel, body, kept,
                                                     timeout=FORWARD_TIMEOUT_S)
                except (OSError, http.client.HTTPException):
                    rec["closed"] = rec["closed"] or proxy.peer_closed(self.connection)
                    self.close_connection = True
                    return
                if rec["delayed"]:
                    until = rec["t"] + PROGRESS_DELAY_S
                    while time.monotonic() < until and not rec["closed"]:
                        rec["closed"] = proxy.peer_closed(self.connection, min(0.05, max(0.0, until - time.monotonic())))
                    while time.monotonic() < until:
                        time.sleep(0.01)
                self.write(rec, status, raw, kind)

            def do_GET(self):
                self.handle_any()

            def do_POST(self):
                self.handle_any()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    @staticmethod
    def peer_closed(sock, wait=0.0):
        """Whether the client has closed its side: readable with nothing to read."""
        try:
            readable, _, _ = select.select([sock], [], [], wait)
            if not readable:
                return False
            return sock.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def snapshot(self):
        with self.lock:
            return [dict(r) for r in self.records]

    def close(self):
        self.server.shutdown()


def test_ship_press_wiring(tmp_path):
    from mu_spec.server import PAGE_ROUTE
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
    from shipcheck.launch import launch
    from shipcheck.standin import Script, StandIn

    from startcheck.runner import REACH_HOST, UnitCommand, restore, stop

    store = tmp_path / "pristine_store"
    (store / "projects").mkdir(parents=True)
    prompts = tmp_path / "pristine_prompts"
    prompts.mkdir()
    (prompts / "default.md").write_bytes(b"d")
    root = tmp_path / "check"
    root.mkdir()
    routes = action_routes(["p", "q"])
    action_paths = {path for by_project in routes.values() for path in by_project.values()}
    screen_path = bun_value(SRC / "net.ts", "m.SCREEN_PATH")
    progress_path = bun_value(SRC / "net.ts", "m.PROGRESS_PATH")
    views = bun_value(SRC / "views.ts", "m.VIEW_NAMES")
    emit_path = routes["emit_tickets"]["p"]
    page_rel = PAGE_ROUTE.lstrip("/")

    def script(hold):
        return Script(issues=(), links=(), next_number=1, next_id=1, user={"login": "x"}, push=True,
                      refuse={}, unanswered=frozenset(), hold=hold)

    standin = StandIn()
    started = []
    proxy = None
    try:
        standin.reset(script(None))
        restore(str(root), str(store), str(prompts))
        it = launch(UnitCommand("rebuilt", str(REPO)), str(root), dict(os.environ), standin, token=True)
        started.append(it)
        assert it.port is not None, (it.outcome, it.stderr)
        base = f"{REACH_HOST}:{it.port}"
        seed_p(base, routes)
        seed_entries(base, "q")

        proxy = Proxy(base, PREFIX + page_rel)
        proxy.progress_path = progress_path
        host_url = f"http://127.0.0.1:{proxy.port}/"

        def reads(records, path):
            return [r for r in records if r["path"] == path]

        def project_of(r):
            return r["body"].get("project") if isinstance(r["body"], dict) else None

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                context = browser.new_context(viewport=VIEWPORT)

                def frame_of(pg):
                    return pg.wait_for_selector("iframe#f").content_frame()

                def pick(frame, name):
                    kind = frame.evaluate(PICK, name)
                    assert kind is not None, (name, frame.evaluate("() => document.body.innerText"))
                    if kind == "select":
                        frame.select_option("[data-check-pick]", label=name)
                    else:
                        frame.click("[data-check-pick]")

                def open_ship(pg):
                    """Open the host page, choose p in the picker and the Ship view, and wait for p's Ship screen."""
                    pg.goto(host_url)
                    frame = frame_of(pg)
                    frame.wait_for_function("() => document.body && document.body.innerText.length > 0")
                    pg.wait_for_timeout(500)
                    pick(frame, "p")
                    frame.wait_for_function(f"(names) => ({VIEW_ITEMS})(names) !== null", arg=views)
                    frame.evaluate(f"([names, i]) => ({VIEW_ITEMS})(names)[i].click()", [views, views.index("ship")])
                    frame.wait_for_function("() => /Ship\\s*·\\s*p\\b/.test(document.body.innerText)"
                                            " && document.body.innerText.includes('Ship to GitHub')", timeout=15000)
                    pg.wait_for_timeout(500)
                    return frame

                def label(e):
                    return e.evaluate("e => e.innerText || e.value || ''")

                def is_other(r):
                    return (r["path"] not in (screen_path, progress_path, page_rel) and r["path"] not in action_paths
                            and r["path"] != "")

                # the token check's control, found on a throwaway page with every action refused unsent
                proxy.refuse = frozenset(action_paths)
                probe = context.new_page()
                frame = open_ship(probe)
                count = len(frame.query_selector_all(CLICKABLE))
                check_at = None
                for i in range(count):
                    if i:
                        frame = open_ship(probe)
                    controls = frame.query_selector_all(CLICKABLE)
                    if i >= len(controls):
                        break
                    seen = len(proxy.snapshot())
                    controls[i].evaluate("e => e.click()")
                    probe.wait_for_timeout(1000)
                    if any(is_other(r) for r in proxy.snapshot()[seen:]):
                        check_at = i
                        break
                probe.close()
                proxy.refuse = frozenset()
                assert check_at is not None, "no control of p's Ship screen sends the token check"

                # p's Ship screen, the token check passed and its confirmation open
                pg = context.new_page()
                frame = open_ship(pg)
                idle = [label(e) for e in frame.query_selector_all(CLICKABLE)]
                frame.query_selector_all(CLICKABLE)[check_at].evaluate("e => e.click()")
                frame.wait_for_function("() => document.body.innerText.includes('Checked just now')", timeout=15000)
                seen = {}
                new = []
                for e in frame.query_selector_all(CLICKABLE):
                    t = label(e)
                    seen[t] = seen.get(t, 0) + 1
                    if seen[t] > idle.count(t):
                        new.append(e)
                ships = [e for e in new if re.search(r"ship", label(e), re.IGNORECASE)] or new
                assert ships, frame.evaluate("() => document.body.innerText")

                # the stand-in now holds the run's first indexed request
                standin.reset(script(1))
                samples = []     # (monotonic, on p's Ship screen, waiting shown, done shown)

                def sample():
                    try:
                        s = frame.evaluate(SHIP_STATE)
                    except PlaywrightError:
                        return
                    samples.append((time.monotonic(), s["ship"], s["waiting"], s["done"]))

                def watch(seconds, until=None):
                    end = time.monotonic() + seconds
                    while time.monotonic() < end:
                        if until is not None and until():
                            return True
                        sample()
                        time.sleep(SAMPLE_S)
                    return until() if until is not None else False

                # Act: the Ship press
                ships[0].click()
                assert watch(10, lambda: any(r["path"] == emit_path for r in proxy.snapshot())), "no emit_tickets request"
                a = next(r for r in proxy.snapshot() if r["path"] == emit_path)["t"]
                assert watch(30, standin.held), "the stand-in's hold never arrived"

                # 1. the first progress ask about one second after the press, for p
                assert watch(5, lambda: any(r["t"] > a for r in reads(proxy.snapshot(), progress_path)))
                first = next(r for r in reads(proxy.snapshot(), progress_path) if r["t"] > a)
                assert a + 0.95 <= first["t"] <= a + 1.6, (first["t"] - a, first)
                assert project_of(first) == "p", first

                # 2. while held, the asks for p follow one another
                watch(4)

                # 3. the progress answers delayed; a switch to q 0.5 s after an ask r
                proxy.delay_progress = True
                assert watch(5, lambda: any(r["delayed"] for r in proxy.snapshot())), "no progress ask while delayed"
                r_ask = next(r for r in proxy.snapshot() if r["delayed"])
                assert project_of(r_ask) == "p", r_ask
                time.sleep(max(0.0, r_ask["t"] + 0.5 - time.monotonic()))
                switch_at = time.monotonic()
                pick(frame, "q")
                time.sleep(max(0.0, switch_at + 5.0 - time.monotonic()))
                away = proxy.snapshot()
                proxy.delay_progress = False
                back_at = time.monotonic()
                pick(frame, "p")

                r_now = next(r for r in proxy.snapshot() if r["t"] == r_ask["t"] and r["delayed"])
                assert r_now["closed"] is True, ("the outstanding progress ask was not aborted", r_now)
                heads = [r for r in reads(away, screen_path) if r["t"] >= switch_at and project_of(r) == "q"]
                assert heads, "no head read for q after the switch"
                head_at = heads[0]["t"]
                on_q = [r for r in reads(away, progress_path) if r["t"] >= head_at]
                assert on_q == [], ("progress asked while q was shown", on_q)

                def run_answer(r):
                    return (r["path"] == screen_path and r["t"] >= back_at and project_of(r) == "p"
                            and r["answered_at"] is not None and isinstance(r["answer"], dict)
                            and isinstance(r["answer"].get("run"), dict)
                            and isinstance(r["answer"]["run"].get("id"), str))

                assert watch(15, lambda: any(run_answer(r) for r in proxy.snapshot())), "no screen answer names the run"
                named_at = min(r["answered_at"] for r in proxy.snapshot() if run_answer(r))
                assert watch(5, lambda: any(r["t"] >= named_at and project_of(r) == "p"
                                            for r in reads(proxy.snapshot(), progress_path))), \
                    "no progress ask for p after switching back"
                watch(3)

                # 4. release: one ended answer, one whole read within 1 s, no more asks for that run
                standin.release()

                def ended(r):
                    return isinstance(r["answer"], dict) and r["answer"].get("state") == "ended"

                assert watch(90, lambda: any(ended(r) for r in reads(proxy.snapshot(), progress_path))), \
                    "no progress answer said ended"
                end = next(r for r in reads(proxy.snapshot(), progress_path) if ended(r))
                end_at = end["answered_at"]
                assert watch(30, lambda: any(r["path"] == emit_path and r["answered_at"] is not None
                                             for r in proxy.snapshot())), "the Ship press got no answer"
                watch(4)
                assert watch(10, lambda: frame.evaluate(SHIP_STATE)["done"]), "the Ship control never shows done"
                records = proxy.snapshot()

                wholes = [r for r in reads(records, screen_path) if end_at < r["t"] <= end_at + 1.0
                          and isinstance(r["body"], dict) and r["body"].get("part") == "whole"]
                assert len(wholes) == 1, wholes
                run_ids = {r["answer"]["run"]["id"] for r in records
                           if isinstance(r["answer"], dict) and isinstance(r["answer"].get("run"), dict)
                           and isinstance(r["answer"]["run"].get("id"), str)}
                run_ids |= {r["body"]["run"] for r in reads(records, progress_path)
                            if r["t"] <= end["t"] and isinstance(r["body"], dict) and isinstance(r["body"].get("run"), str)}
                later = [r for r in reads(records, progress_path) if r["t"] > end["t"]
                         and isinstance(r["body"], dict) and r["body"].get("run") in run_ids]
                assert later == [], later

                # 2. (judged) each ask 0.95 s or more after the previous one's answer, outside the switch
                asks = [r for r in reads(records, progress_path) if a < r["t"] <= end["t"] and project_of(r) == "p"]
                for segment in ([r for r in asks if r["t"] < switch_at], [r for r in asks if r["t"] >= back_at]):
                    assert segment, (switch_at, back_at, asks)
                    for prev, nxt in itertools.pairwise(segment):
                        if prev["answered_at"] is not None:
                            assert nxt["t"] - prev["answered_at"] >= 0.95, (prev, nxt)
                # the Ship control: waiting until the press's own answer, done after it
                emit = next(r for r in records if r["path"] == emit_path)
                answered_at = emit["answered_at"]
                while_waiting = [s for s in samples if a + 0.5 < s[0] < answered_at - SAMPLE_S
                                 and not (switch_at <= s[0] <= back_at + 3.0) and s[1]]
                assert while_waiting, "the Ship screen was never sampled while the press waited"
                assert all(s[2] for s in while_waiting), while_waiting

                # 5. the background screen asks, press to end, stay at least 9.95 s apart
                after_action = [r["answered_at"] for r in records if r["path"] in action_paths and r["answered_at"]]

                def background(r):
                    return (isinstance(r["body"], dict) and r["body"].get("project") == "p"
                            and r["body"].get("held") is not None and r["body"].get("part") != "rest"
                            and not any(t <= r["t"] <= t + 1.0 for t in after_action))

                bg = [r for r in reads(records, screen_path) if a < r["t"] <= end_at and background(r)]
                for segment in ([r for r in bg if r["t"] < switch_at], [r for r in bg if r["t"] >= back_at]):
                    for prev, nxt in itertools.pairwise(segment):
                        assert nxt["t"] - prev["t"] >= 9.95, (prev, nxt)
            finally:
                browser.close()
    finally:
        if proxy is not None:
            proxy.close()
        for s in started:
            stop(s)
        standin.close()
