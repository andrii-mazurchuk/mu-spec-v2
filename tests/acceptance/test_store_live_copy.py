"""On restored acceptance copies the rebuilt unit numbers its first log record as the running unit does (T-417).

A differential test: neither unit is imported by this process. The inputs come from S-519's
acceptance_inputs fixture; each unit is started alone by S-491's start through the shared launch, on
a copy restored from the staged store, and reached only through S-519's call at the routes GET /tools
lists. Start-up time and the largest project's read time are reported, never gated. Modules under
test are imported inside the test, so a module not yet written fails only the tests that need it.
"""

import json
import re
import statistics
import sys
import time

START_VARIANT = "hhhhhh"  # every value healthy, reached on HEALTHY_PORT
READS = 100


def decode(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return None


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


def listing(raw: bytes) -> list:
    doc = decode(raw)
    lists = [v for v in doc.values() if isinstance(v, list)] if isinstance(doc, dict) else [doc]
    assert len(lists) == 1 and isinstance(lists[0], list), f"the answer holds no single listing: {raw!r}"
    return lists[0]


def names(raw: bytes) -> list[str]:
    return [p if isinstance(p, str) else p["name"] for p in listing(raw)]


class Tools:
    """The started unit's tools, as its GET /tools lists them."""

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

    def __call__(self, name: str, **args: object) -> tuple[int, bytes]:
        return self.send(self.request(name, **args))

    def send(self, req: tuple[str, str, object]) -> tuple[int, bytes]:
        return self.call(self.port, *req)


def plain_files(d) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in d.iterdir() if p.is_file()}


def grown(before: dict[str, bytes], after: dict[str, bytes]) -> dict[str, int]:
    """Each file that grew by an append whose first line is a JSON object holding 'seq': its seq."""
    out = {}
    for name, data in after.items():
        old = before.get(name)
        if old is None or len(data) <= len(old) or not data.startswith(old):
            continue
        head = decode(data[len(old):].split(b"\n")[0])
        if isinstance(head, dict) and "seq" in head:
            out[name] = head["seq"]
    return out


def test_next_seq_and_reported_costs(request, tmp_path, record_property):
    # S-519's session fixture, looked up in the test; its helpers are taken from the conftest module
    # that defined it, whatever name pytest imported that module under
    inputs = request.getfixturevalue("acceptance_inputs")
    conftest = sys.modules[type(inputs).__module__]
    call, fill = conftest.call, conftest.fill

    from startcheck.ownenv import own_env
    from unitcheck.session import open_session

    from startcheck.runner import STORE, UnitCommand, restore, start, stop

    root = tmp_path / "check"
    root.mkdir()
    PL = root / STORE / "projects"
    env = own_env()
    session = open_session(env)
    W = None
    seqs, files, costs = {}, {}, {}
    try:
        for label, install in (("running", inputs.running_install), ("rebuilt", inputs.rebuilt_install)):
            restore(str(root), inputs.staged_store, inputs.pristine_prompts)
            before = plain_files(PL)
            t0 = time.perf_counter()
            unit = start(UnitCommand(label, install), START_VARIANT, str(root), env, session.launcher())
            costs[f"t_start_{label}"] = time.perf_counter() - t0
            try:
                assert unit.outcome == "listening" and unit.port is not None, (label, unit.outcome, unit.stderr)
                tool = Tools(call, fill, unit.port)
                if W is None:
                    status, raw = tool("list_projects")
                    assert status == 200, f"list_projects answered {status}: {raw!r}"
                    P = names(raw)[0]
                    status, raw = tool("get_spine", project=P)
                    assert status == 200, f"get_spine of {P} answered {status}: {raw!r}"
                    E = next(ids(decode(raw)), None)
                    assert E is not None, f"the spine of {P} holds no 'id'"
                    W = tool.request("raise_issue", project=P, target=E, kind="additive", claim="seq check",
                                     actor="seq-check", idempotency_key="seq-check-1")
                status, raw = tool.send(W)
                assert 200 <= status < 300, f"W was refused by {label}: {status} {raw!r}"
                if label == "rebuilt":
                    status, raw = tool("list_projects")
                    counts = {}
                    for p in names(raw):
                        counts[p] = len(list(ids(decode(tool("get_spine", project=p)[1]))))
                    big = max(counts, key=lambda p: counts[p])
                    timings = []
                    for _ in range(READS):
                        r0 = time.perf_counter()
                        tool("get_spine", project=big)
                        timings.append(time.perf_counter() - r0)
                    costs["t_read"] = statistics.median(timings)
                    costs["projects"] = len(counts)
                    costs["big_entries"] = counts[big]
            finally:
                stop(unit)
            after = plain_files(PL)
            appended = grown(before, after)
            assert len(appended) == 1, f"{label}: the files grown by a log record are {sorted(appended)!r}"
            (files[label], seqs[label]), = appended.items()
    finally:
        session.close()

    assert files["rebuilt"] == files["running"]
    for k, v in costs.items():
        record_property(k, v)
    print(f"reported costs (never gated): {costs!r}")
    assert seqs["rebuilt"] == seqs["running"]
