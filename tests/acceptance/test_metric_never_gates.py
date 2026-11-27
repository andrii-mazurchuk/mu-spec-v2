"""No measured value changes an admission: fork pairs differing only in a measurement are admitted alike (T-386).

A differential test: neither unit is imported by this process. The inputs come from S-484's
acceptance_inputs fixture; each unit is started alone by S-410's start through the shared launch, on
its own copy restored from the staged store, and reached only through S-484's call. The whole sequence
runs on the running unit first, every request it sends recorded; the unit is stopped, the copy
restored, and the same requests, the same bytes, are sent in the same order to the rebuilt unit.
Modules under test are imported inside the test, so a module not yet written fails only the tests
that need it.
"""

import json
import re
from pathlib import Path

START_VARIANT = "hhhhhh"  # every value healthy, reached on HEALTHY_PORT
ACTOR = "metric-gate"
ID = re.compile(r"^[IBAST]-\d+$")
TIME = re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?")
ROUTE_TABLE = Path("dashboard") / "src" / "actions.ts"


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


def first(raw: bytes, pattern: str) -> str | None:
    return next((s for s in strings(decode(raw)) if re.match(pattern, s)), None)


def admitted(status: int) -> bool:
    return 200 <= status < 300


def N(fork: str, raw: bytes) -> bytes:
    return TIME.sub(b"<time>", raw.replace(fork.encode("utf-8"), b"<fork>"))


def verdict(raw: bytes) -> tuple:
    body = decode(raw)
    body = body if isinstance(body, dict) else {}
    return body.get("admitted"), body.get("error"), body.get("reason")


def new_ids(raw: bytes, spine: bytes) -> set[str]:
    before = set(strings(decode(spine)))
    return {s for s in strings(decode(raw)) if ID.match(s) and s not in before}


def listing(raw: bytes) -> list:
    doc = decode(raw)
    lists = [v for v in doc.values() if isinstance(v, list)] if isinstance(doc, dict) else [doc]
    assert len(lists) == 1 and isinstance(lists[0], list), f"the answer holds no single listing: {raw!r}"
    return lists[0]


def slices(spine: bytes) -> dict[str, list[str]]:
    """Slice name -> the ids of its spine rows, in listed order."""
    out: dict[str, list[str]] = {}
    for row in listing(spine):
        if isinstance(row, dict) and isinstance(row.get("slice"), str) and row["slice"]:
            out.setdefault(row["slice"], []).append(row["id"])
    return out


def ratify_route(rebuilt_install: str) -> tuple[str, str]:
    """The ratify action's method and relative path pattern, as the dashboard's route table declares them."""
    text = (Path(rebuilt_install) / ROUTE_TABLE).read_text(encoding="utf-8")
    m = re.search(r'ratify:\s*\{\s*method:\s*"(\w+)",\s*path:\s*\(project\)\s*=>\s*`([^`]*)`', text)
    assert m, f"{ROUTE_TABLE} declares no route for ratify"
    return m.group(1), m.group(2).replace("${project}", "{project}")


class Script:
    """The running unit's sequence: each request sent, its answer kept, the request recorded for replay."""

    def __init__(self, call, fill, port: int, ratify: tuple[str, str]):
        self.call, self.fill, self.port, self.ratify = call, fill, port, ratify
        status, raw = call(port, "GET", "/tools", None)
        assert status == 200, f"GET /tools answered {status}: {raw!r}"
        self.tools = {t["name"]: t for t in decode(raw)["tools"]}
        self.sent: list[tuple[str, str, object]] = []
        self.answers: list[tuple[int, bytes]] = []
        self.keys: dict[str, int] = {}
        self.judged: list[tuple[str, str, int, int]] = []  # (call name, fork, its spine read, the call)

    def send(self, method: str, path: str, body: object) -> tuple[int, bytes]:
        answer = self.call(self.port, method, path, body)
        self.sent.append((method, path, body))
        self.answers.append(answer)
        return answer

    def tool(self, name: str, fork: str, **args: object) -> tuple[int, bytes]:
        spec = self.tools[name]
        declared = spec.get("input_schema", {}).get("properties", {})
        if "actor" in declared:
            args["actor"] = ACTOR
        if "idempotency_key" in declared:
            self.keys[fork] = self.keys.get(fork, 0) + 1
            args["idempotency_key"] = f"mg-{fork}-{self.keys[fork]}"
        params = re.findall(r"\{(\w+)\}", spec["path"])
        path = self.fill(spec["path"], {p: str(args[p]) for p in params})
        rest = {k: v for k, v in args.items() if k not in params}
        method = spec["method"].upper()
        return self.send(method, path, rest if rest or method != "GET" else None)

    def judge(self, label: str, fork: str, name: str, **args: object) -> tuple[int, bytes]:
        """One call of C or C2, after the fork's spine read its new ids are taken against."""
        self.tool("get_spine", fork, project=fork)
        spine = len(self.answers) - 1
        if name == "ratify":
            method, pattern = self.ratify
            answer = self.send(method, "/" + self.fill(pattern, {"project": fork}).lstrip("/"), {})
        else:
            answer = self.tool(name, fork, project=fork, **args)
        self.judged.append((label, fork, spine, len(self.answers) - 1))
        return answer

    def fork(self, source: str, name: str) -> None:
        status, raw = self.tool("post_request", name, type="initiate", title=f"metric gate {name}")
        assert admitted(status), f"post_request for {name} answered {status}: {raw!r}"
        mid = first(raw, r"^msg-")
        status, raw = self.tool("fork_project", name, project=source, name=name, in_response_to=mid)
        assert admitted(status), f"fork_project {source} -> {name} answered {status}: {raw!r}"


def run_running(script: Script) -> list[tuple[str, str]]:
    """The whole sequence on the running unit, its preconditions asserted; the (lo, hi) fork pairs judged."""
    status, raw = script.tool("list_projects", "")
    assert status == 200, f"list_projects answered {status}: {raw!r}"
    projects = [p if isinstance(p, str) else p["name"] for p in listing(raw)]
    spines = {}
    for p in projects:
        status, raw = script.tool("get_spine", p, project=p)
        assert status == 200, f"get_spine of {p} answered {status}: {raw!r}"
        spines[p] = raw
    ap = [p for p in projects if first(spines[p], r"^I-") is not None]
    assert ap, "no acceptance project holds an 'I-' entry"

    pairs, split_sent = [], False
    for i, p in enumerate(ap, 1):
        e = first(spines[p], r"^I-")
        s = first(spines[p], r"^S-\d+$")
        x = next(((n, ms) for n, ms in slices(spines[p]).items() if len(ms) >= 2), None)
        lo, hi = f"mg-lo-{i}", f"mg-hi-{i}"
        js = {}
        for f in (lo, hi):
            script.fork(p, f)
            status, raw = script.tool("raise_issue", f, project=f, target=e, kind="additive", claim="metric gate")
            assert admitted(status), f"raise_issue on {f} answered {status}: {raw!r}"
            js[f] = first(raw, r"^iss-")
        for scope_args in ({"outcome": "flagged", "issues": [js[hi]], "run_id": "mg-1"},
                           {"outcome": "pass", "run_id": "mg-2"}):
            status, raw = script.tool("record_audit", hi, project=hi, scope=[e], layer="I",
                                      questions=["metric gate?"], auditor=ACTOR, **scope_args)
            assert admitted(status), f"record_audit on {hi} answered {status}: {raw!r}"
        insights = {f: script.tool("get_insights", f, project=f)[1] for f in (lo, hi)}
        assert N(lo, insights[lo]) != N(hi, insights[hi]), f"the audits move no insight of {p}"

        for f in (lo, hi):
            j = js[f]
            status, raw = script.judge("c1", f, "submit_amendment", in_response_to=j, entries=[
                {"layer": "I", "title": "metric gate refused", "derives_from": ["S-999999"]}])
            assert not admitted(status), f"c1 admitted on {f}: {raw!r}"
            status, raw = script.judge("c2", f, "submit_amendment", in_response_to=j,
                                       entries=[{"layer": "I", "title": "metric gate"}])
            assert f != lo or admitted(status), f"c2 refused on {f}: {status} {raw!r}"
            if x is not None:
                script.judge("c3", f, "split_slice", slice=x[0], into="mg-split", members=[x[1][0]])
                split_sent = True
            script.judge("c4", f, "declare_module", path="mg/x.py", implements=[s] if s else [])
        pairs.append((lo, hi))
    assert split_sent, "no acceptance project lists a slice with two members"

    # the score pair
    assert "mg-g" not in projects, "'mg-g' is an acceptance project"
    status, raw = script.tool("post_request", "mg-g", type="initiate", title="metric gate", project="mg-g")
    assert admitted(status), f"post_request for mg-g answered {status}: {raw!r}"
    mid = first(raw, r"^msg-")
    status, raw = script.tool("create_project", "mg-g", project="mg-g", in_response_to=mid)
    assert admitted(status), f"create_project mg-g answered {status}: {raw!r}"

    def amend(entries):
        before = script.tool("get_spine", "mg-g", project="mg-g")[1]
        status, raw = script.tool("submit_amendment", "mg-g", project="mg-g", in_response_to=mid, entries=entries)
        assert admitted(status), f"submit_amendment on mg-g answered {status}: {raw!r}"
        got = list(dict.fromkeys(s for s in strings(decode(raw)) if s in new_ids(raw, before)))
        assert len(got) == 2, f"the amendment answers {got!r}, not two new ids: {raw!r}"
        return got

    i1, i2 = amend([{"layer": "I", "title": "one"}, {"layer": "I", "title": "two"}])
    ba, bb = amend([{"layer": "B", "title": "a", "derives_from": [i1]},
                    {"layer": "B", "title": "b", "derives_from": [i2]}])
    for f in ("mg-one", "mg-many"):
        script.fork("mg-g", f)
    s_one, s_many = {"one": [ba, bb]}, {"one": [ba], "two": [bb]}
    scores = [script.tool("score_slicing", "mg-g", project="mg-g", proposal=s) for s in (s_one, s_many)]
    assert scores[0] != scores[1], f"the two slicings score alike: {scores!r}"
    for f, s in (("mg-one", s_one), ("mg-many", s_many)):
        status, raw = script.tool("submit_proposal", f, project=f, proposal=s)
        assert admitted(status), f"submit_proposal on {f} answered {status}: {raw!r}"
    for f in ("mg-one", "mg-many"):
        status, raw = script.judge("ratify", f, "ratify")
        assert admitted(status), f"ratify on {f} answered {status}: {raw!r}"
        status, raw = script.judge("amend", f, "submit_amendment", in_response_to=mid, slice="one",
                                   entries=[{"layer": "B", "title": "c", "derives_from": [i1]}])
        assert admitted(status), f"C2's amendment on {f} answered {status}: {raw!r}"
    return pairs + [("mg-one", "mg-many")]


def test_measured_value_never_changes_admission(acceptance_inputs, tmp_path):
    from startcheck.ownenv import own_env
    from tests.acceptance.conftest import call, fill
    from unitcheck.session import open_session

    from startcheck.runner import UnitCommand, restore, start, stop

    inputs = acceptance_inputs
    ratify = ratify_route(inputs.rebuilt_install)
    root = tmp_path / "check"
    root.mkdir()
    env = own_env()
    session = open_session(env)

    def started(label, install):
        restore(str(root), inputs.staged_store, inputs.pristine_prompts)
        unit = start(UnitCommand(label, install), START_VARIANT, str(root), env, session.launcher())
        assert unit.outcome == "listening" and unit.port is not None, (label, unit.outcome, unit.stderr)
        return unit

    try:
        unit = started("running", inputs.running_install)
        try:
            script = Script(call, fill, unit.port, ratify)
            pairs = run_running(script)
        finally:
            stop(unit)
        unit = started("rebuilt", inputs.rebuilt_install)
        try:
            rebuilt = [call(unit.port, method, path, body) for method, path, body in script.sent]
        finally:
            stop(unit)
    finally:
        session.close()

    running = script.answers
    judged = {(label, fork): (spine, at) for label, fork, spine, at in script.judged}
    differs = []
    # 1. the two forks of each pair answer each call alike on the rebuilt unit
    for a, b in pairs:
        for label in ("c1", "c2", "c3", "c4", "ratify", "amend"):
            if (label, a) not in judged:
                continue
            (sa, ca), (sb, cb) = judged[(label, a)], judged[(label, b)]
            seen = [(rebuilt[c][0], verdict(rebuilt[c][1]), new_ids(rebuilt[c][1], rebuilt[s][1]))
                    for s, c in ((sa, ca), (sb, cb))]
            if seen[0] != seen[1]:
                differs.append(("pair", label, a, b, rebuilt[ca], rebuilt[cb]))
    # 2. each call answers as on the running unit
    for (label, fork), (_, at) in judged.items():
        if (rebuilt[at][0], N(fork, rebuilt[at][1])) != (running[at][0], N(fork, running[at][1])):
            differs.append(("running", label, fork, running[at], rebuilt[at]))
    assert differs == [], differs
