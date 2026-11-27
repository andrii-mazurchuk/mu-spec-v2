"""The start check's verdict records: one per variant, and the report of a whole run.

Acceptance and test only; never imported by mu_spec. One rule, variant_passed, decides a variant;
the prediction is reported beside it and never enters it. The records hold no exit number and no
error-output text other than the bind-line judgement.
"""

from __future__ import annotations

from dataclasses import dataclass

from startcheck.runner import START_OUTCOMES

__all__ = [
    "EXIT_CLASSES",
    "START_OUTCOMES",
    "CallVerdict",
    "RunReport",
    "VariantVerdict",
    "exit_class",
    "variant_passed",
]

EXIT_CLASSES = ("zero", "nonzero")


def exit_class(exit_code: int | None) -> str | None:
    """'zero' or 'nonzero' for an exit status, None for a start that did not exit."""
    if exit_code is None:
        return None
    return "zero" if exit_code == 0 else "nonzero"


@dataclass(frozen=True)
class CallVerdict:
    call: int  # index into the judgement's call list
    status_match: bool
    body_match: bool  # after the named masks


@dataclass(frozen=True)
class VariantVerdict:
    variant: str
    predicted: str  # 'listening' or 'exited', the start sequence's prediction
    running_outcome: str  # one of START_OUTCOMES
    rebuilt_outcome: str
    running_exit_class: str | None  # exit_class(Started.exit_code)
    rebuilt_exit_class: str | None
    outcome_match: bool  # running_outcome == rebuilt_outcome, and that outcome is not 'hung'
    exit_class_match: bool  # running_exit_class == rebuilt_exit_class
    prediction_fault: bool  # predicted != running_outcome; reported, never part of passed
    running_bind_line: bool  # running unit exited and its whole error output is one bind line
    bind_match: bool | None  # None when running_bind_line is False
    install_values_checked: bool  # the install-commit mask applied in this variant
    install_values_ok: bool  # True when not checked
    health_answered: bool  # both listening and the first call answered 200 on both
    calls: tuple[CallVerdict, ...]  # empty unless both listening
    both_alive: bool | None  # None unless both listening; else both alive after the last call
    passed: bool  # variant_passed(...) of the fields above


def variant_passed(v: VariantVerdict) -> bool:
    """The one pass rule; prediction_fault never enters it."""
    if not (v.outcome_match and v.exit_class_match and v.bind_match is not False
            and v.install_values_ok):
        return False
    if v.running_outcome == "listening" and v.rebuilt_outcome == "listening":
        return (v.health_answered and v.both_alive is True
                and all(c.status_match and c.body_match for c in v.calls))
    return True


@dataclass(frozen=True)
class RunReport:
    verdicts: tuple[VariantVerdict, ...]  # in the order of the variants asked for
    missing: tuple[str, ...]  # ids asked for that hold no verdict (crash, halt)
    healthy_listened: bool  # the 'hhhhhh' verdict exists and both units listened in it
    listening: int  # verdicts where both units listened
    prediction_faults: tuple[str, ...]  # ids whose prediction_fault is True, in verdict order
    passed: bool  # missing is empty and healthy_listened and every verdict's passed
