"""A scripted write history leaves both units' stores and answers identical, call for call (T-422).

A differential test: neither unit is imported by this process. The inputs come from S-508's
acceptance_inputs fixture; each unit is started alone by S-491's start through the shared launch,
from the same empty store, and reached only through S-508's call at the route its own GET /tools
gives. The script below runs on the running unit, then on the rebuilt one: its write answers, the
read answers for each project it leaves and every stored file are compared under the clock rule,
where a number each unit's clock wrote during its own run agrees whatever its text. Modules under
test are imported inside the test, so a module not yet written fails only the tests that need it.
"""

import json
import re
import time

START_VARIANT = "hhhhhh"  # every value healthy, reached on HEALTHY_PORT
ACTOR = "replay"
PROJECTS = ("rp", "rp2")
GATED_READS = ("check_gates", "get_waves")


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


def admitted(status: int, raw: bytes) -> bool:
    body = decode(raw)
    return 200 <= status < 300 and not (isinstance(body, dict) and body.get("admitted") is False)


# ---- the clock rule ----

def tree(text: bytes) -> object:
    """text decoded keeping key order and number texts; raises ValueError when it does not decode."""
    return json.loads(text, object_pairs_hook=lambda pairs: ("obj", pairs),
                      parse_int=lambda s: ("num", s), parse_float=lambda s: ("num", s))


def inside(node: object, window: tuple[float, float]) -> bool:
    return float(node[1]) >= window[0] - 1 and float(node[1]) <= window[1] + 1


def walk(a: object, b: object, wa, wb, at: str) -> str | None:
    """The first position where a and b disagree under the clock rule; None when they agree."""
    if isinstance(a, tuple) and isinstance(b, tuple) and a[0] == b[0] == "num":
        if a[1] == b[1] or (inside(a, wa) and inside(b, wb)):
            return None
        return f"{at}: {a[1]} vs {b[1]}"
    if isinstance(a, tuple) and isinstance(b, tuple) and a[0] == b[0] == "obj":
        ka, kb = [k for k, _ in a[1]], [k for k, _ in b[1]]
        if ka != kb:
            return f"{at}: keys {ka!r} vs {kb!r}"
        for (k, va), (_, vb) in zip(a[1], b[1]):
            found = walk(va, vb, wa, wb, f"{at}.{k}")
            if found is not None:
                return found
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{at}: {len(a)} items vs {len(b)}"
        for i, (va, vb) in enumerate(zip(a, b)):
            found = walk(va, vb, wa, wb, f"{at}[{i}]")
            if found is not None:
                return found
        return None
    if type(a) is type(b) and a == b:
        return None
    return f"{at}: {a!r} vs {b!r}"


def text_differs(ta: bytes, tb: bytes, wa, wb, at: str = "$") -> str | None:
    if ta == tb:
        return None
    try:
        a, b = tree(ta), tree(tb)
    except ValueError:
        return f"{at}: bytes differ and do not decode"
    return walk(a, b, wa, wb, at)


def file_differs(fa: bytes, fb: bytes, wa, wb) -> str | None:
    """A stored file: one text when both decode as one JSON value, else line by line."""
    try:
        tree(fa), tree(fb)
    except ValueError:
        la, lb = fa.split(b"\n"), fb.split(b"\n")
        if len(la) != len(lb):
            return f"{len(la)} lines vs {len(lb)}"
        for i, (x, y) in enumerate(zip(la, lb), 1):
            found = text_differs(x, y, wa, wb, f"line {i}")
            if found is not None:
                return found
        return None
    return text_differs(fa, fb, wa, wb)


def numbers(node: object):
    if isinstance(node, tuple) and node[0] == "num":
        yield node
    elif isinstance(node, tuple) and node[0] == "obj":
        for _, v in node[1]:
            yield from numbers(v)
    elif isinstance(node, list):
        for v in node:
            yield from numbers(v)


def log_lines(data: bytes) -> list | None:
    """A lifecycle log's records, when every line of data is one carrying a 'seq' and a 'kind'."""
    lines = [line for line in data.split(b"\n") if line.strip()]
    records = []
    for line in lines:
        try:
            node = tree(line)
        except ValueError:
            return None
        if not (isinstance(node, tuple) and node[0] == "obj" and {"seq", "kind"} <= {k for k, _ in node[1]}):
            return None
        records.append(node)
    return records or None


# ---- one unit's run ----

class Script:
    """One unit's run of the history: every answer kept as (step, status, body bytes)."""

    def __init__(self, call, fill, port: int):
        self.call, self.fill, self.port = call, fill, port
        status, raw = call(port, "GET", "/tools", None)
        assert status == 200, f"GET /tools answered {status}: {raw!r}"
        self.discovery = decode(raw)
        self.tools = {t["name"]: t for t in self.discovery["tools"]}
        self.answers: list[tuple[str, int, bytes]] = []

    def tool(self, step: str, name: str, **args: object) -> tuple[int, bytes]:
        spec = self.tools[name]
        if "actor" in spec.get("input_schema", {}).get("properties", {}):
            args["actor"] = ACTOR
        params = re.findall(r"\{(\w+)\}", spec["path"])
        path = self.fill(spec["path"], {p: str(args[p]) for p in params})
        rest = {k: v for k, v in args.items() if k not in params}
        method = spec["method"].upper()
        status, raw = self.call(self.port, method, path, rest if rest or method != "GET" else None)
        self.answers.append((step, status, raw))
        return status, raw

    def created(self, raw: bytes) -> list:
        body = decode(raw)
        return list(body["created"]) if isinstance(body, dict) and isinstance(body.get("created"), list) else []

    def history(self) -> None:
        _, raw = self.tool("H1", "post_request", type="initiate", title="replay", project="rp")
        mid = next((s for s in strings(decode(raw)) if s.startswith("msg-")), None)
        assert mid is not None, f"H1 answers no 'msg-' id: {raw!r}"
        self.tool("H2", "create_project", project="rp", in_response_to=mid)
        h3 = {"project": "rp", "in_response_to": mid, "idempotency_key": "r1",
              "entries": [{"layer": "I", "title": "one"}, {"layer": "I", "title": "two"}]}
        _, raw = self.tool("H3", "submit_amendment", **h3)
        made = self.created(raw)
        self.i1 = made[0] if made else "I-missing"
        self.tool("H4", "submit_amendment", **h3)
        h5 = {"project": "rp", "in_response_to": mid, "dry_run": True,
              "entries": [{"layer": "B", "title": "a", "derives_from": [self.i1]}]}
        self.tool("H5", "submit_amendment", **h5)
        h6 = {k: v for k, v in h5.items() if k != "dry_run"} | {"idempotency_key": "r2"}
        _, raw = self.tool("H6", "submit_amendment", **h6)
        made = self.created(raw)
        self.ba = made[0] if made else "B-missing"
        self.tool("H7", "submit_amendment",
                  **(h6 | {"entries": [{"layer": "B", "title": "a-other", "derives_from": [self.i1]}]}))
        self.tool("H8", "submit_amendment", project="rp", in_response_to=mid, idempotency_key="r3",
                  entries=[{"layer": "B", "title": "bad", "derives_from": ["I-999999"]}])
        self.tool("H9", "raise_issue", project="rp", target=self.ba, kind="additive", claim="replay",
                  idempotency_key="r4")
        self.tool("H10", "declare_module", project="rp", path="m/x.py", implements=[])
        _, raw = self.tool("H11", "post_request", type="initiate", title="replay fork", project="rp2")
        fork_mid = next((s for s in strings(decode(raw)) if s.startswith("msg-")), None)
        assert fork_mid is not None, f"H11 answers no 'msg-' id: {raw!r}"
        self.tool("H11 fork", "fork_project", project="rp", name="rp2", in_response_to=fork_mid)

    def reads(self, read_tools, read_calls) -> None:
        status, raw = self.call(self.port, "GET", "/tools", None)
        assert status == 200, f"GET /tools answered {status}: {raw!r}"
        self.rt = read_tools(decode(raw))
        self.read_answers: list[tuple[str, str, str, int, bytes]] = []  # (project, tool, path, status, body)
        self.uncovered = {}
        for p in PROJECTS:
            calls, self.uncovered[p] = read_calls(self.rt, p, {"id": [self.i1, self.ba]})
            for c in calls:
                status, raw = self.call(self.port, "GET", c.path, dict(c.args) if c.args else None)
                self.read_answers.append((p, c.tool, c.path, status, raw))


def store_files(projects) -> dict[str, bytes]:
    return {f.relative_to(projects).as_posix(): f.read_bytes() for f in sorted(projects.rglob("*")) if f.is_file()}


def test_replayed_store_equals_running_unit(request, tmp_path):
    # S-508's session fixture, looked up in the test after its conftest is imported: a conftest
    # not yet written then fails this test as a missing module, not its setup.
    from tests.acceptance.conftest import call, fill, read_calls, read_tools

    inputs = request.getfixturevalue("acceptance_inputs")

    from startcheck.ownenv import own_env
    from unitcheck.session import open_session

    from startcheck.runner import UnitCommand, restore, start, stop

    empty = tmp_path / "empty"
    (empty / "projects").mkdir(parents=True)
    root = tmp_path / "check"
    root.mkdir()
    env = own_env()
    session = open_session(env)

    runs = {}
    try:
        for label, install in (("running", inputs.running_install), ("rebuilt", inputs.rebuilt_install)):
            restore(str(root), str(empty), inputs.pristine_prompts)
            w0 = time.time()
            unit = start(UnitCommand(label, install), START_VARIANT, str(root), env, session.launcher())
            try:
                assert unit.outcome == "listening" and unit.port is not None, (label, unit.outcome, unit.stderr)
                script = Script(call, fill, unit.port)
                script.history()
                script.reads(read_tools, read_calls)
            finally:
                stop(unit)
            w1 = time.time()
            runs[label] = (script, (w0, w1), store_files(root / "store" / "projects"))
    finally:
        session.close()

    (run, wr, f_run), (reb, wb, f_reb) = runs["running"], runs["rebuilt"]
    steps = {step: (status, raw) for step, status, raw in run.answers}

    # 2. preconditions on the running unit's answers
    for step in ("H2", "H3", "H6", "H9", "H10", "H11 fork"):
        assert admitted(*steps[step]), f"the running unit refused {step}: {steps[step]!r}"
    h4 = decode(steps["H4"][1])
    assert isinstance(h4, dict) and h4.get("replayed") is True, f"H4 is no replay: {steps['H4']!r}"
    h5 = decode(steps["H5"][1])
    assert steps["H5"][0] == 200 and isinstance(h5, dict) and h5.get("admitted") is True, steps["H5"]
    assert h5.get("created") == decode(steps["H6"][1]).get("created"), (steps["H5"], steps["H6"])
    assert steps["H7"][0] == 409, steps["H7"]
    assert not admitted(*steps["H8"]), steps["H8"]

    # 1. the history's answers agree call for call
    assert [s for s, _, _ in reb.answers] == [s for s, _, _ in run.answers]
    for (step, s_run, b_run), (_, s_reb, b_reb) in zip(run.answers, reb.answers):
        assert s_reb == s_run, f"{step}: status {s_run} (running) vs {s_reb} (rebuilt): {b_run!r} {b_reb!r}"
        found = text_differs(b_run, b_reb, wr, wb)
        assert found is None, f"{step}: first difference at {found}: {b_run!r} vs {b_reb!r}"

    # 3. the read answers agree call for call; the gate and wave reads were exercised
    print(f"uncovered reads of 'rp': {run.uncovered['rp']!r}")
    names = {t.name for t in run.rt}
    assert set(GATED_READS) <= names, f"the running unit lists no {set(GATED_READS) - names!r}"
    for name in GATED_READS:
        answered = [s for p, t, _, s, _ in run.read_answers if p == "rp" and t == name]
        assert answered and all(s == 200 for s in answered), f"{name} for 'rp' answered {answered!r}"
    assert [a[:3] for a in reb.read_answers] == [a[:3] for a in run.read_answers]
    for (_, tool, path, s_run, b_run), (*_, s_reb, b_reb) in zip(run.read_answers, reb.read_answers):
        assert s_reb == s_run, f"{tool} {path}: status {s_run} (running) vs {s_reb} (rebuilt)"
        found = text_differs(b_run, b_reb, wr, wb)
        assert found is None, f"{tool} {path}: first difference at {found}: {b_run!r} vs {b_reb!r}"

    # 4. the stored files agree path for path; the clock rule was exercised on the lifecycle log
    logs = [r for data in f_run.values() for r in (log_lines(data) or ())]
    assert any(inside(n, wr) for r in logs for n in numbers(r)), (
        "no number in the running unit's lifecycle log lies inside its run's window")
    assert sorted(f_reb) == sorted(f_run), (
        f"paths on one side only: running {sorted(set(f_run) - set(f_reb))!r}, "
        f"rebuilt {sorted(set(f_reb) - set(f_run))!r}")
    for path in sorted(f_run):
        found = file_differs(f_run[path], f_reb[path], wr, wb)
        assert found is None, f"{path}: first difference at {found}"
