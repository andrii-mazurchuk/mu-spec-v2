"""The page's action tables against the unit's refusal catalogue: dashcheck.forms (S-258).

pattern_of is REFUSAL_PATTERN_RULE (S-217) applied in Python; table_faults holds the page's
REFUSAL_FORMS, CONFIRM_KEYS and CONTROL_VIEWS to CATALOGUE (S-357) and REASON_FORM (S-341);
read_tables prints them from the page source root with bun. Modules under test are imported
inside the test, so a module not yet written fails only the tests that need it.
"""

import copy
import re
import string
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DASHBOARD = REPO / "dashboard"
# S-219 ACTION_NAMES, the seven actions every table is keyed by
ACTION_NAMES = ("ratify", "reject", "cut_units", "set_repo", "set_labels", "emit_tickets", "set_delegation")


def test_pattern_of_literals():
    from dashcheck.forms import pattern_of
    from mu_spec.refusals import CATALOGUE

    assert pattern_of("unknown project {project!r}") == r"^unknown project [\s\S]*?$"
    assert pattern_of("a {{b}} (c)") == r"^a \{b\} \(c\)$"
    assert pattern_of("x.y/z|w") == r"^x\.y\/z\|w$"
    assert pattern_of("{a}{b!r:>5}") == r"^[\s\S]*?[\s\S]*?$"
    assert pattern_of("n={n}.") == r"^n=[\s\S]*?\.$"
    assert pattern_of("a b") == r"^a b$"

    for name, member in CATALOGUE.items():
        pattern = pattern_of(member.template)
        re.compile(pattern)
        assert pattern.startswith("^") and pattern.endswith("$"), (name, pattern)


def has_literal_dot(template: str) -> bool:
    return any("." in literal for literal, _, _, _ in string.Formatter().parse(template))


def unescape_first_dot(pattern: str) -> str:
    """pattern with the backslash before its first escaped '.' removed."""
    i = 0
    while i < len(pattern):
        if pattern[i] == "\\":
            if pattern[i + 1] == ".":
                return pattern[:i] + pattern[i + 1:]
            i += 2
        else:
            i += 1
    raise AssertionError(f"{pattern!r} has no escaped '.'")


def test_table_faults():
    from dashcheck.forms import TABLE_NAMES, pattern_of, read_tables, table_faults
    from mu_spec.refusals import CATALOGUE
    from mu_spec.writes.reasonform import REASON_FORM

    dotted = [name for name in CATALOGUE if has_literal_dot(CATALOGUE[name].template)]
    assert dotted, "no CATALOGUE template has a '.' outside its format fields"
    n = dotted[0]

    def member() -> dict:
        return {
            "name": n,
            "status": CATALOGUE[n].status,
            "field": "reason" if n in REASON_FORM else "error",
            "pattern": pattern_of(CATALOGUE[n].template),
        }

    # every action its own member and lists, so a mutation on one action reaches no other
    ref = {
        "action_names": list(ACTION_NAMES),
        "confirm_keys": {a: ["k"] for a in ACTION_NAMES},
        "refusal_forms": {a: [member()] for a in ACTION_NAMES},
        "control_views": {a: ["ship"] for a in ACTION_NAMES},
    }
    assert table_faults(ref) == []

    # M1: one character of one pattern
    m1 = copy.deepcopy(ref)
    form = m1["refusal_forms"]["set_labels"][0]
    form["pattern"] = unescape_first_dot(form["pattern"])
    faults = table_faults(m1)
    assert len(faults) == 1, faults
    assert "set_labels" in faults[0] and n in faults[0], faults

    # M2: one member's status
    m2 = copy.deepcopy(ref)
    m2["refusal_forms"]["cut_units"][0]["status"] += 1
    faults = table_faults(m2)
    assert len(faults) == 1, faults
    assert "cut_units" in faults[0] and n in faults[0], faults

    # M3: one member's field
    m3 = copy.deepcopy(ref)
    form = m3["refusal_forms"]["ratify"][0]
    form["field"] = "error" if form["field"] == "reason" else "reason"
    faults = table_faults(m3)
    assert len(faults) == 1, faults
    assert "ratify" in faults[0], faults

    # M4: a name outside the catalogue
    m4 = copy.deepcopy(ref)
    m4["refusal_forms"]["set_repo"][0]["name"] = "zz_not_a_name"
    faults = table_faults(m4)
    assert faults, "a member named outside CATALOGUE gave no fault"
    assert any("set_repo" in fault for fault in faults), faults

    # M5: below the confirm_keys floor
    m5 = copy.deepcopy(ref)
    m5["confirm_keys"]["reject"] = []
    faults = table_faults(m5)
    assert len(faults) == 1, faults
    assert "reject" in faults[0], faults

    # M6: an action missing from refusal_forms
    m6 = copy.deepcopy(ref)
    del m6["refusal_forms"]["emit_tickets"]
    faults = table_faults(m6)
    assert faults, "refusal_forms without emit_tickets gave no fault"
    assert any("emit_tickets" in fault for fault in faults), faults

    # the page's own printed tables
    t = read_tables(str(DASHBOARD))
    assert set(t) == set(TABLE_NAMES)
    assert table_faults(t) == []


@pytest.mark.parametrize("body", [
    "process.exit(3);\n",
    "console.log('not json');\n",
    "console.log('[1,2]');\n",
], ids=["exits-non-zero", "no-json", "not-an-object"])
def test_read_tables_unreadable(tmp_path, body):
    from dashcheck.forms import TablesUnreadable, read_tables

    root = tmp_path / "page"
    source = root / "check" / "printtables.ts"
    source.parent.mkdir(parents=True)
    source.write_text(body, encoding="utf-8")

    with pytest.raises(TablesUnreadable) as raised:
        read_tables(str(root))
    assert raised.value.reason, "TablesUnreadable carries an empty reason"
