"""mu_spec.dash.readsource: ReadRuntime, SnapshotStore, SnapshotShip and run_read (S-454).

T-171 pins that a SnapshotStore refuses every write and every other project, that records passed
as given are answered without any decode, that given with Ship events is refused, and that the
snapshot's identity, login and type both, reaches the emission read exactly as the real runtime's
cached identity does. The fixture project is created through the registry on a temporary store.
Modules under test are imported inside the test, so a module not yet written fails only the tests
that need it.
"""

import dataclasses
import datetime
import json
import threading
import time

import pytest

from mu_spec.outside import FixedClock

T = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
ACTOR = "a"
LOGIN, TYPE = "octo", "User"
WITHIN_S = 5.0
POLL_S = 0.02


class FakeClient:
    """A GitHubClient stand-in: GET /user answers LOGIN and TYPE, every other request an empty list."""

    def __init__(self) -> None:
        self.requests = []
        self._lock = threading.Lock()

    def request(self, method, path, body=None):
        from mu_spec.github import GitHubResponse

        with self._lock:
            self.requests.append((method, path, body))
        if (method, path.lstrip("/")) == ("GET", "user"):
            return GitHubResponse(status=200, body={"login": LOGIN, "type": TYPE}, headers={})
        return GitHubResponse(status=200, body=[], headers={})


def set_environment(monkeypatch, **texts: str) -> None:
    """Every configuration variable absent but those given, by field name."""
    from mu_spec.outside import ENV_NAMES

    for name in ENV_NAMES.values():
        monkeypatch.delenv(name, raising=False)
    for field, text in texts.items():
        monkeypatch.setenv(ENV_NAMES[field], text)


def request_id(body):
    if isinstance(body, dict):
        for field in ("request_id", "id"):
            if field in body:
                return body[field]
        created = body.get("created")
        if isinstance(created, (list, tuple)) and len(created) == 1:
            return created[0]
    pytest.fail(f"post_request answer names no request id: {body!r}")


def dispatched(rt, tool: str, args: dict):
    from mu_spec.tools.catalogue import dispatch

    outcome = dispatch(rt, tool, args)
    assert 200 <= outcome.status < 300, f"{tool} refused: {outcome!r}"
    return json.loads(json.dumps(outcome.body, default=lambda v: dict(v) if hasattr(v, "items") else list(v)))


def paths_holding(value, wanted, at=()) -> list[tuple]:
    """Every key path inside a decoded JSON value at which it holds wanted."""
    if value == wanted and isinstance(value, str):
        return [at]
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            found += paths_holding(v, wanted, at + (k,))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found += paths_holding(v, wanted, at + (i,))
    return found


def at_path(value, path):
    for step in path:
        if isinstance(value, dict) and step in value:
            value = value[step]
        elif isinstance(value, list) and isinstance(step, int) and step < len(value):
            value = value[step]
        else:
            return KeyError(path)
    return value


def test_read_runtime_isolation(tmp_path, monkeypatch):
    from mu_spec.dash import readsource
    from mu_spec.dash.readsource import Given, ReadOnly, ReadRuntime, SnapshotStore, run_read
    from mu_spec.dash.snapshot import take_snapshot
    from mu_spec.dash.stamps import Stamps, canonical
    from mu_spec.dash.views import BUNDLE
    from mu_spec.registry import build_runtime
    from mu_spec.ship.access import Identity
    from mu_spec.ship.part import build_ship
    from mu_spec.tools.catalogue import dispatch

    # Arrange: "p" created through the registry; the real Ship part's cached identity learned as octo/User
    state = tmp_path / "state"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    set_environment(monkeypatch, port="9100", state_dir=str(state), github_token="t")
    stamps = Stamps()
    runtime = build_runtime(ship_builder=build_ship, observer=stamps, github=FakeClient(), clock=FixedClock(T))
    rid = request_id(dispatched(runtime, "post_request", {
        "type": "initiate", "title": "readsource project", "project": "p", "actor": ACTOR}))
    dispatched(runtime, "create_project", {"project": "p", "in_response_to": rid, "actor": ACTOR})
    ship = runtime.ship
    ship.learn_identity(runtime)
    deadline = time.monotonic() + WITHIN_S
    while ship.identity() is None and time.monotonic() < deadline:
        time.sleep(POLL_S)
    assert ship.identity() == Identity(LOGIN, TYPE), "the real Ship part never cached octo/User"
    r = runtime.store.read("p")
    d = runtime.store.read_inbox()
    config, clock = runtime.config, runtime.clock
    snap = take_snapshot("p", stamps, ship.marked_view, (), lambda: Identity(LOGIN, TYPE))

    # 1. every write refused; only snap.project is held
    st = SnapshotStore(snap)
    for write in (st.write_record, st.create_project, st.write_inbox, st.append_log):
        with pytest.raises(ReadOnly):
            write()
    assert st.read("other") is None
    assert st.project_names() == ["p"]

    # 2. with given: no decode reached, every bundle read answers, the given records are the ones read
    def refuse(*args, **kwargs):
        raise AssertionError("a given record was decoded")

    with monkeypatch.context() as patched:
        for name in ("decode_record", "decode_inbox", "decode_log_line"):
            patched.setattr(readsource, name, refuse)
        rt = ReadRuntime(snap, config, clock, Given(r, d))
        for n in BUNDLE:
            answer = run_read(rt, *BUNDLE[n])
            assert isinstance(answer, bytes), n
            loaded = json.loads(answer)
            assert "status" in loaded and "body" in loaded, n
        assert rt.store.read("p") is r
        assert rt.store.read_inbox() is d

    # 3. given with Ship events is refused
    with pytest.raises(ValueError):
        SnapshotStore(dataclasses.replace(snap, events=(b"x",)), Given(r, d))

    # 4. without given: the emission read equals the real runtime's for the same cached identity
    got = run_read(ReadRuntime(snap, config, clock), *BUNDLE["emission"])
    o = dispatch(runtime, "get_emission", {"project": "p"})
    want = canonical({"status": o.status, "body": o.body})
    assert isinstance(got, bytes) and isinstance(want, bytes)
    assert got == want
    g = json.loads(got)["body"]
    w = json.loads(want)["body"]
    for value in (LOGIN, TYPE):
        paths = paths_holding(w, value)
        assert paths, f"the real runtime's emission body shows no {value!r}"
        for path in paths:
            assert at_path(g, path) == value, (value, path)

    # 5. the identity is the snapshot's own; none for a snapshot taken without one
    assert ReadRuntime(snap, config, clock).ship.identity() is snap.identity
    bare = take_snapshot("p", stamps, ship.marked_view, ())
    assert ReadRuntime(bare, config, clock).ship.identity() is None
