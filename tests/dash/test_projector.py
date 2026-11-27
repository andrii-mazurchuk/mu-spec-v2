"""mu_spec.dash.projector: build, one snapshot to one project's Projection (S-456).

T-453 pins that a build answers each bundle read once and joins heads in HEAD order, that a
repeat build is byte-equal, that each view's token moves with the marker, the cached identity's
type, the inbox and given exactly where coverage says, and that the run part is copied from a
real run registry's progress view alone, never from the emission view. The fixture store is
seeded through the real write path. Modules under test are imported inside the test, so a module
not yet written fails only the tests that need it.
"""

import collections
import collections.abc
import dataclasses
import datetime
import json

from mu_spec.outside import FixedClock

T = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
ACTOR = "a"
MARKER = ("n", 1, "bot")
ORDER_SAME_UNDER_GIVEN = ("order", "read", "waves", "modules", "ship")
INBOX_VIEWS = ("spine", "queues")


class FakeGitHub:
    """A GitHubClient stand-in: every method call on it is recorded as a request, none is sent."""

    def __init__(self) -> None:
        self.requests = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def request(*args, **kwargs):
            self.requests.append((name, args, kwargs))

        return request


def set_environment(monkeypatch, **texts: str) -> None:
    """Every configuration variable absent but those given, by field name."""
    from mu_spec.outside import ENV_NAMES

    for name in ENV_NAMES.values():
        monkeypatch.delenv(name, raising=False)
    for field, text in texts.items():
        monkeypatch.setenv(ENV_NAMES[field], text)


def plain(body):
    """body as JSON would carry it: mappings as dicts, sequences as lists, key order kept."""
    def default(value):
        if isinstance(value, collections.abc.Mapping):
            return dict(value)
        return list(value)

    return json.loads(json.dumps(body, default=default))


def request_id(body):
    if isinstance(body, dict):
        for field in ("request_id", "id"):
            if field in body:
                return body[field]
        created = body.get("created")
        if isinstance(created, list) and len(created) == 1:
            return created[0]
    raise AssertionError(f"post_request answer names no request id: {body!r}")


def dispatched(rt, tool: str, args: dict):
    from mu_spec.tools.catalogue import dispatch

    outcome = dispatch(rt, tool, args)
    assert 200 <= outcome.status < 300, f"{tool} {args!r} refused: {outcome!r}"
    return plain(outcome.body)


def acted(rt, bound, name: str, body: dict):
    """One person-only action on "p" through its declared route; it must answer 2xx."""
    from mu_spec.actions import handle_action
    from mu_spec.declarations import ACTIONS

    (spec,) = [s for s in ACTIONS if s.name == name]
    outcome = handle_action(rt, bound, spec.method, spec.path.replace("{project}", "p"), body)
    assert outcome is not None and 200 <= outcome.status < 300, f"{name} refused: {outcome!r}"
    return plain(outcome.body)


def seeded_runtime(tmp_path, monkeypatch):
    """A runtime over a fresh store holding "p" with an entry in every layer and one cut unit, and an
    inbox holding one request for "p"; its observer is the returned Stamps. Returns (runtime, Stamps,
    {layer: entry id})."""
    from mu_spec.actions import action_handlers, bind_actions
    from mu_spec.dash.stamps import Stamps
    from mu_spec.registry import build_runtime
    from mu_spec.ship.part import build_ship

    state = tmp_path / "state"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    set_environment(monkeypatch, port="9100", state_dir=str(state))
    stamps = Stamps()
    rt = build_runtime(ship_builder=build_ship, observer=stamps, github=FakeGitHub())

    rid = request_id(dispatched(rt, "post_request", {
        "type": "initiate", "title": "projector project", "project": "p", "actor": ACTOR}))
    dispatched(rt, "create_project", {"project": "p", "in_response_to": rid, "actor": ACTOR})
    ids = {}
    parent = []
    for layer in ("I", "B", "A", "S", "T"):
        entry = {"layer": layer, "title": f"p {layer}", "body": f"the {layer} entry of p", "derives_from": parent}
        body = dispatched(rt, "submit_amendment", {
            "project": "p", "in_response_to": rid, "actor": ACTOR, "entries": [entry]})
        assert isinstance(body.get("created"), list) and len(body["created"]) == 1, body
        ids[layer] = body["created"][0]
        parent = [ids[layer]]
    s_id = ids["S"]
    dispatched(rt, "declare_module", {"project": "p", "path": "src/core.py", "implements": [s_id], "actor": ACTOR})
    dispatched(rt, "submit_proposal", {"project": "p", "proposal": {"core": [s_id]}, "note": "first",
                                       "actor": ACTOR})
    bound = bind_actions(action_handlers(rt))
    acted(rt, bound, "ratify", {})
    acted(rt, bound, "cut_units", {})
    return rt, stamps, ids


def parts(projection) -> dict:
    """Each view's head, rest and selections."""
    return {view: (vp.head, vp.rest, dict(vp.selections)) for view, vp in projection.views.items()}


def tokens(projection) -> dict:
    return {view: vp.token for view, vp in projection.views.items()}


def keys_anywhere(value) -> set:
    """Every key of every object anywhere inside a decoded JSON value."""
    found = set()
    if isinstance(value, dict):
        for k, v in value.items():
            found.add(k)
            found |= keys_anywhere(v)
    elif isinstance(value, list):
        for v in value:
            found |= keys_anywhere(v)
    return found


def test_build_parts_and_tokens(tmp_path, monkeypatch):
    from mu_spec.dash import projector
    from mu_spec.dash.projector import Given, ReadRuntime, build
    from mu_spec.dash.runpart import run_part
    from mu_spec.dash.snapshot import strip_progress, take_snapshot
    from mu_spec.dash.stamps import INBOX_KEY, Stamp, record_digest
    from mu_spec.dash.views import BUNDLE, ENTRY_TOOL, HEAD, LAYER_TOOL, LAYERS
    from mu_spec.ship.access import Identity

    from mu_spec.ship.runs import PROGRESS_FIELDS, RunRegistry

    # Arrange: the seeded store, its snapshot, the records the store holds at that snapshot
    rt, stamps, ids = seeded_runtime(tmp_path, monkeypatch)
    config = rt.config
    clock = FixedClock(T)
    snap = take_snapshot("p", stamps, lambda name: (MARKER, None), (), lambda: Identity("bot", "Bot"))
    assert snap.marker == MARKER
    assert snap.progress is None
    given = Given(record=rt.store.read("p"), inbox=rt.store.read_inbox())

    real_run_read = projector.run_read
    calls = []

    def counting(read_rt, tool, inputs):
        calls.append((tool, json.dumps(inputs, sort_keys=True)))
        return real_run_read(read_rt, tool, inputs)

    def pair(tool, inputs):
        return tool, json.dumps(inputs, sort_keys=True)

    # Act
    with monkeypatch.context() as patched:
        patched.setattr(projector, "run_read", counting)
        P = build(snap, config, clock)

    # 1. every bundle read answered exactly once; every other read an entry or a layer page
    assert len(BUNDLE) == 16
    counted = collections.Counter(calls)
    bundle_pairs = {pair(*BUNDLE[name]) for name in BUNDLE}
    assert len(bundle_pairs) == 16
    for name in BUNDLE:
        assert counted[pair(*BUNDLE[name])] == 1, (name, counted[pair(*BUNDLE[name])])
    others = [call for call in calls if call not in bundle_pairs]
    assert others, "no entry or layer read was made"
    assert {tool for tool, _ in others} <= {ENTRY_TOOL, LAYER_TOOL}, others

    # 2. the spine head joins its HEAD reads in order, each run_read's own bytes; spine has no rest, ship has
    alone = ReadRuntime(snap, config, clock, None)
    head = json.loads(P.views["spine"].head.fields)
    assert list(head) == list(HEAD["spine"])
    for name in HEAD["spine"]:
        assert head[name] == json.loads(real_run_read(alone, *BUNDLE[name])), name
    assert P.views["spine"].rest is None
    assert P.views["ship"].rest is not None

    # 3. a second build of the snapshot is byte-equal in every part, token and run part
    P2 = build(snap, config, clock)
    assert set(P2.views) == set(P.views)
    assert parts(P2) == parts(P)
    assert tokens(P2) == tokens(P)
    assert P2.run == P.run

    # 4. read's selections hold every layer, its head B's part; spine's one key per listed entry id
    read = P.views["read"]
    assert set(read.selections) == set(LAYERS)
    assert read.head == read.selections["B"][0]
    spine_names = [name for name in BUNDLE if BUNDLE[name][0] == "get_spine"]
    assert len(spine_names) == 1, spine_names
    listed = [row["id"] for row in json.loads(real_run_read(alone, *BUNDLE[spine_names[0]]))["spine"]]
    assert set(ids.values()) <= set(listed)
    assert set(P.views["spine"].selections) == set(listed)
    assert len(P.views["spine"].selections) == len(set(listed))

    # 5. only the marker's counter changes: ship's token moves, spine's does not
    Q = build(dataclasses.replace(snap, marker=("n", 2, "bot")), config, clock)
    assert Q.views["ship"].token != P.views["ship"].token
    assert Q.views["spine"].token == P.views["spine"].token

    # 6. only the identity's type changes, the login still the marker's: ship's token moves
    R = build(dataclasses.replace(snap, identity=Identity("bot", "User")), config, clock)
    assert R.views["ship"].token != P.views["ship"].token

    # 7. only the inbox digest changes: queues' and spine's tokens move, order's does not
    dispatched(rt, "post_request", {"type": "initiate", "title": "another project", "project": "q",
                                    "actor": ACTOR})
    b2 = stamps.get(INBOX_KEY).data
    assert b2 != snap.inbox.data
    U = build(dataclasses.replace(snap, inbox=Stamp(b2, record_digest(b2), 1)), config, clock)
    for view in INBOX_VIEWS:
        assert U.views[view].token != P.views[view].token, view
    assert U.views["order"].token == P.views["order"].token

    # 8. with given: every part byte-equal; tokens equal off the inbox, different on it
    G = build(snap, config, clock, given=given)
    assert parts(G) == parts(P)
    assert G.run == P.run
    for view in ORDER_SAME_UNDER_GIVEN:
        assert G.views[view].token == P.views[view].token, view
    for view in INBOX_VIEWS:
        assert G.views[view].token != P.views[view].token, view

    # 9. the run part: null without progress; copied from a real registry's progress view, never from run
    assert P.run == b"null"
    runs = RunRegistry(FixedClock(T))
    run = runs.admit("p", "person_ship", None, runs.generation("p"))
    runs.report(run, step="creating", total=5, processed=2)
    pv = runs.progress_view("p")[0]
    assert (pv["stage"], pv["stage_done"], pv["stage_total"], pv["percent"]) == ("issues", 2, 5, 40)
    assert "time_left" not in pv
    ev = strip_progress(pv)
    assert not any(name in ev for name in PROGRESS_FIELDS)
    V = build(dataclasses.replace(snap, run=ev, progress=pv), config, clock)
    v_run = json.loads(V.run)
    assert v_run == json.loads(run_part(pv, "n"))
    assert v_run["id"] == "n:" + str(pv["admitted"])
    assert (v_run["stage"], v_run["stage_done"], v_run["stage_total"], v_run["percent"]) == ("issues", 2, 5, 40)
    assert "time_left" not in v_run
    emission = [name for name in HEAD["ship"] if BUNDLE[name][0] == "get_emission"]
    assert len(emission) == 1, HEAD["ship"]
    held = keys_anywhere(json.loads(V.views["ship"].head.fields)[emission[0]])
    assert not held & set(PROGRESS_FIELDS), held & set(PROGRESS_FIELDS)
    W = build(dataclasses.replace(snap, run=ev, progress=None), config, clock)
    assert W.run == b"null"
