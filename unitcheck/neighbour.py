"""The neighbour simulator every destination other than the GitHub stand-in is routed to.

The check sets it per unit run to one of CONDITIONS and reads back whatever reached it. 'present'
answers each accepted connection with PRESENT_ANSWER as soon as its first read returns, then closes
it; 'hung' accepts, reads and records and never writes, holding the connection until the unit closes
it or set or close is called. Under 'refusing' and 'absent' the interposer never connects or sends
here, so a connection that arrives anyway is read and recorded like under 'hung', and set closes it.
Datagrams are recorded under every condition and never answered. The simulator decides nothing:
what reaches it is evidence for the outbound rule.
"""

import socket
import threading
import time
from dataclasses import dataclass, replace

from startcheck.runner import REACH_HOST

CONDITIONS = ("present", "refusing", "absent", "hung")  # 'present' is the baseline every other check launches with
PRESENT_ANSWER = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
    b"Content-Length: 2\r\nConnection: close\r\n\r\n{}"
)
READ_CHUNK = 65536

_POLL_S = 0.1  # how often the accept and datagram loops look for close
_JOIN_S = 2.0


class NeighbourError(Exception):
    """set was called while a connection accepted under 'present' is still open."""


@dataclass(frozen=True)
class Reached:
    transport: str  # 'tcp' or 'udp'
    at: float  # time.monotonic() in the check's process on arrival
    data: bytes  # what arrived: for tcp the bytes read until the connection closed or the run ended


class Neighbour:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._closed = False
        self._generation = 0  # bumped by set, so a connection of an earlier run never records into this one
        self._reached: list[Reached] = []
        self._open: dict[socket.socket, str] = {}  # accepted and still open -> the condition it was accepted under
        self.condition = "present"

        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind((REACH_HOST, 0))
        self._listener.listen()
        self._listener.settimeout(_POLL_S)
        self._datagrams = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._datagrams.bind((REACH_HOST, 0))
        self._datagrams.settimeout(_POLL_S)
        self.tcp: tuple[str, int] = self._listener.getsockname()[:2]
        self.udp: tuple[str, int] = self._datagrams.getsockname()[:2]

        self._threads = [
            threading.Thread(target=self._accept, name="neighbour-tcp", daemon=True),
            threading.Thread(target=self._receive, name="neighbour-udp", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def set(self, condition: str) -> None:
        if condition not in CONDITIONS:
            raise ValueError(f"unknown condition {condition!r}; expected one of {CONDITIONS}")
        with self._lock:
            if any(held == "present" for held in self._open.values()):
                raise NeighbourError(f"a connection accepted under 'present' is still open; cannot set {condition!r}")
            held = list(self._open)
            self._open.clear()
            for conn in held:
                _drop(conn)
            self._generation += 1
            self._reached = []
            self.condition = condition

    def reached(self) -> tuple[Reached, ...]:
        with self._lock:
            return tuple(self._reached)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            held = list(self._open)
            self._open.clear()
            for conn in held:
                _drop(conn)
        self._listener.close()
        self._datagrams.close()
        for thread in self._threads:
            thread.join(_JOIN_S)

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                if self._closed:
                    return
                continue
            except OSError:
                return
            conn.settimeout(None)
            with self._lock:
                if self._closed:
                    conn.close()
                    return
                condition, generation = self.condition, self._generation
                self._open[conn] = condition
            threading.Thread(
                target=self._serve, args=(conn, condition, generation), name="neighbour-conn", daemon=True
            ).start()

    def _serve(self, conn: socket.socket, condition: str, generation: int) -> None:
        index: int | None = None
        try:
            while True:
                try:
                    chunk = conn.recv(READ_CHUNK)
                except OSError:
                    return
                if not chunk:
                    return
                with self._lock:
                    if generation == self._generation:
                        if index is None:
                            index = len(self._reached)
                            self._reached.append(Reached("tcp", time.monotonic(), chunk))
                        else:
                            entry = self._reached[index]
                            self._reached[index] = replace(entry, data=entry.data + chunk)
                if condition == "present":
                    try:
                        conn.sendall(PRESENT_ANSWER)
                        conn.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    return
        finally:
            with self._lock:
                self._open.pop(conn, None)
            conn.close()

    def _receive(self) -> None:
        while True:
            try:
                data, _ = self._datagrams.recvfrom(READ_CHUNK)
            except TimeoutError:
                if self._closed:
                    return
                continue
            except OSError:
                if self._closed:
                    return
                continue
            arrived = time.monotonic()
            with self._lock:
                self._reached.append(Reached("udp", arrived, data))


def _drop(conn: socket.socket) -> None:
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    conn.close()
