"""Port text: the rebuilt start against the running unit's, for every spelling of a free high port.

The running unit is the install at MU_SPEC_REFERENCE_INSTALL; the rebuilt unit is this repository.
Each is started from its own install root with the start check's argument list, one at a time.
Modules under test are imported inside the test, so a module not yet written fails only the
tests that need it.
"""

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_VAR = "MU_SPEC_REFERENCE_INSTALL"
DEFAULT_PORT = 9006
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


def free_high_port() -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if 1024 < port < 65536 and port != DEFAULT_PORT:
            return port


def wait_port_free(port: int) -> None:
    deadline = time.monotonic() + STOP_WINDOW_S
    while not port_is_free(port):
        if time.monotonic() > deadline:
            pytest.fail(f"port {port} is not free before a start")
        time.sleep(POLL_S)


def listening_ports(pid: int) -> set[int]:
    """The TCP ports the process pid listens on, read from the system's listening sockets."""
    if sys.platform.startswith("linux"):
        inodes = {}
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                rows = Path(table).read_text().splitlines()[1:]
            except OSError:
                continue
            for row in rows:
                fields = row.split()
                if len(fields) > 9 and fields[3] == "0A":
                    inodes[fields[9]] = int(fields[1].rsplit(":", 1)[1], 16)
        ports = set()
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            return ports
        for fd in fds:
            try:
                link = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if link.startswith("socket:[") and link[8:-1] in inodes:
                ports.add(inodes[link[8:-1]])
        return ports
    if sys.platform == "win32":
        out = ""
        for proto in ("TCP", "TCPv6"):
            out += subprocess.run(["netstat", "-ano", "-p", proto], check=False, capture_output=True, text=True).stdout
        ports = set()
        for row in out.splitlines():
            fields = row.split()
            # a listening row's foreign address has port 0; the state word is localised, the port is not
            if len(fields) == 5 and fields[0].startswith("TCP") and fields[2].endswith(":0") and fields[4] == str(pid):
                ports.add(int(fields[1].rsplit(":", 1)[1]))
        return ports
    out = subprocess.run(
        ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-Fn"],
        check=False, capture_output=True, text=True,
    ).stdout
    return {int(row.rsplit(":", 1)[1]) for row in out.splitlines() if row.startswith("n") and ":" in row}


class Outcome:
    def __init__(self, listened: int | None, stderr_lines: list[str], exit_status: int | None) -> None:
        self.L = listened
        self.E = stderr_lines
        self.X = exit_status

    def __repr__(self) -> str:
        return f"Outcome(L={self.L!r}, E={self.E!r}, X={self.X!r})"


def start_outcome(install_root: str, env: dict[str, str], cwd: Path) -> Outcome:
    """Start one unit from its install root and record L, E and X the same way for both units."""
    from startcheck.runner import BOOTSTRAP

    child_env = {name: os.environ[name] for name in PASS_THROUGH if name in os.environ}
    child_env.update(env)
    proc = subprocess.Popen(
        (sys.executable, "-I", "-c", BOOTSTRAP, install_root),
        cwd=cwd, env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    err = []
    drain = threading.Thread(target=lambda: err.append(proc.stderr.read()))
    drain.start()
    listened = None
    exit_status = None
    deadline = time.monotonic() + START_WINDOW_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            exit_status = proc.returncode
            break
        ports = listening_ports(proc.pid)
        if ports:
            listened = min(ports)
            break
        time.sleep(POLL_S)
    if exit_status is None and proc.poll() is not None and listened is None:
        exit_status = proc.returncode
    if proc.poll() is None:
        if listened is not None:
            proc.terminate()
            try:
                proc.wait(timeout=STOP_WINDOW_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        else:
            proc.kill()
            proc.wait()
    drain.join()
    proc.stderr.close()
    return Outcome(listened, err[0].decode("utf-8", "replace").splitlines(), exit_status)


def port_texts(port: int) -> list[str]:
    p = str(port)
    return [
        "",
        "x",
        " " + p,
        p + " ",
        p[:2] + "_" + p[2:],
        "+" + p,
        "-" + p,
        p + ".0",
        "".join(chr(0x0660 + int(c)) for c in p),
        p,
        "0",
        "65536",
    ]


def failed_start(outcome: Outcome) -> bool:
    return outcome.X is not None and outcome.X != 0


def test_port_texts_match_running_unit(tmp_path):
    from mu_spec.outside import ENV_NAMES, ConfigProblem, load_config, start_refusal

    running_root = reference_install()
    rebuilt_root = str(REPO_ROOT)

    pristine = tmp_path / "pristine"
    (pristine / "projects").mkdir(parents=True)
    state = tmp_path / "state"

    port = free_high_port()
    for t in port_texts(port):
        env = {ENV_NAMES["port"]: t, ENV_NAMES["host"]: "127.0.0.1", ENV_NAMES["state_dir"]: str(state)}

        try:
            config, problems = load_config(dict(env), tmp_path)
        except BaseException as exc:  # noqa: BLE001 -- SystemExit included
            pytest.fail(f"load_config raised {exc!r} for port text {t!r}")

        outcomes = []
        for root in (running_root, rebuilt_root):
            wait_port_free(port)
            shutil.rmtree(state, ignore_errors=True)
            shutil.copytree(pristine, state)
            outcomes.append(start_outcome(root, env, tmp_path))
        running, rebuilt = outcomes
        seen = f"t={t!r} running={running!r} rebuilt={rebuilt!r} config.port={config.port!r} problems={problems!r}"

        # 2. the outcomes agree
        assert (running.L is None) == (rebuilt.L is None), seen
        if running.L is not None and rebuilt.L is not None:
            if t == "0":
                assert running.L > 0 and rebuilt.L > 0, seen
            else:
                assert rebuilt.L == running.L, seen
        if running.L is None and rebuilt.L is None:
            assert failed_start(running) and failed_start(rebuilt), seen

        # 3. the outcome agrees with the loader
        if config.port is None:
            assert len(problems) == 1, seen
            assert isinstance(problems[0], ConfigProblem), seen
            assert problems[0].field == "port", seen
            assert rebuilt.L is None, seen
            assert rebuilt.E == [start_refusal(problems)] == running.E, seen
            assert failed_start(rebuilt), seen
        else:
            assert problems == (), seen
            assert start_refusal(problems) is None, seen
            if rebuilt.L is not None:
                if t == "0":
                    assert rebuilt.L > 0, seen
                else:
                    assert rebuilt.L == config.port, seen
            else:
                assert running.L is None, seen
                assert failed_start(rebuilt), seen

        # 4. never the default port, never a port other than the one t names
        assert rebuilt.L != DEFAULT_PORT, seen
        if rebuilt.L is not None and t != "0":
            assert rebuilt.L == port, seen
