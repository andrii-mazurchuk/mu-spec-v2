"""The start check's unit commands from the acceptance-inputs record: the running and rebuilt
UnitCommands, built once, after checking each install holds a unit, that the two differ and that
the prompts folder exists.

Acceptance and test only; never imported by mu_spec. Reads no environment variable, imports
nothing from under tests and declares none of the acceptance inputs: the acceptance fixture alone
declares them and the name of the variable that names the running install, which its caller hands
in as reference_var.
"""

from __future__ import annotations

import os.path
from typing import Protocol

from startcheck.runner import ENTRY_FILE, UnitCommand

NO_UNIT_TEXT = "{label} install {root!r} holds no unit: {entry!r} is not a regular file"
SAME_INSTALL_TEXT = "{name}={root!r} names the rebuilt install, not the running unit"
NO_PROMPTS_TEXT = "{name}={root!r} holds no prompts folder: {prompts!r}"


class InstallFields(Protocol):
    """The three fields read here; the acceptance fixture's AcceptanceInputs satisfies it as built."""

    running_install: str
    rebuilt_install: str
    pristine_prompts: str


class NotAUnit(Exception):
    """An install or prompts folder the start check cannot run; an acceptance test fails on it."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message  # one line, one of the templates above filled in


def entry_path(install_root: str) -> str:
    return os.path.join(install_root, *ENTRY_FILE)


def holds_unit(install_root: str) -> bool:
    return os.path.isfile(entry_path(install_root))


def _same_folder(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def unit_commands(inputs: InstallFields, reference_var: str) -> tuple[UnitCommand, UnitCommand]:
    """(running, rebuilt), both unseeded; the first fault, in the order below, raises NotAUnit."""
    running, rebuilt = inputs.running_install, inputs.rebuilt_install
    for label, root in (("running", running), ("rebuilt", rebuilt)):
        if not holds_unit(root):
            raise NotAUnit(NO_UNIT_TEXT.format(label=label, root=root, entry=entry_path(root)))
    if _same_folder(running, rebuilt):
        raise NotAUnit(SAME_INSTALL_TEXT.format(name=reference_var, root=running))
    if not os.path.isdir(inputs.pristine_prompts):
        raise NotAUnit(NO_PROMPTS_TEXT.format(name=reference_var, root=running,
                                              prompts=inputs.pristine_prompts))
    return UnitCommand("running", running), UnitCommand("rebuilt", rebuilt)
