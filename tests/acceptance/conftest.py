"""The acceptance inputs and the read comparison every acceptance suite takes them from (S-508).

Acceptance only: this module never imports mu_spec, so the harness stands before every product
unit. The one session fixture reads exactly the two INPUT_VARS, each by its own constant, and fails
every acceptance test (never skips one) when an input is unset or names no directory. The helpers
reach a started unit over HTTP only: read_tools takes the read set from its GET /tools answer,
read_calls fills each route for a project, call sends one request, and compare_reads sends each
call to both units and reports the calls whose answers differ.
"""

import http.client
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence  # noqa: UP035 -- the import list S-508 declares
from urllib.parse import quote

import pytest

REFERENCE_INSTALL_VAR = "MU_SPEC_REFERENCE_INSTALL"  # install root of the running unit (the reference install): the folder holding its mu_spec package
ACCEPTANCE_STORE_VAR = "MU_SPEC_ACCEPTANCE_STORE"  # a pristine copy of a store's projects folder
INPUT_VARS = (REFERENCE_INSTALL_VAR, ACCEPTANCE_STORE_VAR)  # exactly the variables any acceptance suite reads, checked in this order; never used as an environment key
PROMPTS_SUBDIR = "prompts"  # the running unit's own prompts folder, under its install root
STAGED_PROJECTS = "projects"  # the one folder inside staged_store
INPROCESS_SUBDIR = "inprocess"  # tests/acceptance/inprocess: the only acceptance modules that import mu_spec
UNSET_TEXT = "{name} is not set: the acceptance checks need it"
MISSING_TEXT = "{name}={value!r} does not name a directory"
NOT_REBUILT_TEXT = "mu_spec was imported from {found!r}, not from the rebuilt install {expected!r}"
REACH_HOST = "127.0.0.1"


@dataclass(frozen=True)
class AcceptanceInputs:
    running_install: str  # absolute: REFERENCE_INSTALL_VAR's directory; the running unit's install_root for every launch
    rebuilt_install: str  # absolute: the repository root holding the rebuilt mu_spec package, from __file__
    pristine_projects: str  # absolute: ACCEPTANCE_STORE_VAR's directory; never written
    staged_store: str  # a session folder holding only STAGED_PROJECTS, a copy of pristine_projects; never written after staging
    pristine_prompts: str  # running_install / PROMPTS_SUBDIR; never written


def _judge(name: str, value: str | None) -> str:
    """The absolute directory value names; fails the requesting test when it is unset or no directory."""
    if not value:
        pytest.fail(UNSET_TEXT.format(name=name), pytrace=False)
    if not os.path.isdir(value):
        pytest.fail(MISSING_TEXT.format(name=name, value=value), pytrace=False)
    return os.path.abspath(value)


@pytest.fixture(scope="session", autouse=True)
def acceptance_inputs(tmp_path_factory) -> AcceptanceInputs:
    running_install = _judge(REFERENCE_INSTALL_VAR, os.environ.get(REFERENCE_INSTALL_VAR))
    pristine_projects = _judge(ACCEPTANCE_STORE_VAR, os.environ.get(ACCEPTANCE_STORE_VAR))
    staged_store = tmp_path_factory.mktemp("staged_store")
    shutil.copytree(pristine_projects, staged_store / STAGED_PROJECTS)
    return AcceptanceInputs(
        running_install=running_install,
        rebuilt_install=str(Path(__file__).resolve().parents[2]),
        pristine_projects=pristine_projects,
        staged_store=str(staged_store.resolve()),
        pristine_prompts=os.path.join(running_install, PROMPTS_SUBDIR),
    )


@dataclass(frozen=True)
class ReadTool:
    name: str
    method: str  # always 'GET'
    path: str  # the route pattern exactly as discovery lists it
    params: tuple[str, ...]  # the '{x}' names in path, in order of appearance


@dataclass(frozen=True)
class ReadCall:
    tool: str
    path: str  # the filled path
    args: Mapping[str, object]  # inputs not placed in the path; {} unless a test adds some


def _params(path: str) -> tuple[str, ...]:
    names = []
    start = path.find("{")
    while start != -1:
        end = path.find("}", start)
        if end == -1:
            break
        names.append(path[start + 1 : end])
        start = path.find("{", end)
    return tuple(names)


def read_tools(discovery: Mapping[str, object]) -> tuple[ReadTool, ...]:
    """The members of discovery['tools'] whose method is 'GET', in listed order."""
    tools = discovery["tools"]
    assert isinstance(tools, list)
    return tuple(
        ReadTool(t["name"], t["method"], t["path"], _params(t["path"])) for t in tools if t.get("method") == "GET"
    )


def fill(path: str, values: Mapping[str, str]) -> str:
    """Each '{x}' in path replaced by quote(values[x], safe=''); KeyError for a missing x."""
    out = path
    for name in _params(path):
        out = out.replace("{" + name + "}", quote(values[name], safe=""), 1)
    return out


def read_calls(
    tools: Sequence[ReadTool], project: str, values: Mapping[str, Sequence[str]]
) -> tuple[list[ReadCall], tuple[str, ...]]:
    """One call per combination of each tool's param values; tools a param has no value for are uncovered."""
    joined: dict[str, Sequence[str]] = {"project": [project], **values}
    calls: list[ReadCall] = []
    uncovered: list[str] = []
    for tool in tools:
        if any(not joined.get(p) for p in tool.params):
            uncovered.append(tool.name)
            continue
        combos: list[dict[str, str]] = [{}]
        for p in tool.params:
            combos = [{**c, p: v} for c in combos for v in joined[p]]
        calls.extend(ReadCall(tool.name, fill(tool.path, c), {}) for c in combos)
    return calls, tuple(uncovered)


def call(port: int, method: str, path: str, args: Mapping[str, object] | None) -> tuple[int, bytes]:
    """One HTTP/1.1 request to REACH_HOST:port; args, when not None, sent as the JSON body."""
    conn = http.client.HTTPConnection(REACH_HOST, port)
    try:
        if args is None:
            conn.request(method, path)
        else:
            body = json.dumps(args).encode("utf-8")
            conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def compare_reads(running_port: int, rebuilt_port: int, calls: Sequence[ReadCall]) -> list[str]:
    """One line per call whose status or body bytes differ between the two units; [] when all are equal."""
    lines = []
    for c in calls:
        args = dict(c.args) if c.args else None
        status_a, body_a = call(running_port, "GET", c.path, args)
        status_b, body_b = call(rebuilt_port, "GET", c.path, args)
        if status_a == status_b and body_a == body_b:
            continue
        offset = next((i for i, (x, y) in enumerate(zip(body_a, body_b)) if x != y), min(len(body_a), len(body_b)))
        where = f"first differing byte at offset {offset}" if body_a != body_b else "bodies equal"
        lines.append(f"{c.tool} {c.path}: status {status_a} (running) vs {status_b} (rebuilt), {where}")
    return lines
