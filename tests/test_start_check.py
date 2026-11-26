"""The start check: its variant set, runner, restore, bind-line reading, judge and quick run.

Modules under test are imported inside each test, so a module not yet written fails only the
tests that need it.
"""

import os

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
