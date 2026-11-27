"""unitcheck.neighbour: the simulator every other destination is routed to, set per unit run to one
of four conditions, recording whatever reaches it.

Each case drives a real Neighbour over loopback with plain client sockets.
The module under test is imported inside each test, so while it is absent only these tests fail.
"""

import socket
import time

WAIT = 2.0


def settled(neighbour, timeout):
    """reached() once it is non-empty, or as it stands when the timeout runs out."""
    deadline = time.monotonic() + timeout
    while True:
        reached = neighbour.reached()
        if reached or time.monotonic() >= deadline:
            return reached
        time.sleep(0.01)


def read_to_eof(conn):
    """Everything conn sends, and whether it ended in EOF rather than a timeout."""
    conn.settimeout(WAIT)
    chunks = []
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                return b"".join(chunks), True
            chunks.append(chunk)
    except TimeoutError:
        return b"".join(chunks), False


def test_hung_holds_present_answers():
    from unitcheck.neighbour import PRESENT_ANSWER, Neighbour

    n = Neighbour()
    try:
        assert n.condition == "present"
        n.set("hung")

        # Under 'hung' the connection is accepted, read and recorded, and never answered.
        c1 = socket.create_connection(n.tcp)
        try:
            c1.sendall(b"x")
            c1.settimeout(WAIT)
            try:
                got = c1.recv(1)
            except TimeoutError:
                got = None
            assert got is None, f"'hung' answered {got!r}"
            r = n.reached()
            assert len(r) == 1, r
            assert r[0].transport == "tcp" and r[0].data == b"x", r

            # set closes the connection 'hung' held open, and clears the record.
            n.set("present")
            c1.settimeout(WAIT)
            try:
                got = c1.recv(1)
            except TimeoutError:
                got = None
            assert got == b"", "set('present') left the held connection open"
            assert n.reached() == ()
        finally:
            c1.close()

        # Under 'present' a request is answered with PRESENT_ANSWER and the connection closed.
        c2 = socket.create_connection(n.tcp)
        try:
            c2.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            answer, eof = read_to_eof(c2)
        finally:
            c2.close()
        assert answer == PRESENT_ANSWER
        assert eof, "'present' kept the connection open after answering"
        r = settled(n, WAIT)
        assert len(r) == 1, r
        assert r[0].transport == "tcp" and r[0].data.startswith(b"GET / HTTP/1.1"), r
    finally:
        n.close()


def test_datagrams_every_condition():
    from unitcheck.neighbour import CONDITIONS, Neighbour

    assert len(CONDITIONS) == 4
    assert set(CONDITIONS) == {"present", "refusing", "absent", "hung"}

    n = Neighbour()
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        u.settimeout(1.0)
        for c in CONDITIONS:
            n.set(c)
            u.sendto(c.encode(), n.udp)
            r = settled(n, 1.0)
            assert len(r) == 1, (c, r)
            assert r[0].transport == "udp" and r[0].data == c.encode(), (c, r)
            try:
                got = u.recvfrom(64)
            except TimeoutError:
                got = None
            assert got is None, f"a datagram was answered under {c!r}: {got!r}"

        # Only the four declared conditions exist; an unknown one changes nothing.
        n.set("hung")
        try:
            n.set("other")
        except ValueError:
            pass
        else:
            raise AssertionError("set('other') did not raise ValueError")
        assert n.condition == "hung"
    finally:
        u.close()
        n.close()
