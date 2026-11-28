"""mu_spec.dash.readsource over the acceptance store: a ReadRuntime answers as dispatch (S-454).

T-452 pins, in process over a working copy of the acceptance store, that on every stamped project
each bundle read, an entry read and a layer read through run_read on a ReadRuntime over the
project's snapshot equal the same tool dispatched on the real runtime over the same copy, after
masking computed_at, that the copy holds a Ship event and a cut-log line among the compared
projects, and that nothing under the copy is written. It launches no unit and imports the rebuilt
mu_spec only. Modules under test are imported inside the test, so a module not yet written fails
only the tests that need it.
"""

import json
import os
import shutil
from pathlib import Path

PAIRS_PER_PROJECT = 16
MASKED_KEY = "computed_at"  # the one clock-written read value (ruling on iss-2386)
SHIP_HISTORY_KINDS = ("tickets_emitted", "ship_failed")


def type_name(value) -> str:
    """value's JSON type name."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def masked(value):
    if isinstance(value, dict):
        return {k: type_name(v) if k == MASKED_KEY else masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [masked(v) for v in value]
    return value


def mask(b: bytes):
    """b decoded, every value under a key named computed_at replaced by its JSON type name."""
    return masked(json.loads(b))


def files_of(root: Path) -> dict:
    """Each file under root, by relative path: its bytes and mtime."""
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_read_runtime_equals_dispatch(acceptance_inputs, tmp_path, monkeypatch):
    from mu_spec.codec import CUT_LOG_NAME, encode_log_line, record_path
    from mu_spec.dash.readsource import ReadRuntime, run_read
    from mu_spec.dash.snapshot import SHIP_EVENT_KINDS, take_snapshot
    from mu_spec.dash.stamps import Stamps, canonical, project_key
    from mu_spec.dash.views import BUNDLE, ENTRY_TOOL, LAYER_TOOL
    from mu_spec.outside import ENV_NAMES
    from mu_spec.registry import build_runtime
    from mu_spec.ship.part import build_ship
    from mu_spec.tools.catalogue import dispatch

    # Arrange: a working copy of the staged store, the real runtime over it, one Ship part for both sides
    state = tmp_path / "state"
    shutil.copytree(acceptance_inputs.staged_store, state)
    monkeypatch.setenv(ENV_NAMES["state_dir"], str(state))
    stamps = Stamps()
    runtime = build_runtime(ship_builder=build_ship, observer=stamps)
    stamps.attach(runtime.store.read)
    ship = runtime.ship
    before = files_of(state)

    def stamped(p: str) -> bool:
        try:
            return stamps.get(project_key(p)) is not None
        except KeyError:
            return False

    projects = [p for p in runtime.store.project_names() if stamped(p)]
    assert projects, f"no stamped project in the copy of {acceptance_inputs.staged_store!r}"

    failures: list[str] = []
    compared = 0
    with_ship_event: list[str] = []
    with_cut_line: list[str] = []

    def compare(p: str, label: str, rt, tool: str, inputs: dict) -> bytes:
        nonlocal compared
        got = run_read(rt, tool, inputs)
        o = dispatch(runtime, tool, {"project": p, **inputs})
        want = canonical({"status": o.status, "body": o.body})
        compared += 1
        if mask(got) != mask(want):
            failures.append(f"{p} {label}: run_read {got[:200]!r} != dispatch {want[:200]!r}")
        return want

    # Act
    for p in projects:
        log = runtime.store.read_log(project=p)
        events = tuple(encode_log_line(record) for record in log if record.kind in SHIP_EVENT_KINDS)
        if any(record.kind in SHIP_HISTORY_KINDS for record in log):
            with_ship_event.append(p)
        cut_log = record_path(runtime.config.root, p).parent / CUT_LOG_NAME
        if cut_log.is_file() and cut_log.read_bytes().strip():
            with_cut_line.append(p)
        snap = take_snapshot(p, stamps, ship.marked_view, events, ship.identity, ship.progress_view)
        rt = ReadRuntime(snap, runtime.config, runtime.clock)

        spine_ids: list = []
        for n in BUNDLE:
            tool, inputs = BUNDLE[n]
            want = compare(p, n, rt, tool, dict(inputs))
            if tool == "get_spine" and not spine_ids:
                body = json.loads(want).get("body")
                if isinstance(body, dict) and isinstance(body.get("spine"), list):
                    spine_ids = [row["id"] for row in body["spine"] if isinstance(row, dict) and "id" in row]
        if spine_ids:
            compare(p, f"{ENTRY_TOOL} {spine_ids[0]}", rt, ENTRY_TOOL, {"id": spine_ids[0]})
        compare(p, f"{LAYER_TOOL} B", rt, LAYER_TOOL, {"layer": "B"})

    # 1. every pair equal after the mask
    assert not failures, "\n".join(failures)

    # 2. at least the bundle's worth of pairs per stamped project
    assert compared >= PAIRS_PER_PROJECT * len(projects), (compared, len(projects))

    # 3. the copy's Ship history and cut log were among the compared projects
    absent = []
    if not with_ship_event:
        absent.append("no compared project holds a tickets_emitted or ship_failed record")
    if not with_cut_line:
        absent.append("no compared project holds a cut-log line")
    assert not absent, "; ".join(absent)

    # 4. nothing under the copy changed
    after = files_of(state)
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    assert not changed, f"files under {os.fspath(state)!r} changed: {changed}"
