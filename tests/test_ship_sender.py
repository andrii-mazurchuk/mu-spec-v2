"""mu_spec.ship.sender: pacing, retrying and the Ship modules' GitHub holders (T-85, T-279).

The sender is driven through a fake GitHubClient answering from a list of
outcomes and a fake sleep recording every wait; no request leaves the process.
The holders check parses the Ship modules' source with ast and never imports them.
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PATH = "repos/o/r/issues"
BODY = {"title": "t"}


class FakeClient:
    """Answers request() from outcomes in order: a GitHubResponse is returned, an exception raised."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def request(self, method, path, body=None):
        self.requests.append((method, path, body))
        outcome = self.outcomes[len(self.requests) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def run(method, outcomes):
    """Send one request through a Sender over a fake; return (answer or raised exception, sleeps, client)."""
    from mu_spec.ship.sender import Sender

    client = FakeClient(outcomes)
    sleeps = []
    sender = Sender(client, sleeps.append, lambda: 0.0)
    body = None if method == "GET" else BODY
    try:
        result = sender.send(method, PATH, body)
    except Exception as exc:  # noqa: BLE001 - the raised exception is the result under test
        result = exc
    assert client.requests == [(method, PATH, body)] * len(client.requests), "every try sends the same copy"
    return result, sleeps, client


def resp(status, headers=None, n=0):
    from mu_spec.github import GitHubResponse

    return GitHubResponse(status=status, body={"try": n, "status": status}, headers=dict(headers or {}))


def conn_fail(n=0):
    from mu_spec.github import GitHubUnreachable

    return GitHubUnreachable(f"connection refused {n}", False)


def timed_out(n=0):
    from mu_spec.github import GitHubUnreachable

    return GitHubUnreachable("read timed out" if n == 0 else f"read timed out {n}", True)


def assert_answer(result, response):
    from mu_spec.ship.sender import Answer

    assert isinstance(result, Answer), f"expected an Answer, got {result!r}"
    assert result.status == response.status
    assert result.body == response.body
    assert dict(result.headers) == dict(response.headers)


# Each row: method, outcomes (built lazily), index of the outcome returned or raised, sleeps, requests.
TABLE = [
    ("GET", lambda: [resp(200)], 0, [], 1),
    ("POST", lambda: [resp(201)], 0, [1.0], 1),
    ("GET", lambda: [conn_fail(1), conn_fail(2), conn_fail(3), resp(200, n=4)], 3, [1.0, 2.0, 4.0], 4),
    ("POST", lambda: [conn_fail(1), conn_fail(2), conn_fail(3), conn_fail(4)], 3,
     [1.0, 1.0, 1.0, 2.0, 1.0, 4.0, 1.0], 4),
    ("PATCH", lambda: [resp(502, n=1), resp(500, n=2), resp(503, n=3), resp(201, n=4)], 3,
     [1.0, 1.0, 1.0, 2.0, 1.0, 4.0, 1.0], 4),
    ("DELETE", lambda: [conn_fail(1), resp(204, n=2)], 1, [1.0, 1.0, 1.0], 2),
    ("POST", lambda: [resp(429, {"retry-after": "3"}, n=1), resp(201, n=2)], 1, [1.0, 3.0, 1.0], 2),
    ("GET", lambda: [resp(503, {"retry-after": "7"}, n=1), resp(200, n=2)], 1, [7.0], 2),
    ("GET", lambda: [resp(403, {"x-ratelimit-remaining": "0"}, n=1), resp(200, n=2)], 1, [1.0], 2),
    ("GET", lambda: [resp(403, {})], 0, [], 1),
    ("GET", lambda: [resp(503, n=1), resp(503, n=2), resp(503, n=3), resp(503, n=4)], 3, [1.0, 2.0, 4.0], 4),
    ("GET", lambda: [resp(404)], 0, [], 1),
]


@pytest.mark.parametrize(
    ("method", "make", "produced", "sleeps", "requests"),
    TABLE,
    ids=[f"{i}-{row[0]}" for i, row in enumerate(TABLE)],
)
def test_pause_and_retry_table(method, make, produced, sleeps, requests):
    from mu_spec.github import GitHubUnreachable

    outcomes = make()
    result, slept, client = run(method, outcomes)

    assert slept == sleeps
    assert len(client.requests) == requests
    expected = outcomes[produced]
    if isinstance(expected, GitHubUnreachable):
        assert result is expected, f"expected the last GitHubUnreachable raised unchanged, got {result!r}"
        assert result.timed_out is False
    else:
        assert_answer(result, expected)


def module_path(name):
    parts = name.split(".")
    path = REPO.joinpath(*parts).with_suffix(".py")
    if not path.is_file():
        path = REPO.joinpath(*parts, "__init__.py")
    assert path.is_file(), f"{name} has no source file"
    return path


def resolve(module, node):
    """The absolute module an ImportFrom names, relative imports resolved against the importing module."""
    if node.level == 0:
        return node.module or ""
    package = module.split(".")
    if module_path(module).name != "__init__.py":
        package = package[:-1]
    base = package[: len(package) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def names_client(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "GitHubClient":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "GitHubClient":
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name.split(".")[-1] == "GitHubClient" or alias.asname == "GitHubClient":
                    return True
    return False


def github_imports(module, tree):
    """Every name imported from mu_spec.github; a plain import of the module itself counts as '*'."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "mu_spec.github" or alias.name.startswith("mu_spec.github."):
                    names.add("*")
        elif isinstance(node, ast.ImportFrom):
            source = resolve(module, node)
            if source == "mu_spec.github" or source.startswith("mu_spec.github."):
                names.update(alias.name for alias in node.names)
            elif source == "mu_spec":
                names.update("*" for alias in node.names if alias.name == "github")
    return names


def test_timeouts_and_holders():
    # 1. A write that timed out after connecting is sent once and raised unchanged.
    for method in ("POST", "PATCH", "PUT", "DELETE"):
        exc = timed_out()
        result, slept, client = run(method, [exc, resp(201, n=2), resp(201, n=3), resp(201, n=4)])
        assert result is exc, f"{method}: expected the write's timeout raised unchanged, got {result!r}"
        assert result.reason == "read timed out"
        assert result.timed_out is True
        assert len(client.requests) == 1, method
        assert slept == [1.0], method

    # 2. A read that times out is retried like a connection failure.
    answer = resp(200, n=2)
    result, slept, client = run("GET", [timed_out(1), answer])
    assert_answer(result, answer)
    assert len(client.requests) == 2
    assert slept == [1.0]

    outcomes = [timed_out(1), timed_out(2), timed_out(3), timed_out(4)]
    result, slept, client = run("GET", outcomes)
    assert result is outcomes[3], f"expected the 4th timeout raised unchanged, got {result!r}"
    assert len(client.requests) == 4
    assert slept == [1.0, 2.0, 4.0]

    # 3. The Ship modules' holders and namers.
    from mu_spec.ship.sender import CLIENT_NAMERS, SHIP_GITHUB_HOLDERS
    from sweeps.source import GITHUB_HOLDERS

    assert SHIP_GITHUB_HOLDERS == (
        "mu_spec.ship.part", "mu_spec.ship.sender", "mu_spec.ship.plan", "mu_spec.ship.access", "mu_spec.actions",
    )
    for member in SHIP_GITHUB_HOLDERS:
        assert member in GITHUB_HOLDERS, f"{member} is not in GITHUB_HOLDERS"
    assert CLIENT_NAMERS == ("mu_spec.ship.part", "mu_spec.ship.sender")

    # 4. Over the SHIP_GITHUB_HOLDERS members' source only.
    trees = {m: ast.parse(module_path(m).read_text(encoding="utf-8"), filename=m) for m in SHIP_GITHUB_HOLDERS}
    assert {m for m, tree in trees.items() if names_client(tree)} == set(CLIENT_NAMERS)
    for member in ("mu_spec.ship.plan", "mu_spec.ship.access"):
        assert github_imports(member, trees[member]) == {"GitHubUnreachable"}, member
    assert github_imports("mu_spec.actions", trees["mu_spec.actions"]) == set()
