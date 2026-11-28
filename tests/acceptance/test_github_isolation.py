"""An unanswered GitHub request is worded as the running unit words it, seen through check_ship_access (T-369).

A differential test: neither unit is imported by this process. The inputs come from S-519's
acceptance_inputs fixture; each unit is started alone by S-491's start through the shared launch
(S-506's InterposedLauncher), on its own copy restored from the staged store, reached only through
S-519's call, and stopped after its last call. The launch sends every GitHub destination to one
stand-in address, which this test chooses: OK, the check's GitHub stand-in (S-499) answering for a
readable repository and a known login; SILENT, a socket server that reads each request and never
answers; DOWN, a port nothing listens on. The in-process half of the S-96 tests, over the stored
copy, is tests/acceptance/inprocess/test_github_isolation_inprocess.py. Modules under test are
imported inside the test, so a module not yet written fails only the tests that need it.
"""

import json
import os
import re
import socket
import threading

START_VARIANT = "hhhhhh"  # every value healthy, reached on HEALTHY_PORT
ACTOR = "unreachable-check"
REACH_HOST = "127.0.0.1"
REPOSITORY = re.compile(r"^[\w.-]+/[\w.-]+$")
BEHAVIOURS = ("OK", "SILENT", "DOWN")


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


def listing(raw: bytes) -> list:
    doc = decode(raw)
    lists = [v for v in doc.values() if isinstance(v, list)] if isinstance(doc, dict) else [doc]
    assert len(lists) == 1 and isinstance(lists[0], list), f"the answer holds no single listing: {raw!r}"
    return lists[0]


class Silent:
    """SILENT: accepts, reads what each connection sends until it closes, records it, never answers."""

    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind((REACH_HOST, 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self.requests: list[bytes] = []
        self.conns: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def _read(self, conn: socket.socket) -> None:
        data = b""
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                if not data:
                    self.requests.append(chunk)
                data += chunk
        except OSError:
            pass

    def log(self) -> tuple[bytes, ...]:
        return tuple(self.requests)

    def close(self) -> None:
        self.listener.close()
        for conn in self.conns:
            try:
                conn.close()
            except OSError:
                pass


class Down:
    """DOWN: an address bound and closed again, so nothing listens on it."""

    def __init__(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((REACH_HOST, 0))
        self.port = s.getsockname()[1]
        s.close()


class Unit:
    """One started unit: its tool declarations from GET /tools, and a call by tool name."""

    def __init__(self, call, fill, port: int):
        self.call, self.fill, self.port = call, fill, port
        status, raw = call(port, "GET", "/tools", None)
        assert status == 200, f"GET /tools answered {status}: {raw!r}"
        self.tools = {t["name"]: t for t in decode(raw)["tools"]}

    def tool(self, name: str, **args: object) -> tuple[int, bytes]:
        spec = self.tools[name]
        declared = spec.get("input_schema", {}).get("properties", {})
        if "actor" in declared:
            args["actor"] = ACTOR
        params = re.findall(r"\{(\w+)\}", spec["path"])
        path = self.fill(spec["path"], {p: str(args[p]) for p in params})
        rest = {k: v for k, v in args.items() if k not in params}
        method = spec["method"].upper()
        return self.call(self.port, method, path, rest if rest or method != "GET" else None)


def repository_project(unit: Unit) -> str | None:
    """The first acceptance project whose get_repo answer names a repository."""
    status, raw = unit.tool("list_projects")
    assert status == 200, f"list_projects answered {status}: {raw!r}"
    for p in listing(raw):
        name = p if isinstance(p, str) else p["name"]
        status, repo = unit.tool("get_repo", project=name)
        if status == 200 and any(REPOSITORY.match(s) for s in strings(decode(repo))):
            return name
    return None


def test_unreachable_wording_matches_running_unit(request, tmp_path):
    # S-519's session fixture, looked up in the test after its conftest is imported: a conftest
    # not yet written then fails this test as a missing module, not its setup.
    from tests.acceptance.conftest import call, fill

    inputs = request.getfixturevalue("acceptance_inputs")

    from shipcheck.standin import Script
    from unitcheck.launch import InterposedLauncher
    from unitcheck.session import open_session

    from mu_spec.outside import ENV_NAMES
    from startcheck.runner import UnitCommand, restore, start, start_env, stop

    root = tmp_path / "check"
    root.mkdir()
    env = dict(os.environ)
    units = (("running", inputs.running_install), ("rebuilt", inputs.rebuilt_install))
    token = start_env(UnitCommand("running", inputs.running_install), START_VARIANT, str(root), env).get(
        ENV_NAMES["github_token"], "")

    session = open_session(env)
    silent, down = Silent(), Down()
    answers: dict[tuple[str, str], tuple[int, bytes]] = {}
    reached: dict[str, int] = {}
    project = None
    try:
        addresses = {"OK": session.standin, "SILENT": silent, "DOWN": down}
        for behaviour in BEHAVIOURS:
            # the one stand-in address the launch sends GitHub to, the same launcher for both units
            launcher = InterposedLauncher(addresses[behaviour], session.neighbour)
            for label, install in units:
                session.standin.reset(Script(issues=(), links=(), next_number=1, next_id=1,
                                             user={"login": ACTOR, "type": "User"}, push=True,
                                             refuse={}, unanswered=frozenset(), hold=None))
                session.neighbour.set("present")
                before = len(silent.log())
                restore(str(root), inputs.staged_store, inputs.pristine_prompts)
                started = start(UnitCommand(label, install), START_VARIANT, str(root), env, launcher)
                assert started.outcome == "listening" and started.port is not None, \
                    (behaviour, label, started.outcome, started.stderr)
                try:
                    unit = Unit(call, fill, started.port)
                    if project is None:
                        project = repository_project(unit)
                        assert project is not None, "no acceptance project's get_repo answer names a repository"
                    answers[(behaviour, label)] = unit.tool("check_ship_access", project=project)
                finally:
                    stop(started)
                if behaviour == "SILENT":
                    reached[label] = len(silent.log()) - before
    finally:
        silent.close()
        session.close()

    # 2. precondition: the call reached GitHub, so the comparison is not vacuous
    ok = answers[("OK", "running")]
    assert answers[("SILENT", "running")][1] != ok[1], (project, ok, answers[("SILENT", "running")])
    assert answers[("DOWN", "running")][1] != ok[1], (project, ok, answers[("DOWN", "running")])
    assert reached.get("running", 0) >= 1 and reached.get("rebuilt", 0) >= 1, reached
    alike = answers[("SILENT", "running")] == answers[("DOWN", "running")]
    print(f"the running unit's SILENT and DOWN answers for {project} are {'alike' if alike else 'different'}")

    # 1. the rebuilt unit answers as the running unit, status and body bytes
    for behaviour in ("SILENT", "DOWN"):
        assert answers[(behaviour, "rebuilt")] == answers[(behaviour, "running")], \
            (behaviour, project, answers[(behaviour, "running")], answers[(behaviour, "rebuilt")])

    # 3. the token is in no answer of either unit
    if token:
        for key, (_, body) in answers.items():
            assert token.encode("utf-8") not in body, key
