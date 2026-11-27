"""mu_spec.dash.read: answer_screen, the one dashboard-only screen read (S-197).

T-147 pins the decision order and the part rule: refusals with their texts and
statuses, the open fallback, filters, and 'unchanged' only when the held parts
cover what is asked; T-169 pins the 188-byte idle answer in its key order, that
names come only with a stale list token, and that every answer for a listed
project carries its run part and no other answer does.
"""

import json


def test_decision_order_and_parts():
    from mu_spec.dash.projection import (
        VIEWS,
        Item,
        Part,
        Projection,
        ProjectList,
        Published,
        ViewProjection,
    )
    from mu_spec.dash.read import answer_screen
    from mu_spec.dash.stamps import canonical, list_token

    T = "b" * 64
    H = Part(b'{"h":1}', ())
    R = Part(b'{"r":1}', ())
    views = {v: ViewProjection(T, H, None, frozenset(), {}) for v in VIEWS}
    views["ship"] = ViewProjection(T, H, R, frozenset(), {})
    views["read"] = ViewProjection(T, Part(b'{"l":"B"}', ()), None, frozenset(), {})
    views["spine"] = ViewProjection(
        T,
        H,
        None,
        frozenset(),
        {
            "B-88 (was B-01)": (
                H,
                Part(b"{}", (Item(frozenset({"B-88 (was B-01)"}), b'"e"'),)),
            )
        },
    )

    pub = Published()
    lt = list_token(["p", "q"])
    pub.publish_list(ProjectList(("p", "q"), lt, canonical(["p", "q"])))
    pub.publish(Projection("p", 1, ("n", 0, None), views))

    def req(**over):
        body = {
            "project": "p",
            "fallback": False,
            "view": "ship",
            "filter": None,
            "part": "whole",
            "held": None,
            "list_token": lt,
        }
        body.update(over)
        return canonical(body)

    def ask(**over):
        a = answer_screen(pub, req(**over))
        return a, json.loads(a.body)

    # 1. the head alone, nothing held: the head, and not complete
    a, j = ask(part="head")
    assert a.status == 200
    assert j["status"] == "parts" and j["complete"] is False
    assert "head" in j and "rest" not in j
    assert j["head"] == {"fields": {"h": 1}, "items": []}
    assert j["token"] == T

    # 2. the whole, the head held at T: only the rest, never 'unchanged'
    a, j = ask(part="whole", held={"token": T, "parts": "head"})
    assert a.status == 200
    assert j["status"] == "parts" and j["complete"] is True
    assert "rest" in j and "head" not in j
    assert j["rest"] == {"fields": {"r": 1}, "items": []}

    # 3. 'unchanged' only when the held parts cover what is asked
    a, j = ask(part="whole", held={"token": T, "parts": "whole"})
    assert a.status == 200
    assert j["status"] == "unchanged" and j["token"] == T
    a, j = ask(part="head", held={"token": T, "parts": "head"})
    assert a.status == 200
    assert j["status"] == "unchanged"
    a, j = ask(part="rest", held={"token": T, "parts": "head"})
    assert j["status"] == "parts" and j["complete"] is True
    assert "rest" in j and "head" not in j

    # 4. a stale held token, or the rest with nothing held: the whole screen
    a, j = ask(part="whole", held={"token": "c" * 64, "parts": "whole"})
    assert j["status"] == "parts" and j["complete"] is True
    assert "head" in j and "rest" in j
    assert j["token"] == T
    a, j = ask(part="rest", held=None)
    assert j["status"] == "parts" and j["complete"] is True
    assert "head" in j and "rest" in j

    # 5. a view with no rest: its whole is its head
    a, j = ask(view="read", part="rest", held=None)
    assert j["status"] == "parts" and j["complete"] is True
    assert "head" in j and "rest" not in j
    assert j["head"] == {"fields": {"l": "B"}, "items": []}

    # 6. a selection answers its filter; an unknown filter is refused with the run part
    a, j = ask(view="spine", filter="B-88 (was B-01)")
    assert a.status == 200
    assert j["status"] == "parts"
    assert j["rest"]["items"] == ["e"]
    a, j = ask(view="spine", filter="B-53 (was B-09)")
    assert a.status == 404
    assert j["status"] == "refused"
    assert j["reason"] == "view spine has no filter B-53 (was B-09)"
    assert "run" in j

    # 7. an unknown view is refused with the run part
    a, j = ask(view="nope")
    assert a.status == 404
    assert j["status"] == "refused"
    assert j["reason"] == "no view named nope"
    assert "run" in j

    # 8. the fallback names the project it answered for, and only when it replaced one
    a, j = ask(project="gone", fallback=True)
    assert a.status == 200
    assert j["project"] == "p" and j["status"] == "parts"
    a, j = ask(project=None, fallback=True)
    assert a.status == 200
    assert j["project"] == "p"
    a, j = ask(project="q", fallback=True)
    assert a.status == 200
    assert "project" not in j
    assert j["status"] == "preparing"

    # 9. a malformed input is refused with the one text, and still carries the list token
    a, j = ask(project=None, fallback=False)
    assert a.status == 400
    assert j == {
        "status": "refused",
        "token": None,
        "reason": "the screen read takes an object with project, fallback, view,"
        " filter, part, held and list_token",
        "list_token": lt,
    }
    a, _ = ask(x=1)
    assert a.status == 400
    a, _ = ask(part="all")
    assert a.status == 400
    a, _ = ask(held={"token": T, "parts": "rest"})
    assert a.status == 400


def test_idle_answer_and_run_part():
    from mu_spec.dash.projection import (
        VIEWS,
        Part,
        Projection,
        ProjectList,
        Published,
        ViewProjection,
    )
    from mu_spec.dash.read import IDLE_ANSWER_MAX, answer_screen
    from mu_spec.dash.stamps import canonical, list_token

    T = "a" * 64

    def views():
        return {
            v: ViewProjection(T, Part(b'{"h":1}', ()), None, frozenset(), {})
            for v in VIEWS
        }

    pub = Published()
    pub.publish_list(
        ProjectList(("p", "q"), list_token(["p", "q"]), canonical(["p", "q"]))
    )
    LT = list_token(["p", "q"])
    pub.publish(Projection("p", 1, ("n", 0, None), views()))

    def ask(project, **over):
        body = {
            "project": project,
            "fallback": False,
            "view": "spine",
            "filter": None,
            "part": "whole",
            "held": {"token": T, "parts": "whole"},
            "list_token": LT,
        }
        body.update(over)
        return canonical(body)

    # 1. the idle answer: exactly these bytes, 22 + 75 + 80 + 11
    a = answer_screen(pub, ask("p"))
    assert a.status == 200
    assert a.body == (
        b'{"status":"unchanged","token":"'
        + T.encode()
        + b'","list_token":"'
        + LT.encode()
        + b'","run":null}'
    )
    assert len(a.body) == 188
    assert len(a.body) <= IDLE_ANSWER_MAX

    # 2. names come with a stale list token, in the declared key order
    a = answer_screen(pub, ask("p", list_token=None))
    j = json.loads(a.body)
    assert list(j) == ["status", "token", "list_token", "names", "run"]
    assert j["names"] == ["p", "q"]

    # 3. a run in progress reaches an 'unchanged' answer, outside the screen token
    pub.publish(
        Projection("p", 2, ("n", 0, None), views(), b'{"id":"n:1","step":"creating"}')
    )
    a = answer_screen(pub, ask("p"))
    j = json.loads(a.body)
    assert a.status == 200
    assert j["status"] == "unchanged"
    assert j["run"] == {"id": "n:1", "step": "creating"}

    # 4. a listed project with nothing published: preparing, with the run part
    a = answer_screen(pub, ask("q"))
    assert a.status == 200
    assert a.body == (
        b'{"status":"preparing","token":null,"list_token":"'
        + LT.encode()
        + b'","run":null}'
    )

    # 5. no run part for a malformed input or an unknown project
    a = answer_screen(pub, b"{}")
    assert a.status == 400
    assert "run" not in json.loads(a.body)
    a = answer_screen(pub, ask("x"))
    j = json.loads(a.body)
    assert a.status == 404
    assert j["reason"] == "no project named x"
    assert "run" not in j

    # 6. an empty store: no_project, never a refusal, names only with a stale list token
    E = list_token([])
    empty = Published()
    empty.publish_list(ProjectList((), E, canonical([])))
    a = answer_screen(empty, ask(None, fallback=True, held=None, list_token=None))
    assert a.status == 200
    j = json.loads(a.body)
    assert list(j.items()) == [
        ("status", "no_project"),
        ("token", None),
        ("list_token", E),
        ("names", []),
    ]
    a = answer_screen(empty, ask(None, fallback=True, held=None, list_token=E))
    assert a.status == 200
    j = json.loads(a.body)
    assert list(j.items()) == [
        ("status", "no_project"),
        ("token", None),
        ("list_token", E),
    ]
