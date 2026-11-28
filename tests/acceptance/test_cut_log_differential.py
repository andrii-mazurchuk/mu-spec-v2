"""A cut on a fork appends one line to one file and leaves the rest untouched, as on the running unit (T-419).

A differential test: neither unit is imported by this process. The inputs come from S-519's
acceptance_inputs fixture; each unit is started alone by S-491's start through the shared launch, on
a copy restored from the staged store, and reached only through S-519's call: tools at the routes
GET /tools lists, the cut at the route the dashboard's route table (ACTION_ROUTES in
dashboard/src/actions.ts, read by running bun) declares for cut_units. Values that are times of call
agree when each lies in its own run's clock window. Modules under test are imported inside the test,
so a module not yet written fails only the tests that need it.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

START_VARIANT = "hhhhhh"  # every value healthy, reached on HEALTHY_PORT
ACTOR = "cut-check"
F = "cut-check"
PROBE = "cut-probe"
EMPTY = "cut-empty"
T0 = 10**18
ROUTE_TABLE = Path("dashboard") / "src" / "actions.ts"
PLACEHOLDER = "zz9cutzz9"  # a project name the route table's path function is given, then put back as {project}
CUT_BODY = {"note": ""}  # what the running unit's page sends for its cut action


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


def listing(raw: bytes) -> list:
    doc = decode(raw)
    lists = [v for v in doc.values() if isinstance(v, list)] if isinstance(doc, dict) else [doc]
    assert len(lists) == 1 and isinstance(lists[0], list), f"the answer holds no single listing: {raw!r}"
    return lists[0]


def cut_route(rebuilt_install: str) -> tuple[str, str]:
    """The cut action's method and relative path pattern, as the dashboard's route table declares them."""
    table = Path(rebuilt_install) / ROUTE_TABLE
    assert table.is_file(), f"{ROUTE_TABLE} is absent"
    script = (
        f"const {{ ACTION_ROUTES }} = await import({json.dumps(table.as_uri())});"
        f"const r = ACTION_ROUTES.cut_units;"
        f"console.log(JSON.stringify({{ method: r.method, path: r.path({json.dumps(PLACEHOLDER)}) }}));"
    )
    done = subprocess.run(["bun", "-e", script], check=False, cwd=table.parent.parent, capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr
    route = json.loads(done.stdout.strip().splitlines()[-1])
    return str(route["method"]).upper(), str(route["path"]).replace(PLACEHOLDER, "{project}")


def named(raw: bytes, *names: str) -> bytes:
    """raw with each project name replaced, so two projects' answers compare by content."""
    for n in names:
        raw = raw.replace(n.encode("utf-8"), b"<project>")
    return raw


# --- the clock rule ---------------------------------------------------------------------------

def _load(text: bytes) -> object:
    return json.loads(text, object_pairs_hook=lambda pairs: ("obj", pairs),
                      parse_int=lambda t: ("num", t), parse_float=lambda t: ("num", t))


def _in(window: tuple[float, float], number: str) -> bool:
    return window[0] <= float(number) <= window[1]


def _walk(x: object, y: object, wx, wy) -> bool:
    if isinstance(x, tuple) and isinstance(y, tuple):
        if x[0] == "num" and y[0] == "num":
            return x[1] == y[1] or (_in(wx, x[1]) and _in(wy, y[1]))
        if x[0] == "obj" and y[0] == "obj":
            return ([k for k, _ in x[1]] == [k for k, _ in y[1]]
                    and all(_walk(a, b, wx, wy) for (_, a), (_, b) in zip(x[1], y[1])))
        return False
    if isinstance(x, list) and isinstance(y, list):
        return len(x) == len(y) and all(_walk(a, b, wx, wy) for a, b in zip(x, y))
    return type(x) is type(y) and x == y


def agree(a: bytes, b: bytes, wa, wb) -> bool:
    """Two JSON texts agree when equal, or equal but for numbers each in its own run's clock window."""
    if a == b:
        return True
    try:
        return _walk(_load(a), _load(b), wa, wb)
    except ValueError:
        return False


def agree_lines(a: bytes, b: bytes, wa, wb) -> bool:
    la, lb = a.split(b"\n"), b.split(b"\n")
    return len(la) == len(lb) and all(agree(x, y, wa, wb) for x, y in zip(la, lb))


# --- the units --------------------------------------------------------------------------------

class Tools:
    """The started unit's tools, as its GET /tools lists them, each called with a JSON body."""

    def __init__(self, call, fill, port: int, cut: tuple[str, str]):
        self.call, self.fill, self.port, self.cut_route = call, fill, port, cut
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

    def ok(self, name: str, **args: object) -> bytes:
        status, raw = self(name, **args)
        assert 200 <= status < 300, f"{name} {args!r} answered {status}: {raw!r}"
        return raw

    def request_id(self, title: str, project: str) -> str:
        raw = self.ok("post_request", type="initiate", title=title, project=project)
        rid = next((s for s in strings(decode(raw)) if s.startswith("msg-")), None)
        assert rid is not None, f"post_request names no request: {raw!r}"
        return rid

    def fork(self, source: str, name: str) -> None:
        rid = self.request_id("cut check", name)
        self.ok("fork_project", project=source, name=name, in_response_to=rid)

    def cut(self, project: str) -> tuple[int, bytes]:
        method, pattern = self.cut_route
        return self.call(self.port, method, "/" + self.fill(pattern, {"project": project}).lstrip("/"), CUT_BODY)


def files_of(d: Path) -> list[Path]:
    return sorted(p for p in d.rglob("*") if p.is_file())


def test_cut_appends_only_and_fork_starts_empty(request, tmp_path, record_property):
    # S-519's session fixture, looked up in the test; its helpers are taken from the conftest module
    # that defined it, whatever name pytest imported that module under
    inputs = request.getfixturevalue("acceptance_inputs")
    conftest = sys.modules[type(inputs).__module__]
    call, fill = conftest.call, conftest.fill

    from startcheck.ownenv import own_env
    from unitcheck.session import open_session

    from startcheck.runner import STORE, UnitCommand, restore, start, stop

    route = cut_route(inputs.rebuilt_install)
    root = tmp_path / "check"
    root.mkdir()
    PL = root / STORE / "projects"
    env = own_env()
    session = open_session(env)
    installs = {"running": inputs.running_install, "rebuilt": inputs.rebuilt_install}

    def started(label: str, clock: dict | None = None):
        restore(str(root), inputs.staged_store, inputs.pristine_prompts)
        if clock is not None:
            clock["w0"] = time.time()
        unit = start(UnitCommand(label, installs[label]), START_VARIANT, str(root), env, session.launcher())
        if unit.outcome != "listening" or unit.port is None:
            stop(unit)
            raise AssertionError((label, unit.outcome, unit.stderr))
        return unit

    try:
        # choosing the source, on the running unit only, a fresh copy for each try
        unit = started("running")
        try:
            projects = [p if isinstance(p, str) else p["name"]
                        for p in listing(Tools(call, fill, unit.port, route).ok("list_projects"))]
        finally:
            stop(unit)
        S = None
        for candidate in projects:
            unit = started("running")
            try:
                tool = Tools(call, fill, unit.port, route)
                tool.fork(candidate, PROBE)
                cuts_s = tool.ok("list_cuts", project=candidate)
                cuts_probe = tool.ok("list_cuts", project=PROBE)
                rid = tool.request_id("cut empty", EMPTY)
                tool.ok("create_project", project=EMPTY, in_response_to=rid)
                modules_s = tool.ok("list_modules", project=candidate)
                modules_empty = tool.ok("list_modules", project=EMPTY)
            finally:
                stop(unit)
            if (named(cuts_s, candidate) != named(cuts_probe, PROBE)
                    and named(modules_s, candidate) != named(modules_empty, EMPTY)):
                S = candidate
                break
        assert S is not None, "no acceptance project has cuts its fork does not carry and declared modules"
        record_property("source", S)
        print(f"cut check source: {S}")

        seen = {}
        for label in ("running", "rebuilt"):
            clock: dict = {}
            unit = started(label, clock)
            try:
                tool = Tools(call, fill, unit.port, route)
                tool.fork(S, F)
                dF = PL / F
                names0 = sorted(p.relative_to(dF).as_posix() for p in files_of(dF))
                for f in files_of(dF):
                    os.utime(f, ns=(T0, T0))
                S0 = {p.relative_to(dF).as_posix(): p.read_bytes() for p in files_of(dF)}
                before = tool("list_cuts", project=F)[1]
                source_cuts = tool("list_cuts", project=S)[1]
                em = tool("get_emission", project=F)[1]
                cut = tool.cut(F)
                S1 = {p.relative_to(dF).as_posix(): (p.read_bytes(), os.stat(p).st_mtime_ns) for p in files_of(dF)}
                after = tool("list_cuts", project=F)[1]
            finally:
                stop(unit)
            w1 = time.time()
            seen[label] = {"names0": names0, "S0": S0, "S1": S1, "before": before, "source_cuts": source_cuts,
                           "em": em, "cut": cut, "after": after, "window": (clock["w0"] - 1, w1 + 1)}
    finally:
        session.close()

    run, reb = seen["running"], seen["rebuilt"]
    wr, wb = run["window"], reb["window"]
    # 1. the fork leaves the same files and answers alike on both units
    assert reb["names0"] == run["names0"]
    assert agree(run["before"], reb["before"], wr, wb), (run["before"], reb["before"])
    assert agree(run["em"], reb["em"], wr, wb), (run["em"], reb["em"])
    # 2. preconditions from the running unit
    assert named(run["before"], F) != named(run["source_cuts"], S), "the fork answers its source's cuts"
    assert 200 <= run["cut"][0] < 300, run["cut"]
    assert run["after"] != run["before"], "the cut changed no cut list on the running unit"
    # 3. one file changed or appeared, by an append; every other file untouched
    changed = {}
    for label, u in seen.items():
        moved = sorted(k for k, (data, _) in u["S1"].items() if u["S0"].get(k) != data)
        assert len(moved) == 1, f"{label}: the cut changed {moved!r}"
        k = moved[0]
        if k in u["S0"]:
            assert u["S1"][k][0].startswith(u["S0"][k]), f"{label}: {k} was rewritten, not appended to"
        for other, data in u["S0"].items():
            if other != k:
                assert u["S1"].get(other) == (data, T0), f"{label}: {other} was touched by the cut"
        changed[label] = k
    assert changed["rebuilt"] == changed["running"]
    # 4. the same answers and the same stored line
    assert reb["cut"][0] == run["cut"][0]
    assert agree(run["cut"][1], reb["cut"][1], wr, wb), (run["cut"], reb["cut"])
    assert agree(run["after"], reb["after"], wr, wb), (run["after"], reb["after"])
    k = changed["running"]
    assert agree_lines(run["S1"][k][0], reb["S1"][k][0], wr, wb), (run["S1"][k][0], reb["S1"][k][0])
