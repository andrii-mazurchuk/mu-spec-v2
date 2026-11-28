"""Ship's and rollback's fixed request plans, the issue markers, and the Ship log they read.

A plan's requests, their order, its result and the Ship-log lines it gives are a function of the
inputs read at admission, the run's kind and GitHub's answers only: nothing here reads the time,
the store or the environment. Every enumeration is ordered by stored data or by GitHub's answers.
A GitHub refusal, or no answer at all, becomes part of the result and the run goes on; any other
exception propagates to the Ship part, which ends the run 'failed'.

A delegated run differs from the person's in one step only: it never creates an issue for a unit
that has none.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote

from mu_spec.github import GitHubUnreachable
from mu_spec.ship.runs import Run, RunRegistry
from mu_spec.ship.sender import WRITE_METHODS, Answer, Sender

TEST_SUFFIX = "-T"
ORDER_SEP = "<-"
SHIP_LINE_FIELDS = ("seq", "at", "cut_seq", "repo", "issues", "wired", "keys")        # keys only when lineage_keyed; 7
ROLLBACK_LINE_FIELDS = ("kind", "seq", "at", "cut_seq", "repo", "undone", "closed", "failed")   # 8
ISSUE_FIELDS = ("id", "number", "url", "fingerprint", "contract", "batch", "labels")       # always present; 7
ISSUE_OPTIONAL = ("follow_up_of", "retest", "root", "linked", "settled")                  # present only when set; 5
SHIP_SEQ_START = 1      # the seq of the first line of an empty Ship log
FINGERPRINT_LEN = 64    # a worked-out ticket fingerprint: exactly 64 characters of 0-9a-f, lower case, no prefix
CONTRACT_PREFIX = "5:"  # a worked-out contract value: CONTRACT_PREFIX + 64 lower-case hex characters

TITLE_LIMIT = 120
KEY_LABEL_PREFIX = "mu-spec:"
KIND_LABELS = ("kind:implementation", "kind:test")
SLICE_LABEL = "slice:{name}"
MACHINE_HEADING = "## Machine-readable"
MACHINE_FIELDS = ("key", "project", "anchor", "kind", "repo", "cut_seq", "batch", "slices", "write_set", "blocked_by")
FINGERPRINT_EXCLUDES = ("cut_seq", "batch")   # the MACHINE_FIELDS members a ticket fingerprint leaves out; 2
FOLLOW_UP_TITLE = "{title} (follow-up to #{number})"
PAGE = 100
CLOSE_NOT_PLANNED = {"state": "closed", "state_reason": "not_planned"}
ANSWERED = "GitHub answered {status}: {message}"
UNREACHABLE = "GitHub was unreachable: {reason}"
TIMED_OUT = "GitHub did not answer in time: {reason}"
TIMED_OUT_WRITE = "GitHub did not answer in time ({reason}); the write may have landed"
STALE_NOT_REMOVED = "a stale order could not be removed: {answer}"
DROPPED_NOT_REMOVED = "a dropped order could not be removed: {answer}"
NO_ISSUE_ORDER = "{blocker} has no issue, so this order could not be declared"
ALREADY_TAKEN = "already been taken"
UNCHANGED_SKIP = "already emitted as #{number}, unchanged"
TOO_LARGE = "the rendered body is too large for the tracker ({n} characters)"
SHIP_FAILURE_PREFIXES = ("could not be rendered: ", "could not be edited: ", "could not be withdrawn: ",
                         "follow-up to #{number} not created: ", "orphan #{number} could not be closed: ")   # 5
FOLLOW_UP_TOO_LARGE = "follow-up to #{number} is too large for the tracker ({n} characters)"
ROLLBACK_SKIPS = {"pickup": "picked up ({label}) -- never edited after pickup",
                  "foreign": "closed as completed by someone else -- left alone",
                  "unreadable": "could not read the issue's state, so it was left untouched: {detail}"}   # 3

_TOKEN_ENV = "MU_SPEC_GITHUB_TOKEN"   # named by the running unit's no-token texts
RUN_REASONS: Mapping[str, str] = {
    "no_repo": "no repo is configured for this project, so there is nowhere to create issues. Set one first.",
    "no_token": f"no GitHub token: set {_TOKEN_ENV} in this unit's environment. It is unit config rather than "
                "project metadata, because it is not project-specific and a secret does not belong in a file "
                "that is read, diffed and shown on a dashboard.",
    "no_cut": "no cut has been taken, so no work has been decided to go out. Take a cut first.",
    "drift": "the graph has moved since cut {seq}, so emitting it would ship work units that are no longer "
             "what the project says. Take a fresh cut, or undo the change.",
    "bad_waves": "'waves' must be a whole number of at least 1, or \"shipped\" for only the waves already out, "
                 "not {value}",
    "bad_unit": "'unit' must be one work unit's key, not {value}",
    "retired": "a ticket would name a superseded entry with no live successor, so nothing was written: "
               "{refused}. Amend the citing entry, take a cut, and press again.",
    "no_push": "the token has no push access to {repo}, so GitHub would create every issue and silently drop "
               "its labels -- the consumer admits by label and would ingest nothing. Use a token with push "
               "access.",
    "empty_cut": "there are no work units in this cut to emit",
    "not_in_cut": "{key} is not a work unit of cut {seq}, so there is nothing to ship for it",
    "rb_no_repo": "no repo is configured for this project, so there is nothing to withdraw from.",
    "rb_no_token": f"no GitHub token: set {_TOKEN_ENV} in this unit's environment. Closing an issue is a write "
                   "like creating one.",
    "rb_no_cut": "no cut has been taken, and no cut_seq was given, so there is nothing to name as the batch to "
                 "withdraw.",
    "rb_bad_seq": "{value} is not a cut sequence number",
    "rb_nothing": "nothing to withdraw: no issue on {repo} is still recorded as open by this unit",
}

BODY_LIMIT = 65536      # the tracker's body ceiling, as the running unit measures it
_ROLLBACK = "rollback"
_LINEAGE = "lineage"
_WITHDRAWN = "not_planned"
_MERGE = "merge"
_KIND_NAMES = ("implementation", "test")
_AFTER_CONTRACT = "\n## Files you may write"
_MACHINE_NOTE = ("The same facts as above, for a consumer that should not parse prose. "
                 "A heading can be reworded; these field names are a contract.")
_IDENT = re.compile(r"\b[IBAST][\-·]\d+(?:" + re.escape(TEST_SUFFIX) + r")?\b")
_WAS = re.compile(rf"({_IDENT.pattern}) \(was {_IDENT.pattern}\)")
_ENTRY_HEAD = re.compile(r"^### ([IBAST][\-·]\d+) — ", re.MULTILINE)
_WRITE_PATH = re.compile(r"^- `([^`]+)`$", re.MULTILINE)
_FOLLOW_TITLE = re.compile(r"\(follow-up to #(\d+)\)\s*$")
_ALREADY_SUB = re.compile(r"duplicate|already", re.IGNORECASE)


# -- the Ship log ----------------------------------------------------------------------------------

def is_test(key: str) -> bool:
    return key.endswith(TEST_SUFFIX)


def order_text(blocked: str, blocker: str) -> str:
    return blocked + ORDER_SEP + blocker


def _ship_lines(ship_log: Sequence[Mapping]) -> list[Mapping]:
    return [line for line in ship_log if "kind" not in line]


def live_issues(ship_log: Sequence[Mapping], repo: str | None) -> dict[str, Mapping]:
    """Every issue recorded as open on repo: one key per issue number, withdrawals matched by number."""
    live: dict[str, Mapping] = {}
    for line in _ship_lines(ship_log):
        if line.get("repo") != repo:
            continue
        for key, issue in (line.get("issues") or {}).items():
            number = issue.get("number")
            if number is not None:
                for other in [k for k, v in live.items() if k != key and v.get("number") == number]:
                    del live[other]
            live[key] = issue
    closed = {number for line in ship_log
              if line.get("kind") == _ROLLBACK and line.get("repo") == repo
              for number in (line.get("closed") or {}).values()}
    return {key: issue for key, issue in live.items() if issue.get("number") not in closed}


def latest_wired(ship_log: Sequence[Mapping]) -> tuple[str, ...]:
    ships = _ship_lines(ship_log)
    return tuple(ships[-1].get("wired") or ()) if ships else ()


def lineage_keyed(ship_log: Sequence[Mapping]) -> bool:
    return all(line.get("keys") == _LINEAGE for line in _ship_lines(ship_log))


def next_seq(ship_log: Sequence[Mapping]) -> int:
    return int(ship_log[-1]["seq"]) + 1 if ship_log else SHIP_SEQ_START


# -- inputs and outcome ----------------------------------------------------------------------------

@dataclass(frozen=True)
class ShipInputs:
    project: str
    kind: str                         # 'person_ship' or 'delegated_ship'
    repo: str | None
    token: str | None
    cut: Mapping | None               # the resolved cut; None when the cut log is empty
    cut_waves: tuple[tuple[str, ...], ...]
    drift: Mapping | None
    retired_citations: tuple
    labels: tuple[str, ...]
    pickup: tuple[str, ...]
    issues: Mapping[str, Mapping]     # live_issues(Ship log, repo)
    orders: tuple[str, ...]           # latest_wired(Ship log)
    lineage_keyed: bool
    next_seq: int                     # the seq this run's Ship line takes
    waves: object                     # the press's 'waves' as given, or None
    unit: object                      # the press's 'unit' as given, or None


@dataclass(frozen=True)
class RollbackInputs:
    project: str
    known: bool                       # False: the project has no committed record
    repo: str | None
    token: str | None
    latest_cut_seq: int | None
    cut_seq: object                   # as given, or None
    issues: Mapping[str, Mapping]     # live_issues(Ship log, repo)
    pickup: tuple[str, ...]
    next_seq: int


@dataclass(frozen=True)
class PlanOutcome:
    result: dict
    lines: tuple[dict, ...]           # the Ship-log lines to append, in order, each without 'at'


class UnknownProject(Exception):
    def __init__(self, project: str) -> None:
        super().__init__("'" + project + "'")
        self.project = project


# -- markers ---------------------------------------------------------------------------------------

def _cut_title(text: str) -> str:
    return text if len(text) <= TITLE_LIMIT else text[:TITLE_LIMIT - 1] + "…"


def title_of(key: str, unit: Mapping) -> str:
    modules = list(unit.get("modules") or ())
    if not modules:
        return _cut_title(key)
    head = f"{key} — {modules[0]}"
    if len(modules) == 1:
        return _cut_title(head)
    joined = f"{key} — {', '.join(modules)}"
    if len(joined) <= TITLE_LIMIT:
        return joined
    return _cut_title(f"{head} (+{len(modules) - 1} more)")


def labels_of(key: str, unit: Mapping, inputs: ShipInputs) -> list[str]:
    labels = [KEY_LABEL_PREFIX + key, KIND_LABELS[1] if is_test(key) else KIND_LABELS[0]]
    labels += [SLICE_LABEL.format(name=name) for name in unit.get("slices") or ()]
    return labels + list(inputs.labels)


def _machine(key: str, unit: Mapping, inputs: ShipInputs) -> dict:
    cut = inputs.cut or {}
    values = {
        "key": key,
        "project": inputs.project,
        "anchor": unit.get("anchor"),
        "kind": _KIND_NAMES[1] if is_test(key) else _KIND_NAMES[0],
        "repo": inputs.repo,
        "cut_seq": cut.get("seq"),
        "batch": inputs.next_seq,
        "slices": list(unit.get("slices") or ()),
        "write_set": list(unit.get("modules") or ()),
        "blocked_by": list((cut.get("edges") or {}).get(key) or ()),
    }
    return {name: values[name] for name in MACHINE_FIELDS}


def _prose(key: str, unit: Mapping, inputs: ShipInputs) -> str:
    """The body before its machine heading."""
    out = ["## Contract"]
    out += [f"### {entry}" for entry in unit.get("entries") or ()]
    out.append("## Files you may write")
    modules = list(unit.get("modules") or ())
    if modules:
        out.append("\n".join(f"- `{path}`" for path in modules))
    else:
        out.append("*Nothing is declared yet.* No module claims this contract, so there is no write set "
                   "-- declare one before building.")
    blockers = list(((inputs.cut or {}).get("edges") or {}).get(key) or ())
    if blockers:
        out.append("## Blocked by")
        out.append("\n".join(f"- `{b}`" for b in blockers))
    return "\n\n".join(out)


def body_of(key: str, unit: Mapping, inputs: ShipInputs) -> str:
    block = json.dumps(_machine(key, unit, inputs), indent=2, ensure_ascii=False)
    return "\n\n".join([_prose(key, unit, inputs), MACHINE_HEADING, _MACHINE_NOTE,
                        "```json\n" + block + "\n```"])


def _fingerprint(key: str, unit: Mapping, inputs: ShipInputs) -> str:
    """The ticket fingerprint: title, prose, the machine block without cut_seq and batch, sorted labels."""
    stable = {k: v for k, v in _machine(key, unit, inputs).items() if k not in FINGERPRINT_EXCLUDES}
    parts = [title_of(key, unit), _prose(key, unit, inputs), stable, sorted(labels_of(key, unit, inputs))]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _machine_block(body: str) -> dict:
    try:
        block = json.loads((body or "").split("```json")[-1].split("```")[0])
    except (ValueError, IndexError):
        return {}
    return block if isinstance(block, dict) else {}


def _names(labels: object) -> list[str]:
    return [str(label.get("name") if isinstance(label, Mapping) else label) for label in labels or ()]  # type: ignore[attr-defined]


def adoptable(issue: Mapping, project: str, key: str) -> bool:
    if "pull_request" in issue:
        return False
    if not any(name.startswith(KEY_LABEL_PREFIX) for name in _names(issue.get("labels"))):
        return False
    block = _machine_block(str(issue.get("body") or ""))
    return block.get("project") == project and block.get("key") == key


# -- the contract value ----------------------------------------------------------------------------

def _digest(section: str) -> str:
    section = _WAS.sub(r"\1", section)
    return hashlib.sha256(" ".join(_IDENT.sub("#", section).split()).encode()).hexdigest()


def _entry_digests(body: str) -> list[str]:
    text = (body or "").replace("\r\n", "\n")
    _, found, rest = text.partition("## Contract")
    section = rest.split(_AFTER_CONTRACT, 1)[0] if found else ""
    parts = _ENTRY_HEAD.split(section)
    return [_digest(parts[0])] + sorted(_digest(parts[i + 1]) for i in range(1, len(parts) - 1, 2))


def _write_paths(body: str) -> list[str]:
    text = (body or "").replace("\r\n", "\n")
    _, found, rest = text.partition(_AFTER_CONTRACT)
    return sorted(_WRITE_PATH.findall(rest.split("\n## ", 1)[0])) if found else []


def _contract_of(body: str) -> str:
    return CONTRACT_PREFIX + hashlib.sha256("\n".join(_entry_digests(body) + _write_paths(body)).encode()).hexdigest()


def _contract_moved(recorded: object, issue_body: str, ticket_body: str) -> bool:
    if isinstance(recorded, str) and recorded.startswith(CONTRACT_PREFIX):
        return recorded != _contract_of(ticket_body)
    return not issue_body or _contract_of(issue_body) != _contract_of(ticket_body)


# -- answers ---------------------------------------------------------------------------------------

def unanswered_reason(e: GitHubUnreachable, method: str) -> str:
    if not e.timed_out:
        return UNREACHABLE.format(reason=e.reason)
    if method.upper() in WRITE_METHODS:
        return TIMED_OUT_WRITE.format(reason=e.reason)
    return TIMED_OUT.format(reason=e.reason)


def render_value(value: object) -> str:
    return repr(value)


def _message(body: object) -> str:
    if not isinstance(body, Mapping):
        return "" if body is None else str(body)[:400]
    parts = [str(body.get("message", ""))]
    for error in body.get("errors") or []:
        if isinstance(error, Mapping):
            parts.append(str(error.get("message") or error.get("code") or error))
    return " | ".join(p for p in parts if p)[:400]


def _ok(answer: Answer) -> bool:
    return 200 <= answer.status < 300


def _answered(answer: Answer) -> str:
    return ANSWERED.format(status=answer.status, message=_message(answer.body))


class _Caller:
    """One sender.send per request, reporting step and counters to the run after each."""

    def __init__(self, sender: Sender, run: Run, runs: RunRegistry, counters: Callable[[], dict]) -> None:
        self._sender = sender
        self._run = run
        self._runs = runs
        self._counters = counters
        self.step: str | None = None

    def tick(self) -> None:
        self._runs.report(self._run, self.step, **self._counters())

    def send(self, method: str, path: str, body: Mapping | None = None) -> tuple[Answer | None, str | None]:
        """(answer, problem): problem is None for a 2xx, else the reason text; answer None when unanswered."""
        try:
            answer = self._sender.send(method, path, body)
        except GitHubUnreachable as e:
            self.tick()
            return None, unanswered_reason(e, method)
        self.tick()
        return answer, (None if _ok(answer) else _answered(answer))

    def pages(self, path: str) -> list[dict] | None:
        """Every item of a paged listing, or None when a page was not answered 2xx."""
        out: list[dict] = []
        page = 1
        while True:
            answer, problem = self.send("GET", f"{path}per_page={PAGE}&page={page}")
            if problem is not None or answer is None:
                return None
            items = answer.body if isinstance(answer.body, list) else []
            out.extend(item for item in items if isinstance(item, dict))
            if len(items) < PAGE:
                return out
            page += 1


# -- recorded issues -------------------------------------------------------------------------------

def _recorded(issue: Mapping) -> dict:
    """A recorded issue with ISSUE_FIELDS first, then the ISSUE_OPTIONAL members set, then anything else."""
    out = {name: issue.get(name) for name in ISSUE_FIELDS}
    out.update({name: issue[name] for name in ISSUE_OPTIONAL if name in issue and issue[name] not in (None, False)})
    out.update({k: v for k, v in issue.items() if k not in out and k not in ISSUE_OPTIONAL})
    return out


def _number(issue: Mapping | None) -> int:
    try:
        return int((issue or {}).get("number") or 0)
    except (TypeError, ValueError):
        return 0


# -- the merge orders, derived on the press --------------------------------------------------------

def _merge_orders(edges: Mapping, issues: Mapping[str, Mapping],
                  open_now: Callable[[str, Mapping], bool]) -> dict[str, tuple[str, ...]]:
    """Unit key -> keys that must merge first: a test follow-up and an implementation follow-up of a
    unit its implementation waits on, transitively, both open; the one opened later merges first."""
    follow = {k: _number(v) for k, v in issues.items()
              if v.get("follow_up_of") and _number(v) and open_now(k, v)}
    out: dict[str, list[str]] = {}
    for test in sorted(k for k in follow if is_test(k)):
        seen: set[str] = set()
        stack = [test[:-len(TEST_SUFFIX)]]
        while stack:
            for blocker in edges.get(stack.pop(), ()) or ():
                if blocker not in seen:
                    seen.add(blocker)
                    stack.append(blocker)
        for impl in sorted(k for k in seen if k in follow and not is_test(k)):
            blocked, first = (impl, test) if follow[test] > follow[impl] else (test, impl)
            out.setdefault(blocked, []).append(first)
    return {k: tuple(v) for k, v in out.items()}


def _cycles(graph: Mapping[str, Sequence[str]]) -> list[list[str]]:
    """The strongly connected groups that loop, each sorted, in sorted order."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    groups: list[list[str]] = []

    def visit(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on.add(node)
        for nxt in graph.get(node, ()) or ():
            if nxt not in index:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            group = []
            while True:
                top = stack.pop()
                on.discard(top)
                group.append(top)
                if top == node:
                    break
            if len(group) > 1 or node in (graph.get(node) or ()):
                groups.append(sorted(group))

    for node in sorted(graph):
        if node not in index:
            visit(node)
    return sorted(groups)


def _route(graph: Mapping[str, Sequence[str]], group: list[str]) -> list[str]:
    """A loop inside group, as [a, ..., a], from its first member."""
    inside = set(group)
    start = group[0]
    path = [start]
    seen = {start}
    node = start
    while True:
        nexts = [n for n in graph.get(node, ()) or () if n in inside]
        if start in nexts:
            return path + [start]
        step = next((n for n in nexts if n not in seen), None)
        if step is None:
            return path + [start]
        path.append(step)
        seen.add(step)
        node = step


def _prune_merge_cycles(edges: Mapping, merge: Mapping) -> tuple[dict[str, tuple[str, ...]], list[dict]]:
    """The merge orders with every loop they close taken out, and the dropped orders reported."""
    kept = {k: list(v) for k, v in merge.items()}
    dropped: list[dict] = []
    while True:
        joined = {k: tuple(edges.get(k, ()) or ()) + tuple(kept.get(k, ())) for k in set(edges) | set(kept)}
        groups = [g for g in _cycles(joined) if any(b in g for k in g for b in kept.get(k, ()))]
        if not groups:
            break
        for group in groups:
            inside = set(group)
            orders = [{"blocked": k, "blocker": b} for k in sorted(inside) for b in kept.get(k, ()) if b in inside]
            dropped.append({"loop": _route(joined, group), "orders": orders})
            for o in orders:
                kept[o["blocked"]].remove(o["blocker"])
    return {k: tuple(v) for k, v in kept.items() if v}, dropped


def _shipped_horizon(order: Sequence[str], edges: Mapping, live: Mapping) -> set[str]:
    """The units with no issue a 'shipped' press creates: what a live issue waits on, transitively, an
    implementation whose own tests are out, and a new leaf one step past the frontier."""
    if not live:
        return set()
    missing = [k for k in order if k not in live]
    mates = [k for k in missing if not is_test(k) and k + TEST_SUFFIX in live]
    allowed, stack = set(mates), [k for k in order if k in live] + mates
    while stack:
        for blocker in edges.get(stack.pop(), ()) or ():
            if blocker not in allowed:
                allowed.add(blocker)
                stack.append(blocker)
    waited: dict[str, set[str]] = {}
    for key in order:
        for blocker in edges.get(key, ()) or ():
            waited.setdefault(blocker, set()).add(key)
    out = {k for k in missing if k in allowed}
    for leaf in missing:
        if leaf in waited or leaf in out:
            continue
        group = {leaf}
        mate = leaf + TEST_SUFFIX
        if not is_test(leaf) and mate in missing and waited.get(mate) == {leaf}:
            group.add(mate)
        if all(b in live or b in out or b in group for m in group for b in edges.get(m, ()) or ()):
            out |= group
    return out


# -- Ship ------------------------------------------------------------------------------------------

def _refused(inputs: ShipInputs, reason: str, **extra: object) -> PlanOutcome:
    return PlanOutcome({"emitted": False, "project": inputs.project, "repo": inputs.repo, "reason": reason,
                        **extra}, ())


def _horizon(waves: object) -> int | str | None:
    """The press's 'waves' read as the running unit reads it; -1 when it does not parse."""
    if waves is None or waves == "shipped":
        return waves  # type: ignore[return-value]
    try:
        return int(waves)  # type: ignore[call-overload, no-any-return]
    except (TypeError, ValueError):
        return -1


def ship_plan(inputs: ShipInputs, sender: Sender, run: Run, runs: RunRegistry) -> PlanOutcome:
    if not inputs.repo:
        return _refused(inputs, RUN_REASONS["no_repo"])
    if not inputs.token:
        return _refused(inputs, RUN_REASONS["no_token"])
    cut = inputs.cut
    if cut is None:
        return _refused(inputs, RUN_REASONS["no_cut"])
    cut_seq = cut.get("seq")
    if inputs.drift:
        return _refused(inputs, RUN_REASONS["drift"].format(seq=cut_seq), drift=inputs.drift)
    horizon = _horizon(inputs.waves)
    if isinstance(horizon, int) and horizon < 1:
        return _refused(inputs, RUN_REASONS["bad_waves"].format(value=render_value(inputs.waves)))
    only = inputs.unit
    if only is not None and (not isinstance(only, str) or not only.strip()):
        return _refused(inputs, RUN_REASONS["bad_unit"].format(value=render_value(only)))
    only = only.strip() if isinstance(only, str) else None
    if inputs.retired_citations:
        refused = "; ".join(f"{r.get('key')} cites {r.get('retired')} (in {r.get('in')})"
                            for r in inputs.retired_citations)
        return _refused(inputs, RUN_REASONS["retired"].format(refused=refused))
    return _Ship(inputs, sender, run, runs, horizon, only).press()


class _Ship:
    """One Ship press past its sendless checks."""

    def __init__(self, inputs: ShipInputs, sender: Sender, run: Run, runs: RunRegistry,
                 horizon: int | str | None, only: str | None) -> None:
        self.inputs = inputs
        self.repo = str(inputs.repo)
        self.base = f"/repos/{self.repo}"
        self.cut: Mapping = inputs.cut or {}
        self.units: Mapping = self.cut.get("units") or {}
        self.edges: dict[str, tuple[str, ...]] = {
            k: tuple(v or ()) for k, v in (self.cut.get("edges") or {}).items()}
        order = [k for wave in inputs.cut_waves for k in wave]
        order += [k for k in self.cut.get("members") or () if k not in order]
        self.order = list(dict.fromkeys(order))
        self.horizon: int | str | None = 0 if inputs.kind == "delegated_ship" else horizon
        self.only = only
        self.pickup = tuple(inputs.pickup)
        self.live: dict[str, dict] = {k: dict(v) for k, v in inputs.issues.items()}
        self.issues: dict[str, dict] = {}
        self.lists: dict[str, list] = {name: [] for name in (
            "created", "failed", "unwired", "wired", "edited", "withdrawn", "touched", "settled", "follow_ups",
            "adopted", "swept", "raced", "held", "skipped", "removed", "dead", "linked", "unlinked",
            "unlabelled")}
        self.work: list[str] = []
        self.to_wire = 0
        self.frozen: set[str] = set()
        self.seen: dict[int, dict] = {}
        self.rendered: set[str] = set()
        self.closed: dict[str, int] = {}
        self.ours: dict[str, list[dict]] | None = None
        self.open_labels: dict[int, list[str]] = {}
        self.call = _Caller(sender, run, runs, self.counters)

    # -- progress ---------------------------------------------------------------------------------

    def counters(self) -> dict:
        n = {name: len(items) for name, items in self.lists.items()}
        processed = (n["created"] + n["edited"] + n["skipped"] + n["failed"] + n["touched"] + n["settled"]
                     + n["follow_ups"] + sum(1 for a in self.lists["adopted"] if a.get("follow_up")))
        return {"total": len(self.work), "processed": processed, "created": n["created"], "edited": n["edited"],
                "withdrawn": n["withdrawn"], "touched": n["touched"], "skipped": n["skipped"],
                "failed": n["failed"], "wired": n["wired"], "unwired": n["unwired"], "to_wire": self.to_wire,
                "adopted": n["adopted"], "swept": n["swept"]}

    def at(self, step: str) -> None:
        self.call.step = step
        self.call.tick()

    def in_scope(self, key: str) -> bool:
        return self.only is None or key == self.only

    def issue_path(self, number: object) -> str:
        return f"{self.base}/issues/{int(number)}"  # type: ignore[call-overload]

    # -- reading issues ---------------------------------------------------------------------------

    def read(self, number: int) -> tuple[dict | None, str | None]:
        answer, problem = self.call.send("GET", self.issue_path(number))
        if problem is not None or answer is None or not isinstance(answer.body, dict):
            return None, problem or "GitHub's answer was not an issue"
        self.seen[number] = answer.body
        return answer.body, None

    def look(self, key: str) -> tuple[str | None, dict]:
        """Why this issue is not ours to write (None if it is), and what was read."""
        state, problem = self.read(_number(self.issues[key]))
        if state is None:
            return ROLLBACK_SKIPS["unreadable"].format(detail=problem), {}
        if state.get("state") == "closed":
            return f"closed as {state.get('state_reason') or 'closed'} by someone else -- left alone", state
        held = [label for label in _names(state.get("labels")) if label in self.pickup]
        if held:
            return ROLLBACK_SKIPS["pickup"].format(label=", ".join(held)), state
        return None, state

    def after(self, key: str, what: str) -> None:
        state, _ = self.read(_number(self.issues.get(key) or self.live.get(key)))
        if state is not None and any(label in self.pickup for label in _names(state.get("labels"))):
            self.lists["raced"].append({"key": key, "number": _number(self.issues.get(key) or self.live.get(key)),
                                        "reason": f"{what} while it was being picked up"})

    def still_settled(self, key: str, fingerprint: str) -> str | None:
        mark = self.issues[key].get("settled")
        if not mark or self.ours is None:
            return None
        number = _number(self.issues[key])
        if number in self.open_labels:
            self.issues[key] = {k: v for k, v in self.issues[key].items() if k != "settled"}
            self.live[key] = self.issues[key]
            return None
        if _number(mark) != number or mark.get("fingerprint") != fingerprint:
            return None
        return str(mark.get("state_reason") or "closed")

    def settle(self, key: str, fingerprint: str, state: Mapping) -> None:
        mark = {"number": _number(self.issues[key]), "fingerprint": fingerprint,
                "state_reason": state.get("state_reason") or "", "key": key}
        self.issues[key] = {**self.issues[key], "settled": mark}

    # -- the press --------------------------------------------------------------------------------

    def press(self) -> PlanOutcome:
        inputs = self.inputs
        answer, problem = self.call.send("GET", self.base)
        if problem is None and answer is not None and isinstance(answer.body, dict):
            permissions = answer.body.get("permissions")
            if isinstance(permissions, dict) and "push" in permissions and not permissions["push"]:
                return _refused(inputs, RUN_REASONS["no_push"].format(repo=self.repo))
        if not self.order:
            return _refused(inputs, RUN_REASONS["empty_cut"])
        if self.only is not None and self.only not in self.order:
            return _refused(inputs, RUN_REASONS["not_in_cut"].format(key=self.only, seq=self.cut.get("seq")))

        self.list_open()
        self.adopt()
        listed = set(self.open_labels)
        kept, dropped = _prune_merge_cycles(self.edges, _merge_orders(self.edges, self.live, self.open_in(listed)))
        self.merge_now, self.dropped = kept, dropped
        missing = self.select()
        self.issues = dict(self.live)
        self.work = [k for k in self.order if (k in self.live or k in missing) and self.in_scope(k)]

        wired_before = {p for p in inputs.orders if all(k in self.live for k in p.split(ORDER_SEP, 1))}
        self.withdraw()
        follow = self.edit()
        self.create(follow, missing)
        now_wired = self.wire(wired_before)
        self.sweep()
        if self.ours is not None:
            self.verify((set(self.open_labels) | {int(i["number"]) for i in self.lists["created"]
                                                  + self.lists["follow_ups"]})
                        - set(self.closed.values()) - {int(s["number"]) for s in self.lists["swept"]})
        return PlanOutcome(self.result(), self.lines(now_wired))

    def list_open(self) -> None:
        items = self.call.pages(f"{self.base}/issues?state=open&")
        if items is None:
            return
        ours: dict[str, list[dict]] = {}
        for item in items:
            if "pull_request" in item:
                continue
            labels = _names(item.get("labels"))
            if not any(name.startswith(KEY_LABEL_PREFIX) for name in labels):
                continue
            block = _machine_block(str(item.get("body") or ""))
            if block.get("project") != self.inputs.project or not block.get("key"):
                continue
            ours.setdefault(str(block["key"]), []).append(item)
        for found in ours.values():
            found.sort(key=_number)
        self.ours = ours
        self.open_labels = {_number(i): _names(i.get("labels")) for found in ours.values() for i in found}

    def adopt_record(self, issue: Mapping) -> dict:
        """A record for an issue this unit made but did not record; its fingerprint is unknown."""
        block = _machine_block(str(issue.get("body") or ""))
        record = {"id": issue.get("id"), "number": issue.get("number"),
                  "url": str(issue.get("html_url") or issue.get("url") or ""), "fingerprint": "",
                  "contract": _contract_of(str(issue.get("body") or "")), "batch": block.get("batch"),
                  "labels": [label for label in _names(issue.get("labels")) if label not in self.pickup]}
        follow = _FOLLOW_TITLE.search(str(issue.get("title") or ""))
        if follow:
            record["follow_up_of"] = int(follow.group(1))
        return record

    def adopt(self) -> None:
        for key in self.order:
            if key in self.live or not (self.ours or {}).get(key) or not self.in_scope(key):
                continue
            first = next((i for i in (self.ours or {})[key] if adoptable(i, self.inputs.project, key)), None)
            if first is None:
                continue
            self.live[key] = self.adopt_record(first)
            self.lists["adopted"].append({"key": key, "number": first.get("number"),
                                          "url": self.live[key]["url"]})

    def open_in(self, numbers: set[int]) -> Callable[[str, Mapping], bool]:
        if self.ours is not None:
            return lambda _k, v: _number(v) in numbers
        return lambda _k, v: not v.get("settled")

    def select(self) -> list[str]:
        """The units with no live issue this press may create; the rest are held."""
        missing = [key for key in self.order if key not in self.live]
        if self.only is not None:
            allowed = {self.only}
        elif self.horizon == "shipped":
            allowed = _shipped_horizon(self.order, self.edges, self.live)
        elif isinstance(self.horizon, int):
            chosen = [wave for wave in self.inputs.cut_waves if any(k not in self.live for k in wave)]
            allowed = {k for wave in chosen[:max(0, self.horizon)] for k in wave}
        else:
            return missing
        self.lists["held"] = [key for key in missing if key not in allowed]
        return [key for key in missing if key in allowed]

    def ticket(self, key: str) -> tuple[str, str, list[str], str]:
        unit = self.units[key]
        self.rendered.add(key)
        return (title_of(key, unit), body_of(key, unit, self.inputs), labels_of(key, unit, self.inputs),
                _fingerprint(key, unit, self.inputs))

    def withdraw(self) -> None:
        if self.only is not None:
            return
        in_cut = set(self.order)
        gone = sorted((k for k in self.live if k not in in_cut), key=lambda k: -_number(self.live[k]))
        for key in gone:
            self.at("withdrawing")
            number = _number(self.live[key])
            reason = self.still_settled(key, "gone")
            if reason is not None:
                self.lists["settled"].append({"key": key, "number": number, "reason":
                                              f"gone from the cut, not closed: closed as {reason} by someone "
                                              "else -- left alone"})
                self.frozen.add(key)
                continue
            why, state = self.look(key)
            if why is not None:
                closed = state.get("state") == "closed"
                (self.lists["settled"] if closed else self.lists["touched"]).append(
                    {"key": key, "number": number, "reason": f"gone from the cut, not closed: {why}"})
                if closed:
                    self.settle(key, "gone", state)
                self.frozen.add(key)
                continue
            _, problem = self.call.send("PATCH", self.issue_path(number), CLOSE_NOT_PLANNED)
            if problem is not None:
                self.lists["failed"].append({"key": key, "reason": SHIP_FAILURE_PREFIXES[2] + problem})
                self.frozen.add(key)
                continue
            self.after(key, "withdrawn")
            self.closed[key] = number
            self.lists["withdrawn"].append({"key": key, "number": number})
            self.issues.pop(key, None)

    def edit(self) -> list[tuple[str, int, tuple[str, str, list[str], str], dict]]:
        """Edit what changed; return the follow-ups to open, as (key, of, ticket, tree)."""
        follow: list[tuple[str, int, tuple[str, str, list[str], str], dict]] = []
        for key in self.order:
            if key not in self.live or not self.in_scope(key):
                continue
            self.at("editing")
            recorded = self.live[key]
            number = _number(recorded)
            try:
                ticket = self.ticket(key)
            except (KeyError, TypeError, ValueError) as exc:
                self.lists["failed"].append({"key": key, "reason": SHIP_FAILURE_PREFIXES[0] + str(exc)})
                self.frozen.add(key)
                continue
            body, fingerprint = ticket[1], ticket[3]
            if fingerprint == recorded.get("fingerprint"):
                self.lists["skipped"].append({"key": key, "number": number, "url": recorded.get("url", ""),
                                              "reason": UNCHANGED_SKIP.format(number=number)})
                continue
            if len(body) > BODY_LIMIT:
                self.lists["failed"].append({"key": key, "reason": TOO_LARGE.format(n=len(body))})
                self.frozen.add(key)
                continue
            reason = self.still_settled(key, fingerprint)
            if reason is not None:
                self.lists["settled"].append({"key": key, "number": number, "reason":
                                              f"context changed, contract did not: closed as {reason} by "
                                              "someone else -- left alone"})
                self.frozen.add(key)
                continue
            why, state = self.look(key)
            if why is not None and state.get("state") == "closed":
                moved = _contract_moved(recorded.get("contract"), str(state.get("body") or ""), body)
                if not moved or state.get("state_reason") != "completed":
                    self.lists["settled"].append({"key": key, "number": number,
                                                  "reason": f"context changed, contract did not: {why}"})
                    self.settle(key, fingerprint, state)
                    self.frozen.add(key)
                    continue
                tree = ({"root": recorded.get("root") or {"id": recorded.get("id"), "number": number}}
                        if self.inputs.lineage_keyed else {})
                root_number = _number(tree.get("root"))
                found = [i for i in (self.ours or {}).get(key, ()) if _number(i) not in (number, root_number)]
                if found:
                    self.issues[key] = {**self.adopt_record(found[0]), "follow_up_of": number, **tree}
                    self.lists["adopted"].append({"key": key, "number": found[0].get("number"),
                                                  "url": self.issues[key]["url"], "follow_up": True})
                    continue
                follow.append((key, number, ticket, tree))
                continue
            if why is not None:
                self.lists["touched"].append({"key": key, "number": number, "reason": f"changed, not edited: {why}"})
                self.frozen.add(key)
                continue
            self.write(key, ticket, state)
        return follow

    def follow_up_text(self, title: str, body: str, of: int) -> tuple[str, str]:
        return (FOLLOW_UP_TITLE.format(title=title, number=of),
                f"> **Follow-up to #{of}.** That issue is closed and its work merged; this contract changed "
                f"after it. Build the difference against what #{of} merged -- everything below is the contract "
                f"as it now stands. If what #{of} merged already satisfies it, close this as already "
                "satisfied.\n\n" + body)

    def write(self, key: str, ticket: tuple[str, str, list[str], str], state: Mapping) -> None:
        """Edit an untouched open issue in place; labels by difference, never by replacement."""
        title, body, labels, fingerprint = ticket
        mine = self.issues[key]
        number = _number(mine)
        of = mine.get("follow_up_of")
        if of:
            title, body = self.follow_up_text(title, body, int(of))
        _, problem = self.call.send("PATCH", self.issue_path(number), {"title": title, "body": body})
        current = _names(state.get("labels"))
        if problem is None:
            add = [label for label in labels if label not in current]
            if add:
                _, problem = self.call.send("POST", self.issue_path(number) + "/labels", {"labels": add})
        if problem is None:
            wrote = tuple(mine.get("labels") or ()) + tuple(self.inputs.labels)
            for label in current:
                if (label.startswith((KEY_LABEL_PREFIX, "kind:", "slice:")) or label in wrote) and label not in labels:
                    _, problem = self.call.send("DELETE", self.issue_path(number) + "/labels/" + quote(label, safe=""))
                    if problem is not None:
                        break
        if problem is not None:
            self.lists["failed"].append({"key": key, "reason": SHIP_FAILURE_PREFIXES[1] + problem})
            self.frozen.add(key)
            return
        self.after(key, "edited")
        self.issues[key] = {**mine, "fingerprint": fingerprint, "contract": _contract_of(ticket[1]),
                            "batch": self.inputs.next_seq, "labels": list(labels)}
        if not any(e["key"] == key for e in self.lists["edited"]):
            self.lists["edited"].append({"key": key, "number": number, "url": mine.get("url", "")})

    def create(self, follow: list[tuple[str, int, tuple[str, str, list[str], str], dict]],
               missing: list[str]) -> None:
        for key, of, ticket, tree in follow:
            self.at("creating")
            self.read(of)
            title, body = self.follow_up_text(ticket[0], ticket[1], of)
            if len(body) > BODY_LIMIT:
                self.lists["failed"].append({"key": key, "reason": FOLLOW_UP_TOO_LARGE.format(number=of, n=len(body))})
                self.frozen.add(key)
                continue
            answer, problem = self.call.send("POST", f"{self.base}/issues",
                                             {"title": title, "body": body, "labels": list(ticket[2])})
            if problem is not None or answer is None or not isinstance(answer.body, dict):
                self.lists["failed"].append({"key": key, "reason": SHIP_FAILURE_PREFIXES[3].format(number=of)
                                             + str(problem)})
                self.frozen.add(key)
                continue
            issue = answer.body
            self.check_labels(key, issue, ticket[2])
            url = str(issue.get("html_url") or "")
            self.issues[key] = {"id": issue.get("id"), "number": issue.get("number"), "url": url,
                                "fingerprint": ticket[3], "contract": _contract_of(ticket[1]),
                                "batch": self.inputs.next_seq, "labels": list(ticket[2]), "follow_up_of": of,
                                **tree}
            self.lists["follow_ups"].append({"key": key, "title": title, "number": issue.get("number"), "url": url,
                                             "follow_up_of": of, "labels": list(ticket[2])})
            if self.inputs.lineage_keyed:
                self.link(key)
        if self.inputs.kind == "delegated_ship":
            return
        for key in missing:
            self.at("creating")
            try:
                title, body, labels, fingerprint = self.ticket(key)
            except (KeyError, TypeError, ValueError) as exc:
                self.lists["failed"].append({"key": key, "reason": SHIP_FAILURE_PREFIXES[0] + str(exc)})
                continue
            if len(body) > BODY_LIMIT:
                self.lists["skipped"].append({"key": key, "reason": TOO_LARGE.format(n=len(body))})
                continue
            payload: dict = {"title": title, "body": body}
            if labels:
                payload["labels"] = list(labels)
            answer, problem = self.call.send("POST", f"{self.base}/issues", payload)
            if problem is not None or answer is None or not isinstance(answer.body, dict):
                self.lists["failed"].append({"key": key, "reason": problem or "GitHub's answer was not an issue"})
                continue
            issue = answer.body
            self.check_labels(key, issue, labels)
            url = str(issue.get("html_url") or "")
            self.issues[key] = {"id": issue.get("id"), "number": issue.get("number"), "url": url,
                                "fingerprint": fingerprint, "contract": _contract_of(body),
                                "batch": self.inputs.next_seq, "labels": list(labels)}
            self.lists["created"].append({"key": key, "title": title, "number": issue.get("number"), "url": url,
                                          "labels": list(labels)})

    def link(self, key: str) -> None:
        """A follow-up attached under its unit's root issue; a failed link is retried by a later press."""
        mine = self.issues[key]
        parent = _number(mine.get("root"))
        answer, problem = self.call.send("POST", self.issue_path(parent) + "/sub_issues",
                                         {"sub_issue_id": mine.get("id")})
        if problem is not None and not (answer is not None and answer.status == 422
                                        and _ALREADY_SUB.search(problem)):
            self.lists["unlinked"].append({"key": key, "number": mine.get("number"), "parent": parent,
                                           "reason": f"not attached under #{parent}, retried next press: {problem}"})
            return
        state, _ = self.read(parent)
        if state is not None and state.get("state") == "open":
            self.lists["raced"].append({"key": key, "number": parent, "reason":
                                        f"open after #{mine.get('number')} was attached as its sub-issue -- "
                                        "reopened by the attach or by someone else; left as it is"})
        self.issues[key] = {**mine, "linked": True}
        self.lists["linked"].append({"key": key, "number": mine.get("number"), "parent": parent})

    def check_labels(self, key: str, issue: Mapping, sent: Sequence[str]) -> None:
        if issue.get("labels") is None:
            return
        kept = _names(issue.get("labels"))
        lost = [label for label in sent if label not in kept]
        if lost:
            self.lists["unlabelled"].append({"key": key, "number": issue.get("number"), "missing": lost,
                                             "reason": "GitHub created the issue without these labels -- does "
                                                       "the token have push access?"})

    def wire(self, wired_before: set[str]) -> set[str]:
        issues = self.issues
        wiring = {k: tuple(self.edges.get(k, ())) + tuple(b for b in self.merge_now.get(k, ())
                                                          if b not in self.edges.get(k, ()))
                  for k in set(self.edges) | set(self.merge_now)}
        wanted = {order_text(blocked, blocker) for blocked in self.order if blocked in issues
                  for blocker in wiring.get(blocked, ())}
        renewed = {f["key"] for f in self.lists["follow_ups"]}
        now_wired = {p for p in wired_before if all(k in issues for k in p.split(ORDER_SEP, 1))
                     and not renewed.intersection(p.split(ORDER_SEP, 1))}

        def touches(blocked: str, blocker: str) -> bool:
            return self.only is None or self.only in (blocked, blocker)

        self.to_wire = sum(1 for blocked in self.order if blocked in issues and blocked not in self.frozen
                           for blocker in wiring.get(blocked, ())
                           if order_text(blocked, blocker) not in now_wired and touches(blocked, blocker))
        self.at("wiring")
        synced: set[str] = set()
        if self.ours is not None:
            for blocked in self.order:
                number = _number(issues.get(blocked))
                if (blocked not in issues or blocked in self.frozen or not self.in_scope(blocked)
                        or number not in self.open_labels
                        or any(label in self.pickup for label in self.open_labels.get(number, ()))):
                    continue
                native = self.call.pages(self.issue_path(number) + "/dependencies/blocked_by?")
                if native is None:
                    continue
                synced.add(blocked)
                want = {issues[b].get("id") for b in wiring.get(blocked, ()) if b in issues}
                have = {link.get("id") for link in native}
                for link in native:
                    if link.get("id") in want or not any(
                            name.startswith(KEY_LABEL_PREFIX) for name in _names(link.get("labels"))):
                        continue
                    _, problem = self.call.send("DELETE", self.issue_path(number)
                                                + f"/dependencies/blocked_by/{link.get('id')}")
                    stale = f"#{link.get('number')}"
                    if problem is not None:
                        self.lists["unwired"].append({"blocked": blocked, "blocker": stale,
                                                      "reason": STALE_NOT_REMOVED.format(answer=problem)})
                        continue
                    self.lists["removed"].append({"blocked": blocked, "blocker": stale})
                now_wired = {p for p in now_wired if p.partition(ORDER_SEP)[0] != blocked}
                now_wired.update(order_text(blocked, b) for b in wiring.get(blocked, ())
                                 if b in issues and issues[b].get("id") in have)
        for blocked in self.order:
            if blocked not in issues or blocked in self.frozen:
                continue
            for blocker in wiring.get(blocked, ()):
                pair = order_text(blocked, blocker)
                if pair in now_wired or not touches(blocked, blocker):
                    continue
                there = issues.get(blocker)
                if not there:
                    self.lists["unwired"].append({"blocked": blocked, "blocker": blocker,
                                                  "reason": NO_ISSUE_ORDER.format(blocker=blocker)})
                    self.call.tick()
                    continue
                answer, problem = self.call.send("POST", self.issue_path(_number(issues[blocked]))
                                                 + "/dependencies/blocked_by", {"issue_id": there.get("id")})
                if problem is not None and not (answer is not None and answer.status == 422
                                                and ALREADY_TAKEN in _message(answer.body)):
                    self.lists["unwired"].append({"blocked": blocked, "blocker": blocker, "reason": problem})
                    continue
                now_wired.add(pair)
                self.lists["wired"].append({"blocked": blocked, "blocker": blocker,
                                            "blocked_number": issues[blocked].get("number"),
                                            "blocker_id": there.get("id")})
        for pair in sorted(wired_before):
            blocked, _, blocker = pair.partition(ORDER_SEP)
            if (pair in wanted or blocked not in issues or blocked in self.frozen or blocked in synced
                    or not self.in_scope(blocked)):
                continue
            source = self.live.get(blocker)
            if not source:
                continue
            _, problem = self.call.send("DELETE", self.issue_path(_number(issues[blocked]))
                                        + f"/dependencies/blocked_by/{source.get('id')}")
            if problem is not None:
                self.lists["unwired"].append({"blocked": blocked, "blocker": blocker,
                                              "reason": DROPPED_NOT_REMOVED.format(answer=problem)})
                continue
            now_wired.discard(pair)
            self.lists["removed"].append({"blocked": blocked, "blocker": blocker})
        return now_wired

    def sweep(self) -> None:
        """Open issues of this project no key owns are closed not planned, unless picked up. As in the
        running unit, the sweep runs only when the project has pickup labels."""
        if self.ours is None or not self.pickup or self.only is not None:
            return
        owned = {_number(v) for v in self.issues.values() if v.get("number") is not None}
        owned |= {_number(v.get("root")) for v in self.issues.values() if _number(v.get("root"))}
        gone_now = set(self.closed.values())
        for key, found in sorted(self.ours.items()):
            for issue in found:
                n = _number(issue)
                if n in owned or n in gone_now:
                    continue
                if any(label in self.pickup for label in _names(issue.get("labels"))):
                    self.lists["touched"].append({"key": key, "number": n, "reason":
                                                  "an open issue no unit owns any more, but it is picked up "
                                                  "-- left alone"})
                    continue
                self.at("sweeping")
                _, problem = self.call.send("PATCH", self.issue_path(n), CLOSE_NOT_PLANNED)
                if problem is not None:
                    self.lists["failed"].append({"key": key, "reason": SHIP_FAILURE_PREFIXES[4].format(number=n)
                                                 + problem})
                    continue
                self.lists["swept"].append({"key": key, "number": n, "url": str(issue.get("html_url") or "")})

    def verify(self, open_now: set[int]) -> None:
        """Every blocker an open, unpicked issue of ours names must be carried by an open issue or one
        closed completed under that key; anything else is reported dead."""
        for blocked in self.order:
            mine = self.issues.get(blocked)
            if (not mine or blocked in self.frozen or blocked not in self.rendered
                    or _number(mine) not in open_now
                    or any(label in self.pickup for label in self.open_labels.get(_number(mine), ()))):
                continue
            blockers = list(self.edges.get(blocked, ())) + [
                b for b in self.merge_now.get(blocked, ()) if b not in self.edges.get(blocked, ())]
            for blocker in blockers:
                why: str | None = f"no issue carries {blocker}"
                number = _number(self.issues.get(blocker))
                if number:
                    if number in open_now:
                        why = None
                    else:
                        state = self.seen.get(number)
                        problem = None
                        if state is None:
                            state, problem = self.read(number)
                        if state is None:
                            why = f"#{number} could not be read to check it: {problem}"
                        else:
                            carried = _machine_block(str(state.get("body") or "")).get("key") or blocker
                            if state.get("state") == "open" or (
                                    state.get("state_reason") == "completed" and carried == blocker):
                                why = None
                            elif state.get("state_reason") != "completed":
                                why = f"#{number} is closed {state.get('state_reason') or 'closed'}"
                            else:
                                why = (f"#{number} was closed completed as {carried}, a key {blocked} does not "
                                       "name for it")
                if why:
                    self.dead_entry(blocked, mine, blocker, why)

    def dead_entry(self, blocked: str, mine: Mapping, blocker: str, why: str) -> None:
        self.lists["dead"].append({"blocked": blocked, "number": mine.get("number"), "blocker": blocker,
                                   "reason": why})

    # -- the outcome ------------------------------------------------------------------------------

    def result(self) -> dict:
        lists = self.lists
        merge_order = [{"blocked": k, "blocker": b, "order": _MERGE}
                       for k in self.order for b in self.merge_now.get(k, ())]
        counted = ("created", "edited", "withdrawn", "touched", "settled", "follow_ups", "adopted", "swept",
                   "raced", "held", "skipped", "failed", "wired", "unwired", "removed", "dead", "linked",
                   "unlinked", "unlabelled")
        counts = {"units": len(self.order), **{name: len(lists[name]) for name in counted},
                  "merge_order": len(merge_order),
                  "dropped_orders": sum(len(d["orders"]) for d in self.dropped)}
        return {
            "emitted": True, "project": self.inputs.project, "repo": self.repo, "cut_seq": self.cut.get("seq"),
            "batch": self.inputs.next_seq, "horizon": self.horizon,
            **{name: lists[name] for name in (
                "created", "failed", "unwired", "wired", "edited", "withdrawn", "touched", "settled",
                "follow_ups", "adopted", "swept", "raced", "held", "skipped", "removed", "dead", "linked",
                "unlinked", "unlabelled")},
            "only": self.only, "merge_order": merge_order, "dropped_orders": self.dropped,
            "unassigned_tests": [], "undeclared_test_paths": [], "counts": counts,
        }

    def lines(self, now_wired: set[str]) -> tuple[dict, ...]:
        ship: dict = {"seq": self.inputs.next_seq, "cut_seq": self.cut.get("seq"), "repo": self.repo,
                      "issues": {k: _recorded(v) for k, v in self.issues.items()},
                      "wired": sorted(now_wired)}
        if self.inputs.lineage_keyed:
            ship["keys"] = _LINEAGE
        if not self.closed:
            return (ship,)
        rollback: dict = {"kind": _ROLLBACK, "seq": self.inputs.next_seq + 1, "cut_seq": self.cut.get("seq"),
                    "repo": self.repo, "undone": [], "closed": dict(self.closed), "failed": []}
        return ship, rollback


# -- rollback --------------------------------------------------------------------------------------

def rollback_plan(inputs: RollbackInputs, sender: Sender, run: Run, runs: RunRegistry) -> PlanOutcome:
    if not inputs.known:
        raise UnknownProject(inputs.project)

    def refused(reason: str) -> PlanOutcome:
        return PlanOutcome({"rolled_back": False, "project": inputs.project, "repo": inputs.repo,
                            "reason": reason}, ())

    if not inputs.repo:
        return refused(RUN_REASONS["rb_no_repo"])
    if not inputs.token:
        return refused(RUN_REASONS["rb_no_token"])
    cut_seq = inputs.cut_seq
    if cut_seq is None:
        if inputs.latest_cut_seq is None:
            return refused(RUN_REASONS["rb_no_cut"])
        cut_seq = inputs.latest_cut_seq
    else:
        try:
            int(cut_seq)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return refused(RUN_REASONS["rb_bad_seq"].format(value=render_value(cut_seq)))
    repo = inputs.repo
    targets = inputs.issues
    if not targets:
        return PlanOutcome({"rolled_back": False, "reason": RUN_REASONS["rb_nothing"].format(repo=repo),
                            "repo": repo, "cut_seq": cut_seq, "project": inputs.project}, ())

    order = sorted(targets, key=lambda k: -_number(targets[k]))
    closed: dict[str, int] = {}
    failed: list[dict] = []
    skipped: list[dict] = []

    def counters() -> dict:
        return {"total": len(order), "processed": len(closed) + len(failed) + len(skipped),
                "closed": len(closed), "failed": len(failed), "skipped": len(skipped)}

    call = _Caller(sender, run, runs, counters)
    call.step = "closing"
    call.tick()
    for key in order:
        number = _number(targets[key])
        path = f"/repos/{repo}/issues/{number}"
        answer, problem = call.send("GET", path)
        if problem is not None or answer is None or not isinstance(answer.body, dict):
            skipped.append({"key": key, "number": number,
                            "reason": ROLLBACK_SKIPS["unreadable"].format(detail=problem)})
            continue
        state = answer.body
        reason = state.get("state_reason")
        if state.get("state") == "closed" and reason != _WITHDRAWN:
            skipped.append({"key": key, "number": number,
                            "reason": f"closed as {reason or 'closed'} by someone else -- left alone"})
            continue
        held = [label for label in _names(state.get("labels")) if label in inputs.pickup]
        if held and state.get("state") != "closed":
            skipped.append({"key": key, "number": number,
                            "reason": ROLLBACK_SKIPS["pickup"].format(label=", ".join(held))})
            continue
        _, problem = call.send("PATCH", path, CLOSE_NOT_PLANNED)
        if problem is not None:
            failed.append({"key": key, "number": number, "reason": problem})
            continue
        closed[key] = number

    result = {"rolled_back": True, "project": inputs.project, "repo": repo, "cut_seq": cut_seq,
              "closed": [{"key": k, "number": n} for k, n in closed.items()], "failed": failed,
              "skipped": skipped,
              "counts": {"targeted": len(order), "closed": len(closed), "failed": len(failed),
                         "skipped": len(skipped)}}
    undone = sorted({int(targets[k]["batch"]) for k in closed
                     if isinstance(targets[k].get("batch"), int) and not isinstance(targets[k]["batch"], bool)})
    line = {"kind": _ROLLBACK, "seq": inputs.next_seq, "cut_seq": cut_seq, "repo": repo, "undone": undone,
            "closed": closed, "failed": list(failed)}
    return PlanOutcome(result, (line,))
