"""End to end: the events check over the replay check's histories and start store, on the acceptance copy.

The replay check runs first over the staged acceptance store and hands off its histories and start
store; the events check then replays each history twice on the running unit and once on the rebuilt
unit, and every verdict must pass. The events module's unit_run is wrapped so each history's first
declaration run can be judged: a history that begins with a creation or fork line has it admitted, which
only a check copying the start store, where the replayed project is absent, gives. The modules under
test are imported inside the test, so while they are absent only this test fails.
"""


def test_events_end_to_end(acceptance_inputs, tmp_path, monkeypatch):
    import inspect

    import unitcheck.events
    from startcheck.ownenv import own_env as launch_env
    from unitcheck.events import run
    from unitcheck.replay import CREATE_TOOL, FORK_TOOL
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

    real_unit_run = unitcheck.events.unit_run
    made = {}

    def recording(*args, **kwargs):
        bound = inspect.signature(real_unit_run).bind(*args, **kwargs)
        r = real_unit_run(*args, **kwargs)
        made[(bound.arguments["history"].name, bound.arguments["setting"].label)] = r
        return r

    monkeypatch.setattr(unitcheck.events, "unit_run", recording)
    verdicts = run(histories, running, rebuilt, str(check_root), start_store, inputs.pristine_prompts, own_env)

    assert len(verdicts) == len(histories), (len(verdicts), len(histories))
    assert [v.history for v in verdicts] == [h.name for h in histories], verdicts
    for v in verdicts:
        assert v.not_driven == (), (v.history, v.not_driven)
        assert v.digests_ok, v
        assert v.all_ended, v
        assert v.mismatches == (), (v.history, v.mismatches)
        assert v.undeclared == (), (v.history, v.undeclared)
        assert v.outbound == (), (v.history, v.outbound)
        assert v.passed, v

    # Creation admitted: a history beginning with a create or fork line has it answered 2xx on decl1.
    created = [h for h in histories
               if h.calls and h.calls[0].kind == "tool" and h.calls[0].name in (CREATE_TOOL, FORK_TOOL)]
    print(f"histories beginning with a creation or fork line: {len(created)}")
    refused = []
    for h in created:
        status = made[(h.name, "decl1")].calls[0].status
        print(f"{h.name}: decl1 call 0 ({h.calls[0].name}) answered {status}")
        if status is None or not 200 <= status < 300:
            refused.append((h.name, status))
    assert refused == [], refused

    for v in verdicts:
        print(f"{v.history}: never_varied {v.never_varied}, login_lookups {dict(v.login_lookups)}")
    print(f"uncovered: {report.uncovered}")
