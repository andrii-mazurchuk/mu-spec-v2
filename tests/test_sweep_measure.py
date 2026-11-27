"""The measurement rules and floor, run through the source sweep's own entry (T-374).

The sweep under test is sweeps.source.sweep (S-401), inside which S-400's measurement rules 1 to 4
and its floor run; it is never re-implemented here. Each case copies the repository's mu_spec package
into tmp_path, writes fixture modules into the copy or removes one floor member from it, and runs the
sweep on the directory holding the copy. Assertions rely only on a failure's rule and subject.
Modules under test are imported inside the test, so a module not yet written fails only the tests
that need it.
"""

import shutil
from pathlib import Path

import pytest

MU_SPEC = Path(__file__).resolve().parents[1] / "mu_spec"
# S-400's floor, one removal per member: M1 to M4 a module, M5 to M7 a whole package
FLOOR_MODULES = ("mu_spec.refusals", "mu_spec.keys", "mu_spec.writer", "mu_spec.registry")
FLOOR_PACKAGES = ("mu_spec.rules", "mu_spec.writes", "mu_spec.reads")

# name -> ({module: source} written into a fresh copy, the expected (rule, subject))
FIXTURES = {
    "F1": ({"mu_spec.writes.f1": "import mu_spec.measure"}, ("measure-1", "mu_spec.writes.f1")),
    "F2": ({"mu_spec.reads.f2": "from mu_spec import measure", "mu_spec.writes.f2w": "import mu_spec.reads.f2"},
           ("measure-1", "mu_spec.writes.f2w -> mu_spec.reads.f2 -> mu_spec.measure")),
    "F3": ({"mu_spec.reads.f3": "class H:\n    check_order = ()"}, ("measure-3", "mu_spec.reads.f3")),
    "F4": ({"mu_spec.util.f4": "import mu_spec.measure"}, ("measure-2", "mu_spec.util.f4")),
}
F5 = "mu_spec.measure.f5"


def copy_package(root: Path) -> Path:
    assert MU_SPEC.is_dir(), "mu_spec is absent"
    shutil.copytree(MU_SPEC, root / "mu_spec", ignore=shutil.ignore_patterns("__pycache__"))
    return root


def place(root: Path, module: str, source: str) -> None:
    """Write source as module's file in the copy, creating each missing package with an empty __init__."""
    parts = module.split(".")
    package = root / parts[0]
    for part in parts[1:-1]:
        package = package / part
        if not package.is_dir():
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
    (package / (parts[-1] + ".py")).write_text(source + "\n", encoding="utf-8", newline="")


def as_package(root: Path) -> None:
    """The copy's measurement module made a package of the same name, holding the module as __init__."""
    single = root / "mu_spec" / "measure.py"
    package = root / "mu_spec" / "measure"
    if single.is_file():
        package.mkdir()
        single.rename(package / "__init__.py")
    assert (package / "__init__.py").is_file(), "the copy holds no mu_spec.measure"


def measure(failures: list) -> list:
    return [f for f in failures if f.rule.startswith("measure-")]


def test_measure_dependency_rules(tmp_path):
    from sweeps.source import sweep

    # 1. the unchanged package passes
    unchanged = copy_package(tmp_path / "unchanged")
    assert sweep(unchanged) == []

    misses = []
    for name, (modules, expected) in FIXTURES.items():
        root = copy_package(tmp_path / name)
        for module, source in modules.items():
            place(root, module, source)
        failures = sweep(root)
        if expected not in [(f.rule, f.subject) for f in measure(failures)]:
            misses.append((name, expected, failures))

    # F5: a module under the measurement package importing the store breaks rule 4
    root = copy_package(tmp_path / "F5")
    as_package(root)
    place(root, F5, "import mu_spec.store")
    failures = sweep(root)
    if not any(f.rule == "measure-4" and F5 in f.subject for f in measure(failures)):
        misses.append(("F5", ("measure-4", F5), failures))

    # 2. each fixture fails its own rule, naming its subject
    assert misses == [], misses


@pytest.mark.parametrize("member", FLOOR_MODULES + FLOOR_PACKAGES)
def test_measure_floor_per_member(tmp_path, member):
    from sweeps.source import sweep

    root = copy_package(tmp_path)
    assert sweep(root) == []
    for package in FLOOR_PACKAGES:
        directory = root.joinpath(*package.split("."))
        assert (directory / "__init__.py").is_file(), f"{package} has no __init__.py in the package"
        inner = [p for p in directory.rglob("*.py") if p != directory / "__init__.py"]
        assert inner, f"{package} holds no module besides its __init__"

    gone = root.joinpath(*member.split("."))
    if member in FLOOR_PACKAGES:
        shutil.rmtree(gone)
    else:
        path = gone.with_name(gone.name + ".py")
        assert path.is_file(), f"{member} is absent from the package"
        path.unlink()
    failures = sweep(root)

    # 3. the floor names the missing member
    assert ("floor", member) in [(f.rule, f.subject) for f in failures], failures
    # 4. and fails by the floor alone
    assert measure(failures) == [], failures
    assert [f for f in failures if f.rule != "floor"] == [], failures
