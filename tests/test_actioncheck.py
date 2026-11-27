"""The differential action check, actioncheck: its case tables, its empty-log judgement and the check
itself over both real units against the Ship check's stand-in (T-82, T-83, T-108).

The log test drives run over two fake units, each an install root holding a fake mu_spec package
that the Ship check's launch starts like a real one, so every request either fake sends to GitHub
lands in the stand-in's log. The end-to-end test runs the running unit, the install at
MU_SPEC_REFERENCE_INSTALL, against the rebuilt unit, this repository. Modules under test are
imported inside the test, so a module not yet written fails only the tests that need it.
"""

import json
import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_VAR = "MU_SPEC_REFERENCE_INSTALL"
VERDICT_TERMS = ("status_match", "body_match", "record_match", "log_empty")
CASE_COUNTS = (4, 8, 3, 5, 9, 3, 7)  # per CASES key, in CASES order
PAIR_CASES = ("build_mismatch+dead_end", "build_mismatch+unit_cycle", "dead_end+unit_cycle")

# A fake unit: answers /health, every other GET (the project read) with one fixed body after sending
# GET /user to GitHub, and every POST (an action) with one fixed body; when SEND_POST is set, a POST
# on the set_repo route with the 'ok' case's body first sends POST /repos/o/r/issues to GitHub.
_FAKE_UNIT = """\
import json, os, re, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GITHUB = 'https://' + {host!r}
SEND_POST = {send_post!r}
SET_REPO = re.compile({route!r})
OK_BODY = json.loads({ok_body!r})


def github(method, path, data=None):
    request = urllib.request.Request(GITHUB + path, data=data, method=method,
                                     headers={{'Content-Type': 'application/json'}})
    try:
        urllib.request.urlopen(request, timeout=10).read()
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    def _answer(self, status, doc):
        body = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split('?')[0] == '/health':
            self._answer(200, {{'ok': True}})
            return
        github('GET', '/user')
        self._answer(200, {{'project': 'p', 'repo': None}})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if SEND_POST and SET_REPO.fullmatch(self.path.split('?')[0]) and body == OK_BODY:
            github('POST', '/repos/o/r/issues', b'{{"title": "x"}}')
        self._answer(200, {{'ok': True}})

    def log_message(self, *args):
        pass


ThreadingHTTPServer(('127.0.0.1', int(os.environ['MU_SPEC_PORT'])), Handler).serve_forever()
"""


def reference_install() -> str:
    root = os.environ.get(REFERENCE_VAR)
    if not root:
        pytest.fail(f"{REFERENCE_VAR} is not set: it must name the running unit's install root")
    return root


def scratch(tmp_path):
    """A check root and pristine store and prompts copies, as the Ship check's own tests lay them out."""
    store = tmp_path / "pristine_store"
    (store / "projects").mkdir(parents=True)
    prompts = tmp_path / "pristine_prompts"
    prompts.mkdir()
    (prompts / "default.md").write_bytes(b"d")
    root = tmp_path / "check"
    root.mkdir()
    return str(root), str(store), str(prompts)


def fake_install(where: Path, send_post: bool, route: str, ok_body: str) -> str:
    """An install root whose mu_spec package is the fake unit."""
    from shipcheck.launch import GITHUB_HOST

    package = where / "mu_spec"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    source = _FAKE_UNIT.format(host=GITHUB_HOST, send_post=send_post, route=route, ok_body=ok_body)
    (package / "__main__.py").write_text(source, encoding="utf-8")
    return str(where)


def false_terms(v) -> list[str]:
    return [term for term in VERDICT_TERMS if getattr(v, term) is not True]


def test_case_tables():
    from actioncheck import (
        CASES,
        EMPTY_LOG_ACTIONS,
        PRE_RUN_PAIRS,
        SHIP_CHECK_CASES,
        ActionCase,
        cases,
    )
    from shipcheck.judge import SCENARIOS

    from mu_spec.declarations import ACTIONS

    names = {spec.name for spec in ACTIONS}
    assert len(names) == 8
    assert set(CASES) | set(SHIP_CHECK_CASES) == names
    assert set(CASES) & set(SHIP_CHECK_CASES) == {"emit_tickets"}
    assert "rollback_tickets" not in CASES

    for action, scenarios in SHIP_CHECK_CASES.items():
        for name in scenarios:
            assert name in SCENARIOS, (action, name)

    cs = cases()
    # a cases() also expanding SHIP_CHECK_CASES gives 50, rollback_tickets ones among them
    assert len(cs) == 39
    assert [len(CASES[a]) for a in CASES] == list(CASE_COUNTS)
    # a cases() iterating CASES keys sorted puts emit_tickets second, not ratify
    assert [(c.action, c.case) for c in cs] == [(a, k) for a in CASES for k in CASES[a]]
    assert not [c for c in cs if c.action == "rollback_tickets"]

    for key in CASES:
        assert len(CASES[key]) >= 3, key

    joined = {"+".join(pair) for pair in PRE_RUN_PAIRS}
    for name in PAIR_CASES:
        assert name in CASES["emit_tickets"], name
        assert name in joined, name

    assert set(EMPTY_LOG_ACTIONS) == set(CASES) - {"emit_tickets"}

    for c in cs:
        assert isinstance(c, ActionCase)
        assert isinstance(c.store_prep, str) and c.store_prep, (c.action, c.case)


def test_log_empty_judging(tmp_path, monkeypatch):
    import actioncheck
    from shipcheck import judge
    from startcheck.runner import UnitCommand

    from mu_spec.declarations import ACTIONS

    # the bodies are masked by the Ship check's own mask, not a copy of it
    assert actioncheck.mask is judge.mask

    set_repo = tuple(c for c in actioncheck.cases() if c.action == "set_repo")
    (ok,) = [c for c in set_repo if c.case == "ok"]
    (spec,) = [s for s in ACTIONS if s.name == "set_repo"]
    route = re.escape(spec.path).replace(re.escape("{project}"), "[^/]+")
    ok_body = json.dumps(dict(ok.body))

    running = UnitCommand("running", fake_install(tmp_path / "running_install", False, route, ok_body))
    rebuilt = UnitCommand("rebuilt", fake_install(tmp_path / "rebuilt_install", True, route, ok_body))
    root, store, prompts = scratch(tmp_path)

    # restricted to the set_repo cases, whichever of the two tables run reads them from
    monkeypatch.setattr(actioncheck, "CASES", {"set_repo": actioncheck.CASES["set_repo"]})
    monkeypatch.setattr(actioncheck, "cases", lambda: set_repo)

    verdicts = actioncheck.run(running, rebuilt, root, store, prompts, dict(os.environ))

    by_case = {v.case: v for v in verdicts if v.action == "set_repo"}
    assert set(by_case) == {c.case for c in set_repo}

    # only the rebuilt fake sends the POST: a check reading the running unit's log alone passes it
    v = by_case["ok"]
    assert v.log_empty is False, v
    assert v.passed is False, v
    assert v.status_match is True, v
    assert v.body_match is True, v
    assert v.record_match is True, v

    # both fakes send GET /user on every read: a check keeping IDENTITY_REQUEST fails all of these
    for name, v in by_case.items():
        if name == "ok":
            continue
        assert v.log_empty is True, v
        assert v.passed is True, v


@pytest.mark.acceptance
def test_actions_match_running_unit(tmp_path):
    from actioncheck import cases, run
    from startcheck.runner import UnitCommand

    running = UnitCommand("running", reference_install())
    rebuilt = UnitCommand("rebuilt", str(REPO_ROOT))
    root, store, prompts = scratch(tmp_path)
    own_env = dict(os.environ)

    verdicts = run(running, rebuilt, root, store, prompts, own_env)

    assert len(verdicts) == 39
    assert [(v.action, v.case) for v in verdicts] == [(c.action, c.case) for c in cases()]

    failing = []
    for v in verdicts:
        if v.passed is not True or false_terms(v):
            failing.append(f"{v.action} / {v.case}: false {false_terms(v)}, passed {v.passed}, "
                           f"running {v.running_answer!r}, rebuilt {v.rebuilt_answer!r}")
    assert not failing, "\n".join(failing)
