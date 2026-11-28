"""End to end: the recording-failure check passes the rebuilt unit on every replayed history (T-505).

A differential test: neither unit is imported by this process. The inputs come from S-519's
acceptance_inputs fixture. The replay check (S-481) runs first over the staged store and hands over its
histories and start store; the recording-failure check (S-384) is given exactly those, never the staged
store, and runs every schedule on both units. The basename is unique under tests/
(tests/unitcheck/test_recording_check.py is the recording module's unit test). The modules under test are
imported inside the test, so while they are absent only this test fails.
"""


def test_recording_end_to_end(acceptance_inputs, tmp_path, monkeypatch):
    import unitcheck.recording
    from startcheck.ownenv import own_env as launch_env
    from unitcheck.recording import SCHEDULES, STRETCH_MIN_WRITES, run
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
    own_env = launch_env()

    report, out = replay_run(running, rebuilt, str(check_root), pristine_store, pristine_prompts, own_env,
                             work=str(work))
    assert report.passed, report
    assert out is not None
    histories, start_store = out.histories, out.start_store

    runs = {}
    real_unit_run = unitcheck.recording.unit_run

    def recording_run(*args, **kwargs):
        r = real_unit_run(*args, **kwargs)
        runs[(r.history, r.unit, r.setting.label)] = r
        return r

    monkeypatch.setattr(unitcheck.recording, "unit_run", recording_run)

    verdicts = run(histories, running, rebuilt, str(check_root), start_store, pristine_prompts, own_env)

    assert len(verdicts) == len(SCHEDULES) * len(histories), len(verdicts)
    expected = [(h.name, s) for h in histories for s in SCHEDULES]
    assert [(v.history, v.schedule) for v in verdicts] == expected
    for v in verdicts:
        assert v.not_driven == (), v
        if v.skipped is None:
            assert v.passed is True, v
            continue
        assert v.schedule == "stretch", v
        healthy = runs[(v.history, "running", "healthy")]
        recording = {r.call for r in healthy.records if r.call is not None and r.call >= 0}
        assert len(recording) < STRETCH_MIN_WRITES, (v, sorted(recording))
        print(f"skipped: {v.history} {v.schedule}: {v.skipped}")
    for v in verdicts:
        if v.running_drop_faults:
            print(f"running drop faults: {v.history} {v.schedule}: {v.running_drop_faults}")
