"""A failed bind's one error-output line: its fixed reading and its comparison under three masks.

Acceptance and test only; never imported by mu_spec. Nothing here raises: a text that is not one
bind line reads as None.
"""

from __future__ import annotations

from dataclasses import dataclass

BIND_PREFIX = b"mu-spec cannot bind "
HOST_MARK = b"<host>"
PORT_MARK = b"<port>"
REASON_MARK = b"<reason>"


@dataclass(frozen=True)
class BindLine:
    host: bytes
    port: bytes
    reason: bytes
    ending: bytes  # b"\r\n" or b"\n", as written


def read_bind_line(err: bytes) -> BindLine | None:
    """Read a unit's whole error output as one bind line, or None when it is not one."""
    if err.count(b"\n") != 1 or not err.endswith(b"\n"):
        return None
    ending = b"\r\n" if err.endswith(b"\r\n") else b"\n"
    line = err[: -len(ending)]
    if not line.startswith(BIND_PREFIX):
        return None
    rest = line[len(BIND_PREFIX):]
    i = rest.find(b": ")
    if i < 0:
        return None
    hostport, reason = rest[:i], rest[i + 2:]
    j = hostport.rfind(b":")
    if j < 0:
        return None
    return BindLine(host=hostport[:j], port=hostport[j + 1:], reason=reason, ending=ending)


def masked(line: BindLine) -> bytes:
    """The line with host, port and reason masked; the prefix and ending are kept as written."""
    return BIND_PREFIX + HOST_MARK + b":" + PORT_MARK + b": " + REASON_MARK + line.ending


def _complete(line: BindLine) -> bool:
    return bool(line.host) and bool(line.port) and bool(line.reason)


def bind_match(running_err: bytes, rebuilt_err: bytes) -> bool | None:
    """None when the running unit's output is not one bind line; else whether the two match."""
    running = read_bind_line(running_err)
    if running is None:
        return None
    rebuilt = read_bind_line(rebuilt_err)
    if rebuilt is None or not _complete(running) or not _complete(rebuilt):
        return False
    return masked(running) == masked(rebuilt)
