"""End to end: the independence check over the replay check's histories and start store, on the acceptance copy.

The replay check runs first over the staged acceptance store and hands off its histories and start
store; the independence check then runs the rebuilt unit over each history and the contract calls under
every neighbour condition, and every verdict must pass. The staged store is never given to the
independence check. The basename is unique under tests/ (tests/unitcheck/test_independence_check.py is the
independence module's unit test). The modules under test are imported inside the test, so while they are
absent only this test fails.
"""


def test_independence_end_to_end(acceptance_inputs, tmp_path):
    from startcheck.ownenv import own_env as launch_env
    from unitcheck.independence import run
    from unitcheck.replay import run as replay_run

    from startcheck.runner import UnitCommand

    inputs = acceptance_inputs
    running = UnitCommand("running", inputs.running_install)
    rebuilt = UnitCommand("rebuilt", inputs.rebuilt_install)
    check_root = tmp_path / "check"
    check_root.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    own_env = launch_env()

    report, out = replay_run(running, rebuilt, str(check_root), inputs.staged_store, inputs.pristine_prompts,
                             own_env, work=str(work))
    assert report.passed, report
    assert out is not None
    histories = out.histories
    start_store = out.start_store

    verdicts = run(histories, rebuilt, str(check_root), start_store, inputs.pristine_prompts, own_env)

    assert len(verdicts) == 3 * (len(histories) + 1), [(v.history, v.condition) for v in verdicts]
    names = [h.name for h in histories] + ["contract"]
    for i, name in enumerate(names):
        three = verdicts[3 * i : 3 * i + 3]
        assert [v.history for v in three] == [name] * 3, three
        assert [v.condition for v in three] == ["refusing", "absent", "hung"], three
    for v in verdicts:
        assert v.not_driven == (), (v.history, v.condition, v.not_driven)
        assert v.digests_ok, v
        assert v.answer_faults == (), (v.history, v.condition, v.answer_faults)
        assert v.state_faults == (), (v.history, v.condition, v.state_faults)
        assert v.late == (), (v.history, v.condition, v.late)
        assert v.outbound == (), (v.history, v.condition, v.outbound)
        assert v.passed, v
