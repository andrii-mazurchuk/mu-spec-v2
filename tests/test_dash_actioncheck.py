"""The action order check (S-260) judged end to end: it passes the shipped wiring, tracker and
scheduler and its exports have the declared shape (T-213), and each faulty variant's pass fails,
naming the rule it breaks (T-215).

Every run is `bun run dashboard/check/actioncheck.ts` from the repository root, as a child process.
The exports are read by one `bun -e` script that first replaces process.exit with a recorder, so the
read gives the same exports whether importing the module only exports or also runs its batches and
exits; it then writes one JSON object, on a line of its own, and exits 0 through the saved exit.
"""

import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ACTIONCHECK_TS = REPO / "dashboard" / "check" / "actioncheck.ts"
ACTIONS_TS = REPO / "dashboard" / "src" / "actions.ts"
RUN_COMMAND = ("bun", "run", "dashboard/check/actioncheck.ts")
VARIANT_FLAG = "--variant"
RUN_TIMEOUT_S = 600

BATCHES = 200           # S-207's BATCHES
TRACES_PER_BATCH = 40   # S-207's TRACES_PER_BATCH
OUTCOME_CLASSES = {"confirmed", "refused", "gave_up", "dropped", "deadline", "foreign"}

# T-215's broken implementations, each with the rule names its pass must show; None: exit 1 alone.
LINES = (
    ("L1 release on any answer after the unknown", ("unavailable_span",)),
    ("L2 judge at or below the press mark", ("pre_press_inert",)),
    ("L3 key by (project, action, screen)", ("one_send_per_press",)),
    ("L4 drop a displaced 'not confirmed' record", ("no_silent_end",)),
    ("L5 never set the deadline timer", ("no_silent_end",)),
    ("L6 judge an unchanged arrival on empty parts", ("done_source", "unavailable_span")),
    ("L7 set_labels moved to queues", None),
)


def run_check(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*RUN_COMMAND, *args], check=False, cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        timeout=RUN_TIMEOUT_S,
    )


def read_exports(body: str) -> dict:
    """The object `body` builds from the module's exports `m` (and the page's ACTION_NAMES)."""
    assert ACTIONCHECK_TS.is_file(), "dashboard/check/actioncheck.ts is absent"
    script = (
        "const exit = process.exit.bind(process);"
        "const exits = [];"
        "process.exit = (code) => { exits.push(code); };"
        f"const m = await import({json.dumps(ACTIONCHECK_TS.as_uri())});"
        f"const {{ ACTION_NAMES }} = await import({json.dumps(ACTIONS_TS.as_uri())});"
        f"process.stdout.write('\\n' + JSON.stringify({body}) + '\\n');"
        "exit(0);"
    )
    done = subprocess.run(
        ["bun", "-e", script], check=False, cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        timeout=RUN_TIMEOUT_S,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    # an import that runs the check prints its report first: the object is the last line that parses
    for line in reversed(done.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise AssertionError(f"the export read wrote no JSON object:\n{done.stdout}{done.stderr}")


def names_rule(output: str, name: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", output) is not None


def match(edges: dict[int, list[str]]) -> dict[int, str]:
    """A maximum one-to-one assignment of lines to members (Kuhn's augmenting paths)."""
    owner: dict[str, int] = {}

    def augment(line: int, seen: set[str]) -> bool:
        for m in edges[line]:
            if m in seen:
                continue
            seen.add(m)
            if m not in owner or augment(owner[m], seen):
                owner[m] = line
                return True
        return False

    for line in edges:
        augment(line, set())
    return {line: m for m, line in owner.items()}


def test_action_check_passes_shipped():
    assert ACTIONCHECK_TS.is_file(), "dashboard/check/actioncheck.ts is absent"

    run = run_check()
    assert run.returncode == 0, run.stdout + run.stderr

    ex = read_exports(
        "{ ACTION_BATCHES: m.ACTION_BATCHES, ACTION_TRACES_PER_BATCH: m.ACTION_TRACES_PER_BATCH,"
        " RULES: m.RULES, CONDITIONS: m.CONDITIONS, OUTCOME_CLASSES: m.OUTCOME_CLASSES,"
        " ACTION_TEMPLATES_LENGTH: m.ACTION_TEMPLATES === undefined ? null : m.ACTION_TEMPLATES.length,"
        " ACTION_NAMES }"
    )

    assert ex["ACTION_BATCHES"] == BATCHES, ex["ACTION_BATCHES"]
    assert ex["ACTION_TRACES_PER_BATCH"] == TRACES_PER_BATCH, ex["ACTION_TRACES_PER_BATCH"]

    rules, conditions = ex["RULES"], ex["CONDITIONS"]
    assert isinstance(rules, list) and len(rules) == 14 and len(set(rules)) == 14, rules
    assert isinstance(conditions, list) and len(conditions) == 29 and len(set(conditions)) == 29, conditions
    # a template list shorter than CONDITIONS leaves some condition with no trace guaranteeing it
    assert ex["ACTION_TEMPLATES_LENGTH"] == len(conditions), ex["ACTION_TEMPLATES_LENGTH"]
    assert len(conditions) <= ex["ACTION_TRACES_PER_BATCH"]

    names = ex["ACTION_NAMES"]
    assert names, names
    missing = ["refusal_" + a for a in names if "refusal_" + a not in conditions]
    assert not missing, missing

    assert isinstance(ex["OUTCOME_CLASSES"], list), ex["OUTCOME_CLASSES"]
    assert set(ex["OUTCOME_CLASSES"]) == OUTCOME_CLASSES, ex["OUTCOME_CLASSES"]


def test_action_check_variants_fail():
    ex = read_exports("{ TRACKER_VARIANTS: m.TRACKER_VARIANTS, RULES: m.RULES }")
    members = ex["TRACKER_VARIANTS"]
    # an export read that lost the module at import fails here rather than passing vacuously
    assert isinstance(members, list) and members, members
    assert all(isinstance(m, str) for m in members), members
    assert len(set(members)) == len(members), members
    for _, names in LINES:
        for name in names or ():
            assert name in ex["RULES"], (name, ex["RULES"])

    failing: dict[str, str] = {}
    exits: dict[str, int] = {}
    for m in members:
        p = run_check(VARIANT_FLAG, m)
        exits[m] = p.returncode
        if p.returncode == 1:
            failing[m] = p.stdout + p.stderr

    edges = {
        i: [m for m, out in failing.items() if names is None or any(names_rule(out, n) for n in names)]
        for i, (_, names) in enumerate(LINES)
    }
    assigned = match(edges)
    unmatched = [LINES[i][0] for i in range(len(LINES)) if i not in assigned]
    assert not unmatched, (
        f"no distinct failing variant for {unmatched}; exits {exits}; "
        f"assigned {{{', '.join(f'{LINES[i][0]}: {m}' for i, m in assigned.items())}}}"
    )
