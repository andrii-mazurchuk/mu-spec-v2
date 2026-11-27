"""mu_spec.ship.plan: Ship's and rollback's fixed request plans, markers and Ship-log reading (T-392..T-398).

Every plan runs over a fake Sender that records each (method, path, body) it is handed and answers like a
small GitHub holding issues in memory, unless the case scripts a request's answer; no request leaves the
process. The module under test is imported inside each test, so a module not yet written fails only the
tests that need it.
"""

import datetime
import json
import re

import pytest

from mu_spec.outside import FixedClock

START = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
REPO = "/repos/o/r"
ISSUES = REPO + "/issues"
LISTING = ISSUES + "?state=open&per_page=100&page=1"
CLOSE = {"state": "closed", "state_reason": "not_planned"}

ISSUE_PATH = re.compile(r"^/repos/o/r/issues/(\d+)$")
LABELS_PATH = re.compile(r"^/repos/o/r/issues/(\d+)/labels$")
LABEL_PATH = re.compile(r"^/repos/o/r/issues/(\d+)/labels/(.+)$")
DEPS_LIST_PATH = re.compile(r"^/repos/o/r/issues/(\d+)/dependencies/blocked_by\?")
DEPS_PATH = re.compile(r"^/repos/o/r/issues/(\d+)/dependencies/blocked_by$")
DEP_PATH = re.compile(r"^/repos/o/r/issues/(\d+)/dependencies/blocked_by/(\d+)$")


def gh_issue(number, labels=(), state="open", state_reason=None, title="", body="", **extra):
    """An issue as GitHub answers it; its id is ten times its number."""
    issue = {
        "id": number * 10,
        "number": number,
        "url": f"https://api.github.com/repos/o/r/issues/{number}",
        "html_url": f"https://github.com/o/r/issues/{number}",
        "title": title,
        "body": body,
        "state": state,
        "state_reason": state_reason,
        "labels": [{"name": name} for name in labels],
    }
    issue.update(extra)
    return issue


class FakeGitHub:
    """A Sender over an in-memory repository o/r.

    script maps (method, path) to (status, body), an exception to raise, or a list of those taken in turn
    (the last one repeating). Unscripted requests get the default answers: push access, an empty open-issue
    listing, creates numbered from `numbers` (the k-th create refused 422 when k is in `refuse`), issues
    read and changed in place, and dependency links kept per blocked issue.
    """

    def __init__(self, issues=(), script=None, refuse=(), numbers=(20, 21, 22, 23), links=None):
        self.issues = {issue["number"]: dict(issue) for issue in issues}
        self.script = dict(script or {})
        self.refuse = set(refuse)
        self.numbers = list(numbers)
        self.links = {n: list(ids) for n, ids in (links or {}).items()}
        self.creates = 0
        self.sends = []

    def send(self, method, path, body=None):
        self.sends.append((method, path, None if body is None else dict(body)))
        if (method, path) in self.script:
            outcome = self.script[(method, path)]
            if isinstance(outcome, list):
                outcome = outcome.pop(0) if len(outcome) > 1 else outcome[0]
            if isinstance(outcome, BaseException):
                raise outcome
            return answer(*outcome)
        return answer(*self.default(method, path, body))

    def issue(self, number):
        if number not in self.issues:
            self.issues[number] = gh_issue(number)
        return self.issues[number]

    def by_id(self, issue_id):
        return next((i for i in self.issues.values() if i["id"] == issue_id), gh_issue(issue_id // 10))

    def default(self, method, path, body):
        if method == "GET" and path == REPO:
            return 200, {"full_name": "o/r", "permissions": {"push": True}}
        if method == "GET" and path.startswith(ISSUES + "?"):
            return 200, []
        if method == "POST" and path == ISSUES:
            self.creates += 1
            if self.creates in self.refuse:
                return 422, {"message": "Validation Failed"}
            number = self.numbers.pop(0)
            self.issues[number] = gh_issue(
                number, body.get("labels", ()), title=body.get("title", ""), body=body.get("body", ""))
            return 201, self.issues[number]
        if m := ISSUE_PATH.match(path):
            issue = self.issue(int(m.group(1)))
            if method == "PATCH":
                issue.update(body)
            return 200, issue
        if m := LABELS_PATH.match(path):
            issue = self.issue(int(m.group(1)))
            names = [label["name"] for label in issue["labels"]]
            issue["labels"] += [{"name": n} for n in body.get("labels", ()) if n not in names]
            return 200, issue["labels"]
        if method == "DELETE" and (m := LABEL_PATH.match(path)):
            from urllib.parse import unquote

            issue = self.issue(int(m.group(1)))
            issue["labels"] = [label for label in issue["labels"] if label["name"] != unquote(m.group(2))]
            return 200, issue["labels"]
        if method == "GET" and (m := DEPS_LIST_PATH.match(path)):
            return 200, [self.by_id(i) for i in self.links.get(int(m.group(1)), [])]
        if method == "POST" and (m := DEPS_PATH.match(path)):
            self.links.setdefault(int(m.group(1)), []).append(body["issue_id"])
            return 201, self.by_id(body["issue_id"])
        if method == "DELETE" and (m := DEP_PATH.match(path)):
            ids = self.links.get(int(m.group(1)), [])
            if int(m.group(2)) in ids:
                ids.remove(int(m.group(2)))
            return 200, {}
        return 200, []


def answer(status, body):
    from mu_spec.ship.sender import Answer

    return Answer(status=status, body=body, headers={})


def unreachable(reason, timed_out):
    from mu_spec.github import GitHubUnreachable

    return GitHubUnreachable(reason, timed_out)


def ship_inputs(**changes):
    from mu_spec.ship.plan import ShipInputs

    fields = {"project": "p", "kind": "person_ship", "repo": "o/r", "token": "t", "cut": None, "cut_waves": (),
              "drift": None, "retired_citations": (), "labels": (), "pickup": (), "issues": {}, "orders": (),
              "lineage_keyed": False, "next_seq": 1, "waves": None, "unit": None}
    fields.update(changes)
    return ShipInputs(**fields)


def rollback_inputs(**changes):
    from mu_spec.ship.plan import RollbackInputs

    fields = {"project": "p", "known": True, "repo": "o/r", "token": "t", "latest_cut_seq": 2, "cut_seq": None,
              "issues": {}, "pickup": ("claimed",), "next_seq": 6}
    fields.update(changes)
    return RollbackInputs(**fields)


def press(inputs, fake):
    """ship_plan over a run freshly admitted on inputs.project."""
    from mu_spec.ship.plan import ship_plan
    from mu_spec.ship.runs import RunRegistry

    reg = RunRegistry(FixedClock(START))
    run = reg.admit(inputs.project, inputs.kind, None, 0)
    return ship_plan(inputs, fake, run, reg)


def roll_back(inputs, fake):
    """rollback_plan over a 'rollback' run freshly admitted on inputs.project."""
    from mu_spec.ship.plan import rollback_plan
    from mu_spec.ship.runs import RunRegistry

    reg = RunRegistry(FixedClock(START))
    run = reg.admit(inputs.project, "rollback", None, 0)
    return rollback_plan(inputs, fake, run, reg)


def unit_of(anchor, module, entries=None, slices=("core",)):
    return {"anchor": anchor, "entries": list(entries or [anchor]), "modules": [module], "slices": list(slices),
            "body_bytes": 10}


def recorded(number, key, batch=1, fingerprint="f" * 64):
    """A recorded issue carrying every ISSUE_FIELDS key."""
    kind = "kind:test" if key.endswith("-T") else "kind:implementation"
    return {"id": number * 10, "number": number, "url": f"https://github.com/o/r/issues/{number}",
            "fingerprint": fingerprint, "contract": "5:" + "0" * 64, "batch": batch,
            "labels": ["mu-spec:" + key, kind, "slice:core"]}


def creates(fake):
    return [body for method, path, body in fake.sends if (method, path) == ("POST", ISSUES)]


def block_span(body):
    """(start, end) of the json text of the one code block after the last machine heading."""
    from mu_spec.ship.plan import MACHINE_HEADING

    start = body.rindex(MACHINE_HEADING) + len(MACHINE_HEADING)
    found = list(re.finditer(r"```json[^\n]*\n(.*?)```", body[start:], re.DOTALL))
    assert len(found) == 1, f"expected one json code block after the machine heading, got {len(found)}"
    return start + found[0].start(1), start + found[0].end(1)


def machine_block(body):
    start, end = block_span(body)
    return json.loads(body[start:end])


def with_block(body, **changes):
    """body with its machine block's fields changed."""
    start, end = block_span(body)
    block = {**json.loads(body[start:end]), **changes}
    return body[:start] + json.dumps(block, indent=2, ensure_ascii=False) + "\n" + body[end:]


def test_refusals_before_requests():
    from mu_spec.ship.plan import RUN_REASONS, render_value

    key = "S-396 (was S-04)"
    base = {"project": "p", "kind": "person_ship", "repo": "o/r", "token": "t",
            "cut": {"seq": 1, "members": [key], "units": {key: unit_of(key, "a.py")}, "edges": {key: []}},
            "cut_waves": ((key,),), "next_seq": 1, "lineage_keyed": True}

    def refused(script=None, **changes):
        fake = FakeGitHub(script=script)
        out = press(ship_inputs(**{**base, **changes}), fake)
        assert out.result["emitted"] is False
        assert out.lines == ()
        return out, fake.sends

    # 1. no repo (and no token): the repo is checked first, nothing is sent
    out, sends = refused(repo=None, token=None)
    assert out.result == {"emitted": False, "project": "p", "repo": None, "reason":
                          "no repo is configured for this project, so there is nowhere to create issues. Set one first."}
    assert list(out.result) == ["emitted", "project", "repo", "reason"]
    assert sends == []

    # 2. no token: refused before the push check
    out, sends = refused(token=None)
    assert out.result["reason"] == RUN_REASONS["no_token"]
    assert sends == []

    # 3. no cut
    out, sends = refused(cut=None, cut_waves=())
    assert out.result["reason"] == "no cut has been taken, so no work has been decided to go out. Take a cut first."
    assert sends == []

    # 4. drift since the cut, attached to the result
    out, sends = refused(drift={"since": 1})
    assert out.result["reason"] == (
        "the graph has moved since cut 1, so emitting it would ship work units that are no longer what the "
        "project says. Take a fresh cut, or undo the change.")
    assert out.result["drift"] == {"since": 1}
    assert sends == []

    # 5. bad waves, rendered with render_value
    prefix = "'waves' must be a whole number of at least 1, or \"shipped\" for only the waves already out, not "
    out, sends = refused(waves=0)
    assert out.result["reason"] == prefix + "0"
    assert sends == []
    out, sends = refused(waves="0")
    assert out.result["reason"] == prefix + "'0'"
    assert sends == []

    # 6. a unit that is not a key
    out, sends = refused(unit=5)
    assert out.result["reason"] == "'unit' must be one work unit's key, not 5"
    assert sends == []

    # 7. no push access: exactly the one GET of the repository
    out, sends = refused(script={("GET", REPO): (200, {"permissions": {"push": False}})})
    assert sends == [("GET", REPO, None)]
    assert out.result["reason"] == (
        "the token has no push access to o/r, so GitHub would create every issue and silently drop its labels "
        "-- the consumer admits by label and would ingest nothing. Use a token with push access.")

    # 8. a unit not in the cut, decided after the push check
    out, sends = refused(unit="S-369 (was S-09)")
    assert sends == [("GET", REPO, None)]
    assert out.result["reason"] == "S-369 (was S-09) is not a work unit of cut 1, so there is nothing to ship for it"

    # 9. render_value is repr of the decoded value
    assert render_value(0) == "0"
    assert render_value("0") == "'0'"
    assert render_value("x") == "'x'"


def adoption_case(kind):
    from mu_spec.ship.plan import body_of

    units = {k: unit_of("S-" + k[1], k + ".py") for k in ("u1", "u2", "u3")}
    inputs = ship_inputs(kind=kind, cut={"seq": 2, "members": ["u1", "u2", "u3"], "units": units,
                                         "edges": {"u1": [], "u2": [], "u3": []}},
                         cut_waves=(("u1", "u2", "u3"),), lineage_keyed=False, next_seq=5)
    seven = gh_issue(7, ["mu-spec:u2", "kind:implementation"], body=body_of("u2", units["u2"], inputs))
    eight = gh_issue(8, ["mu-spec:u3"], body=body_of("u3", units["u3"], inputs),
                     pull_request={"url": "https://api.github.com/repos/o/r/pulls/8"})
    fake = FakeGitHub(issues=[seven, eight], script={("GET", LISTING): (200, [seven, eight])})
    return units, inputs, fake, press(inputs, fake)


def test_adopt_then_create_and_delegated():
    from mu_spec.ship.plan import ISSUE_FIELDS, body_of, labels_of, title_of

    # 1. the person's Ship: push check, one listing page, u2 adopted, u1 then u3 created (#8 is a pull request)
    units, inputs, fake, out = adoption_case("person_ship")
    assert fake.sends[0] == ("GET", REPO, None)
    assert fake.sends[1] == ("GET", LISTING, None)
    assert not any("page=2" in path for _, path, _ in fake.sends)
    assert creates(fake) == [
        {"title": title_of(k, units[k]), "body": body_of(k, units[k], inputs), "labels": labels_of(k, units[k], inputs)}
        for k in ("u1", "u3")
    ]
    assert all(body["title"] != title_of("u2", units["u2"]) for body in creates(fake))
    assert out.result["emitted"] is True
    assert [(a["key"], a["number"]) for a in out.result["adopted"]] == [("u2", 7)]
    assert out.result["batch"] == 5

    # 2. one Ship line recording adopted and created issues
    assert len(out.lines) == 1
    line = out.lines[0]
    assert list(line) == ["seq", "cut_seq", "repo", "issues", "wired"]
    assert line["seq"] == 5
    assert line["cut_seq"] == 2
    assert line["repo"] == "o/r"
    assert line["issues"]["u1"]["number"] == 20
    assert line["issues"]["u3"]["number"] == 21
    assert line["issues"]["u2"]["number"] == 7
    assert line["issues"]["u1"]["batch"] == 5
    for key, issue in line["issues"].items():
        assert list(issue)[:len(ISSUE_FIELDS)] == list(ISSUE_FIELDS), key

    # 3. a delegated run adopts the same way and creates nothing
    units, inputs, fake, out = adoption_case("delegated_ship")
    assert fake.sends[0] == ("GET", REPO, None)
    assert fake.sends[1] == ("GET", LISTING, None)
    assert creates(fake) == []
    issues = out.lines[0]["issues"]
    assert "u2" in issues
    assert "u1" not in issues
    assert "u3" not in issues


def two_unit_cut():
    units = {k: unit_of("S-" + k[1], k + ".py") for k in ("u1", "u2")}
    return {"seq": 2, "members": ["u2", "u1"], "units": units, "edges": {"u1": ["u2"], "u2": []}}


def test_failures_and_reasons():
    from mu_spec.ship.plan import order_text, unanswered_reason

    # 1. the k-th create refused: listed as failed, no number, and the run goes on
    units = {k: unit_of("S-" + k[1], k + ".py") for k in ("u1", "u2", "u3")}
    cut = {"seq": 2, "members": ["u1", "u2", "u3"], "units": units, "edges": {"u1": [], "u2": [], "u3": []}}
    for k in (1, 2, 3):
        fake = FakeGitHub(refuse={k})
        out = press(ship_inputs(cut=cut, cut_waves=(("u1", "u2", "u3"),), next_seq=5), fake)
        refused = "u" + str(k)
        assert out.result["failed"] == [{"key": refused, "reason": "GitHub answered 422: Validation Failed"}], k
        assert len(creates(fake)) == 3, k
        assert set(out.lines[0]["issues"]) == {"u1", "u2", "u3"} - {refused}, k

    # 2. the one order refused or unanswered in each way, or already in place
    order_path = ISSUES + "/21/dependencies/blocked_by"
    order = order_text("u1", "u2")
    assert order == "u1<-u2"
    outcomes = {
        "a": ((500, {"message": "boom"}), "GitHub answered 500: boom"),
        "b": (unreachable("connection refused", False), "GitHub was unreachable: connection refused"),
        "c": (unreachable("read timed out", True),
              "GitHub did not answer in time (read timed out); the write may have landed"),
        "d": ((422, {"message": "Validation Failed: issue has already been taken"}), None),
    }
    lines = {}
    for case, (outcome, reason) in outcomes.items():
        fake = FakeGitHub(script={("POST", order_path): outcome})
        out = press(ship_inputs(cut=two_unit_cut(), cut_waves=(("u2",), ("u1",)), next_seq=5), fake)
        assert creates(fake)[0]["title"].startswith("u2"), case
        assert ("POST", order_path, {"issue_id": 200}) in fake.sends, case
        line = out.lines[0]
        assert line["issues"]["u2"]["number"] == 20, case
        assert line["issues"]["u1"]["number"] == 21, case
        if reason is None:
            assert out.result["unwired"] == [], case
            assert order in line["wired"], case
        else:
            assert out.result["unwired"] == [{"blocked": "u1", "blocker": "u2", "reason": reason}], case
            assert order not in line["wired"], case
        lines[case] = (line, fake)

    # 3. the next Ship sends again exactly the order not recorded as in place
    for case in ("a", "d"):
        line, first = lines[case]
        links = {21: [200]} if case == "d" else {}
        fake = FakeGitHub(issues=first.issues.values(), links=links)
        press(ship_inputs(cut=two_unit_cut(), cut_waves=(("u2",), ("u1",)), issues=dict(line["issues"]),
                          orders=tuple(line["wired"]), next_seq=line["seq"] + 1), fake)
        resent = [s for s in fake.sends if s[:2] == ("POST", order_path)]
        if case == "a":
            assert resent == [("POST", order_path, {"issue_id": 200})]
        else:
            assert resent == []

    # 4. the unanswered reason depends on timed_out and the method
    assert unanswered_reason(unreachable("read timed out", True), "GET") == "GitHub did not answer in time: read timed out"
    assert unanswered_reason(unreachable("read timed out", True), "PATCH") == (
        "GitHub did not answer in time (read timed out); the write may have landed")
    assert unanswered_reason(unreachable("read timed out", False), "POST") == "GitHub was unreachable: read timed out"

    # 5. a blocker whose create was refused leaves the order undeclared
    fake = FakeGitHub(refuse={1})
    out = press(ship_inputs(cut=two_unit_cut(), cut_waves=(("u2",), ("u1",)), next_seq=5), fake)
    assert {"blocked": "u1", "blocker": "u2", "reason": "u2 has no issue, so this order could not be declared"} in (
        out.result["unwired"])


def test_markers_and_adoptable():
    from mu_spec.ship.plan import (
        MACHINE_FIELDS,
        TITLE_LIMIT,
        adoptable,
        body_of,
        is_test,
        labels_of,
        title_of,
    )

    unit = unit_of("S-338 (was S-05)", "a.py", slices=("beta", "alpha"))
    cut = {"seq": 2, "members": ["k1"], "units": {"k1": unit}, "edges": {"k1": []}}
    inputs = ship_inputs(labels=("team", "later"), next_seq=3, cut=cut, cut_waves=(("k1",),))

    # 1. labels: key, kind by suffix, slices in stored order, then the project's labels
    assert labels_of("k1", unit, inputs) == [
        "mu-spec:k1", "kind:implementation", "slice:beta", "slice:alpha", "team", "later"]
    assert labels_of("S-4-T", unit, inputs)[1] == "kind:test"
    assert is_test("S-4-T") is True
    assert is_test("S-396 (was S-04)") is False

    # 2. the machine block: its fields in order, and the project's name only there
    body = body_of("k1", unit, inputs)
    block = machine_block(body)
    assert list(block) == list(MACHINE_FIELDS)
    assert block["key"] == "k1"
    assert block["project"] == "p"
    assert block["anchor"] == "S-338 (was S-05)"
    assert block["repo"] == "o/r"
    assert block["cut_seq"] == 2
    assert block["batch"] == 3
    assert block["slices"] == ["beta", "alpha"]
    assert block["write_set"] == ["a.py"]
    assert block["blocked_by"] == []
    start, end = block_span(body)
    assert not re.search(r"\bp\b", body[:start] + body[end:]), "the project's name appears outside the machine block"

    # 3. titles
    assert title_of("k1", unit).startswith("k1 — a.py")
    assert TITLE_LIMIT == 120
    assert len(title_of("k" * 130, unit)) == TITLE_LIMIT

    # 4. an edit removes the labels this unit no longer carries, each name quoted whole
    beta = unit_of("S-338 (was S-05)", "a.py", slices=("beta",))
    stale_labels = ["mu-spec:k1", "kind:implementation", "slice:a b", "slice:scope/x"]
    issues = {"k1": {"id": 70, "number": 7, "url": "https://github.com/o/r/issues/7", "fingerprint": "stale",
                     "contract": "stale", "batch": 1, "labels": list(stale_labels)}}
    edit = ship_inputs(labels=("team", "later"), next_seq=3, issues=issues, cut_waves=(("k1",),),
                       cut={"seq": 2, "members": ["k1"], "units": {"k1": beta}, "edges": {"k1": []}})
    fake = FakeGitHub(issues=[gh_issue(7, stale_labels, title="k1 — old", body="old body")])
    press(edit, fake)
    sent = [(method, path) for method, path, _ in fake.sends]
    assert ("DELETE", ISSUES + "/7/labels/slice%3Aa%20b") in sent
    assert ("DELETE", ISSUES + "/7/labels/slice%3Ascope%2Fx") in sent
    added = [body for method, path, body in fake.sends if (method, path) == ("POST", ISSUES + "/7/labels")]
    assert added and "slice:beta" in added[0]["labels"]

    # 5. adoption needs a non-PR issue with a key label whose block names this project and key
    issue = {"number": 9, "labels": [{"name": "mu-spec:k1"}], "body": body}
    assert adoptable(issue, "p", "k1") is True
    assert adoptable({**issue, "pull_request": {"url": "u"}}, "p", "k1") is False
    assert adoptable({**issue, "labels": []}, "p", "k1") is False
    assert adoptable({**issue, "body": with_block(body, project="other")}, "p", "k1") is False
    assert adoptable({**issue, "body": with_block(body, key="k2")}, "p", "k1") is False


def rollback_fake(issues=None, script=None):
    seeded = issues if issues is not None else [
        gh_issue(10), gh_issue(9), gh_issue(8), gh_issue(5, state="closed", state_reason="completed"),
        gh_issue(3, ["claimed"])]
    base = {("PATCH", ISSUES + "/9"): (403, {"message": "Forbidden"}),
            ("GET", ISSUES + "/8"): unreachable("x", False)}
    return FakeGitHub(issues=seeded, script={**base, **(script or {})})


ROLLBACK_ISSUES = {"k3": recorded(3, "k3", 1), "k5": recorded(5, "k5", 1), "k8": recorded(8, "k8", 4),
                   "k9": recorded(9, "k9", 4), "k10": recorded(10, "k10", 4)}


def test_rollback_plan():
    from mu_spec.ship.plan import RUN_REASONS, UnknownProject

    # 1. every live issue, highest number first; no listing and no push check
    fake = rollback_fake()
    out = roll_back(rollback_inputs(issues=ROLLBACK_ISSUES), fake)
    assert fake.sends == [
        ("GET", ISSUES + "/10", None), ("PATCH", ISSUES + "/10", CLOSE),
        ("GET", ISSUES + "/9", None), ("PATCH", ISSUES + "/9", CLOSE),
        ("GET", ISSUES + "/8", None), ("GET", ISSUES + "/5", None), ("GET", ISSUES + "/3", None),
    ]

    # 2. the result
    result = out.result
    assert list(result) == ["rolled_back", "project", "repo", "cut_seq", "closed", "failed", "skipped", "counts"]
    assert result["rolled_back"] is True
    assert result["cut_seq"] == 2
    assert result["closed"] == [{"key": "k10", "number": 10}]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["key"] == "k9"
    assert result["failed"][0]["number"] == 9
    assert result["failed"][0]["reason"]
    skipped = {s["key"]: s["reason"] for s in result["skipped"]}
    assert [(s["key"], s["number"]) for s in result["skipped"]] == [("k8", 8), ("k5", 5), ("k3", 3)]
    assert skipped["k3"] == "picked up (claimed) -- never edited after pickup"
    assert skipped["k5"] == "closed as completed by someone else -- left alone"
    assert skipped["k8"].startswith("could not read the issue's state, so it was left untouched: ")
    assert result["counts"] == {"targeted": 5, "closed": 1, "failed": 1, "skipped": 3}

    # 3. one rollback line
    assert len(out.lines) == 1
    line = out.lines[0]
    assert list(line) == ["kind", "seq", "cut_seq", "repo", "undone", "closed", "failed"]
    assert line["kind"] == "rollback"
    assert line["seq"] == 6
    assert line["closed"] == {"k10": 10}
    assert line["failed"] == result["failed"]

    # 4. an issue already closed not_planned is closed again
    fake = rollback_fake([gh_issue(11, state="closed", state_reason="not_planned")])
    roll_back(rollback_inputs(issues={"k11": recorded(11, "k11")}), fake)
    assert ("PATCH", ISSUES + "/11", CLOSE) in fake.sends

    # 5. an unknown project raises before any request
    fake = rollback_fake()
    with pytest.raises(UnknownProject) as raised:
        roll_back(rollback_inputs(known=False, issues=ROLLBACK_ISSUES), fake)
    assert str(raised.value) == "'p'"
    assert fake.sends == []

    # 6. refusals, each with no request and no line
    cases = [
        ({"repo": None}, "no repo is configured for this project, so there is nothing to withdraw from."),
        ({"token": None}, RUN_REASONS["rb_no_token"]),
        ({"cut_seq": "x"}, "'x' is not a cut sequence number"),
        ({"latest_cut_seq": None, "cut_seq": None},
         "no cut has been taken, and no cut_seq was given, so there is nothing to name as the batch to withdraw."),
        ({"issues": {}}, "nothing to withdraw: no issue on o/r is still recorded as open by this unit"),
    ]
    for changes, reason in cases:
        fake = rollback_fake()
        out = roll_back(rollback_inputs(**{"issues": ROLLBACK_ISSUES, **changes}), fake)
        assert out.result["rolled_back"] is False, changes
        assert out.result["reason"] == reason, changes
        assert fake.sends == [], changes
        assert out.lines == (), changes
        if changes == {"issues": {}}:
            assert list(out.result) == ["rolled_back", "reason", "repo", "cut_seq", "project"]

    # 7. a cut_seq given as text or number is accepted and recorded as given
    for given in ("5", 5):
        out = roll_back(rollback_inputs(issues=ROLLBACK_ISSUES, cut_seq=given), rollback_fake())
        assert out.result["rolled_back"] is True, given
        assert out.result["cut_seq"] == given
        assert type(out.result["cut_seq"]) is type(given)


def I(n):  # the contract's name for a recorded issue numbered n
    return {"id": n * 10, "number": n, "url": "u" + str(n), "fingerprint": "f", "contract": "c", "batch": 1,
            "labels": []}


def ship_log():
    return [
        {"seq": 1, "at": 1.0, "cut_seq": 1, "repo": "o/r", "issues": {"a": I(41), "b": I(42)}, "wired": ["a<-b"]},
        {"seq": 2, "at": 2.0, "cut_seq": 1, "repo": "o/r", "issues": {"c": I(41), "b": I(42)}, "wired": []},
        {"kind": "rollback", "seq": 3, "at": 3.0, "cut_seq": 1, "repo": "o/r", "undone": [2], "closed": {"b": 42},
         "failed": []},
        {"seq": 4, "at": 4.0, "cut_seq": 1, "repo": "x/y", "issues": {"d": I(43)}, "wired": []},
        {"seq": 5, "at": 5.0, "cut_seq": 2, "repo": "o/r", "issues": {"b": I(44), "c": I(41)}, "wired": ["c<-b"]},
    ]


def test_ship_log_reading():
    from mu_spec.ship.plan import (
        ORDER_SEP,
        SHIP_SEQ_START,
        latest_wired,
        lineage_keyed,
        live_issues,
        next_seq,
        order_text,
    )

    log = ship_log()
    rollback = {"kind": "rollback", "seq": 6, "repo": "o/r", "undone": [], "closed": {}, "failed": []}

    # 1. withdrawals by number on the same repository
    assert {k: v["number"] for k, v in live_issues(log, "o/r").items()} == {"b": 44, "c": 41}
    assert {k: v["number"] for k, v in live_issues(log[:3], "o/r").items()} == {"c": 41}
    assert {k: v["number"] for k, v in live_issues(log, "x/y").items()} == {"d": 43}
    assert live_issues(log, None) == {}

    # 2. a settled issue and a rollback's failed entry stay live
    settled = {**I(50), "settled": {"number": 50, "fingerprint": "f", "state_reason": "completed", "key": "e"}}
    longer = [*log, {"seq": 6, "repo": "o/r", "issues": {"e": settled}, "wired": []}]
    assert "e" in live_issues(longer, "o/r")
    longer.append({"kind": "rollback", "seq": 7, "repo": "o/r", "undone": [], "closed": {},
                   "failed": [{"key": "e", "number": 50, "reason": "r"}]})
    assert "e" in live_issues(longer, "o/r")

    # 3. the last Ship line's wired
    assert latest_wired([]) == ()
    assert latest_wired(log) == ("c<-b",)
    assert latest_wired([*log, rollback]) == ("c<-b",)

    # 4. lineage-keyed exactly when every Ship line carries keys 'lineage'
    assert lineage_keyed([]) is True
    assert lineage_keyed(log) is False
    keyed = [line if line.get("kind") == "rollback" else {**line, "keys": "lineage"} for line in log]
    assert lineage_keyed(keyed) is True
    keyed[1] = {k: v for k, v in keyed[1].items() if k != "keys"}
    assert lineage_keyed(keyed) is False

    # 5. the next seq counts every line
    assert next_seq([]) == SHIP_SEQ_START
    assert next_seq(log) == 6
    assert next_seq(log[:3]) == 4

    # 6. order text
    assert order_text("S-396 (was S-04)", "S-4-T") == "S-396 (was S-04)<-S-4-T"
    assert ORDER_SEP == "<-"


def lines_inputs(**changes):
    test_key, impl_key = "S-4-T", "S-396 (was S-04)"
    cut = {"seq": 3, "members": [test_key, impl_key],
           "units": {test_key: unit_of(impl_key, "t.py", entries=["T-272 (was T-01)"]),
                     impl_key: unit_of(impl_key, "a.py")},
           "edges": {impl_key: [test_key], test_key: []}}
    return ship_inputs(**{"cut": cut, "cut_waves": ((test_key,), (impl_key,)), "next_seq": 5, **changes})


def test_ship_lines():
    from mu_spec.ship.plan import title_of

    test_key, impl_key = "S-4-T", "S-396 (was S-04)"

    # 1. one Ship line: created units with batch next_seq, and the wired order
    inputs = lines_inputs(lineage_keyed=False)
    fake = FakeGitHub()
    out = press(inputs, fake)
    units = inputs.cut["units"]
    assert [body["title"] for body in creates(fake)] == [title_of(test_key, units[test_key]),
                                                         title_of(impl_key, units[impl_key])]
    assert ("POST", ISSUES + "/21/dependencies/blocked_by", {"issue_id": 200}) in fake.sends
    assert len(out.lines) == 1
    line = out.lines[0]
    assert list(line) == ["seq", "cut_seq", "repo", "issues", "wired"]
    assert line["seq"] == 5
    assert line["cut_seq"] == 3
    assert set(line["issues"]) == {test_key, impl_key}
    assert line["issues"][test_key]["number"] == 20
    assert line["issues"][test_key]["batch"] == 5
    assert line["issues"][impl_key]["number"] == 21
    assert line["issues"][impl_key]["batch"] == 5
    assert line["wired"] == ["S-396 (was S-04)<-S-4-T"]

    # 2. lineage-keyed: 'keys' last
    out = press(lines_inputs(lineage_keyed=True), FakeGitHub())
    line = out.lines[0]
    assert list(line)[-1] == "keys"
    assert line["keys"] == "lineage"

    # 3. units that left the cut are withdrawn, highest number first, before anything is created
    gone = {"S-410 (was S-08)": recorded(15, "S-410 (was S-08)"), "S-369 (was S-09)": recorded(12, "S-369 (was S-09)")}
    fake = FakeGitHub(issues=[gh_issue(15, gone["S-410 (was S-08)"]["labels"]),
                              gh_issue(12, gone["S-369 (was S-09)"]["labels"])])
    out = press(lines_inputs(issues=gone, lineage_keyed=False), fake)
    first_create = next(i for i, s in enumerate(fake.sends) if s[:2] == ("POST", ISSUES))
    withdrawal = [s for s in fake.sends[:first_create] if s[1] in (ISSUES + "/15", ISSUES + "/12")]
    assert withdrawal == [
        ("GET", ISSUES + "/15", None), ("PATCH", ISSUES + "/15", CLOSE), ("GET", ISSUES + "/15", None),
        ("GET", ISSUES + "/12", None), ("PATCH", ISSUES + "/12", CLOSE), ("GET", ISSUES + "/12", None),
    ]
    assert out.result["withdrawn"] == [{"key": "S-410 (was S-08)", "number": 15},
                                       {"key": "S-369 (was S-09)", "number": 12}]
    assert "closed" not in out.result
    assert len(out.lines) == 2
    ship_line, rollback_line = out.lines
    assert "S-410 (was S-08)" not in ship_line["issues"]
    assert "S-369 (was S-09)" not in ship_line["issues"]
    assert rollback_line["kind"] == "rollback"
    assert rollback_line["seq"] == 6
    assert rollback_line["cut_seq"] == 3
    assert rollback_line["repo"] == "o/r"
    assert rollback_line["closed"] == {"S-410 (was S-08)": 15, "S-369 (was S-09)": 12}
    assert rollback_line["failed"] == []
    assert all("at" not in line for line in out.lines)

    # 4. a run ending emitted false stores nothing
    fake = FakeGitHub()
    out = press(lines_inputs(token=None), fake)
    assert out.lines == ()
    assert fake.sends == []
