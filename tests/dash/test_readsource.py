"""mu_spec.dash.readsource: SnapshotStore, SnapshotShip, ReadRuntime and run_read (S-454).

T-171 pins that a SnapshotStore refuses every write and every other project, that records given by
the store boundary are answered without decoding, that given with Ship events is refused, and that
the cached identity's login and type reach the emission read exactly as on the real runtime. The
fixture project is created through the registry on a temporary store; GitHub is a fake client that
answers GET /user, and no request leaves the process. Modules under test are imported inside the
test, so a module not yet written fails only the tests that need it.
"""

import collections.abc
import dataclasses
import datetime
import json
import threading
import time

import pytest

from mu_spec.outside import FixedClock

T = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
ACTOR = "a"
POLL_S = 0.02
LEARN_WINDOW_S = 5.0
MISSING = object()


class FakeClient:
    """A GitHubClient stand-in: request() records each call; GET /user answers octo, a User; all else []."""

    def __init__(self) -> None:
        self.requests = []
        self._lock = threading.Lock()

    def request(self, method, path, body=None):
        from mu_spec.github import GitHubResponse

        with self._lock:
            self.requests.append((method, path, body))
        if (method, path.lstrip("/")) == ("GET", "user"):
            return GitHubResponse(status=200, body={"login": "octo", "type": "User"}, headers={})
        return GitHubResponse(status=200, body=[], headers={})


def set_environment(monkeypatch, **texts: str) -> None:
    """Every configuration variable absent but those given, by field name."""
    from mu_spec.outside import ENV_NAMES

    for name in ENV_NAMES.values():
        monkeypatch.delenv(name, raising=False)
    for field, text in texts.items():
        monkeypatch.setenv(ENV_NAMES[field], text)


def request_id(body):
    if isinstance(body, collections.abc.Mapping):
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
    assert 200 <= outcome.status < 300, f"{tool} {args!r} refused: {outcome!r}"
    return outcome.body


def wait_until(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"{what} within {timeout} s")
        time.sleep(POLL_S)


def leaves(value, path=()):
    """Every (key path, value) of a decoded JSON value's non-container leaves."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from leaves(v, path + (k,))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from leaves(v, path + (i,))
    else:
        yield path, value


def at(value, path):
    """The value at path inside a decoded JSON value, or MISSING."""
    for step in path:
        in_object = isinstance(value, dict) and isinstance(step, str) and step in value
        in_array = isinstance(value, list) and isinstance(step, int) and 0 <= step < len(value)
        if in_object or in_array:
            value = value[step]
        else:
            return MISSING
    return value


def runtime_with_p(tmp_path, monkeypatch):
    """A runtime with a token over a temporary store, "p" created through the registry, its observer
    the returned Stamps, its clock FixedClock(T) and its Ship part's cached identity learned as
    Identity("octo", "User") from the fake GET /user."""
    from mu_spec.dash.stamps import Stamps
    from mu_spec.registry import build_runtime
    from mu_spec.ship.access import Identity
    from mu_spec.ship.part import build_ship

    state = tmp_path / "state"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    set_environment(monkeypatch, port="9100", state_dir=str(state), github_token="t")
    stamps = Stamps()
    rt = build_runtime(ship_builder=build_ship, observer=stamps, clock=FixedClock(T), github=FakeClient())
    rid = request_id(dispatched(rt, "post_request", {
        "type": "initiate", "title": "read source project", "project": "p", "actor": ACTOR}))
    dispatched(rt, "create_project", {"project": "p", "in_response_to": rid, "actor": ACTOR})
    rt.ship.learn_identity(rt)
    wait_until(lambda: rt.ship.identity() is not None, LEARN_WINDOW_S, "the login was not learned")
    assert rt.ship.identity() == Identity("octo", "User")
    return rt, stamps


def test_read_runtime_isolation(tmp_path, monkeypatch):
    from mu_spec.dash import readsource
    from mu_spec.dash.readsource import (
        Given,
        ReadOnly,
        ReadRuntime,
        SnapshotStore,
        run_read,
    )
    from mu_spec.dash.snapshot import take_snapshot
    from mu_spec.dash.stamps import canonical
    from mu_spec.dash.views import BUNDLE
    from mu_spec.ship.access import Identity
    from mu_spec.tools.catalogue import dispatch

    # Arrange: "p" through the registry, its record and the inbox doc, one snapshot of it
    runtime, stamps = runtime_with_p(tmp_path, monkeypatch)
    ship = runtime.ship
    config = runtime.config
    clock = runtime.clock
    r = runtime.store.read("p")
    d = runtime.store.read_inbox()
    assert r is not None
    snap = take_snapshot("p", stamps, ship.marked_view, (), lambda: Identity("octo", "User"))

    # 1. every write refused; another project unknown; only "p" listed
    st = SnapshotStore(snap)
    with pytest.raises(ReadOnly):
        st.write_record()
    with pytest.raises(ReadOnly):
        st.create_project()
    with pytest.raises(ReadOnly):
        st.write_inbox()
    with pytest.raises(ReadOnly):
        st.append_log()
    assert st.read("other") is None
    assert st.project_names() == ["p"]

    # 2. with given: no decode function is reached, every bundle read answers, the given objects answered
    def boom(*args, **kwargs):
        raise AssertionError("a decode function was called with given passed")

    with monkeypatch.context() as patched:
        patched.setattr(readsource, "decode_record", boom)
        patched.setattr(readsource, "decode_inbox", boom)
        patched.setattr(readsource, "decode_log_line", boom)
        rt = ReadRuntime(snap, config, clock, Given(r, d))
        for name in BUNDLE:
            out = run_read(rt, *BUNDLE[name])
            assert isinstance(out, bytes), name
            assert {"status", "body"} <= set(json.loads(out)), name
        assert rt.store.read("p") is r
        assert rt.store.read_inbox() is d

    # 3. given with Ship events is refused
    with pytest.raises(ValueError):
        SnapshotStore(dataclasses.replace(snap, events=(b"x",)), Given(r, d))

    # 4. the emission read through the snapshot equals the real runtime's, login and type included
    got = run_read(ReadRuntime(snap, config, clock), *BUNDLE["emission"])
    assert isinstance(got, bytes)
    assert ship.identity() == Identity("octo", "User")
    o = dispatch(runtime, "get_emission", {"project": "p"})
    want = canonical({"status": o.status, "body": o.body})
    assert isinstance(want, bytes)
    assert got == want
    g = json.loads(got)["body"]
    w = json.loads(want)["body"]
    found = {"octo": 0, "User": 0}
    for path, value in leaves(w):
        if isinstance(value, str) and value in found:
            found[value] += 1
            assert at(g, path) == value, (path, value, at(g, path))
    assert found["octo"] >= 1, w
    assert found["User"] >= 1, w

    # 5. the identity is the snapshot's own; a snapshot with the default identity answers None
    assert ReadRuntime(snap, config, clock).ship.identity() is snap.identity
    bare = take_snapshot("p", stamps, ship.marked_view, ())
    assert bare.identity is None
    assert ReadRuntime(bare, config, clock).ship.identity() is None
