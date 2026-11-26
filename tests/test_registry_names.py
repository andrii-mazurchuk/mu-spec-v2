"""The name the registry checks is the name it writes: a record built under another name is refused.

A create or fork handler that declares a free name and builds a record named after an existing
project raises NameMismatch and leaves that project untouched, and the checks receive
name_of(call), never the built record's name (T-122). Modules under test are imported inside the
test, so a module not yet written fails only the tests that need it.
"""

import datetime

import pytest

from mu_spec.outside import FixedClock

START = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
DOC = {"name": "victim", "entries": {}, "slices": {}, "cuts": []}


class MisnamedCreate:
    """A CreateHandler declaring 'free' and building a record named 'victim'."""

    def __init__(self, check, victim) -> None:
        self.check_order = (("record", check),)
        self.victim = victim

    def name_of(self, call):
        return "free"

    def build(self, call, at):
        from mu_spec.codec import ProjectRecord
        from mu_spec.writer import Built

        return ProjectRecord.from_doc("victim", self.victim.thaw()), Built({}, 200)

    def log_records(self, call, built, at):
        return ()


class MisnamedFork:
    """A ForkHandler forking 'victim' to 'free2' and building a record named 'victim'."""

    def __init__(self, check, victim) -> None:
        self.check_order = (("record", check),)
        self.victim = victim

    def name_of(self, call):
        return "free2"

    def source_of(self, call):
        return "victim"

    def build(self, state, call, at):
        from mu_spec.codec import ProjectRecord
        from mu_spec.writer import Built

        return ProjectRecord.from_doc("victim", self.victim.thaw()), Built({}, 200)

    def log_records(self, call, built, at):
        return ()


def test_name_mismatch(tmp_path):
    from mu_spec.codec import ProjectRecord, record_path
    from mu_spec.registry import NameMismatch, Registry
    from mu_spec.store import Store
    from mu_spec.writer import WriteCall

    from mu_spec.outside import ENV_NAMES, load_config

    D = tmp_path / "store"
    config = load_config({ENV_NAMES["port"]: "9100", ENV_NAMES["mu_spec_root"]: str(D)}, tmp_path)[0]
    assert config.root == D
    store = Store(config)
    store.create_project(ProjectRecord.from_doc("victim", DOC))
    V = record_path(D, "victim").read_bytes()
    registry = Registry(store, FixedClock(START))
    victim = store.read("victim")
    seen = []

    def check(ctx):
        seen.append(ctx.project)

    call = WriteCall("fx", {}, "a", None, False)

    # 1. a create declaring 'free' and building 'victim': NameMismatch, the checks saw 'free'
    with pytest.raises(NameMismatch) as mismatch:
        registry.create(call, MisnamedCreate(check, victim))
    assert mismatch.value.checked == "free"
    assert mismatch.value.built == "victim"
    assert seen == ["free"]

    # 2. the victim is untouched and nothing was created under 'free'
    assert record_path(D, "victim").read_bytes() == V
    assert "free" not in registry.names()
    assert not (D / "free").exists()

    # 3. the same for a fork declaring 'free2'
    seen.clear()
    with pytest.raises(NameMismatch) as mismatch:
        registry.fork(call, MisnamedFork(check, victim))
    assert mismatch.value.checked == "free2"
    assert mismatch.value.built == "victim"
    assert seen == ["free2"]
    assert record_path(D, "victim").read_bytes() == V
    assert "free2" not in registry.names()
