"""Reads are stable across hash seeds, and project names come in code-point order (T-408).

A differential test: neither unit is imported by this process. The inputs come from S-519's
acceptance_inputs fixture; each unit is started alone by S-491's start through the shared launch, on
the same healthy variant and own environment, and reached only through S-519's call. The rebuilt
unit seeds three projects under hash seed 0; the seeded store is then read twice, under seed 0 and
under a seed s1 for which a set of the fixture's ids iterates differently, and every read must agree
(epoch numbers the clock wrote during a run compared by position and type only). Modules under test
are imported inside the test, so a module not yet written fails only the tests that need it.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time

START_VARIANT = "hhhhhh"  # every value healthy, reached on HEALTHY_PORT
ACTOR = "order-stable"
PROJECTS = ("seed-b", "seed-a", "Seed-C")  # creation order; code point order is Seed-C, seed-a, seed-b
SEEDED = "Seed-C"
BEHAVIOURS = 8
SEED_LIMIT = 1000
MAX_VALUES = 3
PRECONDITION_TOOLS = ("get_spine", "get_waves", "check_gates", "list_issues")
PROBE = "import json, sys\ns = set()\nfor i in json.loads(sys.argv[1]):\n    s.add(i)\nprint(json.dumps(list(s)))\n"


def decode(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def strings(doc: object):
    """Every string in a decoded answer, keys included, in document order."""
    if isinstance(doc, str):
        yield doc
    elif isinstance(doc, dict):
        for k, v in doc.items():
            yield from strings(k)
            yield from strings(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from strings(v)


def keyed(doc: object, key: str):
    """Every string that is the value of a key named key, anywhere in a decoded answer, in document order."""
    if isinstance(doc, dict):
        for k, v in doc.items():
            if k == key and isinstance(v, str):
                yield v
            yield from keyed(v, key)
    elif isinstance(doc, list):
        for v in doc:
            yield from keyed(v, key)


def admitted(status: int) -> bool:
    return 200 <= status < 300


def project_names(raw: bytes) -> list[str]:
    """The project names of a list_projects answer, in listed order."""
    doc = decode(raw)
    lists = [v for v in doc.values() if isinstance(v, list)] if isinstance(doc, dict) else [doc]
    assert len(lists) == 1 and isinstance(lists[0], list), f"list_projects holds no single listing: {raw!r}"
    return [p if isinstance(p, str) else p["name"] for p in lists[0]]


class Writer:
    """Calls the started unit's tools at the method and path its GET /tools lists."""

    def __init__(self, call, fill, port: int):
        self.call, self.fill, self.port = call, fill, port
        status, raw = call(port, "GET", "/tools", None)
        assert status == 200, f"GET /tools answered {status}: {raw!r}"
        self.tools = {t["name"]: t for t in decode(raw)["tools"]}
        self.keys = 0

    def tool(self, name: str, **args: object) -> tuple[int, bytes]:
        spec = self.tools[name]
        declared = spec.get("input_schema", {}).get("properties", {})
        if "actor" in declared:
            args["actor"] = ACTOR
        if "idempotency_key" in declared:
            self.keys += 1
            args["idempotency_key"] = f"os-{self.keys}"
        params = re.findall(r"\{(\w+)\}", spec["path"])
        path = self.fill(spec["path"], {p: str(args[p]) for p in params})
        rest = {k: v for k, v in args.items() if k not in params}
        method = spec["method"].upper()
        return self.call(self.port, method, path, rest if rest or method != "GET" else None)

    def must(self, name: str, **args: object) -> bytes:
        status, raw = self.tool(name, **args)
        assert admitted(status), f"{name} {args!r} answered {status}: {raw!r}"
        return raw


def created(raw: bytes, n: int) -> list[str]:
    body = decode(raw)
    ids = body.get("created") if isinstance(body, dict) else None
    assert isinstance(ids, list) and len(ids) == n and all(isinstance(i, str) for i in ids), (
        f"the amendment answer lists no {n} created ids: {raw!r}")
    return ids


def seed_store(w: Writer) -> list[str]:
    """Projects 'seed-b', 'seed-a', 'Seed-C', each with I, eight behaviours of I and an issue on each; IDS."""
    ids_of = {}
    for p in PROJECTS:
        raw = w.must("post_request", type="initiate", title=f"order stable {p}", project=p)
        mid = next((s for s in strings(decode(raw)) if s.startswith("msg-")), None)
        assert mid is not None, f"post_request answer names no request id: {raw!r}"
        w.must("create_project", project=p, in_response_to=mid)
        raw = w.must("submit_amendment", project=p, in_response_to=mid,
                     entries=[{"layer": "I", "title": "order intent", "body": "the intent the behaviours serve"}])
        (intent,) = created(raw, 1)
        raw = w.must("submit_amendment", project=p, in_response_to=mid, entries=[
            {"layer": "B", "title": f"order behaviour {n}", "body": "a behaviour deriving from the intent",
             "derives_from": [intent]} for n in range(BEHAVIOURS)])
        ids_of[p] = created(raw, BEHAVIOURS)
        for b in ids_of[p]:
            w.must("raise_issue", project=p, target=b, kind="additive", claim=f"order issue on {b}")
    return ids_of[SEEDED]


def probe(env: dict[str, str], seed: int, ids: list[str]) -> list[str]:
    from startcheck.runner import HASH_SEED_NAME

    out = subprocess.run([sys.executable, "-s", "-c", PROBE, json.dumps(ids)], env={**env, HASH_SEED_NAME: str(seed)},
                         capture_output=True, check=True)
    return json.loads(out.stdout)


def files_under(root: str) -> dict[str, bytes]:
    found = {}
    for here, _, names in os.walk(root):
        for name in names:
            path = os.path.join(here, name)
            with open(path, "rb") as f:
                found[os.path.relpath(path, root)] = f.read()
    return found


def kept(raw: bytes) -> object:
    """A body decoded with key order and every number's stored digits kept."""
    return json.loads(raw, object_pairs_hook=lambda pairs: ("obj", pairs),
                      parse_int=lambda s: ("num", s), parse_float=lambda s: ("num", s))


def is_num(v: object) -> bool:
    return isinstance(v, tuple) and len(v) == 2 and v[0] == "num"


def is_obj(v: object) -> bool:
    return isinstance(v, tuple) and len(v) == 2 and v[0] == "obj"


def first_difference(a: object, b: object, windows: tuple[tuple[float, float], tuple[float, float]],
                     at: str = "$") -> str | None:
    """The first position where two kept trees differ, a clock position never differing; None when they agree."""
    if is_num(a) and is_num(b):
        (lo0, hi0), (lo1, hi1) = windows
        if lo0 <= float(a[1]) <= hi0 and lo1 <= float(b[1]) <= hi1:
            return None
        return None if a[1] == b[1] else f"{at}: number {a[1]} vs {b[1]}"
    if is_obj(a) and is_obj(b):
        ka, kb = [k for k, _ in a[1]], [k for k, _ in b[1]]
        if ka != kb:
            return f"{at}: keys {ka!r} vs {kb!r}"
        for (k, va), (_, vb) in zip(a[1], b[1]):
            d = first_difference(va, vb, windows, f"{at}.{k}")
            if d is not None:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{at}: array length {len(a)} vs {len(b)}"
        for i, (va, vb) in enumerate(zip(a, b)):
            d = first_difference(va, vb, windows, f"{at}[{i}]")
            if d is not None:
                return d
        return None
    if is_num(a) or is_num(b) or is_obj(a) or is_obj(b) or isinstance(a, list) or isinstance(b, list):
        return f"{at}: {a!r} vs {b!r}"
    if type(a) is not type(b) or a != b:
        return f"{at}: {a!r} vs {b!r}"
    return None


def disagreement(one: tuple[int, bytes], two: tuple[int, bytes],
                 windows: tuple[tuple[float, float], tuple[float, float]]) -> str | None:
    (s0, b0), (s1, b1) = one, two
    if s0 != s1:
        return f"status {s0} vs {s1}"
    if b0 == b1:
        return None
    try:
        t0, t1 = kept(b0), kept(b1)
    except ValueError:
        return "a body does not decode"
    return first_difference(t0, t1, windows)


def test_reads_stable_across_hash_seeds(request, tmp_path):
    # S-519's session fixture, looked up in the test after its conftest is imported: a conftest
    # not yet written then fails this test as a missing module, not its setup.
    from tests.acceptance.conftest import call, fill, read_calls, read_tools

    inputs = request.getfixturevalue("acceptance_inputs")

    from startcheck.ownenv import own_env
    from unitcheck.session import open_session

    from startcheck.runner import UnitCommand, restore, start, stop
    from startcheck.variants import PASS_THROUGH, STORE

    root = tmp_path / "check"
    root.mkdir()
    store = os.path.join(str(root), STORE)
    seeded = tmp_path / "seeded"
    env = own_env()
    session = open_session(env)

    def started(cmd, pristine_store):
        restore(str(root), pristine_store, inputs.pristine_prompts)
        unit = start(cmd, START_VARIANT, str(root), env, session.launcher())
        assert unit.outcome == "listening" and unit.port is not None, (cmd, unit.outcome, unit.stderr)
        return unit

    runs = {}
    try:
        # 1. the read set, from the running unit's discovery
        unit = started(UnitCommand("running", inputs.running_install), inputs.staged_store)
        try:
            status, raw = call(unit.port, "GET", "/tools", None)
            assert status == 200, f"GET /tools of the running unit answered {status}: {raw!r}"
            rt = read_tools(json.loads(raw))
        finally:
            stop(unit)

        # 2. the fixture, written by the rebuilt unit under seed 0
        unit = started(UnitCommand("rebuilt", inputs.rebuilt_install, 0), inputs.staged_store)
        try:
            w = Writer(call, fill, unit.port)
            status, raw = w.tool("list_projects")
            assert status == 200, f"list_projects answered {status}: {raw!r}"
            present = [p for p in PROJECTS if p in project_names(raw)]
            assert present == [], f"the store already holds {present!r}"
            ids = seed_store(w)
        finally:
            stop(unit)
        shutil.copytree(store, seeded)
        print(f"os.listdir of the seeded store's projects folder: {os.listdir(seeded / 'projects')!r}")

        # 3. s1: the first seed under which a set of IDS iterates otherwise than under seed 0
        probe_env = {k: env[k] for k in PASS_THROUGH if k in env}
        base = probe(probe_env, 0, ids)
        s1 = next((s for s in range(1, SEED_LIMIT + 1) if probe(probe_env, s, ids) != base), None)
        assert s1 is not None, f"no seed in 1..{SEED_LIMIT} iterates a set of {ids!r} otherwise than seed 0"

        # Act, with 4. the calls K formed from the seed-0 answers
        k: list[tuple[str, object]] = []
        for seed in (0, s1):
            restore(str(root), str(seeded), inputs.pristine_prompts)
            w0 = time.time()
            unit = start(UnitCommand("rebuilt", inputs.rebuilt_install, seed), START_VARIANT, str(root), env,
                         session.launcher())
            assert unit.outcome == "listening" and unit.port is not None, (seed, unit.outcome, unit.stderr)
            try:
                if seed == 0:
                    k = form_calls(call, fill, read_calls, rt, unit.port, ids)
                answers = [call(unit.port, "GET", c.path, c.args or None) for _, c in k]
                after = files_under(store)
            finally:
                stop(unit)
            w1 = time.time()
            runs[seed] = (answers, after, (w0 - 1, w1 + 1))
    finally:
        session.close()

    (answers0, after0, window0), (answers1, after1, window1) = runs[0], runs[s1]

    # 1. every call agrees
    assert len(answers0) == len(answers1) == len(k)
    differs = []
    for (p, c), one, two in zip(k, answers0, answers1):
        d = disagreement(one, two, (window0, window1))
        if d is not None:
            differs.append(f"{c.tool} {c.path} (project {p}): {d}")
    assert differs == [], differs

    # 2. the precondition: the reads that would carry a set order were called and answered
    names = [t.name for t in rt]
    missing = [n for n in (*PRECONDITION_TOOLS, "list_projects") if n not in names]
    assert missing == [], f"the read set lacks {missing!r}"
    for tool in PRECONDITION_TOOLS:
        seen = [answers0[i][0] for i, (p, c) in enumerate(k) if p == SEEDED and c.tool == tool]
        assert seen and all(s == 200 for s in seen), f"{tool} for {SEEDED} answered {seen!r}"
    listed = [i for i, (_, c) in enumerate(k) if c.tool == "list_projects"]
    assert listed and all(answers0[i][0] == 200 for i in listed), "list_projects did not answer 200"

    # 3. project names in code point order under both seeds
    for answers in (answers0, answers1):
        got = project_names(answers[listed[0]][1])
        assert all(p in got for p in PROJECTS), got
        assert got.index("Seed-C") < got.index("seed-a") < got.index("seed-b"), got

    # 4. no read wrote
    pristine = files_under(str(seeded))
    for after in (after0, after1):
        assert sorted(after) == sorted(pristine)
        changed = [rel for rel in pristine if after[rel] != pristine[rel]]
        assert changed == [], f"reads changed {changed!r}"


def form_calls(call, fill, read_calls, rt, port: int, ids: list[str]) -> list[tuple[str, object]]:
    """K: for each listed project, its read_calls over values found in its project-only answers."""
    tool = next(t for t in rt if t.name == "list_projects")
    status, raw = call(port, "GET", tool.path, None)
    assert status == 200, f"list_projects answered {status}: {raw!r}"
    wanted = list(dict.fromkeys(x for t in rt for x in t.params if x != "project"))
    project_only = [t for t in rt if t.params == ("project",)]
    k = []
    for p in project_names(raw):
        docs = [decode(call(port, "GET", fill(t.path, {"project": p}), None)[1]) for t in project_only]
        values = {}
        for x in wanted:
            found = list(dict.fromkeys(v for d in docs for v in keyed(d, x)))
            if p in PROJECTS and x == "id":
                found = [ids[0], *(v for v in found if v != ids[0])]
            values[x] = found[:MAX_VALUES]
        calls, _ = read_calls(rt, p, values)
        for t in rt:
            for x in t.params:
                if x != "project" and not values.get(x):
                    print(f"uncovered: {t.name} parameter {x!r} on project {p!r}")
        k.extend((p, c) for c in calls)
    return k
