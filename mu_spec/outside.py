"""Every outside input except GitHub, each entering at one named point.

Configuration is read once from the injected environment by the running unit's per-value table;
the clock is the only reader of time; the install and the prompts directory are read-only. No other
part of the package reads the environment, the working directory, the time or a file of its own.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import pathlib
import string
import time
import types
from collections.abc import Mapping, Sequence
from typing import Protocol

ENV_NAMES: Mapping[str, str] = types.MappingProxyType({
    "state_dir": "HOLONIC_STATE_DIR", "host": "MU_SPEC_HOST", "port": "MU_SPEC_PORT",
    "prompts_dir": "MU_SPEC_PROMPTS_DIR", "git_sha": "HOLONIC_GIT_SHA",
    "github_token": "MU_SPEC_GITHUB_TOKEN", "mu_spec_root": "MU_SPEC_ROOT",
})
# used only when the variable is absent, never when its text is ''; a field not a key has no default
ABSENT_DEFAULTS: Mapping[str, object] = types.MappingProxyType(
    {"state_dir": "state", "host": "127.0.0.1", "port": 9006, "prompts_dir": "prompts"})
EMPTY_DIR = "."  # what an empty state or prompts text becomes
PROJECTS_SUBDIR = "projects"  # the projects location is <state dir>/projects unless MU_SPEC_ROOT is given

START_FAIL_LINE = "mu-spec cannot start: {name}={value!r}: {reason}"

_PACKAGE_DIR = pathlib.Path(__file__).resolve().parent


@dataclasses.dataclass(frozen=True)
class Config:
    state_dir: pathlib.Path
    root: pathlib.Path
    root_text: str
    host: str
    port: int | None
    prompts_dir: pathlib.Path
    git_sha: str | None
    github_token: str | None = dataclasses.field(repr=False)
    mu_spec_root: str | None
    raw: Mapping[str, str | None]


@dataclasses.dataclass(frozen=True)
class ConfigProblem:
    field: str
    value: str
    reason: str


def read_environment() -> dict[str, str]:
    """A copy of os.environ; the only read of the environment in the package."""
    return dict(os.environ)


def read_working_directory() -> pathlib.Path:
    """The working directory; the only read of it in the package."""
    return pathlib.Path(os.getcwd())


def _dir_text(environ: Mapping[str, str], field: str) -> str:
    text = environ.get(ENV_NAMES[field])
    if text is None:
        return str(ABSENT_DEFAULTS[field])
    return text if text != "" else EMPTY_DIR


def load_config(environ: Mapping[str, str], cwd: pathlib.Path,
                install_commit: str | None = None) -> tuple[Config, tuple[ConfigProblem, ...]]:
    """The running unit's table, each value resolved on its own; never reads, exits or raises."""
    raw = {field: environ.get(name) for field, name in ENV_NAMES.items() if field != "github_token"}
    problems: list[ConfigProblem] = []

    state_text = _dir_text(environ, "state_dir")
    state_dir = cwd / state_text

    root_given = environ.get(ENV_NAMES["mu_spec_root"])
    mu_spec_root = root_given if root_given else None
    if mu_spec_root is None:
        root = state_dir / PROJECTS_SUBDIR
        root_text = str(pathlib.Path(state_text) / PROJECTS_SUBDIR)
    else:
        root = cwd / mu_spec_root
        root_text = mu_spec_root

    host_text = environ.get(ENV_NAMES["host"])
    host = str(ABSENT_DEFAULTS["host"]) if host_text is None else host_text

    port_text = environ.get(ENV_NAMES["port"])
    port: int | None
    if port_text is None:
        port = int(ABSENT_DEFAULTS["port"])  # type: ignore[call-overload]
    else:
        try:
            port = int(port_text)
        except ValueError as error:
            port = None
            problems.append(ConfigProblem("port", port_text, str(error)))

    prompts_dir = cwd / _dir_text(environ, "prompts_dir")

    sha_text = environ.get(ENV_NAMES["git_sha"])
    git_sha = sha_text.strip() if sha_text else install_commit

    token_text = environ.get(ENV_NAMES["github_token"])
    github_token = token_text.strip() if token_text and token_text.strip() else None

    config = Config(
        state_dir=state_dir, root=root, root_text=root_text, host=host, port=port,
        prompts_dir=prompts_dir, git_sha=git_sha, github_token=github_token,
        mu_spec_root=mu_spec_root, raw=types.MappingProxyType(raw),
    )
    return config, tuple(problems)


def start_refusal(problems: Sequence[ConfigProblem]) -> str | None:
    """The start's one refusal line for a port problem, or None when there is none."""
    for p in problems:
        if p.field == "port":
            return START_FAIL_LINE.format(name=ENV_NAMES["port"], value=p.value, reason=p.reason)
    return None


def location_refusal(config: Config, error: OSError) -> str:
    """The start's one refusal line when the store cannot make the projects location."""
    name = ENV_NAMES["mu_spec_root"] if config.mu_spec_root is not None else ENV_NAMES["state_dir"]
    return START_FAIL_LINE.format(name=name, value=config.root_text, reason=str(error))


def _is_hex(text: str) -> bool:
    return text != "" and all(c in string.hexdigits for c in text)


def install_commit() -> str | None:
    """The installed copy's commit from the .git beside the package; never raises, runs nothing."""
    git = InstallAssets().root.parent / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if _is_hex(head):
            return head
        if not head.startswith("ref: "):
            return None
        name = head[len("ref: "):].strip()
        loose = git / name
        if loose.is_file():
            lines = loose.read_text(encoding="utf-8").splitlines()
            return lines[0].strip() if lines else ""
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] == name:
                return fields[0]
    except (OSError, UnicodeDecodeError):
        return None
    return None


class Clock(Protocol):
    def now(self) -> datetime.datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    """The only code in the package that reads time."""

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class FixedClock:
    """A clock for tests: the k-th now() is start + k*step, the k-th monotonic() k*step seconds."""

    def __init__(self, start: datetime.datetime, step: datetime.timedelta = datetime.timedelta(0)) -> None:
        self._start = start
        self._step = step
        self._now_calls = 0
        self._monotonic_calls = 0

    def now(self) -> datetime.datetime:
        k = self._now_calls
        self._now_calls += 1
        return self._start + k * self._step

    def monotonic(self) -> float:
        k = self._monotonic_calls
        self._monotonic_calls += 1
        return (k * self._step).total_seconds()


def _under(base: pathlib.Path, relpath: str) -> pathlib.Path | None:
    """base / relpath when relpath is relative with no '..' part, else None."""
    rel = pathlib.Path(relpath)
    if rel.anchor or rel.is_absolute() or ".." in rel.parts:
        return None
    return base / rel


@dataclasses.dataclass(frozen=True)
class InstallAssets:
    root: pathlib.Path = dataclasses.field(default_factory=lambda: _PACKAGE_DIR)

    def read(self, relpath: str) -> bytes | None:
        path = _under(self.root, relpath)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None


@dataclasses.dataclass(frozen=True)
class Prompts:
    directory: pathlib.Path

    def read(self, name: str) -> str | None:
        path = _under(self.directory, name + ".md")
        if path is None:
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return data.decode("utf-8")
