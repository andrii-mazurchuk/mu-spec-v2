"""In process over a working copy of the acceptance store: every read through a ReadRuntime equals dispatch (S-454).

T-502 pins, for every stamped project of the copy, that each bundle read, an entry read and a layer
read answered by run_read on a ReadRuntime over the project's snapshot equal the same tool
dispatched on the real runtime over the same copy, after masking the one clock-written value
(computed_at). Both sides share one Ship part: one marker, one nonce, one cached identity. The copy
is never written by a read. An in-process check: it launches no unit, never touches sys.path, and
imports the rebuilt mu_spec only, inside the test.

The basename carries the _inprocess suffix: tests/dash/test_readsource.py shares the stem, and with
no __init__.py under tests/ two files of one basename collide at collection.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MASKED_KEY = "computed_at"  # the one clock-written read value (ruling on iss-2386)
MIN_PAIRS_PER_PROJECT = 16
LAYER = "B"
COLLECT_DIRS = ("tests/dash", "tests/acceptance/inprocess")


def json_type(value) -> str:
    """The JSON type name of a decoded JSON value."""
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
    """value with every value under a key named MASKED_KEY replaced by its JSON type name."""
    if isinstance(value, dict):
        return {k: json_type(v) if k == MASKED_KEY else masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [masked(v) for v in value]
    return value


def mask(b: bytes):
    return masked(json.loads(b))


def files_state(root: Path) -> dict:
    """Each file under root: its bytes and its mtime."""
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_read_runtime_equals_dispatch(acceptance_inputs, tmp_path, monkeypatch):
    from mu_spec.codec import CUT_LOG_NAME, encode_log_line
    from mu_spec.dash.readsource import ReadRuntime, run_read
    from mu_spec.dash.snapshot import SHIP_EVENT_KINDS, take_snapshot
    from mu_spec.dash.stamps import Stamps, canonical
    from mu_spec.dash.views import BUNDLE, ENTRY_TOOL, LAYER_TOOL
    from mu_spec.registry import build_runtime
    from mu_spec.ship.part import build_ship
    from mu_spec.tools.catalogue import dispatch

    from mu_spec.outside import ENV_NAMES

    # Arrange: a working copy of the staged store; the runtime over it; one shared Ship part
    state = tmp_path / "state"
    shutil.copytree(acceptance_inputs.staged_store, state)
    monkeypatch.setenv(ENV_NAMES["state_dir"], str(state))
    stamps = Stamps()
    runtime = build_runtime(ship_builder=build_ship, observer=stamps)
    stamps.attach(runtime.store.read)
    ship = runtime.ship
    before = files_state(state)

    spine_names = [name for name in BUNDLE if BUNDLE[name][0] == "get_spine"]
    assert len(spine_names) == 1, spine_names

    failures: list[str] = []
    pairs = 0
    stamped: list[str] = []
    with_ship_event: list[str] = []
    with_cut_line: list[str] = []

    def compare(p, label, snap, tool, inputs):
        got = run_read(ReadRuntime(snap, runtime.config, runtime.clock), tool, inputs)
        o = dispatch(runtime, tool, {"project": p, **inputs})
        want = canonical({"status": o.status, "body": o.body})
        if mask(got) != mask(want):
            failures.append(f"{p} {label}: run_read {got[:300]!r} != dispatch {want[:300]!r}")
        return o

    # Act
    for p in runtime.store.project_names():
        events = tuple(
            encode_log_line(record) for record in runtime.store.read_log(project=p) if record.kind in SHIP_EVENT_KINDS
        )
        try:
            snap = take_snapshot(p, stamps, ship.marked_view, events, ship.identity, ship.progress_view)
        except KeyError:
            continue  # no project stamp
        stamped.append(p)
        if events:
            with_ship_event.append(p)
        record = runtime.store.read(p)
        cut = record.logs.get(CUT_LOG_NAME) if record is not None else None
        if cut is not None and cut.lines:
            with_cut_line.append(p)

        spine = None
        for name in BUNDLE:
            tool, inputs = BUNDLE[name]
            o = compare(p, name, snap, tool, dict(inputs))
            pairs += 1
            if name == spine_names[0]:
                spine = o
        assert spine is not None
        rows = spine.body.get("spine") if 200 <= spine.status < 300 else None
        if rows:
            compare(p, f"{ENTRY_TOOL} {rows[0]['id']}", snap, ENTRY_TOOL, {"id": rows[0]["id"]})
            pairs += 1
        compare(p, f"{LAYER_TOOL} {LAYER}", snap, LAYER_TOOL, {"layer": LAYER})
        pairs += 1

    # 1. every pair equal after the mask
    assert stamped, "the acceptance copy holds no stamped project"
    assert not failures, "\n".join(failures)

    # 2. at least 16 pairs per stamped project
    assert pairs >= MIN_PAIRS_PER_PROJECT * len(stamped), (pairs, stamped)

    # 3. a Ship event and a cut-log line were compared
    absent = []
    if not with_ship_event:
        absent.append("no compared project holds a tickets_emitted or ship_failed record")
    if not with_cut_line:
        absent.append("no compared project holds a cut-log line")
    assert not absent, "; ".join(absent)

    # 4. no file under state changed
    after = files_state(state)
    assert sorted(after) == sorted(before), sorted(set(after) ^ set(before))
    changed = [name for name in before if before[name] != after[name]]
    assert not changed, changed

    # 5. collecting tests/dash and tests/acceptance/inprocess together: no collection error for this file
    root = acceptance_inputs.rebuilt_install
    here = os.path.basename(__file__)
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *COLLECT_DIRS],
        check=False,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = collected.stdout + collected.stderr
    errors = [
        line for line in output.splitlines()
        if here in line and ("ERROR" in line or "mismatch" in line)
    ]
    assert not errors, output
    assert f"{here}::test_read_runtime_equals_dispatch" in output, output
