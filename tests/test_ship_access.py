"""mu_spec.ship.access: the pre-Ship access check and learn_login (T-94).

Both are driven through a fake Sender answering from a script in order and
recording every send; no request leaves the process.
"""

import re
from types import SimpleNamespace

USER = {"login": "octo", "type": "User"}


class FakeSender:
    """Answers send() from outcomes in order: an exception is raised, anything else returned as an answer."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.sends = []

    def send(self, method, path, body=None):
        self.sends.append((method, path))
        assert len(self.sends) <= len(self.outcomes), f"unscripted send {method} {path}"
        outcome = self.outcomes[len(self.sends) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def answer(status, body=None):
    return SimpleNamespace(status=status, body=body if body is not None else {}, headers={})


def unreachable(reason):
    from mu_spec.github import GitHubUnreachable

    return GitHubUnreachable(reason, False)


def test_access_check_and_learn_login():
    from mu_spec.ship.access import ACCESS_REASONS, Identity, access_check, learn_login

    senders = []

    def check(repo, token, outcomes):
        sender = FakeSender(outcomes)
        senders.append(sender)
        result, identity = access_check("p", repo, token, sender)
        return result, identity, sender.sends

    def reason_is(result, key):
        template = ACCESS_REASONS[key]
        head = re.split(r"[{%]", template)[0]
        assert result["reason"] == template or (head and result["reason"].startswith(head)), (key, result)

    assert set(ACCESS_REASONS) >= {"no_token", "no_repo", "token_refused", "repo_unseen", "no_push", "unreachable"}

    # 1. No token: the answer alone, nothing sent.
    result, identity, sends = check("o/r", None, [])
    assert result["ok"] is False
    assert result["reason"] == ACCESS_REASONS["no_token"]
    assert identity is None
    assert sends == []

    # 2. A token but no repository: nothing sent.
    result, identity, sends = check(None, "t", [])
    assert result["ok"] is False
    assert result["reason"] == ACCESS_REASONS["no_repo"]
    assert sends == []

    # 3. The token refused: one send, of /user.
    result, identity, sends = check("o/r", "t", [answer(401)])
    assert result["ok"] is False
    assert result["reason"] == ACCESS_REASONS["token_refused"]
    assert identity is None
    assert sends == [("GET", "/user")]

    # 4. /user before /repos; no push permission.
    result, identity, sends = check("o/r", "t", [answer(200, USER), answer(200, {"permissions": {"push": False}})])
    assert sends == [("GET", "/user"), ("GET", "/repos/o/r")]
    assert result["ok"] is False
    assert result["reason"] == ACCESS_REASONS["no_push"]
    assert identity == Identity("octo", "User")

    # 5. Push allowed: ok. Permissions missing: ok, with the note.
    pushed, identity, sends = check("o/r", "t", [answer(200, USER), answer(200, {"permissions": {"push": True}})])
    assert pushed["ok"] is True
    assert identity == Identity("octo", "User")
    assert sends == [("GET", "/user"), ("GET", "/repos/o/r")]
    missing, identity, sends = check("o/r", "t", [answer(200, USER), answer(200, {"name": "r"})])
    assert missing["ok"] is True
    assert missing != pushed, "a missing permissions field is answered with the running unit's note"
    assert sends == [("GET", "/user"), ("GET", "/repos/o/r")]

    # 6. The repository unseen; GitHub unreachable carrying its reason.
    result, identity, sends = check("o/r", "t", [answer(200, USER), answer(404)])
    assert result["ok"] is False
    assert result["reason"] == ACCESS_REASONS["repo_unseen"]
    assert sends == [("GET", "/user"), ("GET", "/repos/o/r")]
    result, identity, sends = check("o/r", "t", [unreachable("x")])
    assert result["ok"] is False
    reason_is(result, "unreachable")
    assert any("x" in str(value) for value in result.values()), result
    assert sends == [("GET", "/user")]

    # 7. Nothing about issues is ever asked.
    for sender in senders:
        assert not any("/issues" in path for _, path in sender.sends), sender.sends

    # 8. learn_login: exactly one GET /user, None on any failure.
    for outcomes, expected in (
        ([answer(200, USER)], Identity("octo", "User")),
        ([answer(401), answer(200, USER)], None),
        ([answer(500), answer(200, USER)], None),
        ([unreachable("x"), answer(200, USER)], None),
    ):
        sender = FakeSender(outcomes)
        assert learn_login(sender) == expected, outcomes[0]
        assert sender.sends == [("GET", "/user")], outcomes[0]
