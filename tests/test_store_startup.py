"""Store start-up: listing under the store root, undecodable records, the projects location (T-409).

A plain unit test: no acceptance input, no unit started, no running-unit bytes. Every record is
encoded in process by the rebuilt codec from a literal document. Modules under test are imported
inside the test, so a module not yet written fails only the tests that need it.
"""

import collections
import glob
import os
import pathlib

import pytest


def doc(n):
    return {"name": n, "entries": [{"id": "I-28 (was I-01)", "title": "t-" + n, "tags": ["x"]}], "counters": {"I": 1}}


class Recorder:
    """An InstallObserver recording every call made on it."""

    def __init__(self):
        self.events = []

    def installed(self, project, data, *, notify):
        self.events.append(("installed", project, data, notify))

    def committed(self, project):
        self.events.append(("committed", project))

    def appended(self, record):
        self.events.append(("appended", record.seq))


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def paths_under(d):
    return {p for p in d.rglob("*")}


def same_path(arg, root):
    if isinstance(arg, (str, bytes, os.PathLike)):
        text = os.fsdecode(os.fspath(arg))
        return os.path.normcase(os.path.abspath(text)) == os.path.normcase(os.path.abspath(str(root)))
    return False


def glob_names_root(pattern, root, kwargs):
    if kwargs.get("root_dir") is not None and same_path(kwargs["root_dir"], root):
        return True
    if isinstance(pattern, (str, bytes, os.PathLike)):
        return same_path(os.path.dirname(os.fsdecode(os.fspath(pattern))), root)
    return False


def refuse_listing(monkeypatch, root):
    """Every listing route the files group lets mu_spec.store use raises PermissionError for root."""
    real_listdir, real_scandir, real_walk = os.listdir, os.scandir, os.walk
    real_iterdir, real_pglob = pathlib.Path.iterdir, pathlib.Path.glob
    real_glob, real_iglob = glob.glob, glob.iglob

    def denied():
        return PermissionError(13, "listing refused by the test", str(root))

    def listdir(path="."):
        if same_path(path, root):
            raise denied()
        return real_listdir(path)

    def scandir(path="."):
        if same_path(path, root):
            raise denied()
        return real_scandir(path)

    def walk(top, *args, **kwargs):
        if same_path(top, root):
            raise denied()
        yield from real_walk(top, *args, **kwargs)

    def iterdir(self):
        if same_path(self, root):
            raise denied()
        yield from real_iterdir(self)

    def pglob(self, pattern, *args, **kwargs):
        if same_path(self, root):
            raise denied()
        yield from real_pglob(self, pattern, *args, **kwargs)

    def gglob(pattern, *args, **kwargs):
        if glob_names_root(pattern, root, kwargs):
            raise denied()
        return real_glob(pattern, *args, **kwargs)

    def giglob(pattern, *args, **kwargs):
        if glob_names_root(pattern, root, kwargs):
            raise denied()
        yield from real_iglob(pattern, *args, **kwargs)

    monkeypatch.setattr(os, "listdir", listdir)
    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "walk", walk)
    monkeypatch.setattr(pathlib.Path, "iterdir", iterdir)
    monkeypatch.setattr(pathlib.Path, "glob", pglob)
    monkeypatch.setattr(glob, "glob", gglob)
    monkeypatch.setattr(glob, "iglob", giglob)

    # probe: each patched route refuses the root, as a str and as a path
    for given in (str(root), pathlib.Path(root)):
        probes = {
            "os.listdir": lambda g=given: os.listdir(g),
            "os.scandir": lambda g=given: os.scandir(g),
            "os.walk": lambda g=given: list(os.walk(g)),
            "pathlib.Path.iterdir": lambda g=given: list(pathlib.Path(g).iterdir()),
            "pathlib.Path.glob": lambda g=given: list(pathlib.Path(g).glob("*")),
            "glob.glob": lambda g=given: glob.glob(os.path.join(os.fspath(g), "*")),
            "glob.iglob": lambda g=given: list(glob.iglob(os.path.join(os.fspath(g), "*"))),
        }
        for route, probe in probes.items():
            with pytest.raises(PermissionError):
                probe()
                pytest.fail(f"{route} given {given!r} did not refuse")


def test_startup_listing_and_unreadable(tmp_path, monkeypatch):
    from mu_spec.codec import (
        ProjectRecord,
        RecordUnreadable,
        decode_record,
        encode_record,
        record_path,
        temp_path,
    )
    from mu_spec.store import LocationUnusable, Store

    from mu_spec.outside import load_config

    def cfg(env):
        return load_config(dict(env, MU_SPEC_PORT="9100"), tmp_path)[0]

    G1 = encode_record(ProjectRecord("good1", doc("good1")))
    G2 = encode_record(ProjectRecord("good2", doc("good2")))
    D = tmp_path / "state"
    c = cfg({"HOLONIC_STATE_DIR": str(D)})
    R = c.root
    assert R == D / "projects"
    write(record_path(R, "good1"), G1)
    write(record_path(R, "good2"), G2)
    B = b"{not json"
    write(record_path(R, "bad"), B)
    write(R / "notes.txt", b"notes")
    (R / "empty-dir").mkdir()
    (R / "tmpd").mkdir()
    write(temp_path(record_path(R, "tmpd")), G1)
    write(record_path(D, "stray"), G1)
    obs = Recorder()
    decode_record("good1", G1)

    s = Store(c, observer=obs)

    # 1. only directories holding a manifest under the store root
    assert s.project_names() == ["bad", "good1", "good2"]
    # 2. the undecodable record is remembered, raised on every read, never rewritten
    with pytest.raises(RecordUnreadable):
        s.read("bad")
    with pytest.raises(RecordUnreadable):
        s.read("bad")
    assert record_path(R, "bad").read_bytes() == B
    # 3. the good records
    assert encode_record(s.read("good1")) == G1
    assert encode_record(s.read("good2")) == G2
    # 4. nothing else is a project
    for name in ("notes.txt", "empty-dir", "tmpd", "stray"):
        assert s.read(name) is None, name
    assert s.read_inbox() is None
    assert s.projects_unreadable() is False
    # 5. start-up installs, notify False, nothing else
    assert collections.Counter(obs.events) == collections.Counter(
        [("installed", "good1", G1, False), ("installed", "good2", G2, False)])
    assert len(obs.events) == 2

    # 6. an absent state directory is created with its projects location
    D2 = tmp_path / "absent"
    s2 = Store(cfg({"HOLONIC_STATE_DIR": str(D2)}))
    assert (D2 / "projects").is_dir()
    assert s2.project_names() == []
    assert s2.read_inbox() is None
    assert s2.read_log() == []

    # 7. a state directory, or an override, that is a plain file
    F = tmp_path / "file-state"
    F.write_bytes(b"x")
    c3 = cfg({"HOLONIC_STATE_DIR": str(F)})
    obs3 = Recorder()
    with pytest.raises(LocationUnusable) as caught:
        Store(c3, observer=obs3)
    assert caught.value.location == c3.root
    assert isinstance(caught.value.error, OSError)
    assert obs3.events == []
    assert F.read_bytes() == b"x"
    G = tmp_path / "file-root"
    G.write_bytes(b"x")
    S4 = tmp_path / "s4"
    c4 = cfg({"HOLONIC_STATE_DIR": str(S4), "MU_SPEC_ROOT": str(G)})
    with pytest.raises(LocationUnusable) as caught:
        Store(c4)
    assert caught.value.location == G
    assert isinstance(caught.value.error, OSError)
    assert not S4.exists()

    # 8. MU_SPEC_ROOT given: listed from it, the state directory untouched
    O = tmp_path / "override"
    write(record_path(O, "good1"), G1)
    D5 = tmp_path / "s5"
    write(record_path(D5 / "projects", "good2"), G2)
    before = paths_under(D5)
    s5 = Store(cfg({"HOLONIC_STATE_DIR": str(D5), "MU_SPEC_ROOT": str(O)}))
    assert s5.project_names() == ["good1"]
    assert paths_under(D5) == before

    # 9. a root that cannot be listed
    D6 = tmp_path / "s6"
    write(record_path(D6 / "projects", "good1"), G1)
    c6 = cfg({"HOLONIC_STATE_DIR": str(D6)})
    refuse_listing(monkeypatch, c6.root)
    obs6 = Recorder()
    s6 = Store(c6, observer=obs6)
    assert s6.projects_unreadable() is True
    assert s6.project_names() == []
    assert s6.read("good1") is None
    assert s6.read_inbox() is None
    assert obs6.events == []
