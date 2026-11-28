"""No tool outside the GitHub holders makes a GitHub request, over a copy of the staged acceptance store (T-497).

An in-process check over the stored copy (S-519's second kind): it runs only the rebuilt package, in
the test process, which the subtree's rebuilt_package fixture checks is imported from the rebuilt
install. It launches no unit, never calls call or compare_reads, never imports the shared launch and
needs the staged store only. The working copy is made by S-491's restore; the runtime is built with
a recording fake GitHub client, and every tool not served from a GITHUB_HOLDERS module or the Ship
slice is called on every project, with arguments formed from its declared input shape. The
differential half of the S-96 tests, through launched units, is tests/acceptance/test_github_isolation.py;
this module's basename differs from it so both collect in one session. Modules under test are
imported inside the test, so a module not yet written fails only the tests that need it.
"""

import itertools
import json
import re
from collections.abc import Mapping
from pathlib import Path

ACTOR = "github-isolation"
FORK_PREFIX = "gh-iso-"
ID = re.compile(r"^[IBAST]-\d+$")
READS = ("get_slice_context", "get_spine", "get_waves", "check_gates", "get_plan")
CALL_INPUTS = ("project", "actor", "in_response_to")  # fork_project's inputs that are not the new name


def plain(value: object) -> object:
    """A frozen value as plain dicts and lists."""
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def strings(doc: object):
    """Every string in a decoded answer, keys included, in document order."""
    if isinstance(doc, str):
        yield doc
    elif isinstance(doc, dict):
        for k, v in doc.items():
            yield from strings(k)
            yield from strings(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from strings(v)


def request_id(body: object) -> object:
    """The id a post_request answer names, or None when it names none."""
    if isinstance(body, dict):
        for field in ("request_id", "id"):
            if field in body:
                return body[field]
        created = body.get("created")
        if isinstance(created, list) and len(created) == 1:
            return created[0]
    return None


def records(root: Path) -> dict[str, bytes]:
    """Every file under root other than the forks', by its relative path."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.relative_to(root).parts[0].startswith(FORK_PREFIX)
    }


def changed_records(before: dict[str, bytes], after: dict[str, bytes], forks: list[str]) -> list[str]:
    """The records the test changed beyond what writing on the forks may change.

    A project's own records must be byte for byte unchanged. A file at the store root is shared by
    every project (the inbox, the lifecycle log): its earlier bytes must be an unchanged prefix, and
    every line appended after them must name a fork, since post_request, fork_project and the writes
    on F append there."""
    names = [f.encode("utf-8") for f in forks]
    changed = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if "/" in key or new is None or not new.startswith(old or b""):
            changed.append(key)
            continue
        added = [line for line in new[len(old or b""):].splitlines() if line.strip()]
        if not all(any(n in line for n in names) for line in added):
            changed.append(key)
    return changed


class FakeGitHub:
    """A GitHubClient stand-in: records every request and answers 200 with an empty list."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object]] = []

    def request(self, method, path, body=None):
        from mu_spec.github import GitHubResponse

        self.requests.append((method, path, body))
        return GitHubResponse(200, [], {})


class Arguments:
    """Arguments formed from a tool's input_schema, never written per tool."""

    def __init__(self) -> None:
        self.keys = itertools.count(1)

    def form(self, schema: Mapping, project: str, entry: str) -> dict[str, object]:
        properties = schema.get("properties", {})
        args: dict[str, object] = {}
        for name in schema.get("required", ()):
            kind = properties.get(name, {}).get("type")
            if name == "project":
                args[name] = project
            elif kind == "string" and (name.endswith("id") or name == "entry"):
                args[name] = entry
            elif kind == "string":
                args[name] = "x"
            elif kind in ("integer", "number"):
                args[name] = 1
            elif kind == "boolean":
                args[name] = False
            elif kind == "array":
                args[name] = []
            elif kind == "object":
                args[name] = {}
        if "dry_run" in properties:
            args["dry_run"] = True
        if "idempotency_key" in properties:
            args["idempotency_key"] = f"gh-iso-{next(self.keys)}"
        if "actor" in properties:
            args["actor"] = ACTOR
        return args


def test_non_ship_tools_never_call_github(acceptance_inputs, tmp_path, monkeypatch):
    from mu_spec.registry import build_runtime
    from mu_spec.ship.part import build_ship
    from mu_spec.tools.catalogue import TOOLS, dispatch
    from sweeps.source import GITHUB_HOLDERS

    from startcheck.runner import restore

    # 1. the working copy and the environment
    check_root = tmp_path / "check"
    restore(str(check_root), acceptance_inputs.staged_store, acceptance_inputs.pristine_prompts)
    W = check_root / "store" / "projects"
    monkeypatch.setenv("MU_SPEC_ROOT", str(W))
    monkeypatch.setenv("HOLONIC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MU_SPEC_PORT", "9100")
    monkeypatch.delenv("MU_SPEC_GITHUB_TOKEN", raising=False)

    # 2. the runtime, with the fake client handed to the Ship part
    fake = FakeGitHub()
    rt = build_runtime(ship_builder=build_ship, github=fake)
    assert Path(rt.config.root).resolve() == W.resolve(), rt.config.root
    projects = list(rt.store.project_names())
    assert projects, f"no project found under {W}"
    before = records(W)

    # 3. the tools not served from a GitHub holder or the Ship slice
    L = [d for d in TOOLS
         if d.handler.__module__ not in GITHUB_HOLDERS and not d.handler.__module__.startswith("mu_spec.ship.")]
    names = [d.schema.name for d in L]
    assert L, "every tool is served from a GitHub holder"
    for name in READS:
        assert name in names, f"{name} is served from a GitHub holder"
    shapes = {d.schema.name: d.schema.input_schema for d in TOOLS}

    def on_fork(d) -> bool:
        """A tool that changes state and declares no dry run: called on the fork, never on p."""
        return d.schema.method.upper() != "GET" and "dry_run" not in d.schema.input_schema.get("properties", {})

    def with_actor(name: str, args: dict[str, object]) -> dict[str, object]:
        return {**args, "actor": ACTOR} if "actor" in shapes[name].get("properties", {}) else args

    def fork_args(source: str, target: str, rid: object) -> dict[str, object]:
        """fork_project's inputs as its shape declares them: 'project' the source, the one other required input the new name."""
        shape = shapes["fork_project"]
        named = [k for k in shape.get("required", ()) if k not in CALL_INPUTS]
        assert len(named) == 1, f"fork_project's input shape names no single new-project input: {shape!r}"
        args: dict[str, object] = {"project": source, named[0]: target}
        if "in_response_to" in shape.get("properties", {}):
            args["in_response_to"] = rid
        return with_actor("fork_project", args)

    arguments = Arguments()
    answered: dict[str, dict[str, int]] = {}
    for p in projects:
        # 4. the first entry id of p's spine
        spine = dispatch(rt, "get_spine", arguments.form(shapes["get_spine"], p, "x"))
        entry = next((s for s in strings(plain(spine.body)) if ID.match(s)), "x")

        # 5. the fork F that state-changing tools without a dry run are called on
        fork = FORK_PREFIX + p
        if any(on_fork(d) for d in L):
            req = with_actor("post_request", {"type": "initiate", "title": f"github isolation {p}",
                                              "body": "a fork the GitHub isolation check writes on",
                                              "project": fork})
            posted = dispatch(rt, "post_request", req)
            dispatch(rt, "fork_project", fork_args(p, fork, request_id(plain(posted.body))))

        # Act: every tool in L on p (on F where step 5 says so), whatever the earlier ones answered
        answered[p] = {}
        for d in L:
            on = fork if on_fork(d) else p
            out = dispatch(rt, d.schema.name, arguments.form(d.schema.input_schema, on, entry))
            answered[p][d.schema.name] = out.status

    # 1. no GitHub request
    assert fake.requests == [], fake.requests

    # 2. precondition: the reads ran their whole path on at least one project
    assert any(all(answered[p].get(name) == 200 for name in READS) for p in projects), \
        json.dumps({p: {n: answered[p].get(n) for n in READS} for p in projects})

    # 3. every record under W other than the forks' is unchanged: the projects' own byte for byte,
    # the shared root files but for appended lines naming a fork
    changed = changed_records(before, records(W), [FORK_PREFIX + p for p in projects])
    assert changed == [], changed
