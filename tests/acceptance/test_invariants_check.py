"""The invariants check end to end over the acceptance copy: every attempt formed somewhere, and passing (T-471).

The inputs come from the acceptance_inputs session fixture (S-508), the same staged copy and prompts the
replay check takes; each unit is driven only through the invariants check's own unit runs. Modules under
test are imported inside the test, so a module not yet written fails only the tests that need it.
"""

import hashlib
import os


def snapshot(folder: str) -> dict[str, str]:
    """posix relpath -> sha256 of every file under folder."""
    out = {}
    for dirpath, _, files in os.walk(folder):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, folder).replace(os.sep, "/")] = hashlib.sha256(f.read()).hexdigest()
    return out


def test_invariants_end_to_end(acceptance_inputs, tmp_path):
    from startcheck.ownenv import own_env
    from unitcheck.invariants import NO_ATTEMPT_RULES, run, stored_projects

    from startcheck.runner import UnitCommand

    inputs = acceptance_inputs
    before = snapshot(inputs.pristine_projects)
    check_root = tmp_path / "check"
    check_root.mkdir()
    running = UnitCommand("running", inputs.running_install)
    rebuilt = UnitCommand("rebuilt", inputs.rebuilt_install)

    report = run(running, rebuilt, str(check_root), inputs.staged_store, inputs.pristine_prompts, own_env())

    for a in report.attempts:
        if a.call is None:
            print(f"not formed on {a.project}: {a.attempt} ({a.rule}): {a.why_not}")
    assert report.unformed == (), report.unformed
    assert report.outbound == (), report.outbound
    faulted = [(v.project, v.attempt, v.faults) for v in report.verdicts if v.faults != ()]
    assert faulted == [], faulted
    assert report.no_attempt == NO_ATTEMPT_RULES, report.no_attempt
    assert report.passed is True, report
    assert {a.project for a in report.attempts} == set(stored_projects(inputs.staged_store)), report.attempts
    assert stored_projects(inputs.staged_store), "the staged store holds no project"
    assert snapshot(inputs.pristine_projects) == before, "the pristine projects folder changed"
