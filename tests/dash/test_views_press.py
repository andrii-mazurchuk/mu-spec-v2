"""dashboard/src/views.ts: draw's press points and the pre-Ship token check in headless Chromium (T-217, S-254).

Two rebuilt units (this repository) are started through the Ship check's launch on one restored copy,
seeded through the first unit's own tools and action routes: project "p" with a pending slicing
proposal, one cut and repository "a/b", and project "e" with entries alone. The first unit's token has
push access on the stand-in, the second runs with no token. The harness of the draw-states test draws
shown screens of p and e into a blank page served by http.server, recording every press; window.fetch
is replaced before it loads by a stub that holds each call until the test forwards it to one of the
units through the server's proxy. Modules under test are imported inside the test, so a module not yet
written fails only the tests that need it.
"""

import http.client
import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DASHBOARD = REPO / "dashboard"
SRC = DASHBOARD / "src"
CALL_TIMEOUT_S = 30.0
ROUTE_FIELDS = ("route", "path", "url", "endpoint")
ACTOR = "a"
VIEWPORT = {"width": 1600, "height": 1000}
LIST = {"names": ["e", "p"], "token": "L", "seq": 1}
UNIT_HEADER = "x-check-unit"  # the proxy's routing header, as FETCH_STUB sets it

# The harness: draw a page state built from opened(choice) and the given fields, recording each press
# as [action, screen, target, body] in window.__presses; changed redraws the last state.
HARNESS = """\
import { draw, initialLocal, checkRequest, heldCheck } from %(views)s;
import { opened } from %(apply)s;

const DEFAULT = { available: true, live: null, earlier: [], settled: [], value: undefined };
const w = window as any;
let last: [any, any, any] | null = null;

function show(page: any, local: any, controls: any) {
  last = [page, local, controls];
  w.__asked = [];
  const h = {
    switchTo: () => {},
    setLocal: () => {},
    refresh: () => {},
    press: (action: string, screen: string, target: unknown, body: unknown) => {
      w.__presses.push([action, screen, target, body]);
    },
    control: (action: string) => { w.__asked.push(action); return controls[action] ?? DEFAULT; },
    changed: () => { if (last) show(...last); },
  };
  document.body.replaceChildren(draw(page, local, "12:00", h));
}

w.__presses = [];
w.__asked = [];
w.__draw = show;
w.__redraw = () => { if (last) show(...last); };
w.__opened = opened;
w.__initialLocal = initialLocal;
w.__checkRequest = checkRequest;
w.__heldCheck = heldCheck;
"""

# Installed before the harness loads: every fetch is recorded and held; __release(i, unit) forwards
# call i unchanged to the named unit through the server's proxy and resolves it with that answer.
FETCH_STUB = """
(() => {
  const realFetch = window.fetch.bind(window);
  window.__fetches = [];
  window.__held = [];
  window.__answers = [];
  window.fetch = (input, init) => {
    const path = typeof input === "string" ? input : input.url;
    const method = (init && init.method) || "GET";
    window.__fetches.push({ path, method });
    return new Promise((resolve) => window.__held.push({ path, init: init || {}, resolve }));
  };
  window.__release = async (i, unit) => {
    const c = window.__held[i];
    const headers = { ...(c.init.headers || {}), "x-check-unit": unit };
    const r = await realFetch(c.path, { ...c.init, headers, signal: undefined });
    const text = await r.text();
    window.__answers[i] = { status: r.status, text };
    c.resolve(new Response(text, { status: r.status, headers: { "content-type": r.headers.get("content-type") || "" } }));
    return window.__answers[i];
  };
})();
"""

BLANK = b"<!doctype html><html><head><meta charset='utf-8'></head><body></body></html>"

CLICKABLE = "button, [role=button], input[type=button], input[type=submit]"
TEXT_FIELDS = "textarea, input:not([type]), input[type=text]"


# ---- the unit: tools and action routes, as tests/test_keys.py calls them


def http_request(base, method, path, body=None, headers=None):
    host, port = base.rsplit(":", 1)
    conn = http.client.HTTPConnection(host, int(port), timeout=CALL_TIMEOUT_S)
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
    except ValueError:
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
    """project initiated, created and holding I and B (B deriving from I); returns (request id, I, B)."""
    rid = request_id(call(base, "post_request",
                          {"type": "initiate", "project": project, "title": f"{project} project", "actor": ACTOR}))
    call(base, "create_project", {"project": project, "in_response_to": rid, "actor": ACTOR})
    ids = []
    for layer in ("I", "B"):
        entry = {"layer": layer, "title": f"{project} {layer}", "body": f"the {layer} entry of {project}",
                 "derives_from": ids[-1:]}
        ids.append(created_id(call(base, "submit_amendment", {
            "project": project, "in_response_to": rid, "actor": ACTOR, "entries": [entry]})))
    return rid, ids[0], ids[1]


def seed_p(base, routes):
    """p: I, B, A, S, a module implementing S, a ratified proposal, its cut, repository a/b and a second,
    pending proposal."""
    rid, _, b_id = seed_entries(base, "p")
    parent = b_id
    for layer in ("A", "S"):
        entry = {"layer": layer, "title": f"p {layer}", "body": f"the {layer} entry of p", "derives_from": [parent]}
        parent = created_id(call(base, "submit_amendment", {
            "project": "p", "in_response_to": rid, "actor": ACTOR, "entries": [entry]}))
    s_id = parent
    call(base, "declare_module", {"project": "p", "path": "src/core.py", "implements": [s_id], "actor": ACTOR})
    call(base, "submit_proposal", {"project": "p", "proposal": {"core": [s_id]}, "note": "first", "actor": ACTOR})
    act(base, routes["ratify"]["p"], {})
    act(base, routes["cut_units"]["p"], {})
    act(base, routes["set_repo"]["p"], {"repo": "a/b"})
    call(base, "submit_proposal", {"project": "p", "proposal": {"main": [s_id]}, "note": "second", "actor": ACTOR})


def unit_keys(value):
    """Every value under a "key" field of a get_units answer."""
    if isinstance(value, dict):
        found = [value["key"]] if isinstance(value.get("key"), str) else []
        return found + [k for v in value.values() for k in unit_keys(v)]
    if isinstance(value, list):
        return [k for v in value for k in unit_keys(v)]
    return []


def leaves(value):
    """Every string in a JSON value."""
    if isinstance(value, str):
        return [value]
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return [s for child in children for s in leaves(child)]


# ---- the dashboard modules, read through bun


def bun_value(module, expr):
    script = f"const m = await import({json.dumps(module.as_uri())}); console.log(JSON.stringify({expr}));"
    done = subprocess.run(["bun", "-e", script], check=False, cwd=DASHBOARD, capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def action_routes(projects):
    """{action: {project: relative path}} from dashboard/src/actions.ts ACTION_ROUTES."""
    expr = ("Object.fromEntries(Object.entries(m.ACTION_ROUTES).map(([a, r]) => "
            f"[a, Object.fromEntries({json.dumps(list(projects))}.map(p => [p, r.path(p)]))]))")
    return bun_value(SRC / "actions.ts", expr)


def build_harness(where):
    entry = where / "harness.ts"
    entry.write_text(HARNESS % {"views": json.dumps((SRC / "views.ts").as_posix()),
                                "apply": json.dumps((SRC / "apply.ts").as_posix())}, encoding="utf-8")
    out = where / "out"
    done = subprocess.run(["bun", "build", str(entry), "--outdir", str(out), "--target", "browser"],
                          check=False, cwd=DASHBOARD, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    return (out / "harness.js").read_bytes()


def screen(base, project, view):
    """The shown screen of one whole screen read."""
    path = bun_value(SRC / "net.ts", "m.SCREEN_PATH")
    body = {"project": project, "fallback": False, "view": view, "filter": None, "part": "whole",
            "held": None, "list_token": None}
    status, raw, _ = http_request(base, "POST", "/" + path, json.dumps(body).encode("utf-8"))
    j = decode(raw)
    assert status == 200 and isinstance(j, dict) and j.get("status") == "parts", (project, view, status, raw)
    assert "head" in j and "rest" in j, (project, view, raw)
    return {"status": "shown", "token": j["token"], "parts": "whole", "head": j["head"], "rest": j["rest"]}


def serve(harness, units):
    """The blank page, the harness, and a proxy forwarding any other request to the unit its header names."""

    class Handler(BaseHTTPRequestHandler):
        def answer(self, status, body, kind):
            self.send_response(status)
            self.send_header("Content-Type", kind or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def forward(self):
            unit = self.headers.get(UNIT_HEADER)
            if unit not in units:
                self.answer(404, b"", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            kept = {k: v for k, v in self.headers.items() if k.lower() == "content-type"}
            status, raw, kind = http_request(units[unit], self.command, self.path, body, kept)
            self.answer(status, raw, kind)

        def do_GET(self):
            if self.path == "/harness.js":
                self.answer(200, harness, "text/javascript")
            elif self.path == "/" and UNIT_HEADER not in self.headers:
                self.answer(200, BLANK, "text/html")
            else:
                self.forward()

        def do_POST(self):
            self.forward()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_press_points_and_token_check(tmp_path):
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
    harness = build_harness(tmp_path)
    routes = action_routes(["p", "e"])
    cmd = UnitCommand("rebuilt", str(REPO))

    standin = StandIn()
    started = []
    server = None
    try:
        standin.reset(Script(issues=(), links=(), next_number=1, next_id=1, user={"login": "x"}, push=True,
                             refuse={}, unanswered=frozenset(), hold=None))
        restore(str(root), str(store), str(prompts))
        passing = launch(cmd, str(root), dict(os.environ), standin, token=True)
        started.append(passing)
        assert passing.port is not None, (passing.outcome, passing.stderr)
        base = f"{REACH_HOST}:{passing.port}"
        seed_p(base, routes)
        seed_entries(base, "e")
        keys = unit_keys(call(base, "get_units", {"project": "p"}))
        assert keys, "get_units names no cut unit of p"
        shown = {("p", "queues"): screen(base, "p", "queues"), ("p", "ship"): screen(base, "p", "ship"),
                 ("e", "ship"): screen(base, "e", "ship")}
        refused = launch(cmd, str(root), dict(os.environ), standin, token=False)
        started.append(refused)
        assert refused.port is not None, (refused.outcome, refused.stderr)

        server = serve(harness, {"passing": base, "refused": f"{REACH_HOST}:{refused.port}"})
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                pg = browser.new_page(viewport=VIEWPORT)
                pg.add_init_script(FETCH_STUB)
                pg.goto(f"http://127.0.0.1:{server.server_address[1]}/")
                pg.add_script_tag(url="/harness.js", type="module")
                pg.wait_for_function("() => typeof window.__draw === 'function'")

                def draw(project, view):
                    pg.evaluate("([c, f]) => window.__draw({...window.__opened(c), ...f}, window.__initialLocal({}), {})",
                                [{"project": project, "view": view, "filter": None},
                                 {"list": LIST, "screen": shown[(project, view)]}])

                def presses():
                    return pg.evaluate("() => window.__presses")

                def clear():
                    pg.evaluate("() => { window.__presses = []; }")

                def clickables():
                    return pg.query_selector_all(CLICKABLE)

                def label(e):
                    return e.evaluate("e => e.innerText || e.value || ''")

                def by_text(pattern):
                    found = [e for e in clickables() if re.search(pattern, label(e), re.IGNORECASE)]
                    assert found, (pattern, pg.evaluate("() => document.body.innerText"))
                    return found[0]

                def text():
                    return pg.evaluate("() => document.body.innerText")

                def fetches():
                    return pg.evaluate("() => window.__fetches")

                # 1. p queues: 'Reject with note' presses nothing and opens the note field
                draw("p", "queues")
                fields = len(pg.query_selector_all(TEXT_FIELDS))
                by_text(r"reject with note").click()
                assert presses() == []
                opened_fields = pg.query_selector_all(TEXT_FIELDS)
                assert len(opened_fields) > fields, text()
                opened_fields[-1].fill("n1")
                by_text(r"confirm rejection").click()
                p = presses()
                assert len(p) == 1, p
                assert p[0][:2] == ["reject", "queues"] and p[0][2]["kind"] == "proposal", p
                assert "n1" in json.dumps(p[0][3]), p
                pg.evaluate("() => window.__redraw()")
                by_text(r"ratify").click()
                p = presses()
                assert len(p) == 2 and p[1][0] == "ratify", p

                # 2. e ship: the empty-state note's 'Cut work units'
                clear()
                draw("e", "ship")
                by_text(r"cut work units").click()
                p = presses()
                assert len(p) == 1, p
                assert p[0][:2] == ["cut_units", "ship"] and p[0][2]["kind"] == "units", p

                # 3. p ship: the delegation toggle
                clear()
                draw("p", "ship")
                boxes = pg.query_selector_all("input[type=checkbox]")
                near = [b for b in boxes if b.evaluate(
                    "b => { for (let p = b, i = 0; p && i < 4; p = p.parentElement, i++)"
                    " if (/delegat/i.test(p.innerText)) return true; return false; }")]
                assert near, text()
                near[0].click()
                p = presses()
                assert len(p) == 1, p
                assert p[0][:2] == ["set_delegation", "ship"] and p[0][2]["kind"] == "value", p

                # 4. p ship: the check control is the one control whose press sends a request
                draw("p", "ship")
                idle = [label(e) for e in clickables()]
                idle_count = len(idle)
                check_at = None
                for i in range(idle_count):
                    draw("p", "ship")
                    clear()
                    clickables()[i].evaluate("e => e.click()")
                    if fetches():
                        check_at = i
                        break
                assert check_at is not None, "no control of the Ship screen sends the token check"
                want = pg.evaluate("() => window.__checkRequest('p')")
                f = fetches()
                assert len(f) == 1, f
                assert (f[0]["method"], f[0]["path"]) == (want["method"], want["path"]), (f, want)
                assert presses() == []
                assert pg.evaluate("() => window.__heldCheck().status") == "checking"
                pg.evaluate("() => window.__redraw()")
                check = clickables()[check_at]
                assert check.evaluate("e => e.disabled") is True
                assert "checking" in label(check), label(check)
                check.evaluate("e => e.click()")
                assert len(fetches()) == 1, fetches()

                # 5. the token-False unit's answer: its refusal text, no confirmation, no press
                answer = pg.evaluate("() => window.__release(0, 'refused')")
                pg.wait_for_function("() => window.__heldCheck().status === 'answered'")
                reasons = [s for s in leaves(decode(answer["text"].encode())) if len(s) >= 8]
                assert any(s in text() for s in reasons), (answer, text())
                assert len(clickables()) == idle_count, text()
                assert presses() == []

                # 6. a second check, answered by the stand-in's unit: the confirmation and its Ship press
                clickables()[check_at].evaluate("e => e.click()")
                assert len(fetches()) == 2, fetches()
                pg.evaluate("() => window.__release(1, 'passing')")
                pg.wait_for_function("() => window.__heldCheck().n === 2 && window.__heldCheck().status === 'answered'")
                assert "Checked just now" in text(), text()
                after = clickables()
                assert len(after) > idle_count, text()
                assert presses() == []
                seen = {}
                new = []
                for e in after:
                    t = label(e)
                    seen[t] = seen.get(t, 0) + 1
                    if seen[t] > idle.count(t):
                        new.append(e)
                ships = [e for e in new if re.search(r"ship", label(e), re.IGNORECASE)] or new
                assert ships, text()
                ships[0].click()
                p = presses()
                assert len(p) == 1, p
                assert p[0][:2] == ["emit_tickets", "ship"] and p[0][2]["kind"] == "units", p
                assert set(keys) <= set(p[0][2]["keys"]), (keys, p)

                # 7. every request went to a relative path, none to a person-only action's
                action_paths = {path for by_project in routes.values() for path in by_project.values()}
                for f in fetches():
                    path = f["path"]
                    assert path and not path.startswith(("/", "\\")) and not re.match(r"^[a-zA-Z][\w+.-]*:", path), f
                    assert path not in action_paths, f
            finally:
                browser.close()
    finally:
        if server is not None:
            server.shutdown()
        for s in started:
            stop(s)
        standin.close()
