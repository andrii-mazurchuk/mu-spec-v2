"""Record dashboard/test/fixtures/effects.json, the effect fixtures of dashboard/test/effects.test.ts (T-475).

The running unit, the install at MU_SPEC_REFERENCE_INSTALL, is started through the shared launch on
copies restored from the acceptance store (MU_SPEC_ACCEPTANCE_STORE), a fresh copy per group, and
called over HTTP on 127.0.0.1. Each read is taken by its read name (mu_spec.dash.views BUNDLE) at its
tool's method and path in the unit's own tool discovery; each action at its method and path in the
unit's /actions. Every fact is read from an answer as today's page reads it to draw it: a spine row's
slice is its 'slice', a unit is cut when its key is in the units read's cut, and out when the emission
read's 'already' holds it.

P is the first project, in Python string order of the list_projects names, whose spine shows a row in
a slice and whose cut makes C non-empty. MAP, P's slicing as its spine shows it, is the proposal the
ratify and reject groups seed before any other call. Run on its own: it is named so that a run of
the suite does not collect it. The modules it drives are imported inside the test, so a module not
yet written fails only this recorder.
"""

import contextlib
import http.client
import json
import os
import shutil
import urllib.parse
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "dashboard" / "test" / "fixtures" / "effects.json"
REFERENCE_VAR = "MU_SPEC_REFERENCE_INSTALL"
STORE_VAR = "MU_SPEC_ACCEPTANCE_STORE"
CALL_TIMEOUT_S = 120.0
SEED_NOTE = "effects fixture"
SEED_ACTOR = "effects-recorder"
REJECT_NOTE = "n"
CUT_BODY = {"note": ""}  # the cut as today's page sends it


def from_env(name: str, what: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} is not set: it must name {what}")
    return value


def decode(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return None


class Unit:
    """One started running unit: its tool and action routes, and calls to it."""

    def __init__(self, host: str, port: int, bundle: dict, routes: "Unit | None" = None) -> None:
        """routes: a unit of the same install already started, whose discovery is reused, so the
        first call this one is sent is the recorder's own."""
        self.host, self.port, self.bundle = host, port, bundle
        self.tools = routes.tools if routes else self.listing("/tools", "tools")
        self.actions = routes.actions if routes else self.listing("/actions", "actions")

    def send(self, method: str, path: str, body: object = None) -> tuple[int, object]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=CALL_TIMEOUT_S)
        try:
            data = None if body is None else json.dumps(body).encode("utf-8")
            headers = {"Content-Type": "application/json"} if data is not None else {}
            conn.request(method, path, body=data, headers=headers)
            answer = conn.getresponse()
            return answer.status, decode(answer.read())
        finally:
            conn.close()

    def listing(self, path: str, field: str) -> dict[str, dict]:
        status, body = self.send("GET", path)
        assert status == 200 and isinstance(body, dict), f"GET {path} answered {status}: {body!r}"
        return {d["name"]: d for d in body.get(field) or () if isinstance(d, dict) and isinstance(d.get("name"), str)}

    def tool(self, name: str, args: dict) -> tuple[int, object]:
        """One tool call at its declared method and path: path parameters from args, the rest the
        query of a GET or the JSON body of any other method."""
        assert name in self.tools, f"the running unit's discovery lists no tool {name!r}"
        spec = self.tools[name]
        method, route = str(spec["method"]).upper(), "/" + str(spec["path"]).lstrip("/")
        rest = dict(args)
        for param in [p.split("}")[0] for p in route.split("{")[1:]]:
            route = route.replace("{" + param + "}", urllib.parse.quote(str(rest.pop(param)), safe=""))
        if method == "GET":
            query = urllib.parse.urlencode(rest)
            return self.send(method, route + ("?" + query if query else ""))
        return self.send(method, route, rest)

    def read(self, name: str, project: str) -> object:
        """P's read by its read name; it must answer 200."""
        tool, args = self.bundle[name]
        status, body = self.tool(tool, {"project": project, **dict(args)})
        assert status == 200, f"read {name} ({tool}) of {project!r} answered {status}: {body!r}"
        return body

    def act(self, name: str, project: str, body: dict) -> tuple[int, object]:
        """A person's action at its declared method and path."""
        assert name in self.actions, f"the running unit's /actions lists no action {name!r}"
        spec = self.actions[name]
        path = "/" + spec["path"].lstrip("/").replace("{project}", urllib.parse.quote(project, safe=""))
        return self.send(str(spec["method"]).upper(), path, body)


def slicing(spine: object) -> dict[str, list[str]]:
    """Slice name -> sorted entry ids of the spine rows shown in it; rows in no slice left out."""
    rows = spine.get("spine") if isinstance(spine, dict) else None
    out: dict[str, list[str]] = {}
    seen: dict[str, str] = {}
    for row in rows or ():
        name = row.get("slice") if isinstance(row, dict) else None
        if not isinstance(name, str) or not name:
            continue
        entry = row["id"]
        assert seen.get(entry, name) == name, f"entry {entry!r} is shown in slices {seen[entry]!r} and {name!r}"
        seen[entry] = name
        out.setdefault(name, []).append(entry)
    return {name: sorted(set(ids)) for name, ids in out.items()}


def cut_keys(units: object) -> set[str]:
    cut = units.get("cut") if isinstance(units, dict) else None
    return {u["key"] for u in (cut or {}).get("units") or () if isinstance(u, dict) and "key" in u}


def out_keys(emission: object) -> set[str]:
    already = emission.get("already") if isinstance(emission, dict) else None
    return {k for k, v in (already or {}).items() if v}


def new_cut(units_before: object, emission_before: object, units_cut: object) -> list[str]:
    """C: the keys units_cut shows as cut, neither cut nor out before."""
    return sorted(cut_keys(units_cut) - cut_keys(units_before) - out_keys(emission_before))


def seed(unit: Unit, project: str, mapping: dict) -> None:
    """The waiting proposal of the ratify and reject groups, sent before any other call."""
    status, body = unit.tool("submit_proposal", {"project": project, "proposal": mapping,
                                                 "note": SEED_NOTE, "actor": SEED_ACTOR})
    assert 200 <= status < 300, f"submit_proposal for {project!r} answered {status}: {body!r}"
    shown = unit.read("proposal", project)
    assert isinstance(shown, dict) and shown.get("status") == "pending", f"no waiting proposal after seeding: {shown!r}"


def pressed(unit: Unit, name: str, project: str, body: dict) -> tuple[int, object]:
    status, answer = unit.act(name, project, body)
    assert 200 <= status < 300, f"{name} {body!r} on {project!r} answered {status}: {answer!r}"
    return status, answer


def test_record_effect_fixtures(tmp_path):
    from mu_spec.dash.views import BUNDLE
    from unitcheck.launch import launch
    from unitcheck.session import open_session

    from startcheck.runner import REACH_HOST, UnitCommand, restore, stop

    running = from_env(REFERENCE_VAR, "the running unit's install root")
    store = from_env(STORE_VAR, "a pristine copy of the acceptance store's projects folder")
    cmd = UnitCommand(label="running", install_root=running)
    root = tmp_path / "check"
    root.mkdir()
    pristine_store = tmp_path / "pristine-store"
    shutil.copytree(store, pristine_store / "projects")
    pristine_prompts = Path(running) / "prompts"
    own_env = dict(os.environ)

    session = open_session(own_env)

    routes = None

    @contextlib.contextmanager
    def fresh_copy():
        restore(str(root), str(pristine_store), str(pristine_prompts))
        it = launch(cmd, str(root), own_env, session.standin, session.neighbour)
        try:
            yield Unit(REACH_HOST, it.started.port, BUNDLE, routes)
        finally:
            stop(it.started)

    try:
        # P: the first project whose spine shows a slice and whose cut makes C non-empty
        project = None
        with fresh_copy() as unit:
            routes = unit
            listed = unit.tool("list_projects", {})
            assert listed[0] == 200 and isinstance(listed[1], dict), f"list_projects answered {listed!r}"
            for name in sorted(listed[1].get("projects") or ()):
                if not slicing(unit.read("spine", name)):
                    continue
                before, emission = unit.read("units", name), unit.read("emission", name)
                status, _ = unit.act("cut_units", name, CUT_BODY)
                if 200 <= status < 300 and new_cut(before, emission, unit.read("units", name)):
                    project = name
                    break
        assert project is not None, ("no project of the acceptance store has a spine row in a slice and a "
                                     "cut that makes C non-empty")

        # cut group, unseeded: the spine, units and emission before any press, the units after the cut
        with fresh_copy() as unit:
            spine_before = unit.read("spine", project)
            units_before = unit.read("units", project)
            emission_before = unit.read("emission", project)
            pressed(unit, "cut_units", project, CUT_BODY)
            units_cut = unit.read("units", project)
        assert new_cut(units_before, emission_before, units_cut), f"C is empty for {project!r}"
        mapping = slicing(spine_before)
        assert mapping, f"no spine row of {project!r} is in a slice"

        # repo group, unseeded: the set_repo 'o/n' answer as sent, the repo read after it and after 'o/m'
        with fresh_copy() as unit:
            status, answer = pressed(unit, "set_repo", project, {"repo": "o/n"})
            set_answer = {"status": status, "body": answer}
            repo_on = unit.read("repo", project)
            pressed(unit, "set_repo", project, {"repo": "o/m"})
            repo_om = unit.read("repo", project)

        # ratify group, seeded: the proposal read before any press, the proposal and spine after ratify
        with fresh_copy() as unit:
            seed(unit, project, mapping)
            prop_before = unit.read("proposal", project)
            pressed(unit, "ratify", project, {})
            prop_ratified = unit.read("proposal", project)
            spine_ratified = unit.read("spine", project)

        # reject group, seeded: the proposal read after reject with note 'n'
        with fresh_copy() as unit:
            seed(unit, project, mapping)
            pressed(unit, "reject", project, {"note": REJECT_NOTE})
            prop_rejected = unit.read("proposal", project)
    finally:
        session.close()

    fixtures = {
        "repo_on": repo_on,
        "repo_om": repo_om,
        "SET": set_answer,
        "prop_before": prop_before,
        "prop_ratified": prop_ratified,
        "spine_ratified": spine_ratified,
        "prop_rejected": prop_rejected,
        "units_before": units_before,
        "units_cut": units_cut,
        "emission_before": emission_before,
        "spine_before": spine_before,
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(fixtures, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
