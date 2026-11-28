"""End to end: the independence check over the replay check's histories and start store (T-470).

A differential test: neither unit is imported by this process. The inputs come from S-508's
acceptance_inputs fixture; the replay check (S-481) runs first over the staged store and hands off the
histories and the start store, and the independence check runs the rebuilt unit over each of them and the
contract calls under every neighbour condition. Modules under test are imported inside the test, so a
module not yet written fails only the tests that need it.
"""


def test_independence_end_to_end(acceptance_inputs, tmp_path):
    from startcheck.ownenv import own_env
    from unitcheck.independence import run
    from unitcheck.replay import run as replay_run

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
    env = own_env()

    report, out = replay_run(running, rebuilt, str(check_root), pristine_store, pristine_prompts, env,
                             work=str(work))
    assert report.passed, report
    assert out is not None
    histories, start_store = out.histories, out.start_store

    verdicts = run(histories, rebuilt, str(check_root), start_store, pristine_prompts, env)

    assert len(verdicts) == 3 * (len(histories) + 1), [(v.history, v.condition) for v in verdicts]
    names = [h.name for h in histories] + ["contract"]
    for i, name in enumerate(names):
        three = verdicts[3 * i : 3 * i + 3]
        assert [v.history for v in three] == [name] * 3, three
        assert [v.condition for v in three] == ["refusing", "absent", "hung"], three
    for v in verdicts:
        assert v.not_driven == (), v
        assert v.digests_ok, v
        assert v.answer_faults == (), v
        assert v.state_faults == (), v
        assert v.late == (), v
        assert v.outbound == (), v
        assert v.passed, v
