"""The scheduler check (S-207) judged end to end: it passes the page's scheduler deterministically
(T-162) and rejects two wrong schedulers (T-163).

Each test runs `bun run dashboard/check/schedcheck.ts` as a child process; the mutants run it in a
scratch copy of dashboard/ whose src/scheduler.ts wraps the real one and replaces step.
"""

import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DASHBOARD = REPO / "dashboard"
SCHEDCHECK_TS = DASHBOARD / "check" / "schedcheck.ts"

BATCHES = 200
COUNTERS = (
    "hide_near_ask",
    "hide_end_before_wait",
    "hide_end_after_wait",
    "opened_hidden",
    "opened_visible",
    "fail_run_4_then_success",
    "preparing_ask",
    "preparing_head",
    "preparing_refresh",
    "interval_default",
    "interval_mid",
    "interval_long",
    "press_during_ask",
    "action_during_ask",
    "overlap_heads_in_order",
    "overlap_heads_reversed",
    "overlap_rest_after_newer_head",
    "ship_own_press_3_asks",
    "ship_found_3_asks",
    "progress_fail_2",
    "end_between_asks_finishing",
    "press_awaited_ended",
    "hide_during_run",
    "switch_away_back_during_run",
    "run_part_after_ended",
    "new_run_after_end",
)

MUTANT_HEAD = 'import * as real from "./scheduler.real";\nexport * from "./scheduler.real";\n\n'

MUTANT_A = MUTANT_HEAD + (
    "export function step(s: real.State, e: real.Event): real.Step {\n"
    "  const r = real.step(s, e);\n"
    '  if (e.type === "refresh") return { ...r, state: { ...r.state, lastAskAt: e.now } };\n'
    "  return r;\n"
    "}\n"
)

MUTANT_B = MUTANT_HEAD + (
    "export function step(s: real.State, e: real.Event): real.Step {\n"
    "  const r = real.step(s, e);\n"
    '  if (e.type === "ship_sent" && r.state.track !== null) {\n'
    "    return { ...r, state: { ...r.state, track: { ...r.state.track, pressAwaited: false } } };\n"
    "  }\n"
    "  return r;\n"
    "}\n"
)


def run_check(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bun", "run", "dashboard/check/schedcheck.ts"], check=False, cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", timeout=600,
    )


def first_count_after(line: str, name: str) -> int | None:
    """The first integer after the counter's name on the line, stopping at the next counter's name."""
    m = re.search(rf"\b{name}\b", line)
    if not m:
        return None
    rest = line[m.end():]
    cut = [n.start() for c in COUNTERS if c != name for n in [re.search(rf"\b{c}\b", rest)] if n]
    if cut:
        rest = rest[: min(cut)]
    num = re.search(r"\d+", rest)
    return int(num.group()) if num else None


def test_schedcheck_passes_and_is_deterministic():
    assert SCHEDCHECK_TS.is_file(), "dashboard/check/schedcheck.ts is absent"

    first = run_check(REPO)
    second = run_check(REPO)

    # 1. Both runs exit 0.
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr

    # 2. The two reports are byte-equal: the draws are keyed by seed, kind and label only.
    assert first.stdout == second.stdout

    # 3. The report names 200 batches and gives every one of the 26 counters a value of at least 1.
    out = first.stdout
    # 200 and "batch" must be adjacent tokens ("200 batches", "batches: 200", "BATCHES = 200"), so a
    # report of "5 batches, 200 traces" does not pass.
    names_batches = rf"\b{BATCHES}\s+batch|\bbatch\w*\s*[:=]?\s*{BATCHES}\b"
    assert any(
        re.search(names_batches, line, flags=re.IGNORECASE) for line in out.splitlines()
    ), "the report does not name 200 batches"
    for name in COUNTERS:
        counts = [n for line in out.splitlines() for n in [first_count_after(line, name)] if n is not None]
        assert counts, f"the report gives no value for counter {name}"
        assert all(n >= 1 for n in counts), f"the report gives counter {name} a value of 0"


def scratch_with(tmp_path: Path, mutant: str) -> Path:
    shutil.copytree(DASHBOARD, tmp_path / "dashboard")
    src = tmp_path / "dashboard" / "src"
    (src / "scheduler.ts").rename(src / "scheduler.real.ts")
    (src / "scheduler.ts").write_text(mutant, encoding="utf-8")
    return tmp_path


def test_schedcheck_rejects_mutants(tmp_path):
    assert SCHEDCHECK_TS.is_file(), "dashboard/check/schedcheck.ts is absent"

    # Mutant A: a refresh resets the background clock, so T8's ask 3 goes at 21000, not 20000.
    a = run_check(scratch_with(tmp_path / "a", MUTANT_A))
    out_a = a.stdout + a.stderr
    assert a.returncode == 1, out_a
    assert any(
        re.search(r"\brule\s*7\b", line, flags=re.IGNORECASE) and re.search(r"\bT8\b", line)
        for line in out_a.splitlines()
    ), f"mutant A's output does not name rule 7 for T8:\n{out_a}"

    # Mutant B: the awaited Ship press is forgotten, so T14's early 'ended' drops the track (S5).
    b = run_check(scratch_with(tmp_path / "b", MUTANT_B))
    out_b = b.stdout + b.stderr
    assert b.returncode == 1, out_b
    lines = out_b.splitlines()
    names_s5 = any(re.search(r"\bS5\b", line) and re.search(r"\bT14\b", line) for line in lines)
    names_counter = any(
        re.search(r"\bpress_awaited_ended\b", line)
        and (
            re.search(r"\bT14\b", line)
            or re.search(r"empty|zero", line, flags=re.IGNORECASE)
            or first_count_after(line, "press_awaited_ended") == 0
        )
        for line in lines
    )
    assert names_s5 or names_counter, f"mutant B's output names neither S5 nor press_awaited_ended for T14:\n{out_b}"
