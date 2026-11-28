"""dashboard/src/page.ts: start's wiring in headless Chromium, with a fake WireActions (T-176, T-493, S-511).

A harness entry, written and built with bun into the test's scratch folder, replaces window.fetch with a
stub that records each call and holds it until the test answers it, then calls start once with a fake
WireActions recording every port call it receives with the shown project and the fetch count at the
call. It is served with a blank page by http.server, and with a frame page holding one iframe of it;
each browser context is fresh, so localStorage is empty, and a headless page is visible. Modules under
test are imported inside the test, so a module not yet written fails only the tests that need it.
"""

import json
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DASHBOARD = REPO / "dashboard"
SRC = DASHBOARD / "src"
WAIT_MS = 5000
BUN = shutil.which("bun") or "bun"

# The harness: the fetch stub first, then start(root, fakeWire). fakeWire counts its calls in
# window.__wired, keeps deps in window.__deps, and records every port call in window.__calls.
HARNESS = """\
import { start } from %(page)s;

const w = window as any;
w.__fetches = [];
w.__pending = [];
w.__calls = [];
w.__wired = 0;
w.fetch = (input: any, init: any) => {
  const path = typeof input === "string" ? input : input.url;
  w.__fetches.push({ path, body: JSON.parse(init.body) });
  return new Promise((resolve) => w.__pending.push(resolve));
};
w.__answer = (i: number, status: number, body: unknown) => {
  w.__pending[i](new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }));
};

const DEFAULT = { available: true, live: null, earlier: [], settled: [], value: undefined };

function fakeWire(deps: any) {
  w.__wired += 1;
  w.__deps = deps;
  const rec = (member: string) => (...args: unknown[]) => {
    w.__calls.push({ member, args, shown: deps.shown(), fetches: w.__fetches.length });
  };
  return {
    screenSent: rec("screenSent"),
    screenAnswered: rec("screenAnswered"),
    switched: rec("switched"),
    press: rec("press"),
    control: () => DEFAULT,
  };
}

start(document.getElementById("root") as Element, fakeWire as any);
"""

BLANK = (b"<!doctype html><html><head><meta charset='utf-8'></head><body><div id='root'></div>"
         b"<script type='module' src='/harness.js'></script></body></html>")
FRAME = (b"<!doctype html><html><head><meta charset='utf-8'></head><body>"
         b"<iframe src='/'></iframe></body></html>")

# Installed before any page script, in every frame: one observer on the document records, in order,
# 'mark' for each class record on the root element whose class list holds the dark mark, and 'draw'
# for the first childList record on #root.
ORDER = """\
(() => {
  const mark = %(mark)s;
  window.__order = [];
  let drawn = false;
  new MutationObserver((records) => {
    for (const r of records) {
      if (r.type === "attributes" && r.target === document.documentElement
          && document.documentElement.classList.contains(mark)) {
        window.__order.push("mark");
      } else if (r.type === "childList" && !drawn && r.target.id === "root") {
        drawn = true;
        window.__order.push("draw");
      }
    }
  }).observe(document, { subtree: true, childList: true, attributes: true, attributeFilter: ["class"] });
})();
"""


def read_dark_mark():
    script = (f"const r = await import({json.dumps((SRC / 'render.ts').as_uri())});"
              "console.log(JSON.stringify(r.DARK_MARK));")
    done = subprocess.run([BUN, "-e", script], check=False, cwd=DASHBOARD, capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    mark = json.loads(done.stdout.strip().splitlines()[-1])
    assert isinstance(mark, str) and mark, mark
    return mark


def build_harness(where):
    entry = where / "harness.ts"
    entry.write_text(HARNESS % {"page": json.dumps((SRC / "page.ts").as_posix())}, encoding="utf-8")
    out = where / "out"
    done = subprocess.run([BUN, "build", str(entry), "--outdir", str(out), "--target", "browser"],
                          check=False, cwd=DASHBOARD, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    return (out / "harness.js").read_bytes()


def serve(harness):
    """The blank page, the frame page and the harness; nothing else."""

    class Handler(BaseHTTPRequestHandler):
        def answer(self, status, body, kind):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/harness.js":
                self.answer(200, harness, "text/javascript")
            elif self.path == "/":
                self.answer(200, BLANK, "text/html")
            elif self.path == "/frame":
                self.answer(200, FRAME, "text/html")
            else:
                self.answer(404, b"", "text/plain")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_start_with_fake_wiring(tmp_path):
    from playwright.sync_api import sync_playwright

    harness = build_harness(tmp_path)
    server = serve(harness)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                pg = browser.new_context().new_page()
                pg.goto(f"http://127.0.0.1:{server.server_address[1]}/")
                pg.wait_for_function("() => window.__wired >= 1 && window.__fetches.length >= 1", timeout=WAIT_MS)

                def fetches():
                    return pg.evaluate("() => window.__fetches")

                def calls(member):
                    return [c for c in pg.evaluate("() => window.__calls") if c["member"] == member]

                def wait_calls(member, n):
                    pg.wait_for_function("([m, n]) => window.__calls.filter(c => c.member === m).length >= n",
                                         arg=[member, n], timeout=WAIT_MS)
                    return calls(member)

                # 1. one wiring, one page-open read with no project, and no screenSent for it
                time.sleep(0.5)
                assert pg.evaluate("() => window.__wired") == 1
                f = fetches()
                assert len(f) == 1, f
                assert f[0]["path"] == "dash/screen", f
                assert f[0]["body"]["project"] is None and f[0]["body"]["fallback"] is True, f
                assert calls("screenSent") == [], calls("screenSent")

                # 2. the open read's answer reaches screenAnswered after apply: the choice already names p
                pg.evaluate("(b) => window.__answer(0, 200, b)", {
                    "status": "parts", "project": "p", "token": "t1", "complete": True,
                    "head": {"fields": {}, "items": []}, "rest": {"fields": {}, "items": []},
                    "list_token": "L", "names": ["p", "q"], "run": None})
                answered = wait_calls("screenAnswered", 1)
                assert len(answered) == 1, answered
                c = answered[0]
                assert c["args"][0] == 1, c
                assert c["args"][1]["cls"] == "parts" and c["args"][1]["project"] == "p", c
                assert c["shown"] == "p", c

                # 3. a confirmation for a project not shown sends no read
                pg.evaluate("() => window.__deps.deliver({ type: 'action_confirmed', project: 'q' })")
                time.sleep(0.5)
                assert len(fetches()) == 1, fetches()
                assert calls("screenSent") == [], calls("screenSent")

                # 4. a confirmation for the shown project: one after-action read, told to the port first
                pg.evaluate("() => window.__deps.deliver({ type: 'action_confirmed', project: 'p' })")
                pg.wait_for_function("() => window.__fetches.length >= 2", timeout=WAIT_MS)
                f = fetches()
                assert len(f) == 2, f
                assert f[1]["path"] == "dash/screen", f
                body = f[1]["body"]
                assert body["project"] == "p" and body["part"] == "whole", body
                assert body["held"] == {"token": "t1", "parts": "whole"}, body
                sent = calls("screenSent")
                assert len(sent) == 1, sent
                s = sent[0]
                assert s["fetches"] == 1, s
                assert s["args"][0] > 1, s
                assert s["args"][1] == "p", s
                assert s["args"][2]["head"] == {}, s

                # 5. an unchanged answer reaches screenAnswered with cls unchanged
                pg.evaluate("(b) => window.__answer(1, 200, b)",
                            {"status": "unchanged", "token": "t1", "list_token": "L", "run": None})
                answered = wait_calls("screenAnswered", 2)
                assert len(answered) == 2, answered
                assert answered[1]["args"][0] == s["args"][0], answered
                assert answered[1]["args"][1]["cls"] == "unchanged", answered

                # 6. the wiring is built once for the page's life
                time.sleep(3.0)
                assert pg.evaluate("() => window.__wired") == 1
            finally:
                browser.close()
    finally:
        server.shutdown()


def test_start_marks_side_once(tmp_path):
    from playwright.sync_api import sync_playwright

    mark = read_dark_mark()
    harness = build_harness(tmp_path)
    server = serve(harness)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    marked = "(m) => document.documentElement.classList.contains(m)"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                def open_page(scheme, path):
                    ctx = browser.new_context(color_scheme=scheme)
                    ctx.add_init_script(ORDER % {"mark": json.dumps(mark)})
                    pg = ctx.new_page()
                    pg.goto(base + path)
                    return pg

                # 1. top-level, dark: marked, and marked before the first draw
                pg = open_page("dark", "/")
                pg.wait_for_function("() => window.__wired >= 1", timeout=WAIT_MS)
                time.sleep(2.0)
                assert pg.evaluate(marked, mark) is True
                order = pg.evaluate("() => window.__order")
                assert "mark" in order and "draw" in order, order
                assert order.index("mark") < order.index("draw"), order

                # 2. the preference turns light: the mark stays
                pg.emulate_media(color_scheme="light")
                time.sleep(2.0)
                assert pg.evaluate(marked, mark) is True

                # 3. top-level, light: never marked, not even when the preference turns dark
                pg = open_page("light", "/")
                pg.wait_for_function("() => window.__wired >= 1", timeout=WAIT_MS)
                time.sleep(2.0)
                assert pg.evaluate(marked, mark) is False
                order = pg.evaluate("() => window.__order")
                assert "mark" not in order, order
                pg.emulate_media(color_scheme="dark")
                time.sleep(2.0)
                assert pg.evaluate(marked, mark) is False

                # 4. framed, dark: the preference took inside the frame, and the frame stays light
                pg = open_page("dark", "/frame")
                pg.wait_for_function("() => document.querySelector('iframe') !== null", timeout=WAIT_MS)
                frame = pg.wait_for_selector("iframe", timeout=WAIT_MS).content_frame()
                assert frame is not None
                frame.wait_for_function("() => window.__wired >= 1", timeout=WAIT_MS)
                assert frame.evaluate("() => matchMedia('(prefers-color-scheme: dark)').matches") is True
                time.sleep(2.0)
                assert frame.evaluate(marked, mark) is False
            finally:
                browser.close()
    finally:
        server.shutdown()
