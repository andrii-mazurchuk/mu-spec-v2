"""The start check: its variant set, runner, restore, bind-line reading, judge and quick run.

Modules under test are imported inside each test, so a module not yet written fails only the
tests that need it.
"""

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
