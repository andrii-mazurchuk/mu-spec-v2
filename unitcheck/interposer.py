"""The interposer each launched unit's interpreter, and each child it spawns, loads from outside the unit.

install(args) is called by a bootstrap before the unit's package is imported. From then on every
lookup, connection, bind, datagram and process start the process makes is reported to the check
on the control stream before it is decided; numeric lookups are answered here from the parsed
literal and never reach the system; every name goes to the GitHub stand-in or the neighbour
simulator; and the one lifecycle log is followed record by record, each held to its b"\\n", so
the check can fail whole records under one of four fault modes.

The interposer imports nothing of either unit: it is the same instrument inside both.
"""

import atexit
import base64
import builtins
import contextlib
import errno
import io
import ipaddress
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

MODES = ("off", "every", "mutation", "event", "kth")
MUTATION_KIND = "mutation"
TERMINATOR = b"\n"
DISK_FULL = errno.ENOSPC
UNREACHABLE = errno.ENETUNREACH
REFUSED = errno.ECONNREFUSED
HOST_NOT_FOUND = 1
LOOKUP_OPS = ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo")
FORWARD_LOOKUPS = ("getaddrinfo", "gethostbyname", "gethostbyname_ex")
REVERSE_LOOKUPS = ("gethostbyaddr", "getnameinfo")
LOOKUP_EVENTS = tuple("socket." + n for n in LOOKUP_OPS)
SEND_EVENTS = ("socket.connect", "socket.sendto", "socket.sendmsg", "socket.bind")
PROCESS_EVENTS = (
    "subprocess.Popen",
    "os.system",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "os.fork",
    "os.forkpty",
    "os.startfile",
    "_winapi.CreateProcess",
    "_posixsubprocess.fork_exec",
)
OPS = ("lookup", "connect", "send", "bind", "spawn", "process")
OUTCOMES = ("passed", "failed", "lost")
BREAKS = ("torn", "unparsed", "child")
TYPE_PROTOS = (
    (socket.SOCK_STREAM, socket.IPPROTO_TCP),
    (socket.SOCK_DGRAM, socket.IPPROTO_UDP),
    (socket.SOCK_RAW, 0),
)
WIN_TYPE_PROTOS = ((0, 0),)
BIND_NAME_REASON = "name not resolved under the check"
NUMERIC_ERROR_CASES = ("v4_as_v6", "v6_as_v4", "v6_by_name", "v6_by_name_ex")
CALIBRATION = {
    "v4_as_v6": ("getaddrinfo", "127.0.0.1", socket.AF_INET6),
    "v6_as_v4": ("getaddrinfo", "::1", socket.AF_INET),
    "v6_by_name": ("gethostbyname", "::1", None),
    "v6_by_name_ex": ("gethostbyname_ex", "::1", None),
}
ERROR_CLASSES = ("gaierror", "herror", "OSError")
INJECTED_ERRNO = 424242

NumericError = tuple[str, str | None, int | None, str | None]

_CLASSES: dict[str, type[OSError]] = {"gaierror": socket.gaierror, "herror": socket.herror, "OSError": OSError}
_NUMERIC_FLAGS = socket.NI_NUMERICHOST | socket.NI_NUMERICSERV
_LOOKUP_EVENT_SET = frozenset(LOOKUP_EVENTS)
_SEND_EVENT_SET = frozenset(SEND_EVENTS)
_PROCESS_EVENT_SET = frozenset(PROCESS_EVENTS)
_FIELDS = (
    "control",
    "standin",
    "github_host",
    "neighbour_tcp",
    "neighbour_udp",
    "condition",
    "log_path",
    "mode",
    "k",
    "numeric_errors",
)


class InterposerError(Exception):
    """install could not reach the check's control listener; the launch fails, not the judgement."""


@dataclass(frozen=True)
class LaunchArgs:
    control: tuple[str, int]
    standin: tuple[str, int]
    github_host: str
    neighbour_tcp: tuple[str, int]
    neighbour_udp: tuple[str, int]
    condition: str
    log_path: str
    mode: str
    k: int | None
    numeric_errors: tuple[NumericError, ...]
    process: int = 0


def _address(value: object, field: str) -> tuple[str, int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or not isinstance(value[0], str)
        or not isinstance(value[1], int)
        or isinstance(value[1], bool)
    ):
        raise ValueError(f"launch document field {field!r} is not a [host, port] pair: {value!r}")
    return (str(value[0]), int(value[1]))


def _numeric_errors(value: object) -> tuple[NumericError, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"numeric_errors is not a list: {value!r}")  # noqa: TRY004 - launch_args raises ValueError
    rows = []
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) != 4:
            raise ValueError(f"numeric_errors row is not four elements: {row!r}")
        case, name, code, text = row
        if name is not None and name not in ERROR_CLASSES:
            raise ValueError(f"numeric_errors row names no error class: {row!r}")
        rows.append((case, name, code, text))
    if tuple(r[0] for r in rows) != NUMERIC_ERROR_CASES:
        raise ValueError(f"numeric_errors cases are not {NUMERIC_ERROR_CASES}: {value!r}")
    return tuple(rows)


def launch_args(doc: Mapping) -> LaunchArgs:
    """The one place a decoded JSON launch document becomes LaunchArgs."""
    missing = [f for f in _FIELDS if f not in doc]
    if missing:
        raise ValueError(f"launch document is missing {missing}")
    return LaunchArgs(
        control=_address(doc["control"], "control"),
        standin=_address(doc["standin"], "standin"),
        github_host=str(doc["github_host"]),
        neighbour_tcp=_address(doc["neighbour_tcp"], "neighbour_tcp"),
        neighbour_udp=_address(doc["neighbour_udp"], "neighbour_udp"),
        condition=str(doc["condition"]),
        log_path=str(doc["log_path"]),
        mode=str(doc["mode"]),
        k=None if doc["k"] is None else int(doc["k"]),
        numeric_errors=_numeric_errors(doc["numeric_errors"]),
        process=int(doc.get("process", 0)),
    )


# --- process state -------------------------------------------------------------------------------

_ARGS: LaunchArgs | None = None
_CONTROL: socket.socket | None = None
_REPORT_LOCK = threading.Lock()
_FOLLOW = threading.Lock()  # the one follower lock: next, the mode and every stream's held bytes
_LOCAL = threading.local()
_STATE: dict[str, Any] = {"next": 1, "mode": "off", "target": None}
_HELD: dict[int, "_Stream"] = {}  # streams holding bytes, torn at exit
_FDS: dict[int, "_Stream"] = {}  # os.open descriptors on the log
_CALIBRATED: list[tuple[NumericError, ...]] = []
_REAL: dict[str, Any] = {}


def report(message: Mapping) -> None:
    """One message on the control stream, with "process" added; flushed before it returns."""
    sock = _CONTROL
    if sock is None or _ARGS is None:
        return
    body = dict(message)
    body["process"] = _ARGS.process
    line = (json.dumps(body, default=_jsonable) + "\n").encode("utf-8")
    with _REPORT_LOCK:
        try:
            sock.sendall(line)
        except OSError:
            pass


def _jsonable(value: object) -> object:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


@contextlib.contextmanager
def _wrapping() -> Iterator[None]:
    _LOCAL.depth = getattr(_LOCAL, "depth", 0) + 1
    try:
        yield
    finally:
        _LOCAL.depth -= 1


def wrapping() -> AbstractContextManager[None]:
    """Marks the calling thread as inside a wrapper while it is entered."""
    return _wrapping()


def _inside() -> bool:
    return getattr(_LOCAL, "depth", 0) > 0


def _text(host: object) -> str | None:
    if host is None:
        return None
    if isinstance(host, (bytes, bytearray)):
        return bytes(host).decode("ascii", "replace")
    return str(host)


def _attempt(op: str, event: str, *, name=None, dest=None, asked=None, numeric=False, child=None) -> None:
    report(
        {
            "t": "attempt",
            "op": op,
            "event": event,
            "name": _text(name),
            "dest": None if dest is None else [_text(dest[0]), dest[1]],
            "asked": None if asked is None else [_text(asked[0]), asked[1]],
            "numeric": numeric,
            "child": child,
            "at": time.time(),
        }
    )


# --- the numeric rule ----------------------------------------------------------------------------


def _parse(host: object) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int] | None:
    """The literal and its scope when host is numeric text, else None; None and '' are not parsed here."""
    if isinstance(host, (bytes, bytearray)):
        try:
            host = bytes(host).decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(host, str):
        return None
    literal, _, scope = host.partition("%")
    try:
        ip = ipaddress.ip_address(literal)
    except ValueError:
        return None
    return ip, _scope(scope)


def _scope(scope: str) -> int:
    if not scope:
        return 0
    if scope.isdigit():
        return int(scope)
    try:
        return socket.if_nametoindex(scope)
    except (OSError, AttributeError):
        return 0


def _empty(host: object) -> bool:
    return host is None or host == "" or host == b""


def _numeric(host: object) -> bool:
    return _empty(host) or _parse(host) is not None


def local_lookup(op: str, host: object, flags: int = 0) -> bool:
    """Whether a lookup is answered here: a forward lookup of a numeric host, or a getnameinfo asked numeric both."""
    if op in FORWARD_LOOKUPS:
        return _numeric(host)
    if op == "getnameinfo":
        return flags & _NUMERIC_FLAGS == _NUMERIC_FLAGS
    return False


def _hostport(address: object) -> tuple[object, object] | None:
    if isinstance(address, (tuple, list)) and len(address) >= 2:
        return (address[0], address[1])
    return None


# --- calibrated errors and built answers ---------------------------------------------------------


def calibrate() -> tuple[NumericError, ...]:
    """In the check's own process only: the host's own error for each NUMERIC_ERROR_CASES call, once."""
    if _CALIBRATED:
        return _CALIBRATED[0]
    rows: list[NumericError] = []
    for case in NUMERIC_ERROR_CASES:
        op, literal, family = CALIBRATION[case]
        try:
            if op == "getaddrinfo":
                socket.getaddrinfo(literal, None, family or 0, 0, 0, socket.AI_NUMERICHOST)
            elif op == "gethostbyname":
                socket.gethostbyname(literal)
            else:
                socket.gethostbyname_ex(literal)
        except Exception as e:  # noqa: BLE001 - an error none of ERROR_CLASSES covers is recorded as None
            name = next((n for n in ERROR_CLASSES if isinstance(e, _CLASSES[n])), None)
            if name is None:
                rows.append((case, None, None, None))
            else:
                rows.append((case, name, getattr(e, "errno", None), getattr(e, "strerror", None)))
        else:
            rows.append((case, None, None, None))
    _CALIBRATED.append(tuple(rows))
    return _CALIBRATED[0]


def _raise_case(case: str, errors: Sequence | None) -> None:
    if errors is None:
        errors = _ARGS.numeric_errors if _ARGS is not None else calibrate()
    row = next((r for r in errors if r[0] == case), None)
    if row is None or row[1] is None:
        raise socket.gaierror(socket.EAI_NONAME, BIND_NAME_REASON)
    raise _CLASSES[row[1]](row[2], row[3])


def _port(port: object) -> int:
    if port is None:
        return 0
    if isinstance(port, (bytes, bytearray)):
        port = bytes(port).decode("ascii", "replace")
    if isinstance(port, int):
        return int(port)
    if isinstance(port, str):
        if port.isdigit():
            return int(port)
        try:
            return socket.getservbyname(port)
        except OSError as e:
            raise socket.gaierror(getattr(socket, "EAI_SERVICE", socket.EAI_NONAME), str(e)) from None
    raise OSError("Int or String expected")


def _enum(kind: type, value: int) -> int:
    try:
        return kind(value)
    except ValueError:
        return value


def _literal(host: object, family: int, passive: bool):
    """(ip, scope, F) for a numeric host, None and '' read as the any-address or the loopback."""
    if _empty(host):
        six = family == socket.AF_INET6
        text = ("::" if six else "0.0.0.0") if passive else ("::1" if six else "127.0.0.1")
        ip, scope = ipaddress.ip_address(text), 0
    else:
        parsed = _parse(host)
        if parsed is None:
            raise ValueError(f"not a numeric host: {host!r}")
        ip, scope = parsed
    return ip, scope, socket.AF_INET if ip.version == 4 else socket.AF_INET6


def _built_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0, *, errors=None):
    ip, scope, f = _literal(host, family, bool(flags & socket.AI_PASSIVE))
    if family not in (0, f):
        _raise_case("v4_as_v6" if f == socket.AF_INET else "v6_as_v4", errors)
    number = _port(port)
    win = sys.platform == "win32"
    if type != 0:
        if proto == 0 and not win:
            proto = next((p for t, p in TYPE_PROTOS if t == type), 0)
        pairs: Sequence[tuple[int, int]] = ((type, proto),)
    else:
        pairs = WIN_TYPE_PROTOS if win else TYPE_PROTOS
    canonname = (_text(host) or "") if flags & socket.AI_CANONNAME else ""
    sockaddr: tuple = (str(ip), number) if f == socket.AF_INET else (str(ip), number, 0, scope)
    return [
        (_enum(socket.AddressFamily, f), _enum(socket.SocketKind, t), p, canonname, sockaddr) for t, p in pairs
    ]


def built_forward(op: str, *args, errors: Sequence | None = None, **kwargs) -> object:
    """The answer the wrapper gives a numeric forward lookup, built from the parsed literal; never the system's."""
    if op == "getaddrinfo":
        return _built_getaddrinfo(*args, errors=errors, **kwargs)
    if op not in ("gethostbyname", "gethostbyname_ex"):
        raise ValueError(f"not a forward lookup: {op!r}")
    host = args[0] if args else kwargs.get("hostname")
    ip, _, f = _literal(host, 0, False)
    if f == socket.AF_INET6:
        _raise_case("v6_by_name" if op == "gethostbyname" else "v6_by_name_ex", errors)
    if op == "gethostbyname":
        return str(ip)
    return (str(ip), [], [str(ip)])


def built_reverse(sockaddr: tuple, flags: int) -> tuple[str, str]:
    """getnameinfo with both numeric flags, formatted here; never the system's."""
    parsed = _parse(sockaddr[0])
    if parsed is None:
        raise socket.gaierror(socket.EAI_NONAME, BIND_NAME_REASON)
    ip, _ = parsed
    host = str(ip)
    if ip.version == 6 and len(sockaddr) >= 4 and sockaddr[3]:
        host += "%" + str(sockaddr[3])
    return (host, str(sockaddr[1]))


# --- routing -------------------------------------------------------------------------------------


def _args() -> LaunchArgs:
    assert _ARGS is not None
    return _ARGS


def _no_name() -> socket.gaierror:
    return socket.gaierror(socket.EAI_NONAME, BIND_NAME_REASON)


def _route_name(name: object, datagram: bool) -> tuple[str, int] | None:
    """Where a name goes: the stand-in for GitHub's host, else the neighbour; None under 'absent'."""
    args = _args()
    if (_text(name) or "").lower() == args.github_host.lower():
        return args.standin
    if args.condition == "absent":
        return None
    return args.neighbour_udp if datagram else args.neighbour_tcp


def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    numeric = local_lookup("getaddrinfo", host)
    if numeric:
        _attempt("lookup", "getaddrinfo", name=host, numeric=True)
        return built_forward("getaddrinfo", host, port, family, type, proto, flags)
    to = _route_name(host, type == socket.SOCK_DGRAM)
    _attempt("lookup", "getaddrinfo", name=host, dest=to)
    if to is None:
        raise _no_name()
    answer = built_forward("getaddrinfo", to[0], to[1], 0, type, proto, flags & ~socket.AI_CANONNAME)
    canonname = (_text(host) or "") if flags & socket.AI_CANONNAME else ""
    return [(f, t, p, canonname, sa) for f, t, p, _, sa in answer]


def _by_name(op: str):
    def lookup(hostname):
        if local_lookup(op, hostname):
            _attempt("lookup", op, name=hostname, numeric=True)
            return built_forward(op, hostname)
        to = _route_name(hostname, False)
        _attempt("lookup", op, name=hostname, dest=to)
        if to is None:
            raise _no_name()
        if op == "gethostbyname":
            return to[0]
        return (_text(hostname), [], [to[0]])

    lookup.__name__ = op
    return lookup


def _gethostbyaddr(ip_address):
    _attempt("lookup", "gethostbyaddr", name=ip_address)
    parsed = _parse(ip_address)
    if parsed is not None:
        address = str(parsed[0])
    else:
        to = _route_name(ip_address, False)
        if to is None:
            raise _no_name()
        address = to[0]
    if _args().condition == "absent":
        raise socket.herror(HOST_NOT_FOUND, "Unknown host")
    return (_text(ip_address), [], [address])


def _getnameinfo(sockaddr, flags):
    asked = _hostport(sockaddr)
    numeric = local_lookup("getnameinfo", None, flags)
    _attempt("lookup", "getnameinfo", asked=asked, numeric=numeric)
    if numeric:
        return built_reverse(sockaddr, flags)
    if _args().condition == "absent":
        raise _no_name()
    host, port = asked if asked is not None else (sockaddr, 0)
    return (_text(host), str(port))


def _aimed(sock: socket.socket, to: tuple[str, int]) -> tuple:
    """to in the shape the socket's family takes."""
    if sock.family == socket.AF_INET6:
        host = to[0]
        if _parse(host) is not None and ":" not in host:
            host = "::ffff:" + host
        return (host, to[1], 0, 0)
    return to


def _decide_connect(sock: socket.socket, address: object, event: str):
    """Report the connect and decide it: (address to connect to, None) or (None, the error)."""
    args = _args()
    asked = _hostport(address)
    if asked is None:
        _attempt("connect", event)
        return address, None
    host, port = asked
    datagram = sock.type == socket.SOCK_DGRAM
    if _numeric(host):
        target: tuple | None = (host, port)
        name = None
    else:
        name = host
        routed = _route_name(host, datagram)
        target = None if routed is None else routed
    if target is None:
        _attempt("connect", event, name=name, asked=asked)
        return None, _no_name()
    if (target[0], target[1]) in (args.standin, args.control):
        _attempt("connect", event, name=name, asked=asked, dest=target)
        return (address if name is None else _aimed(sock, target)), None
    if args.condition == "absent":
        _attempt("connect", event, name=name, asked=asked)
        return None, OSError(UNREACHABLE, os.strerror(UNREACHABLE))
    if args.condition == "refusing":
        _attempt("connect", event, name=name, asked=asked)
        return None, ConnectionRefusedError(REFUSED, os.strerror(REFUSED))
    to = args.neighbour_udp if datagram else args.neighbour_tcp
    _attempt("connect", event, name=name, asked=asked, dest=to)
    return _aimed(sock, to), None


def _connect(self, address):
    if self is _CONTROL or _inside():
        return _REAL["connect"](self, address)
    target, error = _decide_connect(self, address, "connect")
    if error is not None:
        raise error
    with wrapping():
        return _REAL["connect"](self, target)


def _connect_ex(self, address):
    if self is _CONTROL or _inside():
        return _REAL["connect_ex"](self, address)
    target, error = _decide_connect(self, address, "connect_ex")
    if error is not None:
        if isinstance(error, socket.gaierror):
            raise error
        return error.errno
    with wrapping():
        return _REAL["connect_ex"](self, target)


def _bind(self, address):
    if self is _CONTROL or _inside():
        return _REAL["bind"](self, address)
    asked = _hostport(address)
    if asked is not None and not _numeric(asked[0]):
        _attempt("lookup", "bind", name=asked[0], asked=asked)
        raise socket.gaierror(socket.EAI_NONAME, BIND_NAME_REASON)
    _attempt("bind", "bind", asked=asked, dest=asked)
    with wrapping():
        return _REAL["bind"](self, address)


def _decide_send(sock: socket.socket, address: object, event: str) -> object:
    args = _args()
    asked = _hostport(address)
    if asked is None:
        _attempt("send", event)
        return address
    host = asked[0]
    name = None if _numeric(host) else host
    if name is not None and _route_name(host, True) is None:
        _attempt("send", event, name=name, asked=asked)
        raise _no_name()
    if args.condition == "absent":
        _attempt("send", event, name=name, asked=asked)
        raise OSError(UNREACHABLE, os.strerror(UNREACHABLE))
    _attempt("send", event, name=name, asked=asked, dest=args.neighbour_udp)
    return _aimed(sock, args.neighbour_udp)


def _sendto(self, data, *rest):
    if self is _CONTROL or _inside() or not rest:
        return _REAL["sendto"](self, data, *rest)
    target = _decide_send(self, rest[-1], "sendto")
    with wrapping():
        return _REAL["sendto"](self, data, *rest[:-1], target)


def _sendmsg(self, buffers, ancdata=(), flags=0, address=None):
    if self is _CONTROL or _inside() or address is None:
        if address is None:
            return _REAL["sendmsg"](self, buffers, ancdata, flags)
        return _REAL["sendmsg"](self, buffers, ancdata, flags, address)
    target = _decide_send(self, address, "sendmsg")
    with wrapping():
        return _REAL["sendmsg"](self, buffers, ancdata, flags, target)


def _wrap_lookup(op: str, wrapper: Callable) -> Callable:
    def call(*args, **kwargs):
        if _inside():
            return _REAL[op](*args, **kwargs)
        with wrapping():
            return wrapper(*args, **kwargs)

    call.__name__ = op
    call.__qualname__ = op
    call.__doc__ = getattr(_REAL[op], "__doc__", None)
    return call


# --- the audit hook ------------------------------------------------------------------------------


def _hook(event: str, hargs: tuple) -> None:
    if event not in _LOOKUP_EVENT_SET and event not in _SEND_EVENT_SET and event not in _PROCESS_EVENT_SET:
        return
    if _CONTROL is None or _inside():
        return
    try:
        with wrapping():
            _hooked(event, hargs)
    except Exception:  # noqa: BLE001, S110 - a report must never fail the unit's own call
        pass


def _hooked(event: str, hargs: tuple) -> None:
    if event in _PROCESS_EVENT_SET:
        # A Popen raises its own event and then the platform's; one process start is one attempt.
        if event == "subprocess.Popen":
            _LOCAL.popen = True
        elif getattr(_LOCAL, "popen", False):
            _LOCAL.popen = False
            return
        _attempt("process", event)
        return
    if event in _LOOKUP_EVENT_SET:
        op = event[len("socket.") :]
        if op == "getnameinfo":
            _attempt("lookup", event, asked=_hostport(hargs[0] if hargs else None))
        elif op == "gethostbyaddr":
            _attempt("lookup", event, name=hargs[0] if hargs else None)
        else:
            host = hargs[0] if hargs else None
            _attempt("lookup", event, name=host, numeric=local_lookup(op, host))
        return
    sock, address = (hargs[0], hargs[1]) if len(hargs) >= 2 else (None, None)
    if sock is _CONTROL:
        return
    asked = _hostport(address)
    if event == "socket.bind":
        if asked is not None and not _numeric(asked[0]):
            _attempt("lookup", event, name=asked[0], asked=asked)
        else:
            _attempt("bind", event, asked=asked, dest=asked)
        return
    op = "connect" if event == "socket.connect" else "send"
    name = None if asked is None or _numeric(asked[0]) else asked[0]
    _attempt(op, event, name=name, asked=asked)


# --- the record follower -------------------------------------------------------------------------


class _Stream:
    """The followed bytes of one handle on the log: the record held to its terminator."""

    def __init__(self, writer: Callable[[bytes], "int | None"]):
        self.writer = writer
        self.held: bytes | None = None
        self.ordinal = 0
        self.mode = "off"
        self.target: int | None = None

    def _pass(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            n = self.writer(bytes(view))
            if n is None:
                raise BlockingIOError(errno.EAGAIN, os.strerror(errno.EAGAIN))
            view = view[n:]

    def _begin(self) -> tuple[int, str, int | None]:
        ordinal = _STATE["next"]
        _STATE["next"] = ordinal + 1
        return ordinal, _STATE["mode"], _STATE["target"]

    def write(self, data: bytes) -> int:
        with _FOLLOW:
            if _args().process != 0:
                with wrapping():
                    self._pass(data)
                if data:
                    report({"t": "break", "why": "child", "b64": base64.b64encode(data).decode("ascii")})
                return len(data)
            if not data:
                return 0
            at = data.find(TERMINATOR)
            if at < 0:
                if self.held is None:
                    self.ordinal, self.mode, self.target = self._begin()
                    self.held = b""
                    _HELD[id(self)] = self
                self.held += data
                return len(data)
            if self.held is None:
                self.ordinal, self.mode, self.target = self._begin()
            completed = [(self.ordinal, self.mode, self.target, (self.held or b"") + data[: at + 1])]
            self.held = None
            _HELD.pop(id(self), None)
            start = at + 1
            while True:
                end = data.find(TERMINATOR, start)
                if end < 0:
                    break
                completed.append((*self._begin(), data[start : end + 1]))
                start = end + 1
            tail = data[start:]
            judged = []
            for ordinal, mode, target, chunk in completed:
                kind = _kind(chunk)
                judged.append((ordinal, kind, _selected(mode, target, ordinal, kind), chunk))
            failed = any(selected for _, _, selected, _ in judged)
            if not failed:
                with wrapping():
                    self._pass(b"".join(chunk for *_, chunk in judged))
            for ordinal, kind, selected, chunk in judged:
                b64 = base64.b64encode(chunk).decode("ascii")
                if kind is None:
                    report({"t": "break", "why": "unparsed", "b64": b64})
                outcome = "passed" if not failed else ("failed" if selected else "lost")
                report({"t": "record", "ordinal": ordinal, "kind": kind, "outcome": outcome, "b64": b64})
            if failed:
                raise OSError(DISK_FULL, os.strerror(DISK_FULL))
            if tail:
                self.ordinal, self.mode, self.target = self._begin()
                self.held = tail
                _HELD[id(self)] = self
            return len(data)

    def tear(self) -> None:
        with _FOLLOW:
            held, self.held = self.held, None
            _HELD.pop(id(self), None)
            if held:
                try:
                    with wrapping():
                        self._pass(held)
                finally:
                    report({"t": "break", "why": "torn", "b64": base64.b64encode(held).decode("ascii")})


def _kind(chunk: bytes) -> str | None:
    try:
        parsed = json.loads(chunk)
    except ValueError:
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get("kind"), str):
        return parsed["kind"]
    return None


def _selected(mode: str, target: int | None, ordinal: int, kind: str | None) -> bool:
    if mode == "every":
        return True
    if mode == "mutation":
        return kind == MUTATION_KIND
    if mode == "event":
        return isinstance(kind, str) and kind != MUTATION_KIND
    if mode == "kth":
        return ordinal == target
    return False


class FollowedRaw(io.RawIOBase):
    """Between the real FileIO on the log and the unit's own buffering: sees what reaches the system."""

    def __init__(self, raw: io.FileIO, stream: _Stream | None = None):
        super().__init__()
        self._raw = raw
        self._stream = stream if stream is not None else _Stream(raw.write)
        self._stream.writer = raw.write

    @property
    def name(self):
        return self._raw.name

    @property
    def mode(self):
        return self._raw.mode

    @property
    def closefd(self):
        return self._raw.closefd

    def readable(self) -> bool:
        return self._raw.readable()

    def writable(self) -> bool:
        return self._raw.writable()

    def seekable(self) -> bool:
        return self._raw.seekable()

    def isatty(self) -> bool:
        return self._raw.isatty()

    def fileno(self) -> int:
        return self._raw.fileno()

    def readinto(self, b):
        return self._raw.readinto(b)

    def seek(self, pos: int, whence: int = 0) -> int:
        return self._raw.seek(pos, whence)

    def tell(self) -> int:
        return self._raw.tell()

    def truncate(self, size=None) -> int:
        return self._raw.truncate(size)

    def write(self, b) -> int:
        return self._stream.write(bytes(b))

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._stream.tear()
        finally:
            try:
                self._raw.close()
            finally:
                super().close()


def _full() -> OSError:
    return OSError(DISK_FULL, os.strerror(DISK_FULL))


def _faults_opens() -> bool:
    return _args().process == 0 and _STATE["mode"] == "every"


def _on_log(file: object) -> bool:
    if isinstance(file, int):
        return file in _FDS
    try:
        path = os.fsdecode(os.fspath(file))  # type: ignore[call-overload]
        with wrapping():
            return os.path.normcase(os.path.realpath(path)) == _args().log_path
    except (TypeError, ValueError, OSError):
        return False


def _writes(mode: object) -> bool:
    return isinstance(mode, str) and any(c in mode for c in "wax+")


def _open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
    real = _REAL["open"]
    if _inside() or not _writes(mode) or not _on_log(file):
        return real(file, mode, buffering, encoding, errors, newline, closefd, opener)
    if _faults_opens():
        raise _full()
    return _build(file, mode, buffering, encoding, errors, newline, closefd, opener)


def _build(file, mode, buffering, encoding, errors, newline, closefd, opener):
    """A handle on the log built as io.open builds it, with a FollowedRaw under the unit's buffering."""
    modes = set(mode)
    if modes - set("axrwb+t") or len(mode) > len(modes):
        raise ValueError(f"invalid mode: {mode!r}")
    binary, text = "b" in modes, "t" in modes
    if binary and text:
        raise ValueError("can't have text and binary mode at once")
    if binary and (encoding is not None or errors is not None or newline is not None):
        raise ValueError("binary mode doesn't take an encoding, errors or newline argument")
    rawmode = "".join(c for c in "xrwa+" if c in modes)
    stream = _FDS.pop(file, None) if isinstance(file, int) else None
    with wrapping():
        raw = io.FileIO(file, rawmode, closefd, opener=opener)
    result: Any = FollowedRaw(raw, stream)
    try:
        line_buffering = False
        if buffering == 1 or (buffering < 0 and raw.isatty()):
            buffering = -1
            line_buffering = True
        if buffering < 0:
            try:
                size = os.fstat(raw.fileno()).st_blksize
            except (OSError, AttributeError):
                size = 0
            buffering = size if size > 1 else io.DEFAULT_BUFFER_SIZE
        if buffering == 0:
            if binary:
                return result
            raise ValueError("can't have unbuffered text I/O")
        if "+" in modes:
            result = io.BufferedRandom(result, buffering)
        else:
            result = io.BufferedWriter(result, buffering)
        if binary:
            return result
        result = io.TextIOWrapper(result, io.text_encoding(encoding), errors, newline, line_buffering)
        result.mode = mode
        return result
    except BaseException:
        result.close()
        raise


def _fdopen(fd, mode="r", buffering=-1, encoding=None, *args, **kwargs):
    if not isinstance(fd, int):
        raise TypeError(f"invalid fd type ({type(fd)}, expected integer)")
    return _open(fd, mode, buffering, encoding, *args, **kwargs)


def _os_open(path, flags, mode=0o777, *, dir_fd=None):
    real = _REAL["os.open"]
    on = (
        not _inside()
        and dir_fd is None
        and flags & (os.O_WRONLY | os.O_RDWR)
        and _on_log(path if not isinstance(path, int) else None)
    )
    if not on:
        return real(path, flags, mode, dir_fd=dir_fd)
    if _faults_opens():
        raise _full()
    fd = real(path, flags, mode)
    write = _REAL["os.write"]
    _FDS[fd] = _Stream(lambda chunk: write(fd, chunk))
    return fd


def _os_write(fd, data):
    stream = _FDS.get(fd)
    if stream is None or _inside():
        return _REAL["os.write"](fd, data)
    return stream.write(bytes(data))


def _os_close(fd):
    stream = _FDS.pop(fd, None)
    if stream is not None:
        stream.tear()
    return _REAL["os.close"](fd)


def _at_exit() -> None:
    for stream in list(_HELD.values()):
        try:
            stream.tear()
        except Exception:  # noqa: BLE001, S110 - exit goes on for every other held stream
            pass


# --- control ------------------------------------------------------------------------------------


def _set_mode(mode: str, k: int | None) -> None:
    with _FOLLOW:
        _STATE["mode"] = mode
        _STATE["target"] = _STATE["next"] + k - 1 if mode == "kth" and k is not None else None


def _serve(sock: socket.socket) -> None:
    """The interposer's own reader of the check's messages, answering while unit threads are blocked."""
    buffer = b""
    while True:
        try:
            chunk = sock.recv(65536)
        except OSError:
            return
        if not chunk:
            return
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("t") == "mode":
                if _args().process == 0 and message.get("mode") in MODES:
                    _set_mode(message["mode"], message.get("k"))
                report({"t": "moded"})
            elif message.get("t") == "sync":
                with _FOLLOW:
                    report({"t": "synced", "n": message.get("n")})


def install(args: LaunchArgs) -> None:
    """Connect to the check, then set every hook and wrapper; called before the unit's package is imported."""
    global _ARGS, _CONTROL
    if _CONTROL is not None:
        return
    family = socket.AF_INET6 if ":" in args.control[0] else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.connect(args.control)
    except OSError as e:
        sock.close()
        raise InterposerError(f"cannot reach the check's control listener at {args.control}: {e}") from e
    _ARGS = args
    _CONTROL = sock
    _STATE["next"] = 1
    _STATE["mode"] = args.mode if args.process == 0 else "off"
    _STATE["target"] = args.k if args.process == 0 and args.mode == "kth" else None
    report({"t": "hello", "pid": os.getpid()})
    threading.Thread(target=_serve, args=(sock,), name="unitcheck-interposer", daemon=True).start()

    for op in LOOKUP_OPS:
        _REAL[op] = getattr(socket, op)
    wrappers = {
        "getaddrinfo": _getaddrinfo,
        "gethostbyname": _by_name("gethostbyname"),
        "gethostbyname_ex": _by_name("gethostbyname_ex"),
        "gethostbyaddr": _gethostbyaddr,
        "getnameinfo": _getnameinfo,
    }
    for op in LOOKUP_OPS:
        setattr(socket, op, _wrap_lookup(op, wrappers[op]))
    for method, wrapper in (
        ("connect", _connect),
        ("connect_ex", _connect_ex),
        ("bind", _bind),
        ("sendto", _sendto),
        ("sendmsg", _sendmsg),
    ):
        real = getattr(socket.socket, method, None)
        if real is None:
            continue
        _REAL[method] = real
        setattr(socket.socket, method, wrapper)

    _REAL["open"] = io.open
    builtins.open = _open
    io.open = _open
    pathlib = sys.modules.get("pathlib")
    accessor = getattr(pathlib, "_NormalAccessor", None)
    if accessor is not None and hasattr(accessor, "open"):
        accessor.open = staticmethod(_open)
    logging = sys.modules.get("logging")
    handler = getattr(logging, "FileHandler", None)
    if handler is not None and hasattr(handler, "_builtin_open"):
        handler._builtin_open = _open
    _REAL["os.open"] = os.open
    _REAL["os.write"] = os.write
    _REAL["os.close"] = os.close
    os.open = _os_open
    os.write = _os_write
    os.close = _os_close
    os.fdopen = _fdopen

    sys.addaudithook(_hook)
    atexit.register(_at_exit)
