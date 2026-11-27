"""The start check's predicted outcome per variant, from the start sequence's rules.

Acceptance and test only; never imported by mu_spec. The prediction is reported beside the running
unit's outcome and never decides a verdict: an id where the two differ is a prediction fault.
"""

from __future__ import annotations

from startcheck.variants import ALL_VARIANTS

PREDICTED = ("listening", "exited")

_ALL = frozenset(ALL_VARIANTS)


def predicted_outcome(variant: str) -> str:
    """'exited' or 'listening' for one variant id, read in the start sequence's order."""
    if variant not in _ALL:
        raise ValueError(f"not a variant id: {variant!r}")
    state_dir, host, port = variant[0], variant[1], variant[2]
    override = variant[6] if len(variant) == 7 else "m"
    if port in ("e", "x"):  # an empty or non-integer port
        return "exited"
    if override == "x":  # the override names an existing file
        return "exited"
    if override != "h" and state_dir == "x":  # the projects location lies under a file
        return "exited"
    if host == "x":  # the malformed host does not resolve, so the bind fails
        return "exited"
    return "listening"
