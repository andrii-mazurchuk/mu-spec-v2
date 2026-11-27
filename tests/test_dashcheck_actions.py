"""The action page check: dashcheck.actions (S-468).

T-455 runs the check end to end, the shipped page against the rebuilt unit on the acceptance copy;
T-456 builds six page variants, each one change to the page source, and requires each to fail by the
mode its walk names; T-462 pins scenarios as a pure function of a ranking; T-463 pins ranked_projects
against a fake unit answering recorded list_projects and get_spine answers; T-464 ranks the acceptance
copy and compares with the reference unit's own reads; T-465 records the order of restore, preparation,
launch and ranking inside run, and the 'no answers' floor of an empty verdict list.

The acceptance inputs are those of S-484's fixture: the reference install named by
MU_SPEC_REFERENCE_INSTALL, its prompts folder, and a staged store holding projects/ copied once from
MU_SPEC_ACCEPTANCE_STORE; this module lies outside tests/acceptance, so it stages them the same way
itself. Modules under test are imported inside the test, so a module not yet written fails only the
tests that need it.
"""

import collections
import json
import os
import shutil
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
DASHBOARD = REPO / "dashboard"
REFERENCE_INSTALL_VAR = "MU_SPEC_REFERENCE_INSTALL"
ACCEPTANCE_STORE_VAR = "MU_SPEC_ACCEPTANCE_STORE"
START_VARIANT = "hhhhhh"   # every value healthy, reached on HEALTHY_PORT
# S-219 ACTION_NAMES, the seven actions every table is keyed by
ACTION_NAMES = ("ratify", "reject", "cut_units", "set_repo", "set_labels", "emit_tickets", "set_delegation")
CONDITIONS = ("quiet", "loaded")
CALL_TIMEOUT_S = 30.0
BUILD_TIMEOUT_S = 120.0


# --- the acceptance inputs ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def inputs(tmp_path_factory):
    """running_install, staged_store and pristine_prompts, as S-484's acceptance_inputs gives them."""
    running_install = os.environ.get(REFERENCE_INSTALL_VAR)
    if not running_install:
        pytest.fail(f"{REFERENCE_INSTALL_VAR} is not set: the acceptance checks need it", pytrace=False)
    if not os.path.isdir(running_install):
        pytest.fail(f"{REFERENCE_INSTALL_VAR}={running_install!r} does not name a directory", pytrace=False)
    pristine_projects = os.environ.get(ACCEPTANCE_STORE_VAR)
    if not pristine_projects:
        pytest.fail(f"{ACCEPTANCE_STORE_VAR} is not set: the acceptance checks need it", pytrace=False)
    if not os.path.isdir(pristine_projects):
        pytest.fail(f"{ACCEPTANCE_STORE_VAR}={pristine_projects!r} does not name a directory", pytrace=False)
    staged = tmp_path_factory.mktemp("staged_store")
    shutil.copytree(pristine_projects, staged / "projects")
    return SimpleNamespace(running_install=os.path.abspath(running_install), staged_store=str(staged),
                           pristine_prompts=os.path.join(os.path.abspath(running_install), "prompts"))


@pytest.fixture(scope="session")
def action_verdicts(inputs, tmp_path_factory):
    """S-451 run's verdicts over the running and the rebuilt unit, run once before the page check."""
    from actioncheck import run
    from startcheck.ownenv import own_env

    from startcheck.runner import UnitCommand

    running = UnitCommand("running", inputs.running_install)
    rebuilt = UnitCommand("rebuilt", str(REPO))
    root = tmp_path_factory.mktemp("action_check")
    return run(running, rebuilt, str(root), inputs.staged_store, inputs.pristine_prompts, own_env())


def check_root(tmp_path, name="check"):
    root = tmp_path / name
    root.mkdir()
    return str(root)


# --- HTTP over the declared tools ----------------------------------------------------------------------

def tool(name):
    from mu_spec.declarations import TOOLS

    (spec,) = [t for t in TOOLS if t.name == name]
    return spec.method, "/" + spec.path.lstrip("/")


def tool_path(name, project=None):
    method, path = tool(name)
    if project is not None:
        path = path.replace("{project}", urllib.parse.quote(project, safe=""))
    return method, path


def http_call(port, method, path):
    import http.client

    from startcheck.runner import REACH_HOST

    conn = http.client.HTTPConnection(REACH_HOST, port, timeout=CALL_TIMEOUT_S)
    try:
        conn.request(method, path)
        answer = conn.getresponse()
        return answer.status, answer.read()
    finally:
        conn.close()


def listing_field(doc):
    """The one list-valued top-level field of an answer: the project listing, or the entry listing."""
    fields = [k for k, v in doc.items() if isinstance(v, list)]
    assert len(fields) == 1, ("the answer holds no single listing", doc)
    return fields[0]


def read_json(port, name, project=None):
    status, body = http_call(port, *tool_path(name, project))
    assert status == 200, (name, project, status, body)
    doc = json.loads(body)
    assert isinstance(doc, dict), (name, project, doc)
    return doc


def listed_projects(port):
    doc = read_json(port, "list_projects")
    return list(doc[listing_field(doc)])


def entry_count(port, project):
    doc = read_json(port, "get_spine", project)
    return len(doc[listing_field(doc)])


def started_on_copy(cmd, root, inputs):
    """cmd started through the shared launch on a copy restored from the staged store, no preparation."""
    from startcheck.ownenv import own_env

    from startcheck.runner import restore, start

    restore(root, inputs.staged_store, inputs.pristine_prompts)
    started = start(cmd, START_VARIANT, root, own_env())
    assert started.outcome == "listening" and started.port is not None, \
        (cmd.label, started.outcome, started.stderr)
    return started


def reference_projects(inputs, root):
    """Every project the reference unit's list_projects lists on its own copy."""
    from startcheck.runner import UnitCommand, stop

    started = started_on_copy(UnitCommand("running", inputs.running_install), root, inputs)
    try:
        return listed_projects(started.port)
    finally:
        stop(started)


# --- page variants --------------------------------------------------------------------------------------

def shipped_function(source, name, wrapper):
    """source with its exported function name renamed <name>__shipped and wrapper, a new export, appended."""
    head = f"export function {name}("
    assert source.count(head) == 1, f"the page source does not declare {head!r} once"
    return source.replace(head, f"function {name}__shipped(") + "\n" + wrapper + "\n"


def give_up_classify(status, outcome):
    return f"""export function classifyAction(action: any, result: any): any {{
  if (result.kind === "answer" && result.status === {status}) {{
    return {outcome};
  }}
  return classifyAction__shipped(action, result);
}}"""


V3_CLASSIFY = """export function classifyAction(action: any, result: any): any {
  if (result.kind === "no_answer" && result.reason === "network") {
    return { kind: "refused", status: 0, message: "Nothing was written." };
  }
  return classifyAction__shipped(action, result);
}"""

# an unknown outcome leaves the live record nc_available where the shipped tracker makes it nc_unavailable
V4_TRACK = """export function track(s: any, e: any): any {
  const t = track__shipped(s, e);
  if (e.type !== "outcome") return t;
  const keys = new Map(t.state.keys);
  let changed = false;
  for (const [k, K] of t.state.keys as Map<string, any>) {
    const before: any = s.keys.get(k);
    if (K.live && K.live.id === e.id && K.live.status === "nc_unavailable"
        && before && before.live && before.live.id === e.id && before.live.status === "waiting") {
      keys.set(k, { ...K, live: { ...K.live, status: "nc_available" } });
      changed = true;
    }
  }
  return changed ? { ...t, state: { ...t.state, keys } } : t;
}"""

# a press from another screen than the held record's meets no live record, as if keyed by screen too
V5_TRACK = """export function track(s: any, e: any): any {
  if (e.type === "press") {
    const k = JSON.stringify([e.project, e.action]);
    const K: any = s.keys.get(k);
    if (K && K.live && K.live.screen !== e.screen
        && (K.live.status === "waiting" || K.live.status === "nc_unavailable")) {
      const keys = new Map(s.keys);
      keys.set(k, { ...K, live: null, earlier: [...K.earlier, K.live] });
      return track__shipped({ ...s, keys }, e);
    }
  }
  return track__shipped(s, e);
}"""

V6_FORMS = """export const REFUSAL_FORMS: any = {
  ...REFUSAL_FORMS__shipped,
  set_repo: [
    { ...REFUSAL_FORMS__shipped.set_repo[0], pattern: "^z" + REFUSAL_FORMS__shipped.set_repo[0].pattern.slice(1) },
    ...REFUSAL_FORMS__shipped.set_repo.slice(1),
  ],
};"""


def drift_forms(source):
    head = "export const REFUSAL_FORMS"
    assert source.count(head) == 1, f"the page source does not declare {head!r} once"
    return source.replace(head, "const REFUSAL_FORMS__shipped") + "\n" + V6_FORMS + "\n"


def variant_edits(give_up_status):
    """name -> (page source file, its edit)."""
    confirmed = '{ kind: "confirmed", body: result.body }'
    refused = ('{ kind: "refused", status: result.status, '
               'message: String(result.body && (result.body as any).error) }')
    return {
        "V1": ("src/outcome.ts",
               lambda s: shipped_function(s, "classifyAction", give_up_classify(give_up_status, confirmed))),
        "V2": ("src/outcome.ts",
               lambda s: shipped_function(s, "classifyAction", give_up_classify(give_up_status, refused))),
        "V3": ("src/outcome.ts", lambda s: shipped_function(s, "classifyAction", V3_CLASSIFY)),
        "V4": ("src/tracker.ts", lambda s: shipped_function(s, "track", V4_TRACK)),
        "V5": ("src/tracker.ts", lambda s: shipped_function(s, "track", V5_TRACK)),
        "V6": ("src/outcome.ts", drift_forms),
    }


def build_variant(where, rel, edit):
    """(page source root, install root) of one variant: the page source copied with one edit, built by
    S-13's build into its own page, and a copy of the rebuilt package serving that page."""
    from pagebuild.check import BUILD_COMMAND, COMMITTED_PAGE, OUT_PAGE

    page_root = where / "page"
    shutil.copytree(DASHBOARD, page_root, ignore=shutil.ignore_patterns("node_modules", "out"))
    target = page_root / rel
    source = target.read_text(encoding="utf-8")
    changed = edit(source)
    assert changed != source, rel
    target.write_text(changed, encoding="utf-8")
    done = subprocess.run(list(BUILD_COMMAND), cwd=page_root, capture_output=True, text=True,
                          encoding="utf-8", timeout=BUILD_TIMEOUT_S, check=False)
    assert done.returncode == 0, (where.name, done.stdout, done.stderr)
    page = page_root / OUT_PAGE
    assert page.is_file(), (where.name, OUT_PAGE)

    install = where / "install"
    shutil.copytree(REPO / "mu_spec", install / "mu_spec", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(page, install / COMMITTED_PAGE)
    return str(page_root), str(install)


# --- T-455 ------------------------------------------------------------------------------------------------

@pytest.mark.acceptance
def test_action_page_check_end_to_end(tmp_path, inputs, action_verdicts):
    from actioncheck import prepare
    from dashcheck.actions import run
    from startcheck.ownenv import own_env

    from startcheck.runner import UnitCommand

    projects = reference_projects(inputs, check_root(tmp_path, "listing"))
    rebuilt = UnitCommand("rebuilt", str(REPO))

    report = run(rebuilt, str(DASHBOARD), check_root(tmp_path), inputs.staged_store, inputs.pristine_prompts,
                 own_env(), action_verdicts, prepare)

    assert report.scenario_faults == (), "\n".join(report.scenario_faults)
    assert report.count_faults == (), "\n".join(report.count_faults)
    assert report.forms_faults == (), "\n".join(report.forms_faults)
    assert report.passed is True

    # a report timing only the two scenario projects misses the copy's other projects
    timed = {(p, a, c) for p, a, c, _ in report.answer_times}
    assert timed == {(p, a, c) for p in projects for a in ACTION_NAMES for c in CONDITIONS}, \
        sorted(timed ^ {(p, a, c) for p in projects for a in ACTION_NAMES for c in CONDITIONS})
    for p, a, c, t in report.answer_times:
        assert isinstance(t, float) and t >= 0.0, (p, a, c, t)

    for p, a, n in report.unknown_by_wait:
        assert isinstance(n, int) and not isinstance(n, bool) and n >= 0, (p, a, n)
        assert p in projects and a in ACTION_NAMES, (p, a, n)


# --- T-456 ------------------------------------------------------------------------------------------------

@pytest.mark.acceptance
def test_page_variants_fail(tmp_path, inputs, action_verdicts):
    from actioncheck import prepare
    from dashcheck.actions import run
    from dashcheck.forms import read_tables
    from dashcheck.forward import NODE_GIVE_UP
    from startcheck.ownenv import own_env

    from startcheck.runner import UnitCommand

    give_up_status, _ = NODE_GIVE_UP
    shipped = read_tables(str(DASHBOARD))["refusal_forms"]["set_repo"][0]
    assert shipped["pattern"].startswith("^"), shipped

    reports = {}
    for name, (rel, edit) in variant_edits(give_up_status).items():
        where = tmp_path / name
        where.mkdir()
        page_root, install = build_variant(where, rel, edit)
        if name == "V6":
            drifted = read_tables(page_root)["refusal_forms"]["set_repo"][0]
            assert drifted["name"] == shipped["name"], (drifted, shipped)
            assert drifted["pattern"] == "^z" + shipped["pattern"][1:] != shipped["pattern"], (drifted, shipped)
        reports[name] = run(UnitCommand("rebuilt", install), page_root, check_root(where),
                            inputs.staged_store, inputs.pristine_prompts, own_env(), action_verdicts, prepare)

    def names(faults, *words):
        return any(all(w in f for w in words) for f in faults)

    missed = []
    for name, report in reports.items():
        if report.passed is not False:
            missed.append(f"{name}: passed {report.passed!r}")
    # V1: done at once on the node's give-up, before any judged screen answer
    r = reports["V1"]
    if not (names(r.scenario_faults, "commit_give_up") and names(r.scenario_faults, "give_up_commit_late")):
        missed.append(f"V1: no commit_give_up and give_up_commit_late faults: {r.scenario_faults!r}")
    # V2: the node's error text drawn as a failure
    r = reports["V2"]
    if not names(r.scenario_faults, "commit_give_up"):
        missed.append(f"V2: no commit_give_up fault: {r.scenario_faults!r}")
    # V3: a dropped connection drawn as a failure
    r = reports["V3"]
    if not names(r.scenario_faults, "drop"):
        missed.append(f"V3: no drop fault: {r.scenario_faults!r}")
    # V4: the control available right after the give-up, before the relayed answer
    r = reports["V4"]
    if not names(r.scenario_faults, "give_up_commit_late"):
        missed.append(f"V4: no give_up_commit_late fault: {r.scenario_faults!r}")
    # V5: presses from order and ship while held raise received('cut_units') by 2
    r = reports["V5"]
    if not names(r.count_faults, "cut_units"):
        missed.append(f"V5: no cut_units count fault: {r.count_faults!r}")
    # V6: the drifted pattern differs from pattern_of of the template; no scenario reaches it
    r = reports["V6"]
    if not names(r.forms_faults, "set_repo", shipped["name"]):
        missed.append(f"V6: no forms fault naming set_repo and {shipped['name']}: {r.forms_faults!r}")
    if r.scenario_faults != () or r.count_faults != ():
        missed.append(f"V6: scenario or count faults: {r.scenario_faults!r} {r.count_faults!r}")
    assert not missed, "\n".join(missed)


# --- T-462 ------------------------------------------------------------------------------------------------

def test_scenarios_cover():
    from actioncheck import cases
    from dashcheck.actions import SCENARIO_PROJECTS, scenarios
    from dashcheck.forward import MODES
    from mu_spec.declarations import ACTIONS
    from shipcheck.judge import scenario

    acts = [spec.name for spec in ACTIONS if spec.name != "rollback_tickets"]
    assert len(acts) == 7 and len(MODES) == 4 and SCENARIO_PROJECTS == 2, (acts, MODES, SCENARIO_PROJECTS)
    pairs = {(a, m) for a in acts for m in MODES}

    sc = scenarios(["small", "big", "mid"])
    # one omitting give_up_commit_late gives 48
    assert len(sc) == len(acts) * len(MODES) * SCENARIO_PROJECTS == 56, len(sc)
    assert len(set(sc)) == len(sc), sc
    # a re-sorting scenarios uses {'big', 'mid'}
    assert {s.project for s in sc} == {"small", "big"}, {s.project for s in sc}
    for project in ("small", "big"):
        on = [(s.action, s.mode) for s in sc if s.project == project]
        assert sorted(on) == sorted(pairs), (project, on)
    assert not [s for s in sc if s.action == "rollback_tickets"]

    ok_prep = {c.action: c.store_prep for c in cases() if c.case == "ok"}
    empty_repo = scenario("empty_repo").store_prep
    for s in sc:
        expected = empty_repo if s.action == "emit_tickets" else ok_prep[s.action]
        assert s.store_prep == expected, (s, expected)

    # one padding a one-project ranking to two gives 56 or raises
    only = scenarios(["only"])
    assert len(only) == 28, len(only)
    assert {s.project for s in only} == {"only"}
    assert sorted((s.action, s.mode) for s in only) == sorted(pairs)

    with pytest.raises(ValueError):
        scenarios([])

    assert scenarios(("small", "big", "mid")) == sc
    assert scenarios(["small", "big", "mid"]) == sc


# --- T-463 ------------------------------------------------------------------------------------------------

class FakeUnit:
    """Answers list_projects and get_spine for the projects in spines at their declared method and path,
    404 to anything else; records every request as (method, path)."""

    def __init__(self, host, projects_doc, spines):
        self.lock = threading.Lock()
        self.requests = []
        routes = {tool_path("list_projects"): projects_doc}
        for project, doc in spines.items():
            routes[tool_path("get_spine", project)] = doc
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def handle_any(self):
                path = self.path.split("?", 1)[0]
                with fake.lock:
                    fake.requests.append((self.command, path))
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                doc = routes.get((self.command, path))
                status, body = (200, json.dumps(doc).encode("utf-8")) if doc is not None else (404, b"{}")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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


@pytest.mark.acceptance
def test_ranked_projects_order(tmp_path, inputs):
    from dashcheck.actions import ranked_projects

    from startcheck.runner import REACH_HOST, UnitCommand, stop

    # LP and SP recorded from the reference install on a restored acceptance copy
    started = started_on_copy(UnitCommand("running", inputs.running_install), check_root(tmp_path), inputs)
    try:
        lp = read_json(started.port, "list_projects")
        sp = None
        for project in lp[listing_field(lp)]:
            doc = read_json(started.port, "get_spine", project)
            if len(doc[listing_field(doc)]) >= 9:
                sp = doc
                break
    finally:
        stop(started)
    assert sp is not None, "no project of the acceptance copy lists at least 9 entries"
    lp_field, sp_field = listing_field(lp), listing_field(sp)

    def spine(n):
        return {**sp, sp_field: sp[sp_field][:n]}

    fake = FakeUnit(REACH_HOST, {**lp, lp_field: ["c", "b", "a"]}, {"c": spine(9), "b": spine(5), "a": spine(9)})
    empty = FakeUnit(REACH_HOST, {**lp, lp_field: []}, {})
    try:
        # name order alone gives a, b, c; the listing's order on ties c, a, b; ascending count b, a, c
        assert ranked_projects(fake.port) == ("a", "c", "b")
        seen = fake.seen()
        assert sorted(seen) == sorted([tool_path("list_projects")] + [tool_path("get_spine", p) for p in "cba"]), seen

        with pytest.raises(ValueError):
            ranked_projects(empty.port)
    finally:
        fake.close()
        empty.close()


# --- T-464 ------------------------------------------------------------------------------------------------

def stored_files(root):
    out = {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            full = os.path.join(dirpath, f)
            out[os.path.relpath(full, root)] = Path(full).read_bytes()
    return out


@pytest.mark.acceptance
def test_ranked_projects_on_the_copy(tmp_path, inputs):
    from dashcheck.actions import ranked_projects, scenarios

    from startcheck.runner import UnitCommand, stop
    from startcheck.variants import STORE

    # EXPECT from the reference unit alone, on its own copy
    started = started_on_copy(UnitCommand("running", inputs.running_install), check_root(tmp_path, "running"),
                              inputs)
    try:
        names = listed_projects(started.port)
        counts = {p: entry_count(started.port, p) for p in names}
    finally:
        stop(started)
    expect = tuple(sorted(names, key=lambda p: (-counts[p], p)))
    assert len(expect) >= 3, expect
    assert list(expect) != sorted(names), (expect, counts)

    rebuilt_root = check_root(tmp_path, "rebuilt")
    started = started_on_copy(UnitCommand("rebuilt", str(REPO)), rebuilt_root, inputs)
    try:
        ranked = ranked_projects(started.port)
        assert ranked == expect, (ranked, expect, counts)
        assert {s.project for s in scenarios(ranked)} == set(expect[:2])

        # the ranking writes nothing
        pristine = stored_files(inputs.staged_store)
        copy = stored_files(os.path.join(rebuilt_root, STORE))
        changed = sorted(rel for rel, data in copy.items() if pristine.get(rel) != data)
        assert not changed, changed
    finally:
        stop(started)


# --- T-465 ------------------------------------------------------------------------------------------------

@pytest.mark.acceptance
def test_run_prepares_and_needs_both_units(tmp_path, inputs, monkeypatch):
    import actioncheck
    from dashcheck import actions
    from startcheck.ownenv import own_env

    from startcheck.runner import UnitCommand

    events = []
    real_restore, real_launch, real_ranked = actions.restore, actions.launch, actions.ranked_projects

    def restore(*args, **kwargs):
        events.append(("restore",))
        return real_restore(*args, **kwargs)

    def launch(*args, **kwargs):
        events.append(("launch",))
        return real_launch(*args, **kwargs)

    def ranked_projects(*args, **kwargs):
        ranked = real_ranked(*args, **kwargs)
        events.append(("ranked", ranked))
        return ranked

    def prepare(store_prep, root):
        events.append(("prepare", store_prep))
        actioncheck.prepare(store_prep, root)

    monkeypatch.setattr(actions, "restore", restore)
    monkeypatch.setattr(actions, "launch", launch)
    monkeypatch.setattr(actions, "ranked_projects", ranked_projects)

    report = actions.run(UnitCommand("rebuilt", str(REPO)), str(DASHBOARD), check_root(tmp_path),
                         inputs.staged_store, inputs.pristine_prompts, own_env(), action_verdicts=[],
                         prepare=prepare)

    ranks = [i for i, e in enumerate(events) if e[0] == "ranked"]
    assert len(ranks) == 1, events
    (r_at,) = ranks
    ranked = events[r_at][1]
    assert events[:3] == [("restore",), ("launch",), ("ranked", ranked)], events[:3]
    preps = [i for i, e in enumerate(events) if e[0] == "prepare"]
    # a run preparing before ranking reads a raised store
    assert all(i > r_at for i in preps), events[:r_at + 1]

    s = actions.scenarios(ranked)
    assert len(preps) == len(s), (len(preps), len(s))
    assert collections.Counter(events[i][1] for i in preps) == collections.Counter(x.store_prep for x in s)

    kinds = [e[0] for e in events]
    for i in preps:
        before = [j for j in range(i) if kinds[j] == "restore"]
        assert before, (i, events[:i + 1])
        assert "launch" not in kinds[before[-1] + 1:i], ("launched between restore and prepare", i)
        after = kinds[i + 1:]
        assert "launch" in after, ("no launch after prepare", i)
        nxt = after.index("restore") if "restore" in after else len(after)
        assert after.index("launch") < nxt, ("restored again before launching", i)

    for a in ACTION_NAMES:
        assert any("no answers" in f and "running" in f and a in f for f in report.forms_faults), \
            (a, report.forms_faults)
    assert report.passed is False
