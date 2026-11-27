"""The recorded action answers the page's forms are held to: dashcheck.answers (S-227).

T-200 pins answer_faults: every 2xx holding its action's confirm keys and every refusal one of its
action's refusal forms takes passes, anything else is one fault naming unit, action and source, and an
action with no record from a unit is a 'no answers' fault. T-201 pins from_verdicts (one running and one
rebuilt record per verdict but rollback_tickets) and from_forward (a rebuilt record per action entry
with a unit answer). Modules under test are imported inside the test, so a module not yet written
fails only the tests that need it.
"""

import json
import re
import string

# S-219 ACTION_NAMES, the seven actions every table is keyed by
ACTION_NAMES = ("ratify", "reject", "cut_units", "set_repo", "set_labels", "emit_tickets", "set_delegation")
UNITS = ("running", "rebuilt")


def field_names(template: str) -> list[str]:
    return [field for _, field, _, _ in string.Formatter().parse(template) if field is not None]


def has_literal_dot(template: str) -> bool:
    return any("." in literal for literal, _, _, _ in string.Formatter().parse(template))


def test_answer_faults():
    from dashcheck.answers import AnswerRecord, answer_faults
    from dashcheck.forms import pattern_of
    from mu_spec.refusals import CATALOGUE
    from mu_spec.writes.reasonform import REASON_FORM

    # N as in the table_faults test, its field names also Python identifiers so TEXT can be formatted
    chosen = [name for name in CATALOGUE
              if has_literal_dot(CATALOGUE[name].template)
              and all(f.isidentifier() for f in field_names(CATALOGUE[name].template))]
    assert chosen, "no CATALOGUE template has a '.' outside its fields and only identifier field names"
    n = chosen[0]

    def member() -> dict:
        return {
            "name": n,
            "status": CATALOGUE[n].status,
            "field": "reason" if n in REASON_FORM else "error",
            "pattern": pattern_of(CATALOGUE[n].template),
        }

    ref = {
        "action_names": list(ACTION_NAMES),
        "confirm_keys": {a: ["k"] for a in ACTION_NAMES},
        "refusal_forms": {a: [member()] for a in ACTION_NAMES},
        "control_views": {a: ["ship"] for a in ACTION_NAMES},
    }
    form = member()
    other_field = "error" if form["field"] == "reason" else "reason"
    text = CATALOGUE[n].template.format(**{f: "Q7" for f in field_names(CATALOGUE[n].template)})
    assert re.fullmatch(form["pattern"], text), (form["pattern"], text)

    def rec(unit, a, status, body):
        return AnswerRecord(unit, a, status, body, "case:" + a + "/t")

    base = [rec(u, a, 200, b'{"k": 1}') for a in ACTION_NAMES for u in UNITS]
    base.append(rec("running", "set_labels", form["status"], json.dumps({form["field"]: text}).encode()))
    assert answer_faults(ref, list(base)) == []

    # a 2xx missing the confirm key
    faults = answer_faults(ref, base + [rec("rebuilt", "cut_units", 200, b"{}")])
    assert len(faults) == 1, faults
    assert "rebuilt" in faults[0] and "cut_units" in faults[0] and "case:cut_units/t" in faults[0], faults

    # a 201 holding the key is a success, not a refusal
    faults = answer_faults(ref, base + [rec("rebuilt", "ratify", 201, b'{"k": 1}')])
    assert faults == [], faults

    # a refusal at the form's status whose text no form takes
    wrong = "zz-not-a-catalogue-text-zz"
    assert re.fullmatch(form["pattern"], wrong) is None
    faults = answer_faults(ref, base + [
        rec("rebuilt", "set_labels", form["status"], json.dumps({form["field"]: wrong}).encode())])
    assert len(faults) == 1, faults

    # the catalogue text under the other field
    faults = answer_faults(ref, base + [
        rec("rebuilt", "set_labels", form["status"], json.dumps({other_field: text}).encode())])
    assert len(faults) == 1, faults

    # a gateway page is no form of the action
    faults = answer_faults(ref, base + [rec("rebuilt", "reject", 504, b"<html>gateway</html>")])
    assert len(faults) == 1, faults

    # the floor: no running record for set_labels
    without = [r for r in base if not (r.unit == "running" and r.action == "set_labels")]
    faults = answer_faults(ref, without)
    assert any("no answers" in f and "running" in f and "set_labels" in f for f in faults), faults

    # the floor: an empty source never passes
    faults = answer_faults(ref, [])
    floor = [f for f in faults if "no answers" in f]
    for a in ACTION_NAMES:
        assert any(a in f for f in floor), (a, faults)
    assert any("running" in f for f in floor) and any("rebuilt" in f for f in floor), faults


def test_record_sources():
    from actioncheck import ActionVerdict
    from dashcheck.answers import AnswerRecord, from_forward, from_verdicts
    from dashcheck.forward import Forwarded

    def verdict(action, running, rebuilt):
        return ActionVerdict(action=action, case="ok", status_match=True, body_match=True, record_match=True,
                             log_empty=True, passed=True, running_answer=running, rebuilt_answer=rebuilt)

    v1 = verdict("set_repo", (200, b"R"), (409, b"B"))
    v2 = verdict("rollback_tickets", (200, b"X"), (200, b"X"))
    records = from_verdicts([v1, v2])
    assert len(records) == 2, records
    assert set(records) == {
        AnswerRecord("running", "set_repo", 200, b"R", "case:set_repo/ok"),
        AnswerRecord("rebuilt", "set_repo", 409, b"B", "case:set_repo/ok"),
    }, records

    def entry(n, kind, action, unit_answer):
        return Forwarded(n=n, at=float(n), method="POST", path="x", kind=kind, action=action,
                         view="ship" if kind == "screen" else None, relayed_at=float(n),
                         answered=unit_answer, answered_at=float(n), unit_answer=unit_answer)

    f1 = entry(1, "action", "set_repo", (200, b"U"))
    f2 = entry(2, "action", "set_repo", None)
    f3 = entry(3, "screen", None, (200, b"S"))
    assert from_forward([f1, f2, f3]) == (AnswerRecord("rebuilt", "set_repo", 200, b"U", "forward:1"),)
