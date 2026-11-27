"""Reason-form answering: REASON_FORM, reason_body and answer_body in mu_spec.writes.reasonform.

A reason-form refusal answers with its full text in 'reason', in the running unit's key order,
wherever a writer turn raises it, from the one list the catalogue declares. Modules under test are
imported inside the test, so a module not yet written fails only the tests that need it.
"""

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
REASONFORM_SOURCE = REPO_ROOT / "mu_spec" / "writes" / "reasonform.py"
REASONFORM_PACKAGE = "mu_spec.writes"
# what reasonform may import beyond mu_spec.refusals
IMPORT_ALLOWED = ("__future__", "typing")


def imported_modules(tree: ast.AST, package: str) -> list[str]:
    """Every module an Import or ImportFrom in tree names, relative ones resolved against package."""
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = parts[: len(parts) - node.level + 1]
                names.append(".".join(base + ([node.module] if node.module else [])))
            else:
                names.append(node.module or "")
    return names


def raised(name, /, **kwargs):
    """The Refused that refuse(name, **kwargs) raises."""
    from mu_spec.refusals import Refused, refuse

    with pytest.raises(Refused) as caught:
        refuse(name, **kwargs)
    return caught.value


def test_reason_form_answering():
    from mu_spec.refusals import CATALOGUE, REASON_KEYS, refusal_body
    from mu_spec.writes.reasonform import REASON_FORM, answer_body, reason_body

    # 1. the reason-form names are the catalogue's own list, not a second copy of it
    assert REASON_FORM is REASON_KEYS
    assert len(REASON_FORM) == 11
    assert tuple(REASON_FORM) == (
        "SHIP_BUILD_MISMATCH",
        "SHIP_DEAD_END",
        "SHIP_UNIT_CYCLE",
        "DELEGATED_REFUSED",
        "DELEGATED_CUT_REFUSED",
        "CUT_UNSOUND",
        "CUT_NO_MODULES",
        "PLAN_UNSOUND",
        "AMENDMENT_BROKEN_EDGE",
        "AMENDMENT_BREAKS_SLICES",
        "SPLIT_BREAKS_SLICES",
    )
    for name in REASON_FORM:
        assert name in CATALOGUE, f"REASON_FORM member {name!r} is not a CATALOGUE key"

    # 2. reason_body: extra's keys in extra's order, the message in the 'reason' slot, no 'error'
    r = raised("DELEGATED_REFUSED", extra={"allowed": False, "reason": None, "failed": []}, names="delegation")
    b = reason_body(r)
    assert list(b) == ["allowed", "reason", "failed"]
    assert b["reason"] == r.message
    assert b["allowed"] is False
    assert b["failed"] == []
    assert "error" not in b
    # the body is a new dict: the refusal's own extra still holds None in 'reason'
    assert r.extra["reason"] is None

    # 3. answer_body of a REASON_FORM refusal is its reason_body
    assert answer_body(r) == reason_body(r)

    # 4. an extra with no 'reason' is answered as it is: no top-level 'reason' or 'error' added
    extra = {
        "project": "p",
        "allowed": True,
        "failed": [],
        "base_cut": 3,
        "conditions": {},
        "cut": {"project": "p", "cut": False, "reason": "x"},
    }
    d = raised("DELEGATED_CUT_REFUSED", extra=extra)
    assert answer_body(d) == extra
    assert list(answer_body(d)) == list(extra)
    assert "reason" not in answer_body(d)
    assert "error" not in answer_body(d)

    # 5. another REASON_FORM refusal raised with reason None carries its full text under 'reason'
    s = raised("SHIP_UNIT_CYCLE", extra={"emitted": False, "reason": None}, path="a -> b -> a")
    assert answer_body(s)["reason"] == s.message
    assert "error" not in answer_body(s)

    # 6. answer_body of a refusal outside REASON_FORM is its refusal_body
    u = raised("UNKNOWN_PROJECT", project="p")
    assert u.refusal.name not in REASON_FORM
    assert answer_body(u) == refusal_body(u) == {"error": "unknown project 'p'"}

    # 7. the module imports nothing but mu_spec.refusals and lists no catalogue names of its own
    tree = ast.parse(REASONFORM_SOURCE.read_text(encoding="utf-8"), filename=str(REASONFORM_SOURCE))
    for module in imported_modules(tree, REASONFORM_PACKAGE):
        top = module.split(".")[0]
        assert module == "mu_spec.refusals" or top in IMPORT_ALLOWED, (
            f"mu_spec/writes/reasonform.py imports {module!r}: it may import only mu_spec.refusals"
        )
    for node in ast.walk(tree):
        if isinstance(node, (ast.Tuple, ast.List)):
            listed = [
                e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str) and e.value in CATALOGUE
            ]
            assert not listed, (
                f"mu_spec/writes/reasonform.py line {node.lineno} lists catalogue names {listed!r}: "
                "REASON_FORM is the catalogue's REASON_KEYS, never a second list"
            )
