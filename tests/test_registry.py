"""Tool discovery, declared actions and dispatch lookup match the running unit.

Discovery and the action declaration equal the running unit's GET /tools and GET /actions bodies
as serialised text, key order included (T-03); find_tool prefers a literal segment over a
parameter whatever the listing order and never matches an action path, and bind_handlers accepts
exactly the tool names (T-04). The fixtures are the running unit's bodies committed as received.
Modules under test are imported inside the test, so a module not yet written fails only the tests
that need it.
"""

import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

ACTOR_TAIL = " Send `actor` (your session or agent name): every write is logged under it."


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_discovery_matches_running_unit():
    from mu_spec.declarations import (
        ACTIONS,
        TOOLS,
        actions_body,
        check_registries,
        discovery_body,
    )

    check_registries()

    running_tools = json.loads(fixture_text("running_tools.json"))
    running_actions = json.loads(fixture_text("running_actions.json"))
    assert json.dumps(discovery_body()) == json.dumps(running_tools)
    assert json.dumps(actions_body()) == json.dumps(running_actions)

    names = [t.name for t in TOOLS]
    assert len(TOOLS) == 41
    assert names == [t["name"] for t in running_tools["tools"]]
    assert names[0] == "post_request"
    assert names[-1] == "get_doc"
    assert names[18] == "get_work_unit"
    assert names[20] == "list_cuts"

    assert [a.name for a in ACTIONS] == [
        "cut_units",
        "ratify",
        "reject",
        "set_repo",
        "set_labels",
        "emit_tickets",
        "rollback_tickets",
        "set_delegation",
    ]

    for t in TOOLS:
        assert list(t.input_schema) == ["type", "properties", "required"], t.name
        if t.method == "POST":
            assert list(t.input_schema["properties"])[-1] == "actor", t.name
            assert t.description.endswith(ACTOR_TAIL), t.name
    (list_projects,) = [t for t in TOOLS if t.name == "list_projects"]
    assert list_projects.input_schema["required"] == []
    assert list_projects.input_schema["properties"] == {}

    assert not {a.name for a in ACTIONS} & set(names)


def test_find_tool_precedence():
    from mu_spec.declarations import (
        TOOLS,
        RegistryError,
        bind_handlers,
        find_action,
        find_tool,
    )

    def found(hit):
        return None if hit is None else (hit[0].name, hit[1])

    assert found(find_tool("GET", "/projects/p/units/cuts")) == ("list_cuts", {"project": "p"})
    assert found(find_tool("GET", "/projects/p/units/E-1")) == (
        "get_work_unit",
        {"project": "p", "entry": "E-1"},
    )
    assert found(find_tool("GET", "/projects/p/units")) == ("get_units", {"project": "p"})
    assert find_tool("GET", "/projects/p/slicing/proposal")[0].name == "get_proposal"
    assert find_tool("POST", "/projects/p/slicing/proposal")[0].name == "submit_proposal"
    assert found(find_tool("GET", "/projects")) == ("list_projects", {})
    assert find_tool("GET", "/docs/a/b") is None
    assert find_tool("GET", "/projects//units") is None
    assert find_tool("POST", "/projects/p/units/cut") is None

    assert found(find_action("POST", "/projects/p/units/cut")) == ("cut_units", {"project": "p"})

    def f():
        return None

    tool_names = [t.name for t in TOOLS]
    with pytest.raises(RegistryError):
        bind_handlers({n: f for n in tool_names[:-1]})
    with pytest.raises(RegistryError):
        bind_handlers({**{n: f for n in tool_names}, "not_a_tool": f})
    exact = {n: f for n in tool_names}
    assert bind_handlers(exact) == exact
