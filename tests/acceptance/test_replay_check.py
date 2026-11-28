"""The replay check end to end over the acceptance copy (S-481).

The running unit is the reference install and the rebuilt unit this repository, both taken from the shared
acceptance inputs, and the store is the session's staged copy of the acceptance store. The check must pass and
hand off a start store less exactly the replayed projects, with histories every one of which confirms; every
other project is reported as history incomplete. The modules under test are imported inside the test, so while
they are absent only this test fails.
"""

from pathlib import Path


def tree(root):
    """Every file under root: relative posix path -> bytes."""
    root = Path(root)
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_replay_end_to_end(acceptance_inputs, tmp_path):
    from mu_spec.codec import log_path, record_path
    from startcheck.ownenv import own_env
    from unitcheck.invariants import stored_projects
    from unitcheck.replay import INCOMPLETE, NO_PROJECT, classify, read_lines, run
    from unitcheck.runs import confirm

    from mu_spec.outside import PROJECTS_SUBDIR
    from startcheck.runner import UnitCommand

    inputs = acceptance_inputs
    running = UnitCommand("running", inputs.running_install)
    rebuilt = UnitCommand("rebuilt", inputs.rebuilt_install)
    pristine_store = inputs.staged_store
    pristine_prompts = inputs.pristine_prompts
    check_root = tmp_path / "check"
    check_root.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    projects_before = tree(inputs.pristine_projects)

    report, out = run(running, rebuilt, str(check_root), pristine_store, pristine_prompts, own_env(),
                      work=str(work))

    for p, tally in report.replayed.items():
        print(f"replayed {p}: sent {tally.sent}, rebuilt {tally.rebuilt}, ambiguous {tuple(tally.ambiguous)}, "
              f"left out {tuple(tally.left_out)}")
    for p, count in report.stored_state.items():
        print(f"{p}: {INCOMPLETE} ({count})")
    print(f"uncovered: {tuple(report.uncovered)}")
    assert report.passed, report.failures
    assert report.failures == (), report.failures
    assert report.outbound == (), report.outbound
    assert out is not None

    c = classify(read_lines(pristine_store), stored_projects(pristine_store))
    assert set(report.replayed) == set(c.replayed), (sorted(report.replayed), c.replayed)
    assert set(report.stored_state) == set(c.stored_state), (sorted(report.stored_state), c.stored_state)
    assert [h.name for h in out.histories] == list(c.replayed) + [NO_PROJECT], [h.name for h in out.histories]
    for h in out.histories:
        assert confirm(h) == (), (h.name, confirm(h))

    pristine_projects = Path(pristine_store) / PROJECTS_SUBDIR
    start_projects = Path(out.start_store) / PROJECTS_SUBDIR
    for p in c.replayed:
        assert not Path(record_path(str(start_projects), p)).parent.exists(), p
    for p in c.stored_state:
        folder = Path(record_path(str(pristine_projects), p)).parent
        if not folder.is_dir():
            continue
        kept = Path(record_path(str(start_projects), p)).parent
        assert tree(kept) == tree(folder), p
    log = Path(log_path(str(pristine_projects)))
    started_log = Path(log_path(str(start_projects)))
    assert started_log.read_bytes() == log.read_bytes()

    assert tree(inputs.pristine_projects) == projects_before
