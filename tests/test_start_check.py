"""The start check: its variant set, runner, restore, bind-line reading, judge and quick run.

Modules under test are imported inside each test, so a module not yet written fails only the
tests that need it.
"""

import json
import os
import socket
import time

import pytest

HEALTHY_SHA = "0123456789abcdef0123456789abcdef01234567"


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
    f.write(json.dumps({{'env': dict(os.environ), 'isolated': sys.flags.isolated,
                        'path0': sys.path[0], 'cwd': os.getcwd()}}))
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


def test_runner_outcomes(tmp_path):
    from startcheck.runner import (
        START_ALLOWANCE_S,
        RunnerError,
        UnitCommand,
        alive,
        restore,
        start,
        stop,
    )

    dump = tmp_path / "dump.json"
    fakes = {}
    for name, body in _FAKE_BODIES.items():
        pkg = tmp_path / "fakes" / name / "mu_spec"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "__main__.py").write_text(_FAKE_PREAMBLE.format(dump=str(dump)) + body)
        fakes[name] = str(pkg.parent)

    ps = tmp_path / "ps"
    (ps / "projects" / "a").mkdir(parents=True)
    (ps / "projects" / "a" / "x.json").write_bytes(b"{}")
    pp = tmp_path / "pp"
    pp.mkdir()
    (pp / "default.md").write_bytes(b"d")
    root = str(tmp_path / "check")
    os.mkdir(root)

    from startcheck.variants import environment

    def launch(name, variant):
        restore(root, str(ps), str(pp))
        return start(UnitCommand("rebuilt", fakes[name]), variant, root, os.environ)

    s = launch("serve", "hhhhhh")
    try:
        assert s.outcome == "listening"
        assert s.port == 19406
        assert s.exit_code is None
        assert alive(s)
        seen = json.loads(dump.read_text())
        assert seen["env"] == environment("hhhhhh", root, os.environ)
        assert seen["isolated"] == 1
        assert seen["path0"] == fakes["serve"]
        assert seen["cwd"] == os.path.join(root, "work")
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
            restore(root, str(ps), str(pp))
            with pytest.raises(RunnerError, match=str(taken)):
                start(UnitCommand("rebuilt", fakes["serve"]), "hhhhhh", root, os.environ)
        assert not dump.exists(), taken


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
    assert set(_files(root / "prompts")) == {"default.md"}
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
