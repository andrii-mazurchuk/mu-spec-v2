"""Configuration from outside: absent and empty variables, relative directories, the token, capture once.

The running unit is the install at MU_SPEC_REFERENCE_INSTALL; the rebuilt unit is this repository.
Modules under test are imported inside the test, so a module not yet written fails only the
tests that need it.
"""

import dataclasses
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
REFERENCE_VAR = "MU_SPEC_REFERENCE_INSTALL"
START_WINDOW_S = 10.0
STOP_WINDOW_S = 15.0
POLL_S = 0.1
# what the interpreter itself needs to run on the host; the same for both units
PASS_THROUGH = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")


def reference_install() -> str:
    root = os.environ.get(REFERENCE_VAR)
    if not root:
        pytest.fail(f"{REFERENCE_VAR} is not set: it must name the running unit's install root")
    return root


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port_free(port: int) -> None:
    deadline = time.monotonic() + STOP_WINDOW_S
    while not port_is_free(port):
        if time.monotonic() > deadline:
            pytest.fail(f"port {port} is not free before a start")
        time.sleep(POLL_S)


def empty_folder(folder: pathlib.Path) -> None:
    for child in folder.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def stats_store_root(install_root: str, env: dict[str, str], cwd: pathlib.Path, port: int):
    """Start one unit from its install root in cwd, read GET /stats' metrics.store_root, stop it."""
    from startcheck.runner import BOOTSTRAP

    child_env = {name: os.environ[name] for name in PASS_THROUGH if name in os.environ}
    child_env.update(env)
    proc = subprocess.Popen(
        (sys.executable, "-I", "-c", BOOTSTRAP, install_root),
        cwd=cwd, env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + START_WINDOW_S
        while True:
            if proc.poll() is not None:
                err = proc.stderr.read().decode("utf-8", "replace")
                pytest.fail(f"{install_root} exited {proc.returncode} before answering /stats: {err}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/stats", timeout=2) as answer:
                    body = json.loads(answer.read())
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                if time.monotonic() > deadline:
                    pytest.fail(f"{install_root} did not answer /stats within {START_WINDOW_S} s")
                time.sleep(POLL_S)
        return body["metrics"]["store_root"]
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=STOP_WINDOW_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        proc.stderr.close()


def test_absent_empty_relative_and_token(tmp_path, monkeypatch):
    from mu_spec.outside import EMPTY_DIR, ENV_NAMES, PROJECTS_SUBDIR, load_config

    c = tmp_path / "cwd"
    c.mkdir()
    p0 = "9100"
    N = ENV_NAMES

    def rt(t):
        return str(pathlib.Path(t) / PROJECTS_SUBDIR)

    # 1. only the port given: every other value its absent default
    cfg = load_config({N["port"]: p0}, c)[0]
    assert cfg.state_dir == c / "state"
    assert cfg.root == c / "state" / "projects"
    assert cfg.root_text == rt("state")
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 9100
    assert cfg.prompts_dir == c / "prompts"
    assert cfg.git_sha is None
    assert cfg.github_token is None
    assert cfg.mu_spec_root is None

    # 2. every variable empty except the port: today's empty-value meanings
    empty = {name: "" for name in N.values()}
    empty[N["port"]] = p0
    cfg = load_config(empty, c, "abc123")[0]
    assert cfg.host == ""
    assert cfg.state_dir == c
    assert cfg.root == c / "projects"
    assert cfg.root_text == rt(EMPTY_DIR)
    assert cfg.prompts_dir == c
    assert cfg.git_sha == "abc123"
    assert cfg.github_token is None
    assert cfg.mu_spec_root is None

    # 3. relative state texts join under cwd; the root text is the state text's native path
    cfg = load_config({N["port"]: p0, N["state_dir"]: "rel"}, c)[0]
    assert cfg.state_dir == c / "rel"
    assert cfg.root == c / "rel" / "projects"
    assert cfg.root_text == rt("rel")
    assert load_config({N["port"]: p0, N["state_dir"]: "notes.txt"}, c)[0].root_text == rt("notes.txt")
    assert load_config({N["port"]: p0, N["state_dir"]: "a/b"}, c)[0].root_text == rt("a/b")
    A = tmp_path / "absolute-state"
    cfg = load_config({N["port"]: p0, N["state_dir"]: str(A)}, c)[0]
    assert cfg.state_dir == A
    assert cfg.root_text == rt(str(A))
    cfg = load_config({N["port"]: p0, N["mu_spec_root"]: "r"}, c)[0]
    assert cfg.root == c / "r"
    assert cfg.root_text == "r"
    assert cfg.mu_spec_root == "r"

    # 4. token and sha trimming
    assert load_config({N["port"]: p0, N["github_token"]: "  t  "}, c)[0].github_token == "t"
    assert load_config({N["port"]: p0, N["github_token"]: "   "}, c)[0].github_token is None
    assert load_config({N["port"]: p0, N["git_sha"]: " abc "}, c, "abc123")[0].git_sha == "abc"
    assert load_config({N["port"]: p0, N["git_sha"]: "  "}, c, "abc123")[0].git_sha == ""

    # 5. captured once: later changes to the mapping or the process environment change nothing
    env = {N["port"]: p0, N["github_token"]: "secret-T0K3N"}
    cfg = load_config(env, c)[0]
    before = cfg.state_dir
    env[N["state_dir"]] = "/elsewhere"
    monkeypatch.setenv(N["state_dir"], "/elsewhere")
    assert cfg.state_dir == before

    # 6. the token is never shown
    assert "secret-T0K3N" not in repr(cfg)
    assert "secret-T0K3N" not in repr(cfg.raw)
    bad = load_config({N["port"]: "x", N["github_token"]: "secret-T0K3N"}, c)[1]
    assert bad
    for problem in bad:
        for field in dataclasses.fields(problem):
            assert "secret-T0K3N" not in str(getattr(problem, field.name)), field.name

    # 7. the running and rebuilt units, started in c, report the same store_root in their counts
    port = int(p0) if port_is_free(int(p0)) else free_port()
    step1 = {N["port"]: str(port)}
    step2 = {name: "" for name in N.values()}
    step2[N["port"]] = str(port)
    running_root = reference_install()
    rebuilt_root = str(REPO_ROOT)
    for env in (step1, step2):
        roots = []
        for root in (running_root, rebuilt_root):
            wait_port_free(port)
            empty_folder(c)
            roots.append(stats_store_root(root, env, c, port))
        assert roots[1] == roots[0], env
