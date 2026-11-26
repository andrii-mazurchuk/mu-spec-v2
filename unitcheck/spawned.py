"""The interposer carried into every child the unit starts through multiprocessing's spawn.

A spawned child is a fresh interpreter that never runs the launch's bootstrap, so cover wraps the
command line both spawn starters take from multiprocessing.spawn.get_command_line: the child's -c
program is prefixed with CHILD_PREFIX, which installs the interposer with the child's own process
number and then removes its two inserted arguments, so spawn_main sees the list multiprocessing
built. Every other start is left to the audit hook, which reports it as op 'process'.

Neither CHILD_PREFIX nor base64 holds a double quote or a space, so the Windows starter's quoting of
each argument whole never breaks them.
"""

import base64
import itertools
import json
import multiprocessing.spawn
import multiprocessing.util
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from typing import Any

from unitcheck import interposer
from unitcheck.interposer import LaunchArgs, wrapping

SPAWN_PROGRAM_START = "from multiprocessing.spawn import spawn_main; spawn_main("  # how multiprocessing's own -c program begins
CHILD_PREFIX = (
    "import sys, json, base64; sys.path.insert(0, sys.argv[1]); "
    "import unitcheck.interposer as i; i.install(i.launch_args(json.loads(base64.b64decode(sys.argv[2])))); "
    "sys.path.pop(0); del sys.argv[1:3]; "
)  # holds no '"' character

_LOCAL = threading.local()  # .pending: a command line was built on this thread and its start has not run


def _encoded(child: LaunchArgs) -> str:
    return base64.b64encode(json.dumps(asdict(child)).encode("utf-8")).decode("ascii")


def _report(child: int | None) -> None:
    interposer.report(
        {
            "t": "attempt",
            "op": "spawn",
            "event": "get_command_line",
            "name": None,
            "dest": None,
            "asked": None,
            "numeric": False,
            "child": child,
            "at": time.time(),
        }
    )


def _command_line(original: Callable[..., list[str]], args: LaunchArgs, check_path: str, numbers) -> Callable:
    def get_command_line(**kwargs: Any) -> list[str]:
        with wrapping():
            line = list(original(**kwargs))
        _LOCAL.pending = True
        c = line.index("-c") if "-c" in line else -1
        if c < 0 or c + 1 >= len(line) or not str(line[c + 1]).startswith(SPAWN_PROGRAM_START):
            _report(None)
            return line
        n = next(numbers)
        child = replace(args, process=n, mode="off", k=None)
        _report(n)
        return [*line[:c], "-c", CHILD_PREFIX + line[c + 1], check_path, _encoded(child), *line[c + 2 :]]

    get_command_line.__doc__ = original.__doc__
    return get_command_line


def _started(real: Callable) -> Callable:
    """The start right after a wrapped command line runs inside wrapping(); any other passes as it is."""

    def start(*a: Any, **kw: Any) -> Any:
        if not getattr(_LOCAL, "pending", False):
            return real(*a, **kw)
        _LOCAL.pending = False
        with wrapping():
            return real(*a, **kw)

    start.__name__ = getattr(real, "__name__", "start")
    start.__doc__ = getattr(real, "__doc__", None)
    return start


def cover(args: LaunchArgs, check_path: str) -> None:
    """Once, in process 0, after install and before the unit's package is imported."""
    numbers = itertools.count(1)
    multiprocessing.spawn.get_command_line = _command_line(
        multiprocessing.spawn.get_command_line, args, check_path, numbers
    )
    # The starters call these by module attribute right after building the command line.
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateProcess = _started(_winapi.CreateProcess)
    else:
        multiprocessing.util.spawnv_passfds = _started(multiprocessing.util.spawnv_passfds)
