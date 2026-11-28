"""Path forms get the running unit's diff audit answer on every acceptance project (T-371).

A differential test: neither unit is imported by this process. The inputs come from S-508's
acceptance_inputs fixture; each unit is started alone by S-491's start through the shared launch, on
its own copy restored from the staged store, and reached only through S-508's call at the method and
path GET /tools lists for each tool. The running unit answers first, every audit request it is sent
recorded; it is stopped, the copy restored, and the same requests, the same bytes, are sent in the
same order to the rebuilt unit. Modules under test are imported inside the test, so a module not yet
written fails only the tests that need it.
"""

import json
import re

START_VARIANT = "hhhhhh"  # every value healthy, reached on HEALTHY_PORT
UNDECLARED = "not/declared.py"


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


def paths(doc: object):
    """Every string value under a key named 'path', anywhere in a decoded answer, in document order."""
    if isinstance(doc, dict):
        for k, v in doc.items():
            if k == "path" and isinstance(v, str):
                yield v
            else:
                yield from paths(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from paths(v)


def forms(m: str) -> list[str]:
    """The path forms one declared module is sent in, in order."""
    return ["./" + m, m.replace("/", "\\"), m.replace("/", "//", 1), m + "/", m.upper(), "x/../" + m, "/" + m, m]


class Tools:
    """One unit's tools as GET /tools lists them, each called with '{x}' filled and the rest as the body."""

    def __init__(self, call, fill, port: int):
        self.call, self.fill, self.port = call, fill, port
        status, raw = call(port, "GET", "/tools", None)
        assert status == 200, f"GET /tools answered {status}: {raw!r}"
        self.tools = {t["name"]: t for t in decode(raw)["tools"]}

    def request(self, name: str, **args: object) -> tuple[str, str, object]:
        spec = self.tools[name]
        params = re.findall(r"\{(\w+)\}", spec["path"])
        path = self.fill(spec["path"], {p: str(args[p]) for p in params})
        rest = {k: v for k, v in args.items() if k not in params}
        method = spec["method"].upper()
        return method, path, rest if rest or method != "GET" else None

    def tool(self, name: str, **args: object) -> tuple[int, bytes]:
        return self.call(self.port, *self.request(name, **args))


def test_path_forms_match_running_unit(request, tmp_path):
    # S-508's session fixture, looked up in the test after its conftest is imported: a conftest
    # not yet written then fails this test as a missing module, not its setup.
    from tests.acceptance.conftest import call, fill

    inputs = request.getfixturevalue("acceptance_inputs")

    from startcheck.ownenv import own_env
    from unitcheck.session import open_session

    from startcheck.runner import UnitCommand, restore, start, stop

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
            tools = Tools(call, fill, unit.port)
            status, raw = tools.tool("list_projects")
            assert status == 200, f"list_projects answered {status}: {raw!r}"
            ap = [p if isinstance(p, str) else p["name"] for p in listing(raw)]
            modules = {}
            for p in ap:
                status, raw = tools.tool("list_modules", project=p)
                assert status == 200, f"list_modules of {p} answered {status}: {raw!r}"
                modules[p] = list(dict.fromkeys(paths(decode(raw))))
            assert any("/" in m for ms in modules.values() for m in ms), (
                f"list_modules names no declared path holding '/' under 'path' on any of {ap!r}: {modules!r}")
            sent, running = [], []
            for p in ap:
                if not modules[p]:
                    continue
                touched = [form for m in modules[p] for form in forms(m)] + [UNDECLARED]
                req = tools.request("audit_diff", project=p, touched=touched)
                sent.append((p, req))
                running.append(call(unit.port, *req))
        finally:
            stop(unit)
        unit = started("rebuilt", inputs.rebuilt_install)
        try:
            rebuilt = [call(unit.port, *req) for _, req in sent]
        finally:
            stop(unit)
    finally:
        session.close()

    assert any(status == 200 for status, _ in running), f"the running unit answered no audit_diff with 200: {running!r}"
    differs = [(p, a, b) for (p, _), a, b in zip(sent, running, rebuilt) if a != b]
    assert differs == [], differs
