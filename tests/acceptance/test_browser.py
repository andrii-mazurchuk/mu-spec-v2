"""The browser check passes on the rebuilt page over the acceptance copy (T-450).

The pytest wrapper of dashcheck.browser (S-434): it takes S-508's acceptance_inputs, builds the
shared interposed launch's Launcher as every check's wrapper does, runs run() and requires a pass,
writing the timings table and the colour fault lines out as the report file. Outside
tests/acceptance/inprocess: it launches units and imports no mu_spec module itself. Every start is
recorded, at the runner's start and at the launcher's prepare, so a start that bypasses the launcher
shows as a prepare count below the start count. Modules under test are imported inside the test, so a
module not yet written fails only this test.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

REPORT_FILE = "browser-report.txt"


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


def largest_project(call, fill, read_tools, port: int) -> tuple[str, list[str]]:
    """The project whose get_spine lists the most entries, ties by name order, and every stored name."""
    status, raw = call(port, "GET", "/tools", None)
    assert status == 200, f"GET /tools answered {status}: {raw!r}"
    tools = {t.name: t for t in read_tools(json.loads(raw))}
    status, raw = call(port, "GET", fill(tools["list_projects"].path, {}), None)
    assert status == 200, f"list_projects answered {status}: {raw!r}"
    names = sorted(p if isinstance(p, str) else p["name"] for p in listing(raw))
    assert names, "the acceptance copy stores no project"
    counts = {}
    for name in names:
        status, raw = call(port, "GET", fill(tools["get_spine"].path, {"project": name}), None)
        assert status == 200, f"get_spine of {name} answered {status}: {raw!r}"
        counts[name] = len(listing(raw))
    return min(names, key=lambda n: (-counts[n], n)), names


def normaliser(page):
    """The computed color of a probe in an unstyled document given color: value."""
    page.set_content("<!doctype html><html><head></head><body></body></html>")

    def normalise(value: str) -> str:
        return page.evaluate(
            "(v) => { const d = document.createElement('div'); d.style.color = v;"
            " document.body.appendChild(d); const c = getComputedStyle(d).color; d.remove(); return c; }",
            value,
        )

    return normalise


def test_browser_check_passes(request, tmp_path, monkeypatch):
    # S-508's session fixture, looked up in the test; its helpers are taken from the conftest module
    # pytest loaded, the one that defines the fixture's own AcceptanceInputs.
    inputs = request.getfixturevalue("acceptance_inputs")
    conftest = sys.modules[type(inputs).__module__]
    call, fill, read_tools = conftest.call, conftest.fill, conftest.read_tools

    from dashcheck import browser
    from dashcheck.browser import (
        DECLARED_FALLBACKS,
        DECLARED_SLICES,
        NO_SLICE_VAR,
        PAGE_ASSET,
        PALETTE,
        PROJECT_SWITCH_MARGIN,
        SIDES,
        SLICE_VARS,
        START_VARIANT,
        SWITCH_KINDS,
        declared_faults,
        palette_values,
        run,
        today_values,
    )
    from playwright.sync_api import sync_playwright
    from startcheck.ownenv import own_env
    from unitcheck.session import open_session

    from startcheck import runner
    from startcheck.runner import UnitCommand, restore, start, stop

    running = UnitCommand("running", inputs.running_install)
    rebuilt = UnitCommand("rebuilt", inputs.rebuilt_install)
    pristine_store = inputs.staged_store
    pristine_prompts = inputs.pristine_prompts
    check_root = str(tmp_path)
    env = own_env()
    before = snapshot(pristine_store)

    starts: list[tuple[str, str, object]] = []
    original = runner.start

    def recording_start(cmd, variant, root, own, launcher=None):
        starts.append((cmd.label, variant, launcher))
        return original(cmd, variant, root, own, launcher)

    monkeypatch.setattr(runner, "start", recording_start)
    if hasattr(browser, "start"):
        monkeypatch.setattr(browser, "start", recording_start)

    # 5, second half: no launcher is refused before anything is restored or started.
    with pytest.raises(ValueError):
        run(running, rebuilt, check_root, pristine_store, pristine_prompts, env, None)
    assert os.listdir(check_root) == [], os.listdir(check_root)
    assert starts == []

    session = open_session(env)
    try:
        launcher = CountingLauncher(session.launcher())

        # The project with the most entries, read once from the rebuilt unit on a restored copy.
        restore(check_root, pristine_store, pristine_prompts)
        unit = start(rebuilt, START_VARIANT, check_root, env, launcher)
        assert unit.outcome == "listening" and unit.port is not None, (unit.outcome, unit.stderr)
        try:
            largest, stored = largest_project(call, fill, read_tools, unit.port)
        finally:
            stop(unit)

        starts.clear()
        launcher.prepared.clear()
        report = run(running, rebuilt, check_root, pristine_store, pristine_prompts, env, launcher)
    finally:
        session.close()

    # 4. The timings table and every tokenless and declared fault line, written out before judging.
    lines = ["project\tkind\ttoday_median_s\trebuilt_median_s\tratio\tuntimed"]
    lines += [
        f"{t.project}\t{t.kind}\t{t.today_median_s}\t{t.rebuilt_median_s}\t{t.ratio}\t{', '.join(t.untimed)}"
        for t in report.timings
    ]
    lines += [f"tokenless: {f}" for f in report.tokenless_faults]
    lines += [f"declared: {f}" for f in report.declared_faults]
    out = tmp_path / REPORT_FILE
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written = out.read_text(encoding="utf-8")
    assert all(line in written for line in lines)

    # 5. Every start inside run went through the launcher, on START_VARIANT, one prepare each.
    assert starts, "run started no unit"
    assert all(variant == START_VARIANT for _, variant, _ in starts), starts
    assert all(given is launcher for _, _, given in starts), starts
    assert len(launcher.prepared) == len(starts), (launcher.prepared, starts)
    assert {label for label, _, _ in starts} == {"running", "rebuilt"}, starts

    # 1. A pass, with every fault tuple empty.
    for name in (
        "page_rule_faults",
        "empty_faults",
        "open_faults",
        "wiring_faults",
        "source_faults",
        "timing_faults",
        "tokenless_faults",
        "declared_faults",
    ):
        assert getattr(report, name) == (), (name, getattr(report, name))
    assert report.passed is True

    # 2. The rebuilt page file's palette block against today's page file, independently of the report.
    rebuilt_text = Path(inputs.rebuilt_install, "mu_spec", PAGE_ASSET).read_text(encoding="utf-8")
    today_text = Path(inputs.running_install, "mu_spec", PAGE_ASSET).read_text(encoding="utf-8")
    values, extra = palette_values(rebuilt_text)
    assert extra == [], extra
    fallbacks = {k for k in values if k[0] == "fallback"}
    slices = {k for k in values if k[0] == "slice"}
    assert fallbacks == {("fallback", t, s) for t in PALETTE for s in SIDES}
    assert len(fallbacks) == DECLARED_FALLBACKS == 26
    assert slices == {("slice", v, None) for v in (*SLICE_VARS, NO_SLICE_VAR)}
    assert len(slices) == DECLARED_SLICES == 11
    assert set(values) == fallbacks | slices
    with sync_playwright() as p:
        chromium = p.chromium.launch()
        try:
            normalise = normaliser(chromium.new_page())
            mine = {k: normalise(v) for k, v in values.items()}
            todays = {k: normalise(v) for k, v in today_values(today_text).items()}
        finally:
            chromium.close()
    assert declared_faults(mine, extra, todays) == []

    # 3. A row for every stored project and kind; the switch medians against today's.
    rows = {(t.project, t.kind): t for t in report.timings}
    assert set(rows) >= {(p, k) for p in stored for k in SWITCH_KINDS}, sorted(rows)
    head = rows[(largest, "project")]
    assert head.today_median_s is not None and head.rebuilt_median_s is not None, head
    assert head.rebuilt_median_s <= PROJECT_SWITCH_MARGIN * head.today_median_s, head
    for key, t in rows.items():
        if t.today_median_s is None or t.rebuilt_median_s is None:
            continue
        assert t.ratio is not None, t
        if key != (largest, "project"):
            assert t.rebuilt_median_s < t.today_median_s, t

    # 6. The staged copy is byte-unchanged.
    assert snapshot(pristine_store) == before
