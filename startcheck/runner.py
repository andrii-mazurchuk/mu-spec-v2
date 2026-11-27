"""The start check's runner: restore the pristine copies, start one unit from its own install, class
its start, check it is alive, stop it and wait for its port to refuse.

Acceptance and test only; never imported by mu_spec. Both units run under the check's own
interpreter with LAUNCH_FLAGS (never -I or -E, which would ignore the hash seed), the install chosen
by argument, never by the environment, and the environment built from scratch: the variant's plus
the hash seed. A check's start goes through a Launcher the caller supplies (the shared interposed
launch); a direct start, without one, serves only the runner's own self-tests.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import IO, Protocol

from mu_spec.config import ROOT_NAME, VALUE_NAMES
from startcheck.variants import (
    A_FILE,
    FALLBACK_PORT,
    HEALTHY_PORT,
    NO_DIR,
    PASS_THROUGH,
    PROMPTS,
    STORE,
    WORK,
    environment,
)

START_ALLOWANCE_S = 15.0
STOP_ALLOWANCE_S = 15.0
POLL_S = 0.1
REACH_HOST = "127.0.0.1"
START_OUTCOMES = ("listening", "exited", "hung")
BOOTSTRAP = ("import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); "
             "runpy.run_module('mu_spec', run_name='__main__', alter_sys=True)")
LAUNCH_FLAGS = ("-s",)  # every start: no user site folder; never -I or -E, which ignore the seed
HASH_SEED_NAME = "PYTHONHASHSEED"  # the one PYTHON* variable that ever reaches a unit
DEFAULT_HASH_SEED = 0  # the seed when the test chooses none (0 turns hash randomisation off)
HASH_SEED_RANGE = (0, 4294967295)  # inclusive; the values the interpreter accepts for it
WINDOWS_REQUIRED = ("SYSTEMROOT",)  # names every start's environment must hold on win32
# Every name start_env can ever hold; a launcher's extra_env may hold none of them.
RESERVED_NAMES = PASS_THROUGH + VALUE_NAMES + (ROOT_NAME, HASH_SEED_NAME)

_CONNECT_TIMEOUT_S = 5.0


class RunnerError(Exception):
    """A start the check cannot judge; halts the run, never passes a variant."""


@dataclass(frozen=True)
class UnitCommand:
    label: str  # 'running' or 'rebuilt'
    install_root: str  # absolute folder holding that unit's mu_spec package
    hash_seed: int | None = None  # None means DEFAULT_HASH_SEED


@dataclass(frozen=True)
class Prepared:
    """One start's launch, built by a Launcher for that start only."""

    argv: tuple[str, ...]
    extra_env: Mapping[str, str]
    ready: Callable[[float], bool]
    done: Callable[[], None]


class Launcher(Protocol):
    def prepare(self, cmd: UnitCommand, check_root: str) -> Prepared: ...


class _Drain:
    """A process's stdout and stderr, each read whole by its own thread."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._parts: dict[str, list[bytes]] = {"out": [], "err": []}
        self._threads = [
            threading.Thread(target=self._read, args=(proc.stdout, self._parts["out"]), daemon=True),
            threading.Thread(target=self._read, args=(proc.stderr, self._parts["err"]), daemon=True),
        ]
        for t in self._threads:
            t.start()

    @staticmethod
    def _read(stream: IO[bytes] | None, into: list[bytes]) -> None:
        if stream is None:
            return
        with stream:
            into.extend(iter(lambda: stream.read(65536), b""))

    def join(self) -> tuple[bytes, bytes]:
        for t in self._threads:
            t.join()
        return b"".join(self._parts["out"]), b"".join(self._parts["err"])


@dataclass
class Started:
    outcome: str  # one of START_OUTCOMES
    exit_code: int | None
    port: int | None
    stdout: bytes
    stderr: bytes
    prepared: Prepared | None = None
    _cmd: UnitCommand | None = field(default=None, repr=False)
    _proc: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    _drain: _Drain | None = field(default=None, repr=False)
    _done: bool = field(default=False, repr=False)


def effective_seed(cmd: UnitCommand) -> int:
    return DEFAULT_HASH_SEED if cmd.hash_seed is None else cmd.hash_seed


def interpreter_flags(cmd: UnitCommand) -> tuple[str, ...]:
    return LAUNCH_FLAGS


def seed_env(cmd: UnitCommand) -> dict[str, str]:
    return {HASH_SEED_NAME: str(effective_seed(cmd))}


def start_env(
    cmd: UnitCommand, variant: str, check_root: str, own_env: Mapping[str, str]
) -> dict[str, str]:
    """The variant's environment, built from scratch, plus the hash seed."""
    return environment(variant, check_root, own_env) | seed_env(cmd)


def unit_argv(cmd: UnitCommand) -> tuple[str, ...]:
    return (sys.executable, *interpreter_flags(cmd), "-c", BOOTSTRAP, cmd.install_root)


def _check_seed(cmd: UnitCommand) -> None:
    seed = cmd.hash_seed
    if seed is None:
        return
    low, high = HASH_SEED_RANGE
    if isinstance(seed, bool) or not isinstance(seed, int) or not low <= seed <= high:
        raise ValueError(f"hash_seed of {cmd.label} is {seed!r}, not an int in {HASH_SEED_RANGE}")


def _check_prepared(cmd: UnitCommand, prepared: Prepared) -> None:
    """Refuse a launch that would defeat the seed or set a name the start owns."""
    argv = tuple(prepared.argv)
    if not argv or argv[0] != sys.executable or "-c" not in argv[1:]:
        raise RunnerError(f"launcher for {cmd.label} gives {argv!r}, not {sys.executable} ... -c")
    flags = argv[1 : argv.index("-c", 1)]
    if flags != interpreter_flags(cmd):
        raise RunnerError(f"launcher for {cmd.label} starts it with {flags!r}, "
                          f"not {interpreter_flags(cmd)!r}")
    reserved = sorted(set(prepared.extra_env) & set(RESERVED_NAMES))
    if reserved:
        raise RunnerError(f"launcher for {cmd.label} sets {', '.join(reserved)}, "
                          f"which the start itself owns")


def _remove(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.remove(path)


def restore(check_root: str, pristine_store: str, pristine_prompts: str) -> None:
    """Reset every fixed location under check_root to its pristine state."""
    for rel in (WORK, STORE, PROMPTS, A_FILE, NO_DIR):
        _remove(os.path.join(check_root, rel))
    shutil.copytree(pristine_store, os.path.join(check_root, STORE))
    shutil.copytree(pristine_prompts, os.path.join(check_root, PROMPTS))
    with open(os.path.join(check_root, A_FILE), "wb") as f:
        f.write(b"not a directory\n")
    os.mkdir(os.path.join(check_root, WORK))


def reach_port(variant: str) -> int | None:
    """Where the check reaches a unit: the port code at index 2 of the variant id."""
    code = variant[2]
    if code == "h":
        return HEALTHY_PORT
    if code == "m":
        return FALLBACK_PORT
    return None


def _refused(port: int) -> bool:
    try:
        socket.create_connection((REACH_HOST, port), timeout=_CONNECT_TIMEOUT_S).close()
    except ConnectionRefusedError:
        return True
    except OSError:
        return False
    return False


def _healthy(port: int, timeout: float) -> bool:
    url = f"http://{REACH_HOST}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=max(timeout, POLL_S)) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _finish(started: Started) -> None:
    """Join the draining threads, fill the output and call done, once the process has ended."""
    if started._drain is not None:
        started.stdout, started.stderr = started._drain.join()
        started._drain = None
    if started.prepared is not None and not started._done:
        started._done = True
        started.prepared.done()


def _kill(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def start(
    cmd: UnitCommand,
    variant: str,
    check_root: str,
    own_env: Mapping[str, str],
    launcher: Launcher | None = None,
) -> Started:
    """Start one unit on a variant and class its start as listening, exited or hung."""
    _check_seed(cmd)
    env = start_env(cmd, variant, check_root, own_env)
    prepared = None
    argv = unit_argv(cmd)
    if launcher is not None:
        prepared = launcher.prepare(cmd, check_root)
        _check_prepared(cmd, prepared)
        env.update(prepared.extra_env)
        argv = tuple(prepared.argv)
    if sys.platform == "win32":
        missing = [name for name in WINDOWS_REQUIRED if name not in env]
        if missing:
            raise RunnerError(f"the environment of {cmd.label} lacks {', '.join(missing)}, "
                              f"without which it cannot listen on Windows")
    for guarded in (HEALTHY_PORT, FALLBACK_PORT):
        if not _refused(guarded):
            raise RunnerError(f"port {guarded} answers before starting {cmd.label}")
    stray = sorted(n for n in env if n.upper().startswith("PYTHON") and n != HASH_SEED_NAME)
    if stray:
        raise RunnerError(f"the environment of {cmd.label} holds {', '.join(stray)}, "
                          f"which would reach the unit")
    port = reach_port(variant)
    began = time.monotonic()
    proc = subprocess.Popen(
        list(argv),
        cwd=os.path.join(check_root, WORK),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    started = Started("hung", None, port, b"", b"", prepared, cmd, proc, _Drain(proc))
    if prepared is not None:
        left = START_ALLOWANCE_S - (time.monotonic() - began)
        if not prepared.ready(max(left, 0.0)):
            _kill(proc)
            _finish(started)
            raise RunnerError(f"the instrument of {cmd.label} did not report in")
    deadline = began + START_ALLOWANCE_S
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            started.outcome = "exited" if code != 0 else "hung"
            started.exit_code = code
            _finish(started)
            return started
        if port is not None and _healthy(port, deadline - time.monotonic()):
            started.outcome = "listening"
            return started
        time.sleep(POLL_S)
    started.outcome = "hung"
    _kill(proc)
    _finish(started)
    return started


def alive(started: Started) -> bool:
    """Whether the started process has not ended."""
    return started._proc is not None and started._proc.poll() is None


def stop(started: Started) -> None:
    """Stop a listening unit, fill its output, and wait until its port refuses."""
    proc = started._proc
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=STOP_ALLOWANCE_S)
        except subprocess.TimeoutExpired:
            _kill(proc)
    elif proc is not None:
        proc.wait()
    _finish(started)
    if started.port is None:
        return
    label = started._cmd.label if started._cmd is not None else "unit"
    deadline = time.monotonic() + STOP_ALLOWANCE_S
    while not _refused(started.port):
        if time.monotonic() >= deadline:
            raise RunnerError(f"{label} still answers on port {started.port} after stopping")
        time.sleep(POLL_S)
