"""The unit-contract endpoints, mu_spec.contract: bodies, refusals and the closed answers.

Modules under test are imported inside the test, so a module not yet written fails only the
tests that need it.
"""

import json

import pytest


def test_bodies_and_time():
    from mu_spec.contract import answer
    from mu_spec.declarations import actions_body, discovery_body

    p_calls = []
    c_calls = []

    class FakePrompts:
        def read(self, name):
            p_calls.append(name)
            return "a\r\nb\u00e9" if name == "default.md" else None

    def fake_counts():
        c_calls.append(1)
        return {"b": [2], "a": 1}

    def A(m, path, now=123.5):
        return answer(FakePrompts(), fake_counts, m, path, now)

    def clear():
        p_calls.clear()
        c_calls.clear()

    clear()
    r = A("GET", "/health")
    assert r.status == 200
    assert r.content_type == "application/json"
    assert r.body == b'{"status": "ok"}'
    assert p_calls == [] and c_calls == []

    for path in ("/stats", "/stats/structure"):
        clear()
        r = A("GET", path)
        assert r.status == 200
        assert r.body == b'{"unit": "mu-spec", "computed_at": 123.5, "metrics": {"b": [2], "a": 1}}'
        assert len(c_calls) == 1

    clear()
    r = A("GET", "/prompts/default")
    assert r.status == 200
    assert r.content_type == "text/plain; charset=utf-8"
    assert r.body == b"a\r\nb\xc3\xa9"
    assert p_calls == ["default.md"]

    clear()
    r = A("GET", "/prompts/reference")
    assert r.status == 404
    assert r.body == b'{"error": "no prompt for tier \'reference\'"}'

    clear()
    r = A("GET", "/prompts/nope")
    assert r.status == 404
    assert json.loads(r.body) == {"error": "no prompt for tier 'nope'"}
    assert p_calls == []

    clear()
    r = A("GET", "/prompts/it's")
    assert r.status == 404
    assert r.body == b'{"error": "no prompt for tier \\"it\'s\\""}'
    assert p_calls == []

    clear()
    r = A("GET", "/tools")
    assert r.status == 200
    assert r.body == json.dumps(discovery_body()).encode()
    r = A("GET", "/actions")
    assert r.status == 200
    assert r.body == json.dumps(actions_body()).encode()

    r = A("GET", "/config")
    assert r.status == 200
    items = json.loads(r.body)["config"]
    assert [i["name"] for i in items] == [
        "MU_SPEC_GITHUB_TOKEN",
        "MU_SPEC_PORT",
        "MU_SPEC_HOST",
        "HOLONIC_STATE_DIR",
        "MU_SPEC_ROOT",
        "MU_SPEC_PROMPTS_DIR",
    ]
    assert [i["default"] for i in items] == [None, "9006", "127.0.0.1", "state", None, "prompts"]
    assert items[0]["required"] is True and items[0]["secret"] is True
    for i in items:
        assert list(i) == ["name", "description", "required", "secret", "default"]
    assert b"HOLONIC_GIT_SHA" not in r.body

    r = A("GET", "/skills")
    assert r.status == 200
    skills = json.loads(r.body)
    assert [s["name"] for s in skills["skills"]] == [
        "wayfinder",
        "grilling",
        "domain-modeling",
        "research",
        "prototype",
    ]
    assert all(s["shipped_by_this_unit"] is False for s in skills["skills"])
    assert skills["prompt"] == "/prompts/wayfinding"

    assert p_calls == [] and c_calls == []

    assert A("GET", "/healthz", 0) is None


def test_method_and_closed_answers():
    from mu_spec.contract import CountsUnavailable, answer

    def unavailable():
        raise CountsUnavailable()

    try:
        b"\xff".decode("utf-8")
    except UnicodeDecodeError as e:
        undecodable = e

    class BadBytes:
        def read(self, name):
            raise undecodable

    class Broken:
        def read(self, name):
            raise ValueError("x")

    class Missing:
        def read(self, name):
            return None

    def ok_counts():
        return {}

    for m, path in (("POST", "/health"), ("POST", "/prompts/default"), ("DELETE", "/tools")):
        r = answer(Missing(), ok_counts, m, path, 0)
        assert r.status == 405
        assert r.body == b'{"error": "method not allowed"}'

    for path in ("/stats", "/stats/structure"):
        assert answer(Missing(), unavailable, "GET", path, 0).status is None

    r = answer(Missing(), unavailable, "GET", "/health", 0)
    assert r.status == 200
    assert r.body == b'{"status": "ok"}'
    assert answer(Missing(), unavailable, "GET", "/config", 0).status == 200
    assert answer(Missing(), unavailable, "GET", "/tools", 0).status == 200

    assert answer(BadBytes(), ok_counts, "GET", "/prompts/default", 0).status is None

    r = answer(Missing(), ok_counts, "GET", "/prompts/default", 0)
    assert r.status == 404
    assert r.body == b'{"error": "no prompt for tier \'default\'"}'

    with pytest.raises(ValueError) as info:
        answer(Broken(), ok_counts, "GET", "/prompts/default", 0)
    assert str(info.value) == "x"
    assert not isinstance(info.value, UnicodeDecodeError)

    assert answer(Missing(), ok_counts, "POST", "/no-such", 0) is None
