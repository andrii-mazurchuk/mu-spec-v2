"""Discovery and wrong-typed fields: each answers as the running unit does, or not at all on both (T-361).

A differential test: neither unit is imported by this process. The inputs come from S-508's
acceptance_inputs fixture; each unit is started alone by S-491's start through the shared launch, on
its own copy restored from the staged store, and reached only through S-508's call. The GitHub
stand-in the launch redirects to is opened here and reset to the same script before each unit. On
each unit a fork F of the first listed project is made, then every top-level property of every tool
is given each value of a JSON type its declared shape does not allow, one call each, the rest of the
call valid. The two units' outcomes are compared call by call, times of call masked. Modules under
test are imported inside the test, so a module not yet written fails only the tests that need it.
"""

import http.client
import json
import re

START_VARIANT = "hhhhhh"  # every value healthy, reached on HEALTHY_PORT
ACTOR = "shape-check"
FORK = "shape-check-fork"
TIME = re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?")
PARAM = re.compile(r"\{(\w+)\}")
NO_ANSWER = ("no answer",)
# the candidates, in order, each with the JSON types it satisfies
CANDIDATES = (
    ("x", {"string"}),
    (1, {"integer", "number"}),
    (1.5, {"number"}),
    (True, {"boolean"}),
    (None, {"null"}),
    ([], {"array"}),
    ({}, {"object"}),
)
ROUND_NOT_INT = {"error": "invalid literal for int() with base 10: 'x'"}


def decode(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def listing(raw: bytes) -> list:
    doc = decode(raw)
    lists = [v for v in doc.values() if isinstance(v, list)] if isinstance(doc, dict) else [doc]
    assert len(lists) == 1 and isinstance(lists[0], list), f"the answer holds no single listing: {raw!r}"
    return lists[0]


def ids(doc: object):
    """Every string value under a key 'id', in document order."""
    if isinstance(doc, dict):
        for k, v in doc.items():
            if k == "id" and isinstance(v, str):
                yield v
            yield from ids(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from ids(v)


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


def types_of(prop: object) -> set[str] | None:
    """The type names a property's schema declares, or None when it declares no 'type'."""
    if not isinstance(prop, dict) or "type" not in prop:
        return None
    declared = prop["type"]
    return set(declared) if isinstance(declared, list) else {declared}


def properties(tool: dict) -> dict:
    props = tool.get("input_schema", {}).get("properties", {})
    return props if isinstance(props, dict) else {}


def value_for(name: str, prop: object, entry: str) -> object:
    """base()'s value for one property, by its name and its first declared type other than null."""
    declared = prop.get("type") if isinstance(prop, dict) else None
    names = declared if isinstance(declared, list) else [declared]
    kind = next((t for t in names if t != "null"), None)
    if name == "project":
        return FORK
    if kind == "string":
        return entry if name.endswith("id") or name == "entry" else "x"
    if kind in ("integer", "number"):
        return 1
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object":
        return {}
    return "x"


def base(tool: dict, entry: str, n: int) -> dict:
    props = properties(tool)
    required = tool.get("input_schema", {}).get("required", [])
    args = {name: value_for(name, props.get(name), entry) for name in required}
    for name in PARAM.findall(tool["path"]):
        args.setdefault(name, value_for(name, props.get(name), entry))
    if "dry_run" in props:
        args["dry_run"] = True
    if "idempotency_key" in props:
        args["idempotency_key"] = f"shape-{tool['name']}-{n}"
    if "actor" in props:
        args["actor"] = ACTOR
    return args


def call_list(tools: list) -> list[tuple[dict, str, object]]:
    """C: (tool, property, candidate) in tool order, then property order, then candidate order."""
    out = []
    for tool in tools:
        in_path = set(PARAM.findall(tool["path"]))
        for q, prop in properties(tool).items():
            declared = types_of(prop)
            if declared is None or q in in_path:
                continue
            out.extend((tool, q, v) for v, kinds in CANDIDATES if not kinds & declared)
    return out


class Session:
    """One started unit, reached through S-508's call at its port."""

    def __init__(self, call, fill, port: int):
        self.call, self.fill, self.port = call, fill, port

    def outcome(self, method: str, path: str, body: object) -> tuple:
        try:
            status, raw = self.call(self.port, method, path, body)
        except (http.client.RemoteDisconnected, ConnectionError):
            return NO_ANSWER
        return ("answer", status, TIME.sub(b"<time>", raw))

    def send(self, tool: dict, args: dict) -> tuple:
        params = PARAM.findall(tool["path"])
        path = self.fill(tool["path"], {p: str(args[p]) for p in params})
        rest = {k: v for k, v in args.items() if k not in params}
        method = tool["method"].upper()
        return self.outcome(method, path, rest if rest or method != "GET" else None)

    def discovery(self) -> bytes:
        status, raw = self.call(self.port, "GET", "/tools", None)
        assert status == 200, f"GET /tools answered {status}: {raw!r}"
        return raw


def tool_args(tool: dict, **args: object) -> dict:
    declared = properties(tool)
    if "actor" in declared:
        args["actor"] = ACTOR
    if "idempotency_key" in declared:
        args["idempotency_key"] = f"shape-fork-{tool['name']}"
    return args


def fork(unit: Session, tools: dict, source: str) -> tuple[tuple, tuple]:
    """post_request initiating F, then fork_project from source to F citing it; both outcomes."""
    asked = unit.send(tools["post_request"], tool_args(tools["post_request"], type="initiate",
                                                       title="shape check", project=FORK))
    cited = next((s for s in strings(decode(asked[2])) if s.startswith("msg-")), None) if asked != NO_ANSWER else None
    forked = unit.send(tools["fork_project"], tool_args(tools["fork_project"], project=source, name=FORK,
                                                        in_response_to=cited))
    return asked, forked


def run(unit: Session, tools_running: list, source: str) -> tuple[bytes, list[tuple], tuple, tuple]:
    """Discovery, the forks, then every call of C in order; GET /health after the last."""
    raw = unit.discovery()
    tools = {t["name"]: t for t in tools_running}
    forked = fork(unit, tools, source)
    spine = unit.send(tools["get_spine"], {"project": FORK})
    entry = next(ids(decode(spine[2])), "") if spine != NO_ANSWER else ""
    outcomes = []
    for n, (tool, q, v) in enumerate(call_list(tools_running)):
        args = base(tool, entry, n)
        args[q] = v
        outcomes.append(unit.send(tool, args))
    health = unit.outcome("GET", "/health", None)
    return raw, outcomes, forked, health


def test_discovery_and_wrong_type_fields_match_running_unit(request, tmp_path):
    # S-508's session fixture, looked up in the test after its conftest is imported: a conftest
    # not yet written then fails this test as a missing module, not its setup.
    from tests.acceptance.conftest import call, fill

    inputs = request.getfixturevalue("acceptance_inputs")

    from shipcheck.standin import Script
    from startcheck.ownenv import own_env
    from unitcheck.session import open_session

    from startcheck.runner import UnitCommand, restore, start, stop

    root = tmp_path / "check"
    root.mkdir()
    env = own_env()
    session = open_session(env)

    def started(label, install):
        restore(str(root), inputs.staged_store, inputs.pristine_prompts)
        session.standin.reset(Script(issues=(), links=(), next_number=1, next_id=1, user={"login": "x"}, push=True,
                                     refuse={}, unanswered=frozenset(), hold=None))
        unit = start(UnitCommand(label, install), START_VARIANT, str(root), env, session.launcher())
        assert unit.outcome == "listening" and unit.port is not None, (label, unit.outcome, unit.stderr)
        return unit

    try:
        unit = started("running", inputs.running_install)
        try:
            running = Session(call, fill, unit.port)
            tools_running = decode(running.discovery())["tools"]
            names = {t["name"]: t for t in tools_running}
            listed = running.send(names["list_projects"], {})
            assert listed[:2] == ("answer", 200), f"list_projects answered {listed!r}"
            projects = [p if isinstance(p, str) else p["name"] for p in listing(listed[2])]
            assert projects, "the running unit lists no project"
            assert FORK not in projects, f"{FORK!r} is an acceptance project"
            source = projects[0]
            run_raw, run_out, run_forked, run_health = run(running, tools_running, source)
        finally:
            stop(unit)
        unit = started("rebuilt", inputs.rebuilt_install)
        try:
            reb_raw, reb_out, _, reb_health = run(Session(call, fill, unit.port), tools_running, source)
        finally:
            stop(unit)
    finally:
        session.close()

    asked, forked = run_forked
    assert asked != NO_ANSWER and 200 <= asked[1] < 300, f"post_request for {FORK} on the running unit: {asked!r}"
    assert forked != NO_ANSWER and 200 <= forked[1] < 300, f"fork_project {source} -> {FORK} on the running unit: {forked!r}"

    # 1. discovery: the same bytes, 41 unique names, not sorted
    assert reb_raw == run_raw
    listed_names = [t["name"] for t in tools_running]
    assert len(tools_running) == 41, listed_names
    assert len(set(listed_names)) == 41, listed_names
    assert listed_names != sorted(listed_names), "the running unit lists its tools sorted"

    # 2. every call of C answers alike, or not at all on both; both units still healthy after the last
    calls = call_list(tools_running)
    differs = [(tool["name"], q, v, a, b) for (tool, q, v), a, b in zip(calls, run_out, reb_out) if a != b]
    assert len(reb_out) == len(run_out) == len(calls)
    assert differs == [], differs
    assert run_health[:2] == ("answer", 200), f"running unit GET /health after the last call: {run_health!r}"
    assert reb_health[:2] == ("answer", 200), f"rebuilt unit GET /health after the last call: {reb_health!r}"

    # 3. preconditions on the running unit's outcomes
    raise_issue = next((t for t in tools_running if t["name"] == "raise_issue"), None)
    assert raise_issue is not None, "the running unit lists no raise_issue"
    assert properties(raise_issue).get("round", {}).get("type") == "integer", properties(raise_issue).get("round")
    by_case = {(tool["name"], q, json.dumps(v)): out for (tool, q, v), out in zip(calls, run_out)}
    assert by_case[("raise_issue", "round", "[]")] == NO_ANSWER, by_case[("raise_issue", "round", "[]")]
    round_x = by_case[("raise_issue", "round", '"x"')]
    assert round_x[:2] == ("answer", 400) and decode(round_x[2]) == ROUND_NOT_INT, round_x
    assert any(out != NO_ANSWER and 200 <= out[1] < 300 for out in run_out), "no call of C is admitted"
