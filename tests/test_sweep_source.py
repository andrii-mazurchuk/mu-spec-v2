"""The source sweep's rules, one fixture per listed name (T-65).

The sweep under test is sweeps.source.sweep (S-401); it is never re-implemented here. Each case
copies the repository's mu_spec package into tmp_path, writes one fixture module into the copy and
runs the sweep on tmp_path, the directory holding the copied package. The lists below are copied
from the specification as literals, so a sweep whose own list forgets a member fails that member's
case. Modules under test are imported inside the test, so a module not yet written fails only the
tests that need it.
"""

import shutil
from pathlib import Path

import pytest

MU_SPEC = Path(__file__).resolve().parents[1] / "mu_spec"
FX = "mu_spec.reads.fx"

GITHUB_HOLDERS = (
    "mu_spec.registry",
    "mu_spec.ship.part",
    "mu_spec.ship.sender",
    "mu_spec.ship.plan",
    "mu_spec.ship.access",
    "mu_spec.actions",
    "mu_spec.dash.assemble",
)
FORBIDDEN_EVERYWHERE = (
    "import subprocess", "import multiprocessing", "import pty", "import runpy", "import code", "import codeop",
    "import pickle", "import marshal", "import importlib", "import builtins", "import ctypes",
    "os.system", "os.popen", "os.exec*", "os.spawn*", "os.fork", "os.forkpty", "os.posix_spawn*", "os.startfile",
    "name:eval", "name:exec", "name:compile", "name:__import__", "name:breakpoint", "name:__builtins__",
)
NO_REBIND = ("eval", "exec", "compile", "__import__", "breakpoint", "open")
# group label -> (members, allowed modules)
ONLY_IN = {
    "environment": (
        ("os.environ", "os.environb", "os.getenv", "os.putenv"),
        ("mu_spec.outside",),
    ),
    "time": (
        ("time.time", "time.time_ns", "time.monotonic", "time.perf_counter", "time.localtime", "time.gmtime",
         "time.ctime", "time.strftime", "datetime.datetime.now", "datetime.datetime.utcnow",
         "datetime.datetime.today", "datetime.date.today"),
        ("mu_spec.outside",),
    ),
    "waiting": (
        ("time.sleep",),
        ("mu_spec.ship.part",),
    ),
    "files": (
        ("name:open", "io.open", "os.open", "os.listdir", "os.scandir", "os.walk", "os.stat", "os.lstat",
         "os.replace", "os.rename", "os.remove", "os.unlink", "os.rmdir", "os.fsync", "os.makedirs", "os.mkdir",
         "os.getcwd", "os.chdir", "os.truncate", "os.utime", "os.chmod", "os.path.exists", "os.path.isfile",
         "os.path.isdir", "os.path.getsize", "os.path.realpath", "os.path.abspath", "import shutil",
         "import tempfile", "import glob", "import fileinput"),
        ("mu_spec.store", "mu_spec.outside"),
    ),
    "concrete paths": (
        ("from pathlib import except PurePath, PurePosixPath, PureWindowsPath",),
        ("mu_spec.store", "mu_spec.outside", "mu_spec.codec"),
    ),
    "path holders": (
        (".state_dir", ".prompts_dir", ".root", ".directory"),
        ("mu_spec.store", "mu_spec.outside", "mu_spec.registry"),
    ),
    "outbound": (
        ("import http.client", "import urllib.request", "socket.create_connection"),
        ("mu_spec.github",),
    ),
    "github": (
        ("import mu_spec.github", "name:GitHubClient", ".github", ".ship@load"),
        GITHUB_HOLDERS + ("mu_spec.github",),
    ),
}

# a call for each FORBIDDEN_EVERYWHERE 'os.' entry, a starred one by a member it must match
OS_CALLS = {
    "os.system": "os.system('x')",
    "os.popen": "os.popen('x')",
    "os.exec*": "os.execv('x', ['x'])",
    "os.spawn*": "os.spawnl(0, 'x', 'x')",
    "os.fork": "os.fork()",
    "os.forkpty": "os.forkpty()",
    "os.posix_spawn*": "os.posix_spawnp('x', ['x'], {})",
    "os.startfile": "os.startfile('x')",
}
NAME_USES = {
    "eval": "eval('1')",
    "exec": "exec('1')",
    "compile": "compile('1', 's', 'exec')",
    "__import__": "__import__('os')",
    "breakpoint": "breakpoint()",
    "__builtins__": "b = __builtins__",
    "open": "open('x')",
    "GitHubClient": "client = GitHubClient()",
}


def use_of(entry: str) -> list[str]:
    """Fixture sources each using one list entry, its import written with it."""
    if entry.startswith("import "):
        return [entry]
    if entry.startswith("from "):
        module = entry.split()[1]
        return [f"from {module} import Path", f"import {module}"]
    if entry.startswith("name:"):
        return [NAME_USES[entry[len("name:"):]]]
    if entry.startswith("."):
        attr = entry[1:].removesuffix("@load")
        return [f"x = runtime.{attr}"]
    if entry in OS_CALLS:
        return ["import os\n" + OS_CALLS[entry]]
    return [f"import {entry.split('.')[0]}\nx = {entry}"]


def fail(source: str, label: str, module: str = FX):
    return pytest.param(module, source, label, id=f"fail-{label}-{module}-{source}")


def ok(source: str, module: str = FX):
    return pytest.param(module, source, None, id=f"pass-{module}-{source}")


def cases() -> list:
    out = [pytest.param(None, None, None, id="unchanged")]
    for entry in FORBIDDEN_EVERYWHERE:
        out += [fail(source, "forbidden") for source in use_of(entry)]
    out += [
        fail("compile('1', 's', 'eval')", "forbidden"),
        fail("f = eval", "forbidden"),
        fail("getattr(__builtins__, 'eval')", "forbidden"),
        fail("import code", "forbidden"),
        fail("def compile(): ...", "rebind"),
    ]
    out += [fail(f"{name} = 1", "rebind") for name in NO_REBIND]
    out += [
        ok("import codecs"),
        ok("import re\nre.compile('a')"),
        ok("import re\np = re.compile('a'); p.compile"),
        ok("x.exec()"),
        ok("stream.open()"),
        ok("from pathlib import PurePosixPath"),
        ok("record.resolve(x)"),
        ok("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import pathlib"),
    ]
    for group, (members, allowed) in ONLY_IN.items():
        for entry in members:
            for source in use_of(entry):
                out += [fail(source, f"only-in:{group}"), ok(source, allowed[0])]
    out += [
        fail("from pathlib import Path", "only-in:concrete paths"),
        fail("x = runtime.config.state_dir", "only-in:path holders"),
        fail("x = runtime.github", "only-in:github"),
        fail("x = runtime.ship", "only-in:github"),
        fail("x = runtime.ship.marker", "only-in:github"),
        ok("rt = Runtime(config=c, ship=ship)", "mu_spec.registry"),
    ]
    out += [ok("import mu_spec.github", holder) for holder in GITHUB_HOLDERS]
    out += [fail("import mu_spec.github", "only-in:github")]
    return out


def place(root: Path, module: str, source: str) -> None:
    """Write source as the file holding module in the copy under root, creating it if absent."""
    parts = module.split(".")
    directory = root.joinpath(*parts)
    if directory.is_dir():
        path = directory / "__init__.py"
    else:
        path = root.joinpath(*parts[:-1], parts[-1] + ".py")
        package = root / parts[0]
        for part in parts[1:-1]:
            package = package / part
            if not package.is_dir():
                package.mkdir()
                (package / "__init__.py").write_text("", encoding="utf-8")
    path.write_text(source + "\n", encoding="utf-8", newline="")


@pytest.mark.parametrize(("module", "source", "label"), cases())
def test_sweep_rules_per_name(tmp_path, module, source, label):
    from sweeps.source import Failure, sweep

    assert MU_SPEC.is_dir(), "mu_spec is absent"
    shutil.copytree(MU_SPEC, tmp_path / "mu_spec", ignore=shutil.ignore_patterns("__pycache__"))
    if module is not None:
        place(tmp_path, module, source)
    result = sweep(tmp_path)
    if label is None:
        assert result == []
    else:
        assert Failure(label, module) in result, result
