"""The start sequence, mu_spec.start.main: configure, the startup line, range, assemble, bind, serve.

Over a fake Composition each step's failure is one stderr line and status 1 with nothing after it,
and the root text is copied into the startup line unchanged (T-285). As a process, empty values and
a spaced port listen, and a state directory that is a file is refused before the startup line
(T-284); every expected root text there comes from the outside entry's loader, never spelled here.
Modules under test are imported inside the test, so a module not yet written fails only the tests
that need it.
"""

import dataclasses
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

HOST = "127.0.0.1"
START_WINDOW_S = 15.0
POLL_S = 0.1
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
# what the interpreter itself needs to run on the host
PASS_THROUGH = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def connect_refused(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=5.0):
            return False
    except ConnectionRefusedError:
        return True


@dataclasses.dataclass(frozen=True)
class Cfg:
    host: str
    port: int
    root_text: str


class Fake:
    """A Composition recording its calls; configure and assemble answer as the case gives."""

    def __init__(self, configured=None, configure_raises=None, assemble_raises=None):
        self.calls = []
        self.probe = None
        self._configured = configured
        self._configure_raises = configure_raises
        self._assemble_raises = assemble_raises

    def configure(self):
        self.calls.append("configure")
        if self._configure_raises is not None:
            raise self._configure_raises
        return self._configured

    def assemble(self):
        self.calls.append("assemble")
        port = self._configured.port
        if port != 0:
            self.probe = connect_refused(port)
        if self._assemble_raises is not None:
            raise self._assemble_raises

    def serve(self, listener):
        self.calls.append(("serve", listener.getsockname()[1]))
        listener.close()


def test_start_sequence_order(capsys):
    from mu_spec.declarations import RegistryError
    from mu_spec.registry import StartRefused
    from mu_spec.start import START_FAIL, main

    import mu_spec.outside

    p = free_port()
    rt = str(pathlib.Path("state") / "projects")

    # 1. healthy: configure, assemble with nothing listening yet, then serve on the port
    fake = Fake(Cfg(HOST, p, rt))
    main(fake)
    out, err = capsys.readouterr()
    assert fake.calls == ["configure", "assemble", ("serve", p)]
    assert fake.probe is True
    assert out == f"mu-spec serving on http://{HOST}:{p} (root: {rt})\n"
    assert err == ""

    # 2. configure refused: its line alone, nothing on stdout, nothing after it
    fake = Fake(configure_raises=StartRefused("L"))
    rc = main(fake)
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""
    assert err == "L\n"
    assert fake.calls == ["configure"]

    # 3. out of range: after the startup line, before assemble
    for bad in (70000, -1):
        fake = Fake(Cfg(HOST, bad, rt))
        rc = main(fake)
        out, err = capsys.readouterr()
        assert rc == 1
        assert out == f"mu-spec serving on http://{HOST}:{bad} (root: {rt})\n"
        assert err == f"mu-spec cannot start: MU_SPEC_PORT={bad}: out of range 0-65535\n"
        assert fake.calls == ["configure"]

    # 4. the start-up log unreadable, with an empty host printed empty
    fake = Fake(Cfg("", p, "projects"), assemble_raises=OSError("x"))
    rc = main(fake)
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == f"mu-spec serving on http://:{p} (root: projects)\n"
    assert err == "mu-spec cannot start: lifecycle log unreadable: x\n"
    assert fake.calls == ["configure", "assemble"]

    # 5. a package fault from assemble is never a configuration outcome
    fake = Fake(Cfg(HOST, p, rt), assemble_raises=RegistryError("r"))
    with pytest.raises(RegistryError):
        main(fake)
    out, err = capsys.readouterr()
    assert "lifecycle log unreadable" not in err

    # 6. the port held by another listener: one bind line, no serve
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as h:
        if sys.platform == "win32":
            h.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        h.bind((HOST, p))
        h.listen()
        fake = Fake(Cfg(HOST, p, rt))
        rc = main(fake)
        out, err = capsys.readouterr()
    assert rc == 1
    assert fake.calls == ["configure", "assemble"]
    assert err.count("\n") == 1 and err.endswith("\n")
    assert err.startswith(f"mu-spec cannot bind {HOST}:{p}: ")

    # 7. port 0 is in range and listens wherever the OS chooses
    fake = Fake(Cfg(HOST, 0, rt))
    main(fake)
    out, err = capsys.readouterr()
    assert out == f"mu-spec serving on http://{HOST}:0 (root: {rt})\n"
    assert err == ""
    assert fake.calls[:2] == ["configure", "assemble"]
    assert len(fake.calls) == 3
    tag, q = fake.calls[2]
    assert tag == "serve"
    assert isinstance(q, int) and q > 0

    # 8. the root text copied unchanged, separators mixed on purpose
    fake = Fake(Cfg(HOST, p, "a/b\\c"))
    main(fake)
    out, err = capsys.readouterr()
    assert out == f"mu-spec serving on http://{HOST}:{p} (root: a/b\\c)\n"

    # 9. the one refusal line, re-exported
    assert START_FAIL is mu_spec.outside.START_FAIL_LINE


def _base_env() -> dict[str, str]:
    base = {name: os.environ[name] for name in PASS_THROUGH if name in os.environ}
    base["PYTHONPATH"] = str(REPO_ROOT)
    return base


def _first_line(proc: subprocess.Popen, window_s: float) -> bytes | None:
    got: list[bytes] = []
    reader = threading.Thread(target=lambda: got.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(window_s)
    return got[0] if got else None


def test_start_as_process(tmp_path):
    from mu_spec.outside import load_config

    p = free_port()
    work = tmp_path / "w"
    work.mkdir()
    (work / "notes.txt").write_bytes(b"not a directory\n")
    base = _base_env()

    def rt(env):
        return load_config(env, work)[0].root_text

    def start(env):
        return subprocess.Popen(
            [sys.executable, "-m", "mu_spec"], cwd=work, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # 1. empty values and a spaced port listen, the startup line copying the loader's root text
    env1 = base | {"MU_SPEC_PORT": f" {p} ", "MU_SPEC_HOST": "", "HOLONIC_STATE_DIR": "",
                   "HOLONIC_GIT_SHA": "", "MU_SPEC_GITHUB_TOKEN": "", "MU_SPEC_PROMPTS_DIR": ""}
    proc = start(env1)
    try:
        line = _first_line(proc, START_WINDOW_S)
        assert line, f"no startup line within {START_WINDOW_S}s (exit {proc.poll()})"
        text = line.decode("utf-8")
        text = text.removesuffix("\n").removesuffix("\r")
        assert text == f"mu-spec serving on http://:{p} (root: {rt(env1)})"

        deadline = time.monotonic() + START_WINDOW_S
        while True:
            try:
                with urllib.request.urlopen(f"http://{HOST}:{p}/health", timeout=5.0) as resp:
                    status, body = resp.status, resp.read()
                break
            except OSError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise
                time.sleep(POLL_S)
        assert status == 200
        assert body == b'{"status": "ok"}'
        assert (work / "projects").is_dir()
        assert proc.poll() is None
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=START_WINDOW_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()

    # 2. a state directory that is a file: one refusal line naming the loader's root text, no startup line
    env2 = base | {"MU_SPEC_PORT": str(p), "MU_SPEC_HOST": HOST, "HOLONIC_STATE_DIR": "notes.txt"}
    proc = start(env2)
    try:
        stdout, stderr = proc.communicate(timeout=START_WINDOW_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail(f"the refused start did not exit within {START_WINDOW_S}s")
    assert proc.returncode == 1
    assert stdout == b""
    assert stderr.count(b"\n") == 1 and stderr.endswith(b"\n")
    prefix = ("mu-spec cannot start: HOLONIC_STATE_DIR=" + repr(rt(env2)) + ": ").encode("utf-8")
    assert stderr.startswith(prefix), stderr
    assert connect_refused(p)
