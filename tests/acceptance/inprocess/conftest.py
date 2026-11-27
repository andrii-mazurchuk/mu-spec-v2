"""The in-process subtree: checks over the stored copy that import the rebuilt package (S-508).

Only modules under tests/acceptance/inprocess import mu_spec. rebuilt_package, autouse for the
subtree, fails every check unless mu_spec comes from the rebuilt install at the repository root.
written_store hands a check that asks for it a session copy of the committed store the running unit
wrote (WRITTEN_SUBDIR beside this file); it launches nothing and reads no environment.
"""

import os
import shutil
from pathlib import Path

import pytest

WRITTEN_SUBDIR = "written"  # tests/acceptance/inprocess/written: a committed store root the running unit wrote; data only
WRITTEN_PROJECT = "written-fixture"  # the one project the capture creates
WRITTEN_TITLE = "written fixture"  # the request's and the entry's title
WRITTEN_BODY = 'café ñ → " \\ \nend'  # e-acute, n-tilde, a right arrow, a double quote, a backslash, a newline, then 'end'
WRITTEN_MISSING_TEXT = "{path!r} holds no project {project!r}: the store the running unit wrote has not been captured"
WRITTEN_CR_TEXT = "{path!r} holds a CR byte in {file!r}: the store the running unit wrote was not captured on the node's POSIX platform"

NOT_REBUILT_TEXT = "mu_spec was imported from {found!r}, not from the rebuilt install {expected!r}"


def _same_dir(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


@pytest.fixture(scope="session", autouse=True)
def rebuilt_package(acceptance_inputs: "AcceptanceInputs") -> None:  # noqa: F821
    expected = acceptance_inputs.rebuilt_install
    try:
        import mu_spec
    except ImportError:
        found = None
    else:
        init = getattr(mu_spec, "__file__", None)
        found = os.path.dirname(os.path.dirname(os.path.abspath(init))) if init else None
    if found is None or not _same_dir(found, expected):
        pytest.fail(NOT_REBUILT_TEXT.format(found=found, expected=expected), pytrace=False)


@pytest.fixture(scope="session")
def written_store(acceptance_inputs: "AcceptanceInputs", tmp_path_factory) -> str:  # noqa: F821
    folder = Path(__file__).resolve().parent / WRITTEN_SUBDIR
    if not (folder / WRITTEN_PROJECT).is_dir():
        pytest.fail(WRITTEN_MISSING_TEXT.format(path=str(folder), project=WRITTEN_PROJECT), pytrace=False)
    files = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
    for name in files:
        if b"\r" in (folder / name).read_bytes():
            pytest.fail(WRITTEN_CR_TEXT.format(path=str(folder), file=name), pytrace=False)
    copy = tmp_path_factory.mktemp("written_store") / WRITTEN_SUBDIR
    shutil.copytree(folder, copy)
    return str(copy.resolve())
