"""The start check: its variant set, runner, restore, bind-line reading, judge and quick run.

Modules under test are imported inside each test, so a module not yet written fails only the
tests that need it. The quick run judges the running unit, the install at MU_SPEC_REFERENCE_INSTALL,
against the rebuilt unit, this repository.
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

HEALTHY_SHA = "0123456789abcdef0123456789abcdef01234567"
REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_VAR = "MU_SPEC_REFERENCE_INSTALL"
STORE_VAR = "MU_SPEC_ACCEPTANCE_STORE"


def test_variant_set(tmp_path):
    from startcheck.variants import (
        ALL_VARIANTS,
        OVERRIDE_VARIANTS,
        QUICK,
        VARIANTS,
        environment,
    )

    assert len(VARIANTS) == 4096
    assert VARIANTS[0] == "hhhhhh"
    assert VARIANTS[1] == "hhhhhm"
    assert VARIANTS[3] == "hhhhhx"
    assert VARIANTS[4] == "hhhhmh"
    assert VARIANTS[-1] == "xxxxxx"

    override_ids = "hhhhhhh hhhhhhe hhhhhhx mhhhhhh mhhhhhe mhhhhhx ehhhhhh ehhhhhe ehhhhhx"
    assert OVERRIDE_VARIANTS == tuple((override_ids + " xhhhhhh xhhhhhe xhhhhhx").split())
    assert len(set(ALL_VARIANTS)) == 4108

    single_faults = {"h" * i + code + "h" * (5 - i) for i in range(6) for code in "mex"}
    assert set(QUICK) == {"hhhhhh", "mmmmmm", "eeeeee", "xxxxxx"} | single_faults
    assert len(QUICK) == 22
    assert set(QUICK) <= set(VARIANTS)

    r = str(tmp_path)
    own = {"PATH": "p", "HOME": "h", "FOO": "f", "PYTHONPATH": "z"}
    assert environment("hexmhm", r, own) == {
        "PATH": "p",
        "HOME": "h",
        "HOLONIC_STATE_DIR": os.path.join(r, "store"),
        "MU_SPEC_HOST": "",
        "MU_SPEC_PORT": "abc",
        "HOLONIC_GIT_SHA": HEALTHY_SHA,
    }
    assert environment("xhhhhhe", r, {}) == {
        "HOLONIC_STATE_DIR": os.path.join(r, "notes.txt"),
        "MU_SPEC_HOST": "127.0.0.1",
        "MU_SPEC_PORT": "19406",
        "MU_SPEC_PROMPTS_DIR": os.path.join(r, "prompts"),
        "HOLONIC_GIT_SHA": HEALTHY_SHA,
        "MU_SPEC_GITHUB_TOKEN": "ghp_startcheck_healthy",
        "MU_SPEC_ROOT": "",
    }
    given_root = os.path.join(r, "store", "projects")
    assert environment("hhhhhhh", r, {})["MU_SPEC_ROOT"] == given_root
    assert "MU_SPEC_ROOT" not in environment("hhhhhh", r, {})

    for bad in ("hhhhh", "hhhhhhm", "hhhhhq"):
        with pytest.raises(ValueError):
            environment(bad, r, {})


def test_predicted_counts():
    from startcheck import predict
    from startcheck.variants import ALL_VARIANTS, OVERRIDE_VARIANTS, QUICK, VARIANTS

    predicted_outcome = predict.predicted_outcome

    def listening(ids):
        return {v for v in ids if predicted_outcome(v) == "listening"}

    assert len(listening(VARIANTS)) == 1152  # 3 * 3 * 2 * 4**3
    assert len(VARIANTS) - len(listening(VARIANTS)) == 2944

    override_listening = {"hhhhhhh", "mhhhhhh", "ehhhhhh", "xhhhhhh"} | {
        "hhhhhhe",
        "mhhhhhe",
        "ehhhhhe",
    }
    assert len(listening(OVERRIDE_VARIANTS)) == 7
    assert listening(OVERRIDE_VARIANTS) == override_listening

    assert len(listening(ALL_VARIANTS)) == 1159
    assert len(ALL_VARIANTS) - len(listening(ALL_VARIANTS)) == 2949

    assert len(listening(QUICK)) == 16
    quick_exited = {"xhhhhh", "hxhhhh", "hhehhh", "hhxhhh", "eeeeee", "xxxxxx"}
    assert set(QUICK) - listening(QUICK) == quick_exited

    assert predicted_outcome("xhhhhhh") == "listening"
    assert predicted_outcome("xhhhhhe") == "exited"
    assert predicted_outcome("hhhhhhx") == "exited"
    for v in ("hhmhhh", "hehhhh", "hhhxxx", "mmmmmm"):
        assert predicted_outcome(v) == "listening", v

    for bad in ("hhhhh", "hhhhhhm"):
        with pytest.raises(ValueError):
            predicted_outcome(bad)


def _files(folder):
    """Every regular file under folder, as relative posix path -> bytes."""
    return {
        p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()
    }


def _refused(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=5).close()
    except ConnectionRefusedError:
        return True
    return False


_FAKE_PREAMBLE = """\
import json, os, sys, time
with open({dump!r}, 'w') as f:
    f.write(json.dumps({{'name': __name__, 'env': dict(os.environ),
                        'isolated': sys.flags.isolated,
                        'no_user_site': sys.flags.no_user_site,
                        'hash_randomization': sys.flags.hash_randomization,
                        'flags': sys.orig_argv[1:3], 'path0': sys.path[0], 'cwd': os.getcwd()}}))
"""

_FAKE_BODIES = {
    "serve": """\
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == '/health' else 404)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *args):
        pass

HTTPServer(('127.0.0.1', int(os.environ['MU_SPEC_PORT'])), Handler).serve_forever()
""",
    "fail": "sys.stderr.buffer.write(b'err\\n'); sys.exit(3)\n",
    "zero": "sys.stdout.buffer.write(b'bye\\n'); sys.exit(0)\n",
    "sleep": "time.sleep(60)\n",
}


def _fake_install(root, program=None, package_main=None):
    """A fake install root: mu_spec/__init__.py (empty), and a mu_spec/main.py running program
    under `if __name__ == '__main__':`. A mu_spec/__main__.py exists only when package_main is given.
    """
    (root / "mu_spec").mkdir(parents=True)
    (root / "mu_spec" / "__init__.py").write_text("")
    if program is not None:
        main = "if __name__ == '__main__':\n" + textwrap.indent(program, "    ")
        (root / "mu_spec" / "main.py").write_text(main)
    if package_main is not None:
        (root / "mu_spec" / "__main__.py").write_text(package_main)
    return root


def _pristine(tmp_path):
    """Small pristine store and prompts folders, and an empty check root."""
    ps = tmp_path / "ps"
    (ps / "projects" / "a").mkdir(parents=True)
    (ps / "projects" / "a" / "x.json").write_bytes(b"{}")
    pp = tmp_path / "pp"
    pp.mkdir()
    (pp / "default.md").write_bytes(b"d")
    root = tmp_path / "check"
    root.mkdir()
    return str(root), str(ps), str(pp)


def _env_of(env):
    """A started program's environment less Windows' per-drive '=' names."""
    return {k: v for k, v in env.items() if not k.startswith("=")}


def test_runner_outcomes(tmp_path, monkeypatch):
    from startcheck.runner import (
        BOOTSTRAP,
        ENTRY_FILE,
        ENTRY_MODULE,
        START_ALLOWANCE_S,
        RunnerError,
        UnitCommand,
        alive,
        restore,
        start,
        start_env,
        stop,
    )

    assert ENTRY_MODULE == "mu_spec.main"
    assert ENTRY_FILE == ("mu_spec", "main.py")
    assert "run_module('mu_spec.main', run_name='__main__', alter_sys=True)" in BOOTSTRAP
    assert "run_module('mu_spec'," not in BOOTSTRAP

    dump = tmp_path / "dump.json"
    fakes = {
        name: _fake_install(tmp_path / "fakes" / name, _FAKE_PREAMBLE.format(dump=str(dump)) + body)
        for name, body in _FAKE_BODIES.items()
    }
    fakes["pkgonly"] = _fake_install(
        tmp_path / "fakes" / "pkgonly", package_main="import sys; sys.exit(3)\n"
    )
    root, ps, pp = _pristine(tmp_path)
    monkeypatch.setenv("X_STRAY", "1")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    own = os.environ

    def cmd(r):
        return UnitCommand("rebuilt", str(r))

    def launch(name, variant):
        restore(root, ps, pp)
        return start(cmd(fakes[name]), variant, root, own)

    s = launch("serve", "hhhhhh")
    try:
        assert s.outcome == "listening"
        assert s.port == 19406
        assert s.exit_code is None
        assert s.prepared is None
        assert alive(s)
        d = json.loads(dump.read_text())
        assert d["name"] == "__main__"
        expected = start_env(cmd(fakes["serve"]), "hhhhhh", root, own)
        assert expected["PYTHONHASHSEED"] == "0"
        if sys.platform == "win32":
            assert expected["SYSTEMROOT"] == os.environ["SYSTEMROOT"]
        assert "X_STRAY" not in expected and "PYTHONPATH" not in expected
        assert _env_of(d["env"]) == expected
        assert d["isolated"] == 0
        assert d["no_user_site"] == 1
        assert d["hash_randomization"] == 0
        assert d["flags"] == ["-s", "-c"]
        assert d["path0"] == str(fakes["serve"])
        assert d["cwd"] == os.path.join(root, "work")
    finally:
        stop(s)
    assert _refused(19406)
    assert not alive(s)

    s = launch("fail", "hhhhhh")
    assert s.outcome == "exited"
    assert s.exit_code == 3
    assert s.stderr == b"err\n"

    s = launch("zero", "hhhhhh")
    assert s.outcome == "hung"
    assert s.exit_code == 0
    assert s.stdout == b"bye\n"

    dump.unlink(missing_ok=True)
    s = launch("pkgonly", "hhhhhh")
    assert s.outcome == "exited"
    assert s.exit_code is not None and s.exit_code != 3, s.exit_code
    assert b"mu_spec.main" in s.stderr, s.stderr
    assert not dump.exists()

    assert START_ALLOWANCE_S == 15.0
    began = time.monotonic()
    s = launch("sleep", "hhehhh")
    took = time.monotonic() - began
    assert s.outcome == "hung"
    assert s.exit_code is None
    assert s.port is None
    assert START_ALLOWANCE_S <= took <= START_ALLOWANCE_S + 5, took
    assert not alive(s)

    for taken in (9006, 19406):
        dump.unlink(missing_ok=True)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", taken))
            listener.listen()
            restore(root, ps, pp)
            with pytest.raises(RunnerError, match=str(taken)):
                start(cmd(fakes["serve"]), "hhhhhh", root, own)
        assert not dump.exists(), taken


# A main.py program: writes its hash of 'mu-spec', the interpreter's flags and its environment's
# names, then exits 3.
_HASH_FAKE = """\
import json, os, sys
sys.stdout.write(json.dumps({'hash': repr(hash('mu-spec')), 'hr': sys.flags.hash_randomization,
                             'iso': sys.flags.isolated,
                             'names': sorted(n for n in os.environ if not n.startswith('='))}))
sys.exit(3)
"""


def test_runner_hash_seed(tmp_path):
    from startcheck.runner import (
        BOOTSTRAP,
        RunnerError,
        UnitCommand,
        effective_seed,
        interpreter_flags,
        restore,
        seed_env,
        start,
        start_env,
        unit_argv,
    )
    from startcheck.variants import environment

    for c in (UnitCommand("rebuilt", "R"), UnitCommand("rebuilt", "R", 12345)):
        assert unit_argv(c) == (sys.executable, "-s", "-c", BOOTSTRAP, "R"), c
        assert interpreter_flags(c) == ("-s",), c

    assert effective_seed(UnitCommand("u", "R")) == 0
    assert effective_seed(UnitCommand("u", "R", 7)) == 7
    assert seed_env(UnitCommand("u", "R")) == {"PYTHONHASHSEED": "0"}
    assert seed_env(UnitCommand("u", "R", 7)) == {"PYTHONHASHSEED": "7"}

    h = _fake_install(tmp_path / "h", _HASH_FAKE)
    root, ps, pp = _pristine(tmp_path)
    own = dict(os.environ, PYTHONPATH="x", PYTHONHOME="y")

    variant_env = environment("hhhhhh", root, own)
    assert start_env(UnitCommand("u", "R"), "hhhhhh", root, own) == variant_env | {
        "PYTHONHASHSEED": "0"
    }
    assert start_env(UnitCommand("u", "R", 7), "hhhhhh", root, own) == variant_env | {
        "PYTHONHASHSEED": "7"
    }

    def out(seed):
        restore(root, ps, pp)
        s = start(UnitCommand("rebuilt", str(h), seed), "hhhhhh", root, own)
        assert (s.outcome, s.exit_code) == ("exited", 3), (seed, s)
        return json.loads(s.stdout)

    a, b = out(None), out(None)
    assert a == b
    assert a["hr"] == 0
    assert a["iso"] == 0
    assert "PYTHONHASHSEED" in a["names"]
    assert "PYTHONPATH" not in a["names"] and "PYTHONHOME" not in a["names"]

    assert out(12345) == out(12345)
    assert out(54321)["hash"] != out(12345)["hash"]

    out(4294967295)
    out(0)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 9006))
        listener.listen()
        for bad in (-1, 4294967296, True, "5"):
            restore(root, ps, pp)
            with pytest.raises(ValueError) as caught:
                start(UnitCommand("rebuilt", str(h), bad), "hhhhhh", root, own)
            assert not isinstance(caught.value, RunnerError), bad
        restore(root, ps, pp)
        with pytest.raises(RunnerError, match="9006"):
            start(UnitCommand("rebuilt", str(h), 5), "hhhhhh", root, own)


def test_restore_and_reach_port(tmp_path):
    from startcheck.runner import reach_port, restore

    root = tmp_path / "c"
    ps = tmp_path / "ps"
    (ps / "projects" / "a").mkdir(parents=True)
    (ps / "projects" / "a" / "x.json").write_bytes(b"{}")
    pp = tmp_path / "pp"
    pp.mkdir()
    (pp / "default.md").write_bytes(b"d")
    ps_before, pp_before = _files(ps), _files(pp)

    (root / "work" / "state" / "projects").mkdir(parents=True)
    (root / "work" / "state" / "projects" / "junk").write_bytes(b"junk")
    (root / "work" / "default.md").write_bytes(b"junk")
    (root / "store" / "projects" / "b").mkdir(parents=True)
    (root / "store" / "projects" / "b" / "y.json").write_bytes(b"junk")
    (root / "prompts").mkdir()
    (root / "prompts" / "extra.md").write_bytes(b"junk")
    (root / "notes.txt").write_bytes(b"other")
    (root / "nope").mkdir()

    restore(str(root), str(ps), str(pp))

    assert (root / "work").is_dir()
    assert list((root / "work").iterdir()) == []
    assert _files(root / "store") == {"projects/a/x.json": b"{}"}
    assert _files(root / "prompts") == {"default.md": b"d"}
    assert (root / "notes.txt").read_bytes() == b"not a directory\n"
    assert not (root / "nope").exists()
    assert _files(ps) == ps_before
    assert _files(pp) == pp_before

    assert reach_port("hhhhhh") == 19406
    assert reach_port("hhmhhh") == 9006
    assert reach_port("hhehhh") is None
    assert reach_port("hhxhhh") is None
    assert reach_port("hhhhhhx") == 19406
    assert reach_port("xhhhhhe") == 19406


# Scratch programs a fixture Launcher runs as `-c` text; each writes its environment's pairs to {dump}.
_PROG_DUMP = """\
import json, os, sys
def dump_env():
    with open({dump!r}, 'w') as f:
        f.write(json.dumps(sorted((k, v) for k, v in os.environ.items() if not k.startswith('='))))
"""

_PROG_SERVE = """\
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == '/health' else 404)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *args):
        pass

server = HTTPServer(('127.0.0.1', int(os.environ['MU_SPEC_PORT'])), Handler)
dump_env()
server.serve_forever()
"""


class Rec:
    """A fixture Launcher: records each prepare, each ready argument and each done call."""

    def __init__(self, prog, extra=None, argv=None, ready=lambda left: True):
        self.prog = prog
        self.extra = {"X_CHECK": "1"} if extra is None else extra
        self.argv = argv
        self._ready = ready
        self.prepares = []
        self.returned = []
        self.ready_args = []
        self.done_calls = 0

    def prepare(self, cmd, check_root):
        from startcheck.runner import Prepared

        self.prepares.append((cmd, check_root))

        def ready(left):
            self.ready_args.append(left)
            return self._ready(left)

        def done():
            self.done_calls += 1

        argv = self.argv or (sys.executable, "-s", "-c", self.prog)
        prepared = Prepared(argv=argv, extra_env=self.extra, ready=ready, done=done)
        self.returned.append(prepared)
        return prepared


def test_runner_launcher(tmp_path):
    from startcheck.runner import (
        RunnerError,
        UnitCommand,
        restore,
        start,
        start_env,
        stop,
    )

    dump = tmp_path / "dump.json"
    head = _PROG_DUMP.format(dump=str(dump))
    p_fail = head + "dump_env()\nsys.exit(3)\n"
    p_zero = head + "dump_env()\nsys.exit(0)\n"
    p_serve = head + _PROG_SERVE
    root, ps, pp = _pristine(tmp_path)
    cmd = UnitCommand("rebuilt", str(tmp_path / "unused"))
    own = os.environ

    def launch(rec):
        dump.unlink(missing_ok=True)
        restore(root, ps, pp)
        return start(cmd, "hhhhhh", root, own, rec)

    rec = Rec(p_fail)
    s = launch(rec)
    assert (s.outcome, s.exit_code) == ("exited", 3), s
    assert len(rec.prepares) == 1
    assert len(rec.ready_args) == 1 and 0 < rec.ready_args[0] <= 15.0, rec.ready_args
    assert rec.done_calls == 1
    assert s.prepared is rec.returned[0]
    expected = sorted((start_env(cmd, "hhhhhh", root, own) | {"X_CHECK": "1"}).items())
    assert json.loads(dump.read_text()) == [list(pair) for pair in expected]

    rec = Rec(p_zero)
    s = launch(rec)
    assert (s.outcome, s.exit_code) == ("hung", 0), s
    assert rec.done_calls == 1

    rec = Rec(p_serve)
    s = launch(rec)
    try:
        assert s.outcome == "listening", s
        assert rec.done_calls == 0
    finally:
        stop(s)
    assert rec.done_calls == 1

    refused = [
        Rec(p_fail, extra={"MU_SPEC_PORT": "1"}),
        Rec(p_fail, extra={"PYTHONHASHSEED": "1"}),
        Rec(p_fail, extra={"PYTHONPATH": "x"}),
        Rec(p_fail, extra={"pythonstartup": "x"}),
        Rec(p_fail, argv=(sys.executable, "-I", "-c", p_fail)),
        Rec(p_fail, argv=(sys.executable, "-s", "-E", "-c", p_fail)),
        Rec(p_fail, argv=(sys.executable, "-c", p_fail)),
    ]
    for rec in refused:
        with pytest.raises(RunnerError):
            launch(rec)
        assert len(rec.prepares) == 1, (rec.extra, rec.argv)
        assert not dump.exists(), (rec.extra, rec.argv)

    def wait_then_refuse(left):
        deadline = time.monotonic() + left
        while not dump.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        return False

    rec = Rec(p_serve, ready=wait_then_refuse)
    with pytest.raises(RunnerError, match="rebuilt"):
        launch(rec)
    deadline = time.monotonic() + 5
    while not _refused(19406):
        assert time.monotonic() < deadline, "19406 still answers after a failed ready"
        time.sleep(0.1)
    assert len(rec.prepares) == 1


BIND_SAMPLES = [
    (
        b"mu-spec cannot bind not a host!:19406: [Errno 11001] getaddrinfo failed\r\n",
        (b"not a host!", b"19406", b"[Errno 11001] getaddrinfo failed", b"\r\n"),
    ),
    (
        b"mu-spec cannot bind ::1:9006: [Errno 98] Address already in use\n",
        (b"::1", b"9006", b"[Errno 98] Address already in use", b"\n"),
    ),
    (b"mu-spec cannot bind h:1: a: b\n", (b"h", b"1", b"a: b", b"\n")),
    (b"mu-spec cannot bind :9006: x\n", (b"", b"9006", b"x", b"\n")),
]

NOT_BIND_LINES = [
    b"mu-spec cannot bind h:1: a\nsecond\n",
    b"mu-spec cannot bind h:1: a",
    b"Traceback (most recent call last):\n",
    b"mu-spec cannot bind h1: x\n",
    b"",
]


def test_bind_line_reading():
    from startcheck.bindline import bind_match, masked, read_bind_line

    for err, expected in BIND_SAMPLES:
        line = read_bind_line(err)
        assert line is not None, err
        assert (line.host, line.port, line.reason, line.ending) == expected, err

    for err in NOT_BIND_LINES:
        assert read_bind_line(err) is None, err

    first = BIND_SAMPLES[0][0]
    assert masked(read_bind_line(first)) == b"mu-spec cannot bind <host>:<port>: <reason>\r\n"

    other = b"mu-spec cannot bind not a host!:19406: [Errno -2] Name or service not known\r\n"
    assert bind_match(first, other) is True
    assert bind_match(first, first[:-2] + b"\n") is False
    assert bind_match(first, b"") is False

    empty_host = b"mu-spec cannot bind :9006: x\n"
    assert bind_match(empty_host, empty_host) is False
    assert bind_match(b"Traceback (most recent call last):\n", b"") is None


def test_variant_passed_rule():
    from dataclasses import replace

    from startcheck.verdicts import (
        CallVerdict,
        VariantVerdict,
        exit_class,
        variant_passed,
    )

    base = VariantVerdict(
        variant="hhhhhh",
        predicted="listening",
        running_outcome="listening",
        rebuilt_outcome="listening",
        running_exit_class=None,
        rebuilt_exit_class=None,
        outcome_match=True,
        exit_class_match=True,
        prediction_fault=False,
        running_bind_line=False,
        bind_match=None,
        install_values_checked=False,
        install_values_ok=True,
        health_answered=True,
        calls=(CallVerdict(0, True, True), CallVerdict(1, True, True)),
        both_alive=True,
        passed=True,
    )
    ex = replace(
        base,
        running_outcome="exited",
        rebuilt_outcome="exited",
        running_exit_class="nonzero",
        rebuilt_exit_class="nonzero",
        calls=(),
        health_answered=False,
        both_alive=None,
    )

    assert variant_passed(base) is True
    assert variant_passed(ex) is True

    for term in ("outcome_match", "exit_class_match", "install_values_ok"):
        assert variant_passed(replace(base, **{term: False})) is False, term

    assert variant_passed(replace(ex, bind_match=None)) is True
    assert variant_passed(replace(ex, bind_match=False)) is False
    assert variant_passed(replace(ex, bind_match=True)) is True

    # The running unit decides; a prediction that disagrees is reported apart.
    assert variant_passed(replace(base, prediction_fault=True, predicted="exited")) is True

    assert variant_passed(replace(base, both_alive=False)) is False
    assert variant_passed(replace(base, health_answered=False)) is False
    body_off = (CallVerdict(0, True, True), CallVerdict(1, True, False))
    status_off = (CallVerdict(0, True, True), CallVerdict(1, False, True))
    assert variant_passed(replace(base, calls=body_off)) is False
    assert variant_passed(replace(base, calls=status_off)) is False

    # health_answered and both_alive apply only where both listen.
    assert variant_passed(replace(ex, health_answered=False, both_alive=None)) is True

    hung = replace(
        ex,
        running_outcome="hung",
        rebuilt_outcome="hung",
        running_exit_class="zero",
        rebuilt_exit_class="zero",
        outcome_match=False,
    )
    assert variant_passed(hung) is False

    assert exit_class(None) is None
    assert exit_class(0) == "zero"
    assert exit_class(3) == "nonzero"
    assert exit_class(-9) == "nonzero"
    assert exit_class(1) == exit_class(3)


def test_malformed_host_has_no_colon_space(tmp_path):
    from startcheck.variants import malformed

    # '/x: y' holds ': ', so a host built from check_root fails here.
    for check_root in (str(tmp_path), "/x: y"):
        host = malformed("MU_SPEC_HOST", check_root)
        assert ": " not in host, (check_root, host)
        assert host == "not a host!", (check_root, host)


# A fake install's unit: exits 1 on a present port that is empty or not an integer, else serves every
# path on 127.0.0.1 (9006 when the port is missing). It first appends its install root to {starts}.
_JUDGE_FAKE = """\
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRONG, CFG, STAMP = {wrong!r}, {cfg!r}, {stamp!r}
with open({starts!r}, 'a') as f:
    f.write(ROOT + '\\n')

port_text = os.environ.get('MU_SPEC_PORT')
port = 9006
if port_text is not None:
    try:
        port = int(port_text)
    except ValueError:
        sys.stderr.buffer.write(b'mu-spec cannot start: MU_SPEC_PORT\\n')
        sys.exit(1)


def git_sha():
    given = os.environ.get('HOLONIC_GIT_SHA', '').strip()
    if given:
        return given
    if WRONG:
        return 'a' * 40
    with open(os.path.join(ROOT, '.git', 'HEAD')) as f:
        return f.read().strip()


class Handler(BaseHTTPRequestHandler):
    def _answer(self):
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        path = self.path
        if path in ('/stats', '/stats/structure'):
            metrics = {{'git_sha': git_sha()}}
            if STAMP and path == '/stats':
                metrics['served_at'] = time.time()
            doc = {{'unit': 'mu-spec', 'computed_at': time.time(), 'metrics': metrics}}
        elif CFG and path == '/config':
            doc = {{'x': 1, 'path': '/config'}}
        else:
            doc = {{'path': path}}
        body = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
"""


def _judge_fake(tmp_path, name, head, starts, wrong=False, cfg=False, stamp=False):
    root = tmp_path / "fakes" / name
    (root / "mu_spec").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_bytes(head.encode() + b"\n")
    (root / "mu_spec" / "__init__.py").write_text("")
    main = _JUDGE_FAKE.format(wrong=wrong, cfg=cfg, stamp=stamp, starts=str(starts))
    (root / "mu_spec" / "__main__.py").write_text(main)
    return root


class PassThrough:
    """A Launcher that launches each unit as a direct start would, and records whose start it prepared."""

    def __init__(self):
        self.prepared = []

    def prepare(self, cmd, check_root):
        from startcheck.runner import Prepared, unit_argv

        self.prepared.append(cmd.install_root)
        return Prepared(argv=unit_argv(cmd), extra_env={}, ready=lambda left: True, done=lambda: None)


def _started(starts):
    return starts.read_text().splitlines() if starts.exists() else []


def test_judge_masks_with_fakes(tmp_path):
    from startcheck.judge import CALLS, install_commit, judge_variant, run
    from startcheck.runner import UnitCommand

    starts = tmp_path / "starts.txt"
    A = _judge_fake(tmp_path, "A", "a" * 40, starts)
    B = _judge_fake(tmp_path, "B", "b" * 40, starts)
    Bwrong = _judge_fake(tmp_path, "Bwrong", "b" * 40, starts, wrong=True)
    Bcfg = _judge_fake(tmp_path, "Bcfg", "b" * 40, starts, cfg=True)
    Bstamp = _judge_fake(tmp_path, "Bstamp", "b" * 40, starts, stamp=True)

    def cmd(r):
        return UnitCommand("u", str(r))

    ps = tmp_path / "ps"
    (ps / "projects").mkdir(parents=True)
    pp = tmp_path / "pp"
    pp.mkdir()
    (pp / "default.md").write_bytes(b"d")
    root = tmp_path / "check"
    root.mkdir()
    rest = (str(root), str(ps), str(pp), os.environ)

    assert len(CALLS) == 19
    assert install_commit(str(A)) == "a" * 40

    # computed_at differs on every answer and is masked; git_sha is the healthy value on both.
    v = judge_variant("hhhhhh", cmd(A), cmd(B), *rest)
    assert v.passed is True, v
    assert v.install_values_checked is False, v
    assert len(v.calls) == 19, v
    assert all(c.status_match and c.body_match for c in v.calls), v

    # HOLONIC_GIT_SHA missing: each unit's git_sha is its own install commit, then masked.
    v = judge_variant("hhhhmh", cmd(A), cmd(B), *rest)
    assert v.passed is True, v
    assert v.install_values_checked is True, v
    assert v.install_values_ok is True, v

    v = judge_variant("hhhhmh", cmd(A), cmd(Bwrong), *rest)
    assert v.install_values_ok is False, v
    assert v.passed is False, v

    v = judge_variant("hhhhhh", cmd(A), cmd(Bcfg), *rest)
    assert v.calls[6].status_match is True, v
    assert v.calls[6].body_match is False, v
    assert all(c.status_match and c.body_match for i, c in enumerate(v.calls) if i != 6), v
    assert v.passed is False, v

    # A time-named field outside TIME_FIELDS is never masked.
    v = judge_variant("hhhhhh", cmd(A), cmd(Bstamp), *rest)
    assert v.calls[13].body_match is False, v
    assert v.passed is False, v

    # The floor refuses with a launcher given; the launcher rule refuses with the floor met.
    launcher = PassThrough()
    before = _started(starts)
    with pytest.raises(ValueError):
        run(("hhxhhh",), cmd(A), cmd(B), *rest, launcher=launcher)
    assert launcher.prepared == []
    assert _started(starts) == before
    with pytest.raises(ValueError):
        run(("hhhhhh", "hhxhhh"), cmd(A), cmd(B), *rest, launcher=None)
    assert _started(starts) == before

    launcher = PassThrough()
    report = run(("hhhhhh", "hhxhhh"), cmd(A), cmd(B), *rest, launcher=launcher)
    assert len(report.verdicts) == 2, report
    assert report.missing == (), report
    assert report.healthy_listened is True, report
    assert report.listening == 1, report
    assert report.prediction_faults == (), report
    assert report.passed is True, report
    (exited,) = [v for v in report.verdicts if v.variant == "hhxhhh"]
    assert (exited.running_outcome, exited.rebuilt_outcome) == ("exited", "exited"), exited
    assert exited.running_bind_line is False, exited
    assert exited.bind_match is None, exited
    assert exited.calls == (), exited
    assert launcher.prepared == [str(A), str(B), str(A), str(B)]


def test_quick_variants(tmp_path):
    from startcheck.judge import run
    from startcheck.runner import UnitCommand
    from startcheck.variants import QUICK

    reference = os.environ.get(REFERENCE_VAR)
    if not reference:
        pytest.fail(f"{REFERENCE_VAR} is not set: it must name the running unit's install root")
    running = UnitCommand("running", reference)
    rebuilt = UnitCommand("rebuilt", str(REPO_ROOT))
    check_root = tmp_path / "check"
    check_root.mkdir()
    pristine_store = tmp_path / "pristine_store"
    (pristine_store / "projects").mkdir(parents=True)
    pristine_prompts = REPO_ROOT / "prompts"

    try:
        from unitcheck.session import open_session

        session = open_session(os.environ)
    except Exception as exc:  # noqa: BLE001 - any refusal to give a launcher fails the test
        pytest.fail(f"no shared launcher can be given: open_session raised {exc!r}")
    try:
        launcher = session.launcher()
        report = run(QUICK, running, rebuilt, str(check_root), str(pristine_store),
                     str(pristine_prompts), os.environ, launcher=launcher)
        controls = session.controls()
    finally:
        session.close()

    failing = "\n".join(repr(v) for v in report.verdicts if not v.passed)
    detail = f"prediction faults: {report.prediction_faults}\nfailing verdicts:\n{failing}"
    assert len(report.verdicts) == 22, detail
    assert report.missing == (), detail
    assert report.healthy_listened is True, detail
    assert report.passed is True, detail
    # both units of each variant started through the launcher, one prepare per start
    assert len(controls) == 44, detail


def _acceptance_conftest():
    """tests/acceptance/conftest.py, loaded by path: pytest reads it only when collecting that folder."""
    path = REPO_ROOT / "tests" / "acceptance" / "conftest.py"
    spec = importlib.util.spec_from_file_location("_acceptance_conftest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Recorded(Mapping):
    """A Mapping over d that records every key looked up, tested or iterated."""

    def __init__(self, d):
        self._d = dict(d)
        self.keys_read = set()

    def __getitem__(self, key):
        self.keys_read.add(key)
        return self._d[key]

    def get(self, key, default=None):
        self.keys_read.add(key)
        return self._d.get(key, default)

    def __contains__(self, key):
        self.keys_read.add(key)
        return key in self._d

    def __iter__(self):
        for key in self._d:
            self.keys_read.add(key)
            yield key

    def __len__(self):
        return len(self._d)


def test_read_inputs(tmp_path):
    conftest = _acceptance_conftest()
    InputsMissing, read_inputs = conftest.InputsMissing, conftest.read_inputs

    def install(name, main_is_file=True, prompts="dir"):
        root = tmp_path / name
        (root / "mu_spec").mkdir(parents=True)
        main = root / "mu_spec" / "__main__.py"
        if main_is_file:
            main.write_text("")
        else:
            main.mkdir()
        if prompts == "dir":
            (root / "prompts").mkdir()
        elif prompts == "file":
            (root / "prompts").write_text("")
        return root

    inst = install("inst")
    nopkg = tmp_path / "nopkg"
    nopkg.mkdir()
    dirpkg = install("dirpkg", main_is_file=False)
    noprm = install("noprm", prompts=None)
    filep = install("filep", prompts="file")
    store = tmp_path / "store"
    store.mkdir()
    gone = tmp_path / "gone"
    repo = tmp_path / "repo"
    repo.mkdir()
    R, S = REFERENCE_VAR, STORE_VAR

    def err(env, repo_root=repo):
        with pytest.raises(InputsMissing) as caught:
            read_inputs(env, repo_root)
        return caught.value.message

    assert err({}) == f"acceptance input {R} is not set"
    assert err({R: ""}) == f"acceptance input {R} is not set"
    assert err({R: str(inst)}) == f"acceptance input {S} is not set"
    # both variables are checked before the package
    assert err({R: str(nopkg), S: ""}) == f"acceptance input {S} is not set"

    assert err({R: str(gone), S: str(store)}) == f"acceptance input {R} names no directory: {gone}"
    assert err({R: str(inst), S: str(gone)}) == f"acceptance input {S} names no directory: {gone}"

    for v in (str(nopkg), str(dirpkg)):
        assert err({R: v, S: str(store)}) == f"acceptance input {R} holds no mu_spec package: {v}"

    v = str(inst / "mu_spec" / "..")
    assert err({R: v, S: str(store)}, repo_root=inst) == (
        f"acceptance input {R} names this repository, not the running unit: {v}"
    )

    for v in (str(noprm), str(filep)):
        assert err({R: v, S: str(store)}) == f"acceptance input {R} holds no prompts folder: {v}"

    e = _Recorded({R: str(inst / "mu_spec" / ".."), S: str(store / "."), "MU_SPEC_PORT": "x"})
    assert read_inputs(e, repo) == (inst.resolve(), store.resolve(), (inst / "prompts").resolve())
    assert e.keys_read <= {R, S}, e.keys_read


def test_acceptance_fixture_fails_never_skips(tmp_path):
    base = {
        k: v
        for k, v in os.environ.items()
        if k not in (REFERENCE_VAR, STORE_VAR, "PYTEST_ADDOPTS")
    }
    inst = tmp_path / "inst"
    (inst / "mu_spec").mkdir(parents=True)
    (inst / "mu_spec" / "__main__.py").write_text("")
    (inst / "prompts").mkdir()
    argv = [sys.executable, "-m", "pytest", "tests/acceptance", "-k", "test_quick_variants",
            "-p", "no:cacheprovider", "-rA"]

    def run(env):
        done = subprocess.run(argv, check=False, cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, timeout=300)
        lines = [line for line in done.stdout.splitlines() if line.strip()]
        return done.returncode, done.stdout, lines[-1] if lines else ""

    code, out, summary = run(base)
    assert code == 1, out
    assert f"acceptance input {REFERENCE_VAR} is not set" in out, out
    assert "skipped" not in summary and "passed" not in summary, summary

    code, out, summary = run(base | {REFERENCE_VAR: str(inst)})
    assert code == 1, out
    assert f"acceptance input {STORE_VAR} is not set" in out, out
    assert "skipped" not in summary, summary
