"""The one tool declaration is a leaf, and its declared shape never becomes a check (T-360).

A plain unit test: no acceptance input and no unit started. DECLS and BY_NAME are read from the
rebuilt package as installed; the import closure of mu_spec.tools.schemas is walked by AST; and the
schema-use check S-393 declares is run over a copy of the package source in tmp_path, once on the
copy unchanged and once per fixture or floor case on its own fresh copy. That each member equals the
running unit's discovery entry, in its order, is held by the acceptance differential in
tests/acceptance/test_shape_refusals.py. Modules under test are imported inside the test, so a module
not yet written fails only the tests that need it.
"""

import ast
import re
import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest

PACKAGE = "mu_spec"
SCHEMAS = "mu_spec.tools.schemas"
SWEPT = ("mu_spec.writes", "mu_spec.rules", "mu_spec.reads")
SHAPE_ATTR = "input_schema"
PARAM = re.compile(r"\{([^{}]*)\}")

# (case id, module added or package removed, the module's source or None for a floor case, passes)
CASES = (
    ("writes-imports-by-name", "mu_spec.writes.fx", "from mu_spec.tools.schemas import BY_NAME\n", False),
    ("reads-subscript-attribute", "mu_spec.reads.fx", "def f(t):\n    return BY_NAME[t].input_schema\n", False),
    ("rules-attribute", "mu_spec.rules.fx", "def f(schema):\n    return schema.input_schema\n", False),
    ("writes-keyword-only", "mu_spec.writes.fx", "def f():\n    return ActionSpec(input_schema={})\n", True),
    ("reads-imports-module", "mu_spec.reads.fx", "import mu_spec.tools.schemas\n", False),
    ("floor-writes", "mu_spec.writes", None, False),
    ("floor-rules", "mu_spec.rules", None, False),
    ("floor-reads", "mu_spec.reads", None, False),
)


def module_name(root: Path, path: Path) -> str:
    """The dotted name of a source file under root, the folder holding the package."""
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def module_file(root: Path, name: str) -> Path | None:
    """The source file of a dotted module name under root, or None when no such module is there."""
    base = root.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def imported(name: str, is_package: bool, tree: ast.AST) -> list[tuple[str, tuple[str, ...]]]:
    """(absolute base, names taken from it) for every import in tree; relative imports resolved."""
    package = name.split(".") if is_package else name.split(".")[:-1]
    out: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                head = package[: len(package) - (node.level - 1)]
                base = ".".join(head + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            out.append((base, tuple(alias.name for alias in node.names)))
    return out


def mu_spec_targets(root: Path, base: str, names: tuple[str, ...]) -> set[str]:
    """The mu_spec modules one import reaches: base, or base.n where n is itself a module."""
    if base != PACKAGE and not base.startswith(PACKAGE + "."):
        return set()
    targets = {f"{base}.{n}" for n in names if module_file(root, f"{base}.{n}") is not None}
    if not targets or len(targets) < len(names):
        targets.add(base)
    return targets


def import_closure(root: Path, start: str) -> set[str]:
    """Every mu_spec module reached from start by import and from-import, transitively; start excluded."""
    reached: set[str] = set()
    todo = [start]
    seen = {start}
    while todo:
        name = todo.pop()
        path = module_file(root, name)
        if path is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for base, names in imported(name, path.name == "__init__.py", tree):
            for target in mu_spec_targets(root, base, names):
                reached.add(target)
                if target not in seen:
                    seen.add(target)
                    todo.append(target)
    reached.discard(start)
    return reached


def schema_use(root: Path) -> list[str]:
    """S-393's check over the package under root: one line per breach; [] when the check passes.

    No module under SWEPT imports mu_spec.tools.schemas or reads (ast.Attribute, Load context) an
    attribute named input_schema; and the check fails unless it inspected at least one module under
    each of the three packages, each package's absence its own line.
    """
    problems: list[str] = []
    for package in SWEPT:
        folder = root.joinpath(*package.split("."))
        modules = sorted(folder.rglob("*.py")) if folder.is_dir() else []
        if not modules:
            problems.append(f"{package}: no module inspected (the package is missing)")
            continue
        for path in modules:
            name = module_name(root, path)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for base, names in imported(name, path.name == "__init__.py", tree):
                if SCHEMAS in mu_spec_targets(root, base, names):
                    problems.append(f"{name}: imports {SCHEMAS}")
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr == SHAPE_ATTR:
                    problems.append(f"{name}: reads .{SHAPE_ATTR} at line {node.lineno}")
    return problems


def fresh_copy(source: Path, tmp_path: Path, label: str) -> Path:
    """A copy of the package source; returns the folder holding the copied package."""
    root = tmp_path / label
    shutil.copytree(source, root / PACKAGE, ignore=shutil.ignore_patterns("__pycache__"))
    return root


@pytest.mark.parametrize(("module", "source", "passes"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_decls_leaf_and_no_shape_check(tmp_path, module, source, passes):
    from mu_spec.tools import schemas
    from mu_spec.tools.schemas import BY_NAME, DECLS

    # 1. 41 uniquely named tools, BY_NAME keyed in DECLS order
    assert len(DECLS) == 41, [s.name for s in DECLS]
    assert len({s.name for s in DECLS}) == 41, [s.name for s in DECLS]
    assert list(BY_NAME) == [s.name for s in DECLS]
    for s in DECLS:
        assert BY_NAME[s.name] is s, s.name

    # 2. each member's method, path and input shape
    for s in DECLS:
        assert s.method in ("GET", "POST"), (s.name, s.method)
        assert s.path.startswith("/"), (s.name, s.path)
        assert isinstance(s.input_schema, Mapping), (s.name, type(s.input_schema))
        for x in PARAM.findall(s.path):
            assert x.isidentifier(), (s.name, s.path, x)

    # 3. the import closure of the schemas module, by AST, holds no mu_spec module
    package_dir = Path(schemas.__file__).resolve().parents[1]
    installed_root = package_dir.parent
    assert import_closure(installed_root, SCHEMAS) == set()

    # 4. the schema-use check passes on the unchanged copy, every package holding a module beside __init__
    unchanged = fresh_copy(package_dir, tmp_path, "unchanged")
    for package in SWEPT:
        folder = unchanged.joinpath(*package.split("."))
        others = [p for p in folder.rglob("*.py") if p.name != "__init__.py"] if folder.is_dir() else []
        assert others, f"{package} holds no module other than its __init__"
    assert schema_use(unchanged) == []

    # 5 and 6. this case, on its own fresh copy
    root = fresh_copy(package_dir, tmp_path, "case")
    target = root.joinpath(*module.split("."))
    if source is None:
        shutil.rmtree(target)
    else:
        target.with_suffix(".py").write_text(source, encoding="utf-8")
    problems = schema_use(root)
    if passes:
        assert problems == [], problems
    else:
        assert problems, f"the check passes with {module} {'added' if source else 'removed'}"
        assert all(line.startswith(module + ":") for line in problems), problems
