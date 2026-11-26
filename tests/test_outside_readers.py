"""The one clock's test form and the read-only install and prompt readers.

Modules under test are imported inside the test, so a module not yet written fails only the
tests that need it.
"""

import datetime
import pathlib
import shutil

import pytest

# the committed dashboard page, relative to the mu_spec package directory
PAGE = "dashboard.html"


def test_clock_and_readers(tmp_path, monkeypatch):
    import mu_spec
    from mu_spec.outside import FixedClock, InstallAssets, Prompts

    t = datetime.datetime(2026, 11, 25, 12, 0, tzinfo=datetime.timezone.utc)
    second = datetime.timedelta(seconds=1)
    clk = FixedClock(t, second)
    package = pathlib.Path(mu_spec.__file__).resolve().parent
    pkg = tmp_path / "pkg"
    shutil.copytree(package, pkg)
    (tmp_path / "x").write_bytes(b"OUTSIDE")
    (tmp_path / "abs.txt").write_bytes(b"ABS")
    pages = PAGE
    W = tmp_path / "w"
    (W / pages).parent.mkdir(parents=True, exist_ok=True)
    (W / pages).write_bytes(b"WRONG")
    P = tmp_path / "prompts"
    P.mkdir()
    (P / "default.md").write_bytes(b"hello\n")
    (P / "bad.md").write_bytes(b"\xff")
    (tmp_path / "default.md").write_bytes(b"OUTSIDE")

    assert (pkg / ".." / "x").exists()
    assert (P / ".." / "default.md").exists()

    # 1. a stepping clock starts at its start
    assert [clk.now(), clk.now(), clk.now()] == [t, t + second, t + 2 * second]
    mono = FixedClock(t, second)
    assert [mono.monotonic(), mono.monotonic(), mono.monotonic()] == [0.0, 1.0, 2.0]

    # 2. the default step is zero
    still = FixedClock(t)
    assert [still.now(), still.now()] == [t, t]

    # 3. no escape from the install root
    a = InstallAssets(pkg)
    assert a.read("../x") is None
    assert a.read(str(tmp_path / "abs.txt")) is None
    assert a.read("no/such/file") is None

    # 4. read from the package, never the working directory
    committed = (package / pages).read_bytes()
    r = InstallAssets(package)
    monkeypatch.chdir(W)
    first = r.read(pages)
    assert first == committed
    assert first != b"WRONG"
    assert r.read(pages) == first
    assert a.read(pages) == committed

    # 5. prompts: a prompt named without its '.md', no escape, bytes that are not UTF-8 raise
    pr = Prompts(P)
    assert pr.read("default") == "hello\n"
    assert pr.read("../default") is None
    assert pr.read(str(tmp_path / "default")) is None
    assert pr.read("absent") is None
    with pytest.raises(UnicodeDecodeError):
        pr.read("bad")
