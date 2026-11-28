"""The cost check passes over the acceptance copy, through the shared interposed launch (T-451).

The pytest wrapper of dashcheck.cost (S-433): it takes S-508's acceptance_inputs, builds the shared
interposed launch's StandIn and the Launcher bound to it as every check's wrapper does, runs run()
and requires a pass, writing the timed table out as the report file, one row per Timed. Outside
tests/acceptance/inprocess: it launches units and imports no mu_spec module itself. Every start is
recorded, at the runner's start and at the launcher's prepare, so a start that bypasses the launcher
shows as a prepare count below the start count. The projects, entry ids and unit keys the table must
cover are read once from the rebuilt unit on a restored copy, never from the report. Modules under
test are imported inside the test, so a module not yet written fails only this test.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

REPORT_FILE = "cost-report.txt"
SCREEN_KINDS = ("open", "head", "rest_on_head", "whole", "whole_on_head", "ask_on_whole")
SHIP_KINDS = ("progress_named", "progress_none", "progress_earlier", "end_refresh")
CONDITIONS = ("quiet", "loaded")


class CountingLauncher:
    """The shared launch's Launcher, each prepare recorded before it is passed on."""

    def __init__(self, inner):
        self.inner = inner
        self.prepared: list[tuple[str, str]] = []

    def prepare(self, cmd, check_root):
        self.prepared.append((cmd.label, check_root))
        return self.inner.prepare(cmd, check_root)


def snapshot(root: str) -> dict[str, str]:
    """Every file under root, by relative path, to the digest of its bytes."""
    out = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            out[os.path.relpath(path, root)] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return out


def listing(raw: bytes) -> list:
    doc = json.loads(raw)
    lists = [v for v in doc.values() if isinstance(v, list)] if isinstance(doc, dict) else [doc]
    assert len(lists) == 1 and isinstance(lists[0], list), f"the answer holds no single listing: {raw!r}"
    return lists[0]


def unit_keys(value) -> list[str]:
    """Every value under a "key" field of a get_units answer."""
    if isinstance(value, dict):
        found = [value["key"]] if isinstance(value.get("key"), str) else []
        return found + [k for v in value.values() for k in unit_keys(v)]
    if isinstance(value, list):
        return [k for v in value for k in unit_keys(v)]
    return []


def stored_filters(call, fill, read_tools, port: int) -> dict[str, dict[str, list[str]]]:
    """Per stored project: the ids its get_spine lists and the unit keys its get_units names."""
    status, raw = call(port, "GET", "/tools", None)
    assert status == 200, f"GET /tools answered {status}: {raw!r}"
    tools = {t.name: t for t in read_tools(json.loads(raw))}
    status, raw = call(port, "GET", fill(tools["list_projects"].path, {}), None)
    assert status == 200, f"list_projects answered {status}: {raw!r}"
    names = sorted(p if isinstance(p, str) else p["name"] for p in listing(raw))
    assert names, "the acceptance copy stores no project"
    out = {}
    for name in names:
        status, raw = call(port, "GET", fill(tools["get_spine"].path, {"project": name}), None)
        assert status == 200, f"get_spine of {name} answered {status}: {raw!r}"
        ids = [row["id"] for row in listing(raw) if isinstance(row, dict) and isinstance(row.get("id"), str)]
        status, raw = call(port, "GET", fill(tools["get_units"].path, {"project": name}), None)
        assert status == 200, f"get_units of {name} answered {status}: {raw!r}"
        out[name] = {"spine": ids, "order": unit_keys(json.loads(raw))}
    return out


@pytest.fixture(scope="session")
def acceptance_inputs(request):
    """S-508's acceptance_inputs itself, set up only once the module under test imports.

    The conftest fixture is autouse, so without this it runs before the test body: while dashcheck is
    absent the test would fail on a missing acceptance input instead of on the absent module.
    """
    import dashcheck.cost  # noqa: F401

    return request.getfixturevalue("acceptance_inputs")


def test_cost_check_passes(request, tmp_path, monkeypatch):
    # The modules under test first, so while they are absent this test fails for that reason and not
    # for an acceptance input the environment lacks.
    from dashcheck import cost
    from dashcheck.cost import (
        IDLE_ANSWER_MAX,
        LAYERS,
        START_VARIANT,
        START_WINDOW_S,
        VIEW_NAMES,
        run,
    )
    from unitcheck.session import open_session

    from startcheck import runner
    from startcheck.runner import UnitCommand, restore, start, stop

    # S-508's session fixture, looked up in the test; its helpers are taken from the conftest module
    # pytest loaded, the one that defines the fixture's own AcceptanceInputs.
    inputs = request.getfixturevalue("acceptance_inputs")
    conftest = sys.modules[type(inputs).__module__]
    call, fill, read_tools = conftest.call, conftest.fill, conftest.read_tools

    rebuilt = UnitCommand("rebuilt", inputs.rebuilt_install)
    pristine_store = inputs.staged_store
    pristine_prompts = inputs.pristine_prompts
    check_root = str(tmp_path)
    env = dict(os.environ)
    before = snapshot(pristine_store)

    starts: list[tuple[str, str, object]] = []
    original = runner.start

    # start's own parameter names, so a run() calling it positionally or by keyword is recorded alike.
    def recording_start(cmd, variant, check_root, own_env, launcher=None):
        starts.append((cmd.label, variant, launcher))
        return original(cmd, variant, check_root, own_env, launcher)

    monkeypatch.setattr(runner, "start", recording_start)
    if hasattr(cost, "start"):
        monkeypatch.setattr(cost, "start", recording_start)

    session = open_session(env)
    try:
        standin = session.standin
        launcher = CountingLauncher(session.launcher())

        # 5, second half: no launcher is refused before anything is restored or started.
        with pytest.raises(ValueError):
            run(rebuilt, check_root, pristine_store, pristine_prompts, env, None, standin)
        assert os.listdir(check_root) == [], os.listdir(check_root)
        assert starts == [] and launcher.prepared == []

        # The stored projects and their filter values, read once from the rebuilt unit on a restored copy.
        restore(check_root, pristine_store, pristine_prompts)
        unit = start(rebuilt, START_VARIANT, check_root, env, launcher)
        assert unit.outcome == "listening" and unit.port is not None, (unit.outcome, unit.stderr)
        try:
            stored = stored_filters(call, fill, read_tools, unit.port)
        finally:
            stop(unit)

        starts.clear()
        launcher.prepared.clear()
        report = run(rebuilt, check_root, pristine_store, pristine_prompts, env, launcher, standin)
    finally:
        session.close()

    # 4. The timed table, one row per Timed, written out before judging.
    fields = ("project", "view", "filter", "kind", "condition", "worst_s", "status", "size")
    lines = ["\t".join(fields)]
    lines += ["\t".join(str(getattr(t, f)) for f in fields) for t in report.timed]
    out = tmp_path / REPORT_FILE
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written = out.read_text(encoding="utf-8").splitlines()
    assert len(written) == 1 + len(report.timed)
    assert written == lines

    # 5. Every start inside run went through the launcher, on START_VARIANT, one prepare each.
    assert starts, "run started no unit"
    assert all(variant == START_VARIANT for _, variant, _ in starts), starts
    assert all(given is launcher for _, _, given in starts), starts
    assert len(launcher.prepared) == len(starts), (launcher.prepared, starts)
    assert {label for label, _, _ in starts} == {"rebuilt"}, starts

    # 1. A pass, with every fault tuple empty and both windows inside the start window.
    for name in (
        "over_budget",
        "idle_too_big",
        "slow_node_reads",
        "late_freshness",
        "preparing_after_window",
        "fallback_faults",
        "run_part_faults",
        "progress_faults",
        "end_refresh_faults",
        "never_ended",
    ):
        assert tuple(getattr(report, name)) == (), (name, getattr(report, name))
    assert report.created_preparing is False
    assert report.start_window_s <= START_WINDOW_S, report.start_window_s
    assert report.fork_window_s <= START_WINDOW_S, report.fork_window_s
    assert report.shipped, ("no project was shipped", report.not_shippable)
    assert report.passed is True

    # 2. A row for every stored project, view, filter value and screen read kind, quiet and loaded;
    # the progress and end-of-run rows for every shipped project.
    rows = {(t.project, t.view, t.filter, t.kind, t.condition) for t in report.timed}
    filters = {"read": list(LAYERS)}
    missing = []
    for project, values in stored.items():
        for view in VIEW_NAMES:
            for value in [None, *values.get(view, filters.get(view, []))]:
                for kind in SCREEN_KINDS:
                    for condition in CONDITIONS:
                        if (project, view, value, kind, condition) not in rows:
                            missing.append((project, view, value, kind, condition))
    assert missing == [], missing[:20]
    ship_rows = {(t.project, t.kind) for t in report.timed if t.condition == "ship"}
    absent = [(p, k) for p in report.shipped for k in SHIP_KINDS if (p, k) not in ship_rows]
    assert absent == [], absent

    # 3. Every quiet ask on a held whole is the idle answer.
    assert IDLE_ANSWER_MAX == 1024
    asks = [t for t in report.timed if t.kind == "ask_on_whole" and t.condition == "quiet"]
    assert asks
    loud = [t for t in asks if t.status != "unchanged" or t.size > IDLE_ANSWER_MAX]
    assert loud == [], loud[:20]

    # 6. The staged copy is byte-unchanged.
    assert snapshot(pristine_store) == before
