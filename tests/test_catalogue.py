"""mu_spec.tools.catalogue: the one binding of the 41 tools, kept apart from the dashboard (S-408).

T-379 pins that TOOLS mirrors DECLS by position and identity with a callable handler each, that
list_tools and discovery_body are shaped from DECLS alone, that importing the dashboard route modules
changes neither, that no dashboard path or name reaches find_tool, TOOLS or discovery, that dispatch
answers a dashboard path or an unknown name as not found, and that the catalogue's source imports no
dashboard route module. A plain in-process test: no running unit, no encoder. Modules under test are
imported inside the test, so a module not yet written fails only the tests that need it.
"""

import ast
import importlib
import importlib.util
import json
import re
from collections.abc import Mapping
from pathlib import Path

EIGHT = ["ratify", "reject", "cut_units", "set_repo", "set_labels", "set_delegation", "emit_tickets",
         "rollback_tickets"]
ROUTE_MODULES = ("mu_spec.declarations", "mu_spec.dash.actionroute", "mu_spec.dash.read", "mu_spec.dash.progress")
ENTRY_KEYS = {"name", "description", "method", "path", "input_schema"}
CATALOGUE = "mu_spec.tools.catalogue"
CATALOGUE_PACKAGE = "mu_spec.tools"


def plain(value: object) -> object:
    """A frozen value as plain dicts and lists."""
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def filled(path: str) -> str:
    """A path pattern with each '{x}' parameter filled with 'p'."""
    return re.sub(r"\{[^}]*\}", "p", path)


def resolved(node: ast.ImportFrom) -> str:
    """The absolute module an ImportFrom in the catalogue names, its relative level resolved."""
    if node.level == 0:
        return node.module or ""
    parts = CATALOGUE_PACKAGE.split(".")
    base = parts[: len(parts) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def is_route_module(name: str) -> bool:
    return any(name == m or name.startswith(m + ".") for m in ROUTE_MODULES)


def imported_route_modules(source: str) -> list[str]:
    """Every dashboard route module (or submodule) the source imports, by any import form."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if is_route_module(a.name)]
        elif isinstance(node, ast.ImportFrom):
            module = resolved(node)
            if is_route_module(module):
                found.append(module)
            found += [f"{module}.{a.name}" for a in node.names if is_route_module(f"{module}.{a.name}")]
    return found


def test_list_tools_and_dashboard_separation():
    from mu_spec.dash.progress import PROGRESS_METHOD, PROGRESS_PATH
    from mu_spec.dash.read import SCREEN_METHOD, SCREEN_PATH
    from mu_spec.declarations import ACTIONS
    from mu_spec.tools.catalogue import (
        DASH_ROUTE_MODULES,
        TOOLS,
        discovery_body,
        dispatch,
        find_tool,
        list_tools,
    )
    from mu_spec.tools.schemas import DECLS

    # 1. 41 tools mirroring DECLS by position and identity; every handler callable
    assert len(DECLS) == 41
    assert len(TOOLS) == 41
    assert [d.schema.name for d in TOOLS] == [s.name for s in DECLS]
    for d, s in zip(TOOLS, DECLS, strict=True):
        assert d.schema is s, d.schema.name
        assert callable(d.handler), d.schema.name

    # 2. the discovery entries are the DECLS fields, position by position
    l0 = plain(list_tools())
    assert len(l0) == 41
    for entry, s in zip(l0, DECLS, strict=True):
        assert set(entry) == ENTRY_KEYS, entry
        assert entry["name"] == s.name
        assert entry["description"] == s.description, s.name
        assert entry["method"] == s.method, s.name
        assert entry["path"] == s.path, s.name
        assert entry["input_schema"] == plain(s.input_schema), s.name
    assert plain(discovery_body()) == {"unit": "mu-spec", "tools": l0}

    # 3. importing every dashboard route module changes nothing
    assert tuple(DASH_ROUTE_MODULES) == ROUTE_MODULES
    for name in DASH_ROUTE_MODULES:
        importlib.import_module(name)
    assert plain(list_tools()) == l0
    assert plain(discovery_body()) == {"unit": "mu-spec", "tools": l0}
    assert len(TOOLS) == 41

    # 4. no dashboard path or name reaches find_tool, TOOLS or discovery
    assert (SCREEN_METHOD, SCREEN_PATH) == ("POST", "/dash/screen")
    assert (PROGRESS_METHOD, PROGRESS_PATH) == ("POST", "/dash/progress")
    assert find_tool("POST", "/dash/screen") is None
    assert find_tool("POST", "/dash/progress") is None
    for spec in ACTIONS:
        assert find_tool(spec.method, filled(spec.path)) is None, spec.name
    tool_names = {d.schema.name for d in TOOLS}
    assert not {spec.name for spec in ACTIONS} & tool_names
    assert not set(EIGHT) & tool_names
    for d in TOOLS:
        shape = json.dumps(plain(d.schema.input_schema))
        for text in (d.schema.name, d.schema.description, shape):
            assert "dash/screen" not in text, d.schema.name
            assert "dash/progress" not in text, d.schema.name

    # 5. a dashboard path or an unknown name is answered as not found
    for name in ("dash/screen", "/dash/screen", "/dash/progress", "no_such_tool"):
        outcome = dispatch(object(), name, {})
        assert outcome.status == 404, name
        assert plain(outcome.body) == {"error": "not found"}, name

    # 6. the catalogue's source imports no dashboard route module
    spec = importlib.util.find_spec(CATALOGUE)
    assert spec is not None and spec.origin
    source = Path(spec.origin).read_text("utf-8")
    assert imported_route_modules(source) == [], imported_route_modules(source)
