"""The start check's environment variants: four states over six values, plus the override group.

Acceptance and test only; never imported by mu_spec. A six-character id names the state of each of
VALUE_NAMES in order; a seven-character id adds the state of ROOT_NAME as its seventh character.
Every unit is started at the same fixed paths under one check_root, so both see the same environment.
"""

from __future__ import annotations

import os.path
from collections.abc import Mapping
from itertools import product

from mu_spec.config import ROOT_NAME, VALUE_NAMES

STATES = ("healthy", "missing", "empty", "malformed")
CODES = {"healthy": "h", "missing": "m", "empty": "e", "malformed": "x"}
ROOT_STATES = ("given", "empty", "malformed")  # the override's states other than missing
ROOT_CODES = {"given": "h", "empty": "e", "malformed": "x"}
HEALTHY_PORT = 19406
FALLBACK_PORT = 9006  # where a missing port puts a unit
PASS_THROUGH = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE")

# layout under check_root, an absolute folder the caller names; the same for both units
WORK = "work"  # each unit's working directory; holds every fallback location
STORE = "store"  # healthy HOLONIC_STATE_DIR: pristine copy of the store (holds projects/)
PROMPTS = "prompts"  # healthy MU_SPEC_PROMPTS_DIR: pristine copy of the prompts directory
A_FILE = "notes.txt"  # malformed HOLONIC_STATE_DIR and malformed MU_SPEC_ROOT: an existing regular file
NO_DIR = "nope"  # malformed MU_SPEC_PROMPTS_DIR: never created
ROOT_GIVEN = "store/projects"  # given MU_SPEC_ROOT: the projects folder inside the pristine store copy

_STATE_DIR, _HOST, _PORT, _PROMPTS_DIR, _GIT_SHA, _GITHUB_TOKEN = VALUE_NAMES


def _path(check_root: str, rel: str) -> str:
    return os.path.join(check_root, *rel.split("/"))


def healthy(name: str, check_root: str) -> str:
    """The healthy text of a value; for ROOT_NAME, its given value."""
    texts = {
        _STATE_DIR: _path(check_root, STORE),
        _HOST: "127.0.0.1",
        _PORT: str(HEALTHY_PORT),
        _PROMPTS_DIR: _path(check_root, PROMPTS),
        _GIT_SHA: "0123456789abcdef0123456789abcdef01234567",
        _GITHUB_TOKEN: "ghp_startcheck_healthy",
        ROOT_NAME: _path(check_root, ROOT_GIVEN),
    }
    return texts[name]


def malformed(name: str, check_root: str) -> str:
    """The one fixed malformed text of a value or of ROOT_NAME."""
    texts = {
        _STATE_DIR: _path(check_root, A_FILE),
        _HOST: "not a host!",  # no colon-and-space, so the bind line's fixed reading holds
        _PORT: "abc",
        _PROMPTS_DIR: _path(check_root, NO_DIR),
        _GIT_SHA: "not-a-sha",
        _GITHUB_TOKEN: "ghp_bogus",
        ROOT_NAME: _path(check_root, A_FILE),
    }
    return texts[name]


VARIANTS: tuple[str, ...] = tuple(
    "".join(CODES[s] for s in combo) for combo in product(STATES, repeat=len(VALUE_NAMES))
)
OVERRIDE_VARIANTS: tuple[str, ...] = tuple(
    CODES[s] + "hhhhh" + ROOT_CODES[r] for s, r in product(STATES, ROOT_STATES)
)
ALL_VARIANTS = VARIANTS + OVERRIDE_VARIANTS

_FAULTS = ("m", "e", "x")
QUICK: tuple[str, ...] = (
    ("hhhhhh",)
    + tuple("h" * i + c + "h" * (5 - i) for i in range(6) for c in _FAULTS)
    + tuple(c * 6 for c in _FAULTS)
)

_VALUE_CODES = frozenset(CODES.values())
_OVERRIDE_SET = frozenset(OVERRIDE_VARIANTS)


def environment(variant: str, check_root: str, own_env: Mapping[str, str]) -> dict[str, str]:
    """The environment a unit is started with for one variant id."""
    if len(variant) == 6:
        if not set(variant) <= _VALUE_CODES:
            raise ValueError(f"not a variant id: {variant!r}")
    elif variant not in _OVERRIDE_SET:
        raise ValueError(f"not a variant id: {variant!r}")
    env = {k: own_env[k] for k in PASS_THROUGH if k in own_env}
    for name, code in zip(VALUE_NAMES, variant[:6]):
        if code == "h":
            env[name] = healthy(name, check_root)
        elif code == "x":
            env[name] = malformed(name, check_root)
        elif code == "e":
            env[name] = ""
    if len(variant) == 7:
        code = variant[6]
        if code == "h":
            env[ROOT_NAME] = healthy(ROOT_NAME, check_root)
        elif code == "x":
            env[ROOT_NAME] = malformed(ROOT_NAME, check_root)
        else:
            env[ROOT_NAME] = ""
    return env
