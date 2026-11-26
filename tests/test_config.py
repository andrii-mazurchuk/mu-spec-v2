"""mu_spec.config: the configuration names as views of the outside entry's one table.

Modules under test are imported inside the test, so a module not yet written fails only the
tests that need it.
"""

import ast
import importlib
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_SOURCE = REPO_ROOT / "mu_spec" / "config.py"
ALLOWED_LITERALS = {
    "state_dir",
    "host",
    "port",
    "prompts_dir",
    "git_sha",
    "github_token",
    "mu_spec_root",
}
ALLOWED_IMPORTS = {"mu_spec.outside", "__future__"}


def docstring_nodes(tree: ast.AST) -> set[int]:
    """The ids of the constants that are a module, class or function docstring."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def imported_modules(tree: ast.AST) -> set[str]:
    """Every module the source imports, relative imports resolved inside the mu_spec package."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = ["mu_spec"][: max(0, 2 - node.level)]
                names.add(".".join(package + ([node.module] if node.module else [])))
            else:
                names.add(node.module or "")
    return names


def test_config_names_derived():
    import mu_spec.config as cfg
    import mu_spec.outside as out

    assert cfg.VALUE_FIELDS == ("state_dir", "host", "port", "prompts_dir", "git_sha", "github_token")
    assert len(cfg.VALUE_FIELDS) == 6
    assert set(cfg.VALUE_FIELDS) | {"mu_spec_root"} == set(out.ENV_NAMES)

    assert isinstance(cfg.VALUE_NAMES, tuple)
    assert cfg.VALUE_NAMES == (
        "HOLONIC_STATE_DIR",
        "MU_SPEC_HOST",
        "MU_SPEC_PORT",
        "MU_SPEC_PROMPTS_DIR",
        "HOLONIC_GIT_SHA",
        "MU_SPEC_GITHUB_TOKEN",
    )

    assert cfg.ROOT_NAME == "MU_SPEC_ROOT"
    assert cfg.ROOT_NAME not in cfg.VALUE_NAMES
    assert cfg.ALL_NAMES == cfg.VALUE_NAMES + ("MU_SPEC_ROOT",)

    assert list(cfg.MISSING_DEFAULTS.items()) == [
        ("HOLONIC_STATE_DIR", "state"),
        ("MU_SPEC_HOST", "127.0.0.1"),
        ("MU_SPEC_PORT", 9006),
        ("MU_SPEC_PROMPTS_DIR", "prompts"),
    ]
    assert type(cfg.MISSING_DEFAULTS["MU_SPEC_PORT"]) is int

    assert cfg.EMPTY_DIR is out.EMPTY_DIR
    assert cfg.PROJECTS_SUBDIR is out.PROJECTS_SUBDIR
    assert cfg.EMPTY_DIR == "."
    assert cfg.PROJECTS_SUBDIR == "projects"


def test_config_derives_from_outside(monkeypatch):
    tree = ast.parse(CONFIG_SOURCE.read_text(encoding="utf-8"))
    docstrings = docstring_nodes(tree)
    constants = [node for node in ast.walk(tree) if isinstance(node, ast.Constant)]
    literals = {
        node.value for node in constants if isinstance(node.value, str) and id(node) not in docstrings
    }
    assert literals <= ALLOWED_LITERALS, f"string literals of its own: {sorted(literals - ALLOWED_LITERALS)}"
    assert not any(
        isinstance(node.value, int) and not isinstance(node.value, bool) and node.value == 9006
        for node in constants
    ), "the port default 9006 is written out"
    imports = imported_modules(tree)
    assert imports <= ALLOWED_IMPORTS, f"imports beyond mu_spec.outside: {sorted(imports - ALLOWED_IMPORTS)}"

    import mu_spec.config
    import mu_spec.outside

    try:
        monkeypatch.setattr(mu_spec.outside, "ENV_NAMES", {**mu_spec.outside.ENV_NAMES, "host": "X_HOST"})
        monkeypatch.setattr(
            mu_spec.outside, "ABSENT_DEFAULTS", {**mu_spec.outside.ABSENT_DEFAULTS, "host": "0.0.0.0"}
        )
        importlib.reload(mu_spec.config)
        assert mu_spec.config.VALUE_NAMES[1] == "X_HOST"
        assert mu_spec.config.MISSING_DEFAULTS["X_HOST"] == "0.0.0.0"
        assert "MU_SPEC_HOST" not in mu_spec.config.MISSING_DEFAULTS
    finally:
        monkeypatch.undo()
        importlib.reload(mu_spec.config)
    assert mu_spec.config.VALUE_NAMES[1] == "MU_SPEC_HOST"
