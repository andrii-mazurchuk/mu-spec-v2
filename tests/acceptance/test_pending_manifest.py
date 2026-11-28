"""A pending manifest left by the running unit killed mid-save is settled as the running unit settles it (T-416).

A differential test: neither unit is imported by this process. The inputs come from S-508's
acceptance_inputs fixture; each unit is started alone by S-491's start, on a copy restored before
every start, and reached only through S-508's call at the routes GET /tools lists. The pending
manifest is made by this test's own launcher, which ends the running unit the moment it would roll a
pending manifest forward. Modules under test are imported inside the test, so a module not yet
written fails only the tests that need it.
"""

import http.client
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

START_VARIANT = "hhhhhh"  # every value healthy, reached on HEALTHY_PORT
PENDING = "manifest.pending.json"
PROJECT = "pending-check"
ACTOR = "pending-check"
KILL_STATUS = 9
END_WAIT_S = 15.0
T0 = 10**18
# Ends the process at once when a pending manifest is about to be renamed onto its manifest, then
# starts the unit from argv[1] as the runner's BOOTSTRAP does; argv[2] is the marker made first.
KILL = (
    "import os, runpy, sys\n"
    "def _guard(real):\n"
    "    def wrapped(src, dst, *args, **kwargs):\n"
    f"        if os.path.basename(os.fspath(src)) in ({PENDING!r}, {PENDING.encode()!r}):\n"
    f"            os._exit({KILL_STATUS})\n"
    "        return real(src, dst, *args, **kwargs)\n"
    "    return wrapped\n"
    "os.replace = _guard(os.replace)\n"
    "os.rename = _guard(os.rename)\n"
    "root, marker = sys.argv[1], sys.argv[2]\n"
    "del sys.argv[1:3]\n"
    "open(marker, 'wb').close()\n"
    "sys.path.insert(0, root)\n"
    "runpy.run_module({entry!r}, run_name='__main__', alter_sys=True)\n"
)


def snap(d: Path) -> dict[str, bytes]:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def snapm(d: Path) -> dict[str, tuple[bytes, int]]:
    return {p.relative_to(d).as_posix(): (p.read_bytes(), os.stat(p).st_mtime_ns)
            for p in sorted(d.rglob("*")) if p.is_file()}


def decode(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def strings(doc: object):
    if isinstance(doc, str):
        yield doc
    elif isinstance(doc, dict):
        for k, v in doc.items():
            yield from strings(k)
            yield from strings(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from strings(v)


class Tools:
    """The started unit's tools, as its GET /tools lists them, each called with a JSON body."""

    def __init__(self, call, fill, port: int):
        self.call, self.fill, self.port = call, fill, port
        status, raw = call(port, "GET", "/tools", None)
        assert status == 200, f"GET /tools answered {status}: {raw!r}"
        self.tools = {t["name"]: t for t in decode(raw)["tools"]}

    def __call__(self, name: str, **args: object) -> tuple[int, bytes]:
        spec = self.tools[name]
        if "actor" in spec.get("input_schema", {}).get("properties", {}):
            args.setdefault("actor", ACTOR)
        params = re.findall(r"\{(\w+)\}", spec["path"])
        path = self.fill(spec["path"], {p: str(args[p]) for p in params})
        rest = {k: v for k, v in args.items() if k not in params}
        method = spec["method"].upper()
        return self.call(self.port, method, path, rest if rest or method != "GET" else None)


class KillLauncher:
    """A Launcher whose unit dies exactly when a pending manifest would be rolled forward."""

    def __init__(self, marker: Path):
        self.marker = marker

    def prepare(self, cmd, check_root):
        from startcheck.runner import ENTRY_MODULE, LAUNCH_FLAGS, POLL_S, Prepared

        marker = self.marker

        def ready(seconds: float) -> bool:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if marker.exists():
                    return True
                time.sleep(POLL_S)
            return marker.exists()

        argv = (sys.executable, *LAUNCH_FLAGS, "-c", KILL.replace("{entry!r}", repr(ENTRY_MODULE)),
                cmd.install_root, str(marker))
        return Prepared(argv, {}, ready, lambda: None)


def test_pending_settled_as_running_unit(request, tmp_path, record_property):
    # S-508's session fixture, looked up in the test after its conftest is imported
    from tests.acceptance.conftest import call, fill

    inputs = request.getfixturevalue("acceptance_inputs")

    from startcheck.ownenv import own_env
    from unitcheck.session import open_session

    from startcheck.runner import STORE, UnitCommand, alive, restore, start, stop

    root = tmp_path / "check"
    root.mkdir()
    PL = root / STORE / "projects"
    env = own_env()
    session = open_session(env)
    units = {"running": UnitCommand("running", inputs.running_install),
             "rebuilt": UnitCommand("rebuilt", inputs.rebuilt_install)}

    def settled(label: str, store: str) -> tuple[bytes, dict[str, bytes]]:
        restore(str(root), store, inputs.pristine_prompts)
        unit = start(units[label], START_VARIANT, str(root), env, session.launcher())
        try:
            assert unit.outcome == "listening" and unit.port is not None, (label, unit.outcome, unit.stderr)
            status, listed = Tools(call, fill, unit.port)("list_projects")
            assert status == 200, f"list_projects on {label} answered {status}: {listed!r}"
        finally:
            stop(unit)
        return listed, snap(PL)

    try:
        # Arrange 1: the running unit killed between writing a pending manifest and rolling it forward
        restore(str(root), inputs.staged_store, inputs.pristine_prompts)
        B = snap(PL)
        killer = KillLauncher(tmp_path / "kill.marker")
        unit = start(units["running"], START_VARIANT, str(root), env, killer)
        unanswered = None
        try:
            assert unit.outcome == "listening" and unit.port is not None, (unit.outcome, unit.stderr)
            tool = Tools(call, fill, unit.port)
            steps = [
                ("post_request", {"type": "initiate", "title": "pending check", "project": PROJECT}),
                ("create_project", {"project": PROJECT}),
                ("submit_amendment", {"project": PROJECT, "entries": [{"layer": "I", "title": "pending"}]}),
            ]
            rid = None
            for name, args in steps:
                if rid is not None:
                    args["in_response_to"] = rid
                try:
                    status, raw = tool(name, **args)
                except (http.client.RemoteDisconnected, ConnectionError):
                    unanswered = name
                    break
                if name == "post_request":
                    rid = next((t for t in strings(decode(raw)) if t.startswith("msg-")), None)
                    assert rid is not None, f"post_request answered {status}: {raw!r}"
            deadline = time.monotonic() + END_WAIT_S
            while alive(unit) and time.monotonic() < deadline:
                time.sleep(0.1)
        finally:
            stop(unit)
        assert unanswered is not None, "every call was answered: no save rolled a pending manifest forward"
        assert unit._proc.returncode == KILL_STATUS, unit._proc.returncode
        holders = [d for d in sorted(PL.iterdir()) if d.is_dir() and (d / PENDING).is_file()]
        assert len(holders) == 1, f"pending manifests in {holders!r}"
        Dp = holders[0]
        pending = (Dp / PENDING).read_bytes()
        beside = [p for p in Dp.iterdir() if p.is_file() and p.name != PENDING]
        assert all(p.read_bytes() != pending for p in beside), "the pending manifest equals its manifest"

        # Arrange 2: the three stores
        T1 = tmp_path / "T1"
        shutil.copytree(PL, T1 / "projects")
        T2 = tmp_path / "T2"
        shutil.copytree(T1, T2)
        new_files = sorted(
            p.relative_to(PL).as_posix() for p in Dp.rglob("*")
            if p.is_file() and p.name != PENDING and not p.name.startswith(".")
            and B.get(p.relative_to(PL).as_posix()) != p.read_bytes()
        )
        assert new_files, f"the killed save left no new or changed file in {Dp.name}"
        (T2 / "projects" / new_files[0]).unlink()
        T3 = Path(inputs.staged_store)
        assert not any(p.name == PENDING for p in (T3 / "projects").rglob("*")), "the staged store holds a pending manifest"

        # Act 1: each settled by both units' own start
        outcomes = {}
        for T in (T1, T2):
            listed_running, S_running = settled("running", str(T))
            listed_rebuilt, S_rebuilt = settled("rebuilt", str(T))
            assert not any(Path(k).name == PENDING for k in S_running), T.name
            assert not any(Path(k).name == PENDING for k in S_rebuilt), T.name
            assert sorted(S_rebuilt) == sorted(S_running), T.name
            differ = sorted(k for k in S_running if S_running[k] != S_rebuilt[k])
            assert differ == [], f"{T.name}: files settled otherwise: {differ!r}"
            assert listed_rebuilt == listed_running, T.name
            outcomes[T.name] = S_running
        settled_manifests = [k for k, v in outcomes["T2"].items()
                             if Path(k).parent.name == Dp.name and v == pending]
        report = "rolled forward" if settled_manifests else "discarded"
        record_property("T2 running unit", report)
        print(f"T2: the running unit's settled manifest {report} the pending manifest")

        # Act 2: a store holding no pending manifest is untouched by start-up
        restore(str(root), str(T3), inputs.pristine_prompts)
        for f in PL.rglob("*"):
            if f.is_file():
                os.utime(f, ns=(T0, T0))
        before = snapm(PL)
        unit = start(units["rebuilt"], START_VARIANT, str(root), env, session.launcher())
        try:
            assert unit.outcome == "listening", (unit.outcome, unit.stderr)
        finally:
            stop(unit)
        after = snapm(PL)
        assert sorted(after) == sorted(before)
        changed = sorted(k for k in before if after[k] != before[k])
        assert changed == [], f"start-up changed {changed!r}"
    finally:
        session.close()
