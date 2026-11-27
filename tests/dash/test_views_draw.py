"""dashboard/src/views.ts: draw's states and controls in headless Chromium (T-216, S-254).

The rebuilt unit (this repository) is started through the Ship check's launch on a restored copy and
seeded through its own tools and action routes: project "p" with a pending slicing proposal, one cut
and repository "a/b". One whole screen read of p per view gives the shown screens. A harness entry,
written and built with bun into the test's scratch folder, draws a page state into a blank page served
by http.server, recording every control it is asked for and every press. Modules under test are
imported inside the test, so a module not yet written fails only the tests that need it.
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
LIST = {"names": ["p"], "token": "L", "seq": 1}

# The harness: draw a page state built from opened(choice) and the given fields, recording each control
# asked for in window.__asked (cleared before each draw) and each press in window.__presses.
HARNESS = """\
import { draw, initialLocal, VIEW_NAMES } from %(views)s;
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
    press: (...args: unknown[]) => { w.__presses.push(args); },
    control: (action: string) => { w.__asked.push(action); return controls[action] ?? DEFAULT; },
    changed: () => { if (last) show(...last); },
  };
  document.body.replaceChildren(draw(page, local, "12:00", h));
}

w.__presses = [];
w.__asked = [];
w.__draw = show;
w.__opened = opened;
w.__initialLocal = initialLocal;
w.__views = [...VIEW_NAMES];
"""

BLANK = b"<!doctype html><html><head><meta charset='utf-8'></head><body></body></html>"

# The view list: the deepest element whose children hold, in order, one item per view whose text
# begins with that view's name.
VIEW_ITEMS = """() => {
  const names = window.__views;
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

CLICKABLE = "button, [role=button], input[type=button], input[type=submit]"


# ---- the unit: tools and action routes, as tests/test_keys.py calls them


def http_request(base, method, path, body=None):
    host, port = base.rsplit(":", 1)
    conn = http.client.HTTPConnection(host, int(port), timeout=CALL_TIMEOUT_S)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        answer = conn.getresponse()
        return answer.status, answer.read()
    finally:
        conn.close()


def decode(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return None


def tool_route(base, tool):
    """(method, path) for one tool, as the unit's tool discovery names it."""
    status, raw = http_request(base, "GET", "/tools")
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
    status, raw = http_request(base, method, route, json.dumps(args).encode("utf-8"))
    assert 200 <= status < 300, f"{tool} {args!r} answered {status}: {raw!r}"
    return decode(raw)


def act(base, path, body):
    """POST one person-only action to its relative route; every seeding press must answer 2xx."""
    status, raw = http_request(base, "POST", "/" + path, json.dumps(body).encode("utf-8"))
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


def screens(base, project, views):
    """Each view's shown screen for project, from one whole screen read per view."""
    path = bun_value(SRC / "net.ts", "m.SCREEN_PATH")
    shown = {}
    for view in views:
        body = {"project": project, "fallback": False, "view": view, "filter": None, "part": "whole",
                "held": None, "list_token": None}
        status, raw = http_request(base, "POST", "/" + path, json.dumps(body).encode("utf-8"))
        j = decode(raw)
        assert status == 200 and isinstance(j, dict) and j.get("status") == "parts", (view, status, raw)
        assert "head" in j and "rest" in j, (view, raw)
        shown[view] = {"status": "shown", "token": j["token"], "parts": "whole", "head": j["head"], "rest": j["rest"]}
    return shown


def serve(harness):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body, kind = (harness, "text/javascript") if self.path == "/harness.js" else (BLANK, "text/html")
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def first_entry_id(value):
    """The first entry id found in a read's JSON, depth first."""
    if isinstance(value, str) and re.fullmatch(r"[IBAST]-\d+.*", value):
        return value
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    for child in children:
        found = first_entry_id(child)
        if found is not None:
            return found
    return None


def test_draw_states(tmp_path):
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
    routes = action_routes(["p"])

    standin = StandIn()
    started = []
    server = None
    try:
        standin.reset(Script(issues=(), links=(), next_number=1, next_id=1, user={"login": "x"}, push=True,
                             refuse={}, unanswered=frozenset(), hold=None))
        restore(str(root), str(store), str(prompts))
        it = launch(UnitCommand("rebuilt", str(REPO)), str(root), dict(os.environ), standin, token=True)
        started.append(it)
        assert it.port is not None, (it.outcome, it.stderr)
        base = f"{REACH_HOST}:{it.port}"
        seed_p(base, routes)
        views = bun_value(SRC / "views.ts", "m.VIEW_NAMES")
        shown = screens(base, "p", views)
        spine_id = first_entry_id(shown["spine"]["head"])
        assert spine_id is not None, shown["spine"]["head"]

        server = serve(harness)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                pg = browser.new_page(viewport=VIEWPORT)
                pg.goto(f"http://127.0.0.1:{server.server_address[1]}/")
                pg.add_script_tag(url="/harness.js", type="module")
                pg.wait_for_function("() => typeof window.__draw === 'function'")

                def draw(choice, fields, controls=None):
                    pg.evaluate("([c, f, k]) => window.__draw({...window.__opened(c), ...f}, "
                                "window.__initialLocal({}), k)", [choice, fields, controls or {}])
                    # 12. no element moves the frame, and no draw presses anything
                    moving = pg.evaluate("() => [...document.querySelectorAll('*')].filter(e => "
                                         "e.hasAttribute('href') || e.hasAttribute('target') || e.closest('form'))"
                                         ".map(e => e.outerHTML.slice(0, 80))")
                    assert moving == [], moving
                    assert pg.evaluate("() => window.__presses") == []
                    return pg.evaluate("() => document.body.innerText")

                def asked():
                    return set(pg.evaluate("() => window.__asked"))

                def choice(view, project="p"):
                    return {"project": project, "view": view, "filter": None}

                run1 = {"id": "n:1", "step": "creating", "percent": 40, "total": 4, "processed": 1, "created": 1,
                        "skipped": 0, "failed": 0, "time_left": 9, "started_at": 100}
                prog = {"seq": 3, "state": "running", "run": {**run1, "percent": 60}, "elapsed": 12, "current": True}

                # 2. stored text is drawn as text
                text = draw({"project": "<b>x</b>", "view": "spine", "filter": None},
                            {"list": {"names": ["<b>x</b>"], "token": "L", "seq": 1}, "screen": {"status": "loading"}})
                assert "<b>x</b>" in text
                assert pg.evaluate("() => document.querySelectorAll('b, script, img, a, form').length") == 0

                # 3. the view list's Ship item shows the run while the screen loads
                draw(choice("spine"), {"list": LIST, "run": run1, "screen": {"status": "loading"}})
                items = pg.evaluate(f"() => ({VIEW_ITEMS})().map(e => e.innerText)")
                assert items is not None, pg.evaluate("() => document.body.innerHTML")
                ship_item = items[views.index("ship")]
                assert "creating" in ship_item and "40" in ship_item, ship_item

                # 4. a progress not current is marked so on the Ship view
                text = draw(choice("ship"), {"list": LIST, "screen": shown["ship"], "run": run1,
                                             "progress": {**prog, "current": False}})
                assert "not current" in text
                text = draw(choice("ship"), {"list": LIST, "screen": shown["ship"], "run": run1, "progress": prog})
                assert "not current" not in text

                # 5. a failure's own reason, as text
                text = draw(choice("spine"), {"list": LIST, "screen": {"status": "failed", "reason": "R-zz9 <i>"}})
                assert "R-zz9 <i>" in text
                assert pg.evaluate("() => document.querySelectorAll('i').length") == 0

                # 6. the empty and preparing states: never the failure form, never another choice's data
                def lines(t):
                    return {line.strip() for line in t.splitlines() if line.strip()}

                none = {"project": None, "view": "spine", "filter": None}
                empty_list = {"names": [], "token": "L0", "seq": 1}
                failure = (lines(draw(none, {"list": empty_list, "screen": {"status": "failed", "reason": "R-zz9 <i>"}}))
                           - lines(draw(none, {"list": empty_list, "screen": {"status": "loading"}})))
                text = draw(none, {"list": empty_list, "screen": {"status": "empty"}})
                assert not lines(text) & failure, (text, failure)
                assert "R-zz9" not in text
                assert re.search(r"no project", text, re.IGNORECASE), text
                failure = (lines(draw(choice("spine"), {"list": LIST, "screen": {"status": "failed", "reason": "R-zz9 <i>"}}))
                           - lines(draw(choice("spine"), {"list": LIST, "screen": {"status": "loading"}})))
                text = draw(choice("spine"), {"list": LIST, "screen": {"status": "preparing"}})
                assert not lines(text) & failure, (text, failure)
                assert "R-zz9" not in text
                assert spine_id not in text, (spine_id, text)

                # 7. the selected view, from the choice alone
                selected = (f"() => ({VIEW_ITEMS})().map(e => e.classList.contains('selected') "
                            "|| !!e.querySelector('.selected'))")
                for view in ("queues", "waves"):
                    draw(choice(view), {"list": LIST, "screen": shown[view]})
                    marks = pg.evaluate(selected)
                    assert marks == [v == view for v in views], (view, marks)

                # 8. each view asks for exactly the controls it draws
                expected = {"queues": {"ratify", "reject"}, "order": {"cut_units"},
                            "ship": {"cut_units", "set_repo", "set_labels", "set_delegation", "emit_tickets"},
                            "spine": set(), "waves": set(), "modules": set(), "read": set()}
                for view, names in expected.items():
                    draw(choice(view), {"list": LIST, "screen": shown[view]})
                    assert asked() == names, (view, asked())
                draw(choice("ship"), {"list": LIST, "screen": {"status": "loading"}})
                assert asked() == set(), asked()

                # 9. the cut, unavailable and waiting, on Order and on Ship
                waiting = {"cut_units": {"available": False, "live": {"status": "waiting", "reason": None},
                                         "earlier": [], "settled": []}}
                for view in ("order", "ship"):
                    draw(choice(view), {"list": LIST, "screen": shown[view]}, waiting)
                    cuts = pg.evaluate("""(sel) => [...document.querySelectorAll(sel)]
                        .filter(b => /cut/i.test(b.innerText || b.value || ''))
                        .map(b => {
                          let near = false;
                          for (let p = b, i = 0; p && i < 4; p = p.parentElement, i++)
                            near = near || p.innerText.includes('waiting');
                          return { disabled: b.disabled === true, near };
                        })""", CLICKABLE)
                    assert cuts, (view, pg.evaluate("() => document.body.innerText"))
                    assert all(c["disabled"] and c["near"] for c in cuts), (view, cuts)

                # 10. a failed live record's reason, as text
                failed = {"ratify": {"available": True, "live": {"status": "failed", "reason": "<i>no</i>"},
                                     "earlier": [], "settled": []}}
                text = draw(choice("queues"), {"list": LIST, "screen": shown["queues"]}, failed)
                assert "<i>no</i>" in text
                assert pg.evaluate("() => document.querySelectorAll('i').length") == 0

                # 11. the set_repo control's value in place of the stored repository
                held = {"set_repo": {"available": True, "live": None, "earlier": [], "settled": [], "value": "o/n"}}
                text = draw(choice("ship"), {"list": LIST, "screen": shown["ship"]}, held)
                assert "o/n" in text and "a/b" not in text, text
                text = draw(choice("ship"), {"list": LIST, "screen": shown["ship"]},
                            {"set_repo": {"available": True, "live": None, "earlier": [], "settled": []}})
                assert "a/b" in text, text

                # 12. nothing above pressed anything
                assert pg.evaluate("() => window.__presses") == []
            finally:
                browser.close()
    finally:
        if server is not None:
            server.shutdown()
        for s in started:
            stop(s)
        standin.close()
