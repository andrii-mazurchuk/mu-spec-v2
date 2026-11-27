"""The acceptance inputs fixture, the read comparison helpers and the rebuilt package guard (T-381, T-382, T-384).

The modules under test are tests/acceptance/conftest.py and tests/acceptance/inprocess/conftest.py
(S-484). These are plain unit tests outside tests/acceptance, so neither fixture is set up for them:
the fixtures run only inside child pytest runs over a scratch copy, read back through a junit
report, and the helpers are called on the conftest loaded by file location under the name
'acceptance_conftest'. Every expected message is built from the conftest's own texts, never retyped.
The conftest is loaded inside each test, so a conftest not yet written fails only these tests.
"""

import http.server
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ACCEPTANCE = REPO / "tests" / "acceptance"
CONFTEST = ACCEPTANCE / "conftest.py"
INPROCESS_CONFTEST = ACCEPTANCE / "inprocess" / "conftest.py"

REF = "MU_SPEC_REFERENCE_INSTALL"
STORE = "MU_SPEC_ACCEPTANCE_STORE"
OLD = "MU_SPEC_RUNNING_INSTALL"


def load_conftest():
    """The repository's tests/acceptance/conftest.py as the module 'acceptance_conftest'."""
    assert CONFTEST.is_file(), "tests/acceptance/conftest.py is absent"
    spec = importlib.util.spec_from_file_location("acceptance_conftest", CONFTEST)
    module = importlib.util.module_from_spec(spec)
    sys.modules["acceptance_conftest"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop("acceptance_conftest", None)
        raise
    return module


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def snap(d: Path) -> dict[str, bytes]:
    """{relative path: bytes} of every file under d."""
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*") if p.is_file()}


class Runner:
    """Child pytest runs over SC/tests/acceptance, each with a fresh junit report."""

    def __init__(self, tmp_path: Path, sc: Path):
        self.tmp_path = tmp_path
        self.sc = sc
        self.count = 0

    def env(self, base: dict[str, str], over: dict[str, str]) -> dict[str, str]:
        env = dict(base)
        for name in (REF, STORE, OLD):
            env.pop(name, None)
        env.update(over)
        return env

    def run(self, cwd: Path, env: dict[str, str]) -> dict[str, tuple[bool, str, bool]]:
        """Per testcase name: (not passed, the error or failure child's message, skipped)."""
        self.count += 1
        x = self.tmp_path / f"report-{self.count}.xml"
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(self.sc / "tests" / "acceptance"), "-p", "no:cacheprovider",
             "--junitxml", str(x)],
            check=False, cwd=cwd, env=env, capture_output=True, text=True, timeout=600,
        )
        assert x.is_file(), f"no junit report\n{proc.stdout}\n{proc.stderr}"
        report = {}
        for case in ET.parse(x).getroot().iter("testcase"):
            bad = [c for c in case if c.tag in ("error", "failure")]
            message = bad[0].get("message", "") if bad else ""
            skipped = any(c.tag == "skipped" for c in case)
            report[case.get("name")] = (bool(bad), message, skipped)
        return report


# --- T-381 ---------------------------------------------------------------------------------------


def test_inputs_fail_never_skip_and_staging(tmp_path):
    conftest = load_conftest()
    U = lambda n: conftest.UNSET_TEXT.format(name=n)
    M = lambda n, v: conftest.MISSING_TEXT.format(name=n, value=v)

    sc = tmp_path / "scratch"
    acc = sc / "tests" / "acceptance"
    acc.mkdir(parents=True)
    shutil.copyfile(CONFTEST, acc / "conftest.py")
    dump = tmp_path / "dump.json"
    write(acc / "test_one.py", b"def test_one(): pass\n")
    write(acc / "test_two.py", b"def test_two(): pass\n")
    write(acc / "test_dump.py", (
        "import dataclasses\n"
        "import json\n\n\n"
        "def test_dump(acceptance_inputs):\n"
        f"    with open({str(dump)!r}, 'w', encoding='utf-8') as f:\n"
        "        f.write(json.dumps(dataclasses.asdict(acceptance_inputs)))\n"
    ).encode())
    write(acc / "inprocess" / "test_three.py", b"def test_three(): pass\n")

    ri = tmp_path / "ri"
    write(ri / "prompts" / "default.md", b"hi")
    ri_old = tmp_path / "ri_old"
    write(ri_old / "prompts" / "default.md", b"hi")
    ps = tmp_path / "ps"
    write(ps / "p1" / "manifest.json", b"{}")
    write(ps / "p1" / "sub" / "e.jsonl", b'{"id": "I-1"}\n')
    write(ps / "log.jsonl", b"x\n")
    nope = tmp_path / "nope"
    file = tmp_path / "f.txt"
    file.write_bytes(b"x")
    el = tmp_path / "elsewhere"
    el.mkdir()

    runner = Runner(tmp_path, sc)

    def run(over):
        return runner.run(el, runner.env(dict(os.environ), over))

    three = ("test_one", "test_two", "test_three")

    def all_fail(report, text, absent=None):
        for name in three:
            assert name in report, f"{name} was not run: {sorted(report)}"
            not_passed, message, _ = report[name]
            assert not_passed, f"{name} passed"
            assert text in message, f"{name}: {message!r} does not contain {text!r}"
            if absent is not None:
                assert absent not in message, f"{name}: {message!r} names {absent}"
        assert not any(skipped for _, _, skipped in report.values()), "a testcase was skipped"

    # 0
    assert conftest.INPUT_VARS == (REF, STORE)

    # 1
    all_fail(run({}), U(REF), absent=STORE)

    # 2: the retired name is never read
    all_fail(run({OLD: str(ri_old)}), U(REF), absent=STORE)
    all_fail(run({OLD: str(ri_old), STORE: str(ps)}), U(REF))

    # 3
    all_fail(run({REF: str(nope)}), M(REF, str(nope)))
    all_fail(run({REF: str(file)}), M(REF, str(file)))

    # 4
    all_fail(run({REF: str(ri)}), U(STORE))
    all_fail(run({REF: str(ri), STORE: str(nope)}), M(STORE, str(nope)))

    # 5
    before = snap(ps)
    report = run({REF: str(ri), STORE: str(ps)})
    assert sorted(report) == sorted(three + ("test_dump",))
    for name, (not_passed, message, skipped) in report.items():
        assert not not_passed and not skipped, f"{name}: {message!r}"
    got = json.loads(dump.read_text(encoding="utf-8"))
    running_install = Path(got["running_install"])
    pristine_projects = Path(got["pristine_projects"])
    assert running_install.is_absolute() and pristine_projects.is_absolute()
    assert running_install == ri
    assert pristine_projects == ps
    assert Path(got["pristine_prompts"]) == ri / "prompts"
    assert Path(got["rebuilt_install"]).resolve() == sc.resolve()
    staged = Path(got["staged_store"])
    assert staged.is_dir()
    assert [p.name for p in staged.iterdir()] == ["projects"]
    assert snap(staged / "projects") == before
    assert snap(ps) == before

    # 6
    dump.unlink()
    report = run({OLD: str(ri), REF: str(ri_old), STORE: str(ps)})
    assert sorted(report) == sorted(three + ("test_dump",))
    for name, (not_passed, message, skipped) in report.items():
        assert not not_passed and not skipped, f"{name}: {message!r}"
    got = json.loads(dump.read_text(encoding="utf-8"))
    assert Path(got["running_install"]) == ri_old


# --- T-382 ---------------------------------------------------------------------------------------

DISC = {
    "unit": "mu-spec",
    "tools": [
        {"name": "get_entry", "method": "GET", "path": "/projects/{project}/entries/{id}"},
        {"name": "post_x", "method": "POST", "path": "/x"},
        {"name": "list_projects", "method": "GET", "path": "/projects"},
    ],
}


class StandIn:
    """An http.server on 127.0.0.1 in one thread, recording each request, answering by path."""

    def __init__(self, answers: dict[str, tuple[int, bytes]]):
        self.requests: list[tuple[str, str, str | None, bytes]] = []
        stand_in = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                stand_in.requests.append((self.command, self.path, self.headers.get("Content-Type"), body))
                status, out = answers.get(self.path, (200, b'{"a": 1}'))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST = answer

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def servers():
    a = StandIn({})
    b = StandIn({"/projects/p/entries/S-1": (200, b'{"a": 2}'), "/projects": (404, b'{"a": 1}')})
    try:
        yield a, b
    finally:
        a.close()
        b.close()


def test_read_comparison_helpers(servers):
    conftest = load_conftest()
    a, b = servers
    ReadTool = conftest.ReadTool

    # 1
    assert conftest.read_tools(DISC) == (
        ReadTool("get_entry", "GET", "/projects/{project}/entries/{id}", ("project", "id")),
        ReadTool("list_projects", "GET", "/projects", ()),
    )

    # 2
    assert conftest.fill("/projects/{project}/entries/{id}", {"project": "a b", "id": "S-1"}) == (
        "/projects/a%20b/entries/S-1"
    )
    assert conftest.fill("/p/{project}", {"project": "x/y"}) == "/p/x%2Fy"
    with pytest.raises(KeyError):
        conftest.fill("/p/{project}", {})

    # 3
    calls, unc = conftest.read_calls(conftest.read_tools(DISC), "p", {"id": ["S-1", "S-2"]})
    assert [(c.tool, c.path) for c in calls] == [
        ("get_entry", "/projects/p/entries/S-1"),
        ("get_entry", "/projects/p/entries/S-2"),
        ("list_projects", "/projects"),
    ]
    assert all(c.args == {} for c in calls)
    assert unc == ()
    bare, bare_unc = conftest.read_calls(conftest.read_tools(DISC), "p", {})
    assert [(c.tool, c.path) for c in bare] == [("list_projects", "/projects")]
    assert bare_unc == ("get_entry",)

    # 4
    assert conftest.call(a.port, "GET", "/projects", {"k": 1}) == (200, b'{"a": 1}')
    assert a.requests[-1] == ("GET", "/projects", "application/json", json.dumps({"k": 1}).encode())
    assert conftest.call(a.port, "GET", "/projects", None) == (200, b'{"a": 1}')
    assert a.requests[-1][:2] == ("GET", "/projects")
    assert a.requests[-1][3] == b""

    # 5
    assert conftest.compare_reads(a.port, a.port, calls) == []

    # 6
    lines = conftest.compare_reads(a.port, b.port, calls)
    assert len(lines) == 2, lines
    first, second = lines
    assert "get_entry" in first and "/projects/p/entries/S-1" in first
    assert re.search(r"(?<!\d)6(?!\d)", first), first
    assert "list_projects" in second and "/projects" in second
    assert not any("/projects/p/entries/S-2" in line for line in lines)


# --- T-384 ---------------------------------------------------------------------------------------


def test_rebuilt_package_guard(tmp_path):
    conftest = load_conftest()
    assert INPROCESS_CONFTEST.is_file(), "tests/acceptance/inprocess/conftest.py is absent"

    sc = tmp_path / "scratch"
    acc = sc / "tests" / "acceptance"
    (acc / "inprocess").mkdir(parents=True)
    shutil.copyfile(CONFTEST, acc / "conftest.py")
    shutil.copyfile(INPROCESS_CONFTEST, acc / "inprocess" / "conftest.py")
    write(acc / "inprocess" / "test_in.py", b"def test_in(): pass\n")
    write(sc / "mu_spec" / "__init__.py", b"MARK = 'rebuilt'\n")
    other = tmp_path / "other"
    write(other / "mu_spec" / "__init__.py", b"MARK = 'other'\n")
    ri = tmp_path / "ri"
    write(ri / "prompts" / "default.md", b"hi")
    ps = tmp_path / "ps"
    write(ps / "p1" / "manifest.json", b"{}")

    runner = Runner(tmp_path, sc)
    base = dict(os.environ)
    base.pop("PYTHONPATH", None)
    env = runner.env(base, {REF: str(ri), STORE: str(ps)})

    # 1
    report = runner.run(sc, env)
    assert "test_in" in report, sorted(report)
    not_passed, message, skipped = report["test_in"]
    assert not not_passed and not skipped, message

    # 2
    report = runner.run(other, env)
    not_passed, message, skipped = report["test_in"]
    assert not_passed, "test_in passed with another mu_spec first on sys.path"
    assert not skipped
    assert "mu_spec was imported from" in message and "not from the rebuilt install" in message, message
    assert repr(str(sc)) in message or repr(str(sc.resolve())) in message, message

    # 3
    unset = dict(env)
    unset.pop(REF)
    report = runner.run(sc, unset)
    not_passed, message, skipped = report["test_in"]
    assert not_passed and not skipped
    assert conftest.UNSET_TEXT.format(name=REF) in message, message
