"""Reason-form answering: REASON_FORM, reason_body and answer_body in mu_spec.writes.reasonform.

A reason-form refusal answers with its full text in 'reason', in the running unit's key order,
wherever a writer turn raises it. Modules under test are imported inside the test, so a module
not yet written fails only the tests that need it.
"""

import ast
import pathlib

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


def test_reason_form_answering():
    from mu_spec.refusals import CATALOGUE, refusal_body, refuse
    from mu_spec.writes.reasonform import REASON_FORM, answer_body, reason_body

    # 1. the reason-form names: a floor of five, each a catalogue name
    assert len(REASON_FORM) >= 5
    for name in REASON_FORM:
        assert name in CATALOGUE, f"REASON_FORM member {name!r} is not a CATALOGUE key"
    assert REASON_FORM == (
        "SHIP_BUILD_MISMATCH",
        "DELEGATED_REFUSED",
        "DELEGATED_CUT_REFUSED",
        "CUT_UNSOUND",
        "CUT_NO_MODULES",
    )

    # 2. reason_body: extra's keys in extra's order, the message in the 'reason' slot, no 'error'
    r = refuse("DELEGATED_REFUSED", extra={"allowed": False, "reason": None, "failed": []}, names="delegation")
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

    # 4. answer_body of a refusal outside REASON_FORM is its refusal_body
    u = refuse("UNKNOWN_PROJECT", project="p")
    assert u.refusal.name not in REASON_FORM
    assert answer_body(u) == refusal_body(u) == {"error": "unknown project 'p'"}

    # 5. the module imports nothing but mu_spec.refusals
    tree = ast.parse(REASONFORM_SOURCE.read_text(encoding="utf-8"), filename=str(REASONFORM_SOURCE))
    for module in imported_modules(tree, REASONFORM_PACKAGE):
        top = module.split(".")[0]
        assert module == "mu_spec.refusals" or top in IMPORT_ALLOWED, (
            f"mu_spec/writes/reasonform.py imports {module!r}: it may import only mu_spec.refusals"
        )
