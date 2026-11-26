"""The source sweep's floor: a package missing any listed module fails, naming it (T-66).

The sweep under test is sweeps.source.sweep (S-401). Each case copies the repository's mu_spec
package into tmp_path, deletes the file or package directory holding one listed module and runs the
sweep on tmp_path. The floor is copied from the specification as a literal, GITHUB_HOLDERS
included, so a sweep whose floor forgets a member fails that member's case. Modules under test are
imported inside the test, so a module not yet written fails only the tests that need it.
"""

import shutil
from pathlib import Path

import pytest

MU_SPEC = Path(__file__).resolve().parents[1] / "mu_spec"

GITHUB_HOLDERS = (
    "mu_spec.registry",
    "mu_spec.ship.part",
    "mu_spec.ship.sender",
    "mu_spec.ship.plan",
    "mu_spec.ship.access",
    "mu_spec.actions",
    "mu_spec.dash.assemble",
)
FLOOR = [
    "mu_spec.outside", "mu_spec.order", "mu_spec.refusals", "mu_spec.codec", "mu_spec.github", "mu_spec.store",
    "mu_spec.keys", "mu_spec.tools.schemas", "mu_spec.tools.catalogue", "mu_spec.measure", "mu_spec.diffaudit",
    "mu_spec.writer", "mu_spec.registry",
] + list(GITHUB_HOLDERS)


def holder(root: Path, module: str) -> Path:
    """The package directory or the file holding module under root."""
    directory = root.joinpath(*module.split("."))
    return directory if directory.is_dir() else directory.with_name(directory.name + ".py")


@pytest.mark.parametrize("module", FLOOR)
def test_floor_per_module(tmp_path, module):
    from sweeps.source import Failure, sweep

    assert MU_SPEC.is_dir(), "mu_spec is absent"
    shutil.copytree(MU_SPEC, tmp_path / "mu_spec", ignore=shutil.ignore_patterns("__pycache__"))
    gone = holder(tmp_path, module)
    assert gone.exists(), f"{module} is absent from the package"
    if gone.is_dir():
        shutil.rmtree(gone)
    else:
        gone.unlink()
    result = sweep(tmp_path)
    assert Failure("floor", module) in result, result


def test_floor_on_empty_directory(tmp_path):
    from sweeps.source import GITHUB_HOLDERS as declared
    from sweeps.source import Failure, sweep

    assert declared == GITHUB_HOLDERS
    result = sweep(tmp_path)
    assert result != []
    missing = [m for m in FLOOR if Failure("floor", m) not in result]
    assert missing == [], result
