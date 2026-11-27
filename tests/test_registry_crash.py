"""A create whose replace fails leaves no project, for the registry or for a fresh Store.

The OSError propagates, a temp copy the crash left is not a project, and a create over the same
name then succeeds (T-124). Modules under test are imported inside the test, so a module not yet
written fails only the tests that need it.
"""

import datetime
import os

import pytest

from mu_spec.outside import FixedClock

START = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
DOC = {"name": "p", "entries": {}, "slices": {}, "cuts": []}


class CopyCreate:
    """A CreateHandler creating 'n' from a copy of source's doc."""

    check_order = ()

    def __init__(self, source) -> None:
        self.source = source

    def name_of(self, call):
        return "n"

    def build(self, call, at):
        from mu_spec.codec import ProjectRecord
        from mu_spec.writer import Built

        return ProjectRecord.from_doc("n", self.source.thaw()), Built({"ok": True}, 200)

    def log_records(self, call, built, at):
        return ()


def test_create_crash_leaves_no_project(tmp_path, monkeypatch):
    from mu_spec.codec import ProjectRecord, record_path, temp_path
    from mu_spec.registry import Registry
    from mu_spec.store import Store
    from mu_spec.writer import WriteCall

    from mu_spec.outside import ENV_NAMES, load_config

    D = tmp_path / "store"
    config = load_config({ENV_NAMES["port"]: "9100", ENV_NAMES["mu_spec_root"]: str(D)}, tmp_path)[0]
    assert config.root == D
    store = Store(config)
    store.create_project(ProjectRecord.from_doc("p", DOC))
    registry = Registry(store, FixedClock(START))
    handler = CopyCreate(store.read("p"))
    call = WriteCall("fx", {}, "a", None, False)

    def failing_replace(src, dst, *args, **kwargs):
        raise OSError("replace failed")

    # 1. the replace fails: the OSError reaches the caller and the registry knows no 'n'
    with monkeypatch.context() as patched:
        patched.setattr(os, "replace", failing_replace)
        with pytest.raises(OSError):
            registry.create(call, handler)
        assert registry.names() == ["p"]
        assert registry.writer("n") is None

    # 2. whatever temp copy is left, a fresh Store lists no 'n'
    temp = temp_path(record_path(D, "n"))
    assert Store(config).project_names() == ["p"]

    # 3. unpatched, the same create is admitted and the temp copy is gone
    outcome = registry.create(call, handler)
    assert outcome.status == 200 and dict(outcome.body) == {"ok": True}
    assert registry.names() == ["n", "p"]
    assert not temp.exists()
    assert registry.writer("n") is not None
