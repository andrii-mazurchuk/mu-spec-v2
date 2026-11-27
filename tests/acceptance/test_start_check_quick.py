"""The quick start check: the rebuilt unit judged against the running unit on all 22 quick variants (T-481).

A differential test: neither unit is imported by this process. The units, store and prompts come from
the acceptance fixture's acceptance_inputs, turned into the two commands by startcheck.inputs'
unit_commands; every start of either unit goes through the shared interposed launch's launcher.
Modules under test are imported inside the test, so a module not yet written fails only the tests
that need it. The test never skips: a missing input or launcher fails it.
"""


import sys


def test_quick_variants(acceptance_inputs, tmp_path):
    from startcheck.inputs import unit_commands
    from startcheck.judge import run
    from startcheck.ownenv import own_env
    from unitcheck.session import open_session

    from startcheck.variants import QUICK

    inputs = acceptance_inputs
    # The constant from the acceptance conftest itself, as pytest loaded it: the tests folder is no
    # package, so the module that defined the fixture's record type is the one place to read it from.
    REFERENCE_INSTALL_VAR = sys.modules[type(inputs).__module__].REFERENCE_INSTALL_VAR
    running, rebuilt = unit_commands(inputs, REFERENCE_INSTALL_VAR)
    pristine_store = inputs.staged_store
    pristine_prompts = inputs.pristine_prompts
    env = own_env()
    check_root = tmp_path / "check"
    check_root.mkdir()

    try:
        session = open_session(env)
    except Exception as exc:  # any refusal to give a launcher fails the test, never skips it
        raise AssertionError(f"no shared launcher can be given: open_session raised {exc!r}") from exc
    try:
        launcher = session.launcher()
        report = run(QUICK, running, rebuilt, str(check_root), pristine_store, pristine_prompts, env,
                     launcher=launcher)
        controls = session.controls()
    finally:
        session.close()

    failing = "\n".join(repr(v) for v in report.verdicts if not v.passed)
    detail = f"prediction faults: {report.prediction_faults}\nfailing verdicts:\n{failing}"
    assert len(report.verdicts) == 22, detail
    assert report.missing == (), detail
    assert [v.variant for v in report.verdicts] == list(QUICK), detail
    assert report.healthy_listened is True, detail
    assert report.passed is True, detail
    # both units of each of the 22 variants started through the launcher, one prepare per start
    assert len(controls) == 44, detail
