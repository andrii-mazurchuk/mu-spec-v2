"""The diff audit answers from the given paths and the stored declarations alone (T-372).

The record is built by the unit's own declare_module, so this test never writes a layout for module
declarations itself. The answer must not move with the working directory, the files in it or a git
repository around it, and match must touch no disk and start no process. Modules under test are
imported inside the test, so a module not yet written fails only the tests that need it.
"""

import builtins
import json
import os
import re
import shutil
import subprocess

ACTOR = "da"
PROJECT = "da"
MESSAGE = re.compile(r"^msg-\d+$")
SENTINEL = object()


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


def plain(body: object) -> object:
    return json.loads(json.dumps(body))


def working_dirs(tmp_path) -> list:
    """W1 to W4: the files and a link, nothing, the files inside a git repository, other files and link."""
    w1, w2, w3, w4 = (tmp_path / f"w{i}" for i in range(1, 5))
    (w1 / "src").mkdir(parents=True)
    (w1 / "src" / "real.py").write_text("real = 1\n", encoding="utf-8")
    os.symlink("real.py", w1 / "src" / "link.py")
    w2.mkdir()
    shutil.copytree(w1, w3, symlinks=True)
    git = w3 / ".git"
    for sub in ("objects", "refs/heads", "refs/tags"):
        (git / sub).mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = false\n", encoding="utf-8")
    (w4 / "src").mkdir(parents=True)
    (w4 / "src" / "other.py").write_text("other = 1\n", encoding="utf-8")
    os.symlink("other.py", w4 / "src" / "link.py")
    return [w1, w2, w3, w4]


def test_audit_independent_of_disk(tmp_path, monkeypatch):
    from mu_spec.diffaudit import match
    from mu_spec.registry import build_runtime
    from mu_spec.tools.catalogue import dispatch

    monkeypatch.setenv("HOLONIC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MU_SPEC_PORT", "9100")
    monkeypatch.delenv("MU_SPEC_ROOT", raising=False)
    monkeypatch.delenv("MU_SPEC_GITHUB_TOKEN", raising=False)
    rt = build_runtime(ship_builder=lambda client: SENTINEL, github=FakeGitHub())

    outcome = dispatch(rt, "post_request", {"type": "initiate", "title": "diff audit", "project": PROJECT,
                                            "actor": ACTOR})
    assert outcome.status == 200, f"post_request answered {outcome!r}"
    mids = list(dict.fromkeys(s for s in strings(plain(outcome.body)) if MESSAGE.match(s)))
    assert len(mids) == 1, f"post_request answers {mids!r}, not one message id: {outcome!r}"
    for tool, args in (
        ("create_project", {"project": PROJECT, "in_response_to": mids[0]}),
        ("declare_module", {"project": PROJECT, "path": "src/real.py", "implements": []}),
        ("declare_module", {"project": PROJECT, "path": "src/other.py", "implements": []}),
    ):
        outcome = dispatch(rt, tool, {**args, "actor": ACTOR})
        assert outcome.status == 200, f"{tool} {args!r} answered {outcome!r}"

    record = rt.store.read(PROJECT)
    assert match(record, ["src/real.py"]) != match(record, ["not/declared.py"]), (
        "match finds no declaration of src/real.py in the record declare_module wrote")
    assert match(record, ["src/other.py"]) != match(record, ["src/real.py"]), (
        "match answers src/other.py as it answers src/real.py")

    P = ["src/link.py", "src/real.py", "./src/other.py"]
    answers = []
    for w in working_dirs(tmp_path):
        monkeypatch.chdir(w)
        answers.append(match(record, P))

    raised = []

    def forbidden(name):
        def refuse(*args, **kwargs):
            raised.append(name)
            raise AssertionError(f"match called {name}")

        return refuse

    with monkeypatch.context() as m:
        m.setattr(builtins, "open", forbidden("open"))
        m.setattr(os, "stat", forbidden("os.stat"))
        m.setattr(os.path, "exists", forbidden("os.path.exists"))
        m.setattr(os.path, "isfile", forbidden("os.path.isfile"))
        m.setattr(os.path, "realpath", forbidden("os.path.realpath"))
        m.setattr(os.path, "abspath", forbidden("os.path.abspath"))
        m.setattr(os, "getcwd", forbidden("os.getcwd"))
        m.setattr(subprocess, "Popen", forbidden("subprocess.Popen"))
        try:
            answers.append(match(record, P))
        except AssertionError:
            pass
    assert raised == [], f"match touched disk or started a process: {raised!r}"
    assert all(a == answers[0] for a in answers), answers
