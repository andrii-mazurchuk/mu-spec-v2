"""The source sweep's floor: a copy missing any one floor member fails, naming it (T-376).

The sweep under test is sweeps.source.sweep (S-401). Each case copies the repository's mu_spec
package into tmp_path, checks the unchanged copy passes and holds every floor member, deletes the
file holding one listed module (or the whole directory of one of S-400's packages) and runs the
sweep on tmp_path. GITHUB_HOLDERS and PROCESS_EXEMPT are read from sweeps.source, never retyped. A
root with no mu_spec directory gives a floor failure for every member. Modules under test are
imported inside the test, so a module not yet written fails only the tests that need it.
"""

import shutil
from pathlib import Path

import pytest

MU_SPEC = Path(__file__).resolve().parents[1] / "mu_spec"

LISTED = (
    "mu_spec.outside", "mu_spec.order", "mu_spec.refusals", "mu_spec.codec", "mu_spec.github", "mu_spec.store",
    "mu_spec.keys", "mu_spec.tools.schemas", "mu_spec.tools.catalogue", "mu_spec.measure", "mu_spec.diffaudit",
    "mu_spec.writer", "mu_spec.registry",
)
# S-400's measurement floor: the four admission modules and three packages
MEASURE_MODULES = ("mu_spec.refusals", "mu_spec.keys", "mu_spec.writer", "mu_spec.registry")
MEASURE_PACKAGES = ("mu_spec.rules", "mu_spec.writes", "mu_spec.reads")


def floor() -> list[str]:
    """F: the listed modules, GITHUB_HOLDERS, PROCESS_EXEMPT's keys and S-400's floor, repeats removed."""
    from sweeps.source import GITHUB_HOLDERS, PROCESS_EXEMPT

    names = LISTED + tuple(GITHUB_HOLDERS) + tuple(PROCESS_EXEMPT) + MEASURE_MODULES + MEASURE_PACKAGES
    return list(dict.fromkeys(names))


def members() -> list:
    """One case per member of F; one failing case when sweeps.source cannot be read yet."""
    try:
        return [pytest.param(m, id=m) for m in floor()]
    except ImportError:
        return [pytest.param(None, id="sweeps.source-absent")]


def copy_package(tmp_path: Path) -> Path:
    assert MU_SPEC.is_dir(), "mu_spec is absent"
    shutil.copytree(MU_SPEC, tmp_path / "mu_spec", ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


@pytest.mark.parametrize("member", members())
def test_floor_per_member(tmp_path, member):
    from sweeps.source import Failure, sweep

    every = floor()
    root = copy_package(tmp_path)
    assert sweep(root) == []
    for m in every:
        path = root.joinpath(*m.split("."))
        if m in MEASURE_PACKAGES:
            assert (path / "__init__.py").is_file(), f"{m} has no __init__.py in the package"
            inner = [p for p in path.rglob("*.py") if p != path / "__init__.py"]
            assert inner, f"{m} holds no module besides its __init__"
        else:
            assert path.with_name(path.name + ".py").is_file(), f"{m} is absent from the package"

    gone = root.joinpath(*member.split("."))
    if member in MEASURE_PACKAGES:
        shutil.rmtree(gone)
    else:
        gone.with_name(gone.name + ".py").unlink()
    failures = sweep(root)
    assert Failure("floor", member) in failures, failures
    assert [f for f in failures if f.rule != "floor"] == [], failures


def test_floor_on_root_without_package(tmp_path):
    from sweeps.source import Failure, sweep

    every = floor()
    empty = tmp_path / "empty"
    empty.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.py").write_text("", encoding="utf-8")
    for root in (empty, other):
        failures = sweep(root)
        assert failures != [], root
        missing = [m for m in every if Failure("floor", m) not in failures]
        assert missing == [], (root, failures)
        assert [f for f in failures if f.rule != "floor"] == [], (root, failures)
