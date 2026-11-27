"""The Ship check's judge, shipcheck.judge: its scenario tables, its masks and comparisons, and the
check itself over both real units against the stand-in (T-100, T-101, T-102).

The comparison tests drive run_scenario over scratch fake units started through a recording fake
Launcher and a fake stand-in whose log the test sets per unit, so each judged term is decided by
the test alone. The end-to-end test runs the running unit, the install at MU_SPEC_REFERENCE_INSTALL,
against the rebuilt unit, this repository. Modules under test are imported inside the test, so a
module not yet written fails only the tests that need it.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_VAR = "MU_SPEC_REFERENCE_INSTALL"
STEP_KINDS = ("press_ship", "press_rollback", "auto_ship", "check_access", "await_end", "await_hold",
              "release", "restart", "read")
VERDICT_TERMS = ("requests_match", "answers_match", "reads_match", "run_ids_well_formed")
ORDER_SUFFIX = "/dependencies/blocked_by"
NEVER_ENDS_ALLOWANCE_S = 2.0

# A fake unit: answers /health, every emission read with running set by argv[1], every other read
# with {}, and every write 202 with a run id: a fresh one per write when argv[2] is 'fresh', else
# argv[2] itself.
_FAKE_UNIT = """\
import json, os, secrets, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RUNNING = sys.argv[1] == 'never_ends'
RUN_ID = sys.argv[2]


class Handler(BaseHTTPRequestHandler):
    def _answer(self, status, doc):
        body = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/health':
            self._answer(200, {'ok': True})
        elif path.endswith('/emit'):
            self._answer(200, {'running': RUNNING})
        else:
            self._answer(200, {})

    def _write(self):
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        self._answer(202, {'run_id': secrets.token_hex(6) if RUN_ID == 'fresh' else RUN_ID})

    do_POST = do_PUT = do_PATCH = do_DELETE = _write

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
    """A check root and pristine store and prompts copies, as the start check's own tests lay them out."""
    store = tmp_path / "pristine_store"
    (store / "projects").mkdir(parents=True)
    prompts = tmp_path / "pristine_prompts"
    prompts.mkdir()
    (prompts / "default.md").write_bytes(b"d")
    root = tmp_path / "check"
    root.mkdir()
    return str(root), str(store), str(prompts)


class FakeLauncher:
    """Starts the fake unit for every prepare, in the mode given per unit label, and records the calls."""

    def __init__(self, modes):
        self.modes = modes
        self.prepared = []

    def prepare(self, cmd, check_root):
        from startcheck.runner import Prepared

        running, run_id = self.modes[cmd.label]
        self.prepared.append(cmd.label)
        return Prepared(argv=(sys.executable, "-I", "-c", _FAKE_UNIT, running, run_id), extra_env={},
                        ready=lambda timeout: True, done=lambda: None)


class FakeStandIn:
    """A stand-in whose log after the n-th reset is logs[n-1]; any hold has always arrived."""

    port = 0

    def __init__(self, logs=((), ())):
        self._logs = tuple(logs)
        self.resets = 0

    def reset(self, script):
        self.resets += 1

    def log(self):
        if not self.resets:
            return ()
        return self._logs[min(self.resets, len(self._logs)) - 1]

    def held(self):
        return True

    def release(self):
        pass

    def state_digest(self):
        return ""

    def close(self):
        pass


def req(method, path, accept="application/vnd.github+json", body=b""):
    from shipcheck.standin import Recorded

    headers = (("accept", accept), ("authorization", "Bearer test-token"))
    return Recorded(method=method, path=path, query="", headers=headers, body=body)


def judge_fakes(tmp_path, name, modes=None, logs=((), ()), foreign=None):
    """run_scenario on scenario(name) over the fake units, both started through one FakeLauncher."""
    from shipcheck.judge import run_scenario, scenario
    from startcheck.runner import UnitCommand

    modes = modes or {"running": ("ends", "fresh"), "rebuilt": ("ends", "fresh")}
    launcher = FakeLauncher(modes)
    root, store, prompts = scratch(tmp_path)
    running = UnitCommand("running", str(tmp_path / "running_install"))
    rebuilt = UnitCommand("rebuilt", str(tmp_path / "rebuilt_install"))
    judge_foreign = foreign or (lambda started: ())
    return run_scenario(scenario(name), running, rebuilt, root, store, prompts, dict(os.environ),
                        FakeStandIn(logs), launcher=launcher, foreign=judge_foreign)


def test_scenario_tables():
    from shipcheck.judge import (
        DELEGATED_CONDITIONS,
        RUN_REASON_SCENARIOS,
        SCENARIOS,
        TOKENLESS,
        scenario,
    )

    from mu_spec.ship.plan import RUN_REASONS
    from mu_spec.writes.ship import CONDITIONS

    assert len(SCENARIOS) == 42
    assert len(set(SCENARIOS)) == 42

    assert set(RUN_REASON_SCENARIOS) == set(RUN_REASONS)
    assert len(RUN_REASON_SCENARIOS) == 15
    for reason, name in RUN_REASON_SCENARIOS.items():
        assert name in SCENARIOS, reason
    # one scenario per reason: 'no_cut' and 'rb_no_cut' sharing one scenario fails here
    assert len(set(RUN_REASON_SCENARIOS.values())) == 15

    assert TOKENLESS == ("no_token", "rollback_no_token")
    for n in SCENARIOS:
        sc = scenario(n)
        assert sc.name == n
        # a scenario() launching every scenario with a token fails on 'no_token'
        assert sc.token is (n not in TOKENLESS), n
        kinds = [step.kind for step in sc.steps]
        assert kinds, n
        assert set(kinds) <= set(STEP_KINDS), (n, kinds)

    # the index is the first order POST's; that it is is judged against a real log in T-102
    timed_out = scenario("order_write_timed_out").script
    assert len(timed_out.silent) == 1
    (index,) = timed_out.silent
    assert isinstance(index, int) and index >= 1

    unanswered = scenario("order_unanswered").script
    assert unanswered.unanswered
    assert not unanswered.silent

    assert DELEGATED_CONDITIONS == CONDITIONS


def test_mask_and_comparison(tmp_path, monkeypatch):
    from shipcheck import judge
    from shipcheck.judge import mask

    # 1. time values and the run id masked, every other byte as given
    view = (b'{"running": false, "run_id": "9f2c4e81a07b", "started_at": 1764100000.5, '
            b'"finished_at": 1764100003.0, "created": 3}')
    assert mask(view) == (b'{"running": false, "run_id": "<run>", "started_at": "<time>", '
                          b'"finished_at": "<time>", "created": 3}')

    # 2. the busy message's run id, alone and inside an answer body
    busy = "an emission for 'p' is already running (run 9f2c4e81a07b)"
    masked_busy = "an emission for 'p' is already running (run <run>)"
    assert mask(busy.encode()) == masked_busy.encode()
    assert mask(json.dumps({"error": busy}).encode()) == json.dumps({"error": masked_busy}).encode()

    post_a = req("POST", "/repos/o/r/issues", body=b'{"title": "a"}')
    get_b = req("GET", "/repos/o/r/issues/1")
    user = req("GET", "/user")

    # 3. one header value differs: logs compared with headers as received
    other_accept = req("POST", "/repos/o/r/issues", accept="application/json", body=b'{"title": "a"}')
    v = judge_fakes(tmp_path / "s3", "empty_repo", logs=((post_a, get_b), (other_accept, get_b)))
    assert v.requests_match is False

    # 4. a repeated copy of one POST: logs compared as sets would pass here
    v = judge_fakes(tmp_path / "s4", "empty_repo", logs=((post_a, post_a, get_b), (post_a, get_b)))
    assert v.requests_match is False
    assert v.passed is False

    # 5. GET /user removed wherever it falls: compared in place this would fail
    v = judge_fakes(tmp_path / "s5", "empty_repo",
                    logs=((user, post_a, get_b), (post_a, user, get_b, user)))
    assert v.requests_match is True

    # 6. a foreign name for one start fails the scenario, every other term holding
    starts = []

    def one_foreign(started):
        starts.append(started)
        return ("example.com",) if len(starts) == 1 else ()

    v = judge_fakes(tmp_path / "s6", "empty_repo", foreign=one_foreign)
    assert v.foreign == ("example.com",)
    for term in VERDICT_TERMS:
        assert getattr(v, term) is True, term
    assert v.passed is False

    # 7. a run that never ends fails at END_ALLOWANCE_S, never passes, never hangs
    monkeypatch.setattr(judge, "END_ALLOWANCE_S", NEVER_ENDS_ALLOWANCE_S)
    began = time.monotonic()
    v = judge_fakes(tmp_path / "s7", "empty_repo",
                    modes={"running": ("never_ends", "fresh"), "rebuilt": ("never_ends", "fresh")})
    took = time.monotonic() - began
    assert v.passed is False
    assert took >= NEVER_ENDS_ALLOWANCE_S, took
    assert took < 2 * NEVER_ENDS_ALLOWANCE_S + 60, took
    monkeypatch.undo()

    # 8. run ids judged by shape: two runs sharing one id, and an id not lowercase hex
    shared = {"running": ("ends", "0123456789ab"), "rebuilt": ("ends", "0123456789ab")}
    v = judge_fakes(tmp_path / "s8a", "order_refused", modes=shared)
    assert v.run_ids_well_formed is False
    assert v.passed is False
    upper = {"running": ("ends", "ABCDEF012345"), "rebuilt": ("ends", "ABCDEF012345")}
    v = judge_fakes(tmp_path / "s8b", "empty_repo", modes=upper)
    assert v.run_ids_well_formed is False
    assert v.passed is False


@pytest.mark.acceptance
def test_ship_matches_running_unit(tmp_path):
    from shipcheck.judge import DELEGATED_CONDITIONS, IDENTITY_REQUEST, SCENARIOS, run, scenario
    from shipcheck.standin import StandIn
    from startcheck.runner import UnitCommand

    running = UnitCommand("running", reference_install())
    rebuilt = UnitCommand("rebuilt", str(REPO_ROOT))
    root, store, prompts = scratch(tmp_path)
    own_env = dict(os.environ)

    verdicts = run(SCENARIOS, running, rebuilt, root, store, prompts, own_env)

    by_name = {}
    for v in verdicts:
        by_name.setdefault(v.name, []).append(v)
    for n in SCENARIOS:
        assert by_name.get(n), f"no verdict for {n}"
    assert len(by_name["delegated_each_condition_alone"]) == len(DELEGATED_CONDITIONS) == 6

    failing = []
    for v in verdicts:
        false_terms = [term for term in VERDICT_TERMS if getattr(v, term) is not True]
        if not v.passed or v.foreign != () or false_terms:
            failing.append(f"{v.name}: false {false_terms}, foreign {v.foreign}, passed {v.passed}")
    assert not failing, "\n".join(failing)

    # the write timeout's silent index is the first order POST of the plan's sequence
    sc = scenario("order_write_timed_out")
    (index,) = sc.script.silent
    standin = StandIn()
    try:
        (v,) = run((sc.name,), running, rebuilt, root, store, prompts, own_env, standin=standin)
        indexed = [r for r in standin.log() if (r.method, r.path) != IDENTITY_REQUEST]
    finally:
        standin.close()
    assert v.passed is True, v
    assert len(indexed) >= index
    orders = [i for i, r in enumerate(indexed, 1) if r.method == "POST" and r.path.endswith(ORDER_SUFFIX)]
    assert orders and orders[0] == index, (index, orders)
