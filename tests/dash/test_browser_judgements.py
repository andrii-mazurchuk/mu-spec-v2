"""The browser check's pure judgements: dashcheck.browser's measuring functions (T-448).

No browser and no unit: each judgement the check's verdicts rest on is given hand-built inputs,
worked by hand from S-434's rules. The constants the keys are built from are the module's own.
dashcheck.browser is imported inside the test, so a module not yet written fails only this test.
"""

import math
import re

BOUND = "[A-Za-z0-9_$]"  # S-487's identifier characters: a sink is named when not run into one


def test_pure_judgements():
    from dashcheck.browser import (
        ASK_INTERVAL_S,
        DECLARED_FALLBACKS,
        DECLARED_SLICES,
        IDLE_MAX_READS,
        IDLE_SPAN_S,
        NO_SLICE_VAR,
        PALETTE,
        SIDES,
        SLICE_VARS,
        TIMESTAMP_TOLERANCE_S,
        Place,
        colour_fault,
        declared_faults,
        hide_faults,
        idle_faults,
        marker,
        place_faults,
        strip_declarations,
        strip_palette_block,
    )

    # 1. A swept declaration is stripped to the end of its statement, and nothing after it.
    t = 'export const ADDRESS_SINKS = ["location",\n  "history."];\nconst x = location;\n'
    stripped = strip_declarations(t, ("ADDRESS_SINKS",))
    assert "const x = location;" in stripped, stripped
    assert "ADDRESS_SINKS" not in stripped, stripped
    assert '"history."' not in stripped, stripped
    assert re.search(rf"(?<!{BOUND})location(?!{BOUND})", stripped), stripped

    # 2. The palette block alone, markers included; unchanged without both markers.
    assert strip_palette_block("a /*palette*/ --fallback-ink: #111; /*end palette*/ b") == "a  b"
    assert strip_palette_block("a --fallback-ink: #111; b") == "a --fallback-ink: #111; b"
    assert strip_palette_block("a /*palette*/ x") == "a /*palette*/ x"

    # 3. Idle reads over half-open spans, and the gap between consecutive reads.
    assert idle_faults([0, 10, 20, 30, 40, 50, 60]) == []
    assert idle_faults([0, 9.97, 19.96, 29.95, 39.94, 49.93, 59.92]) == []
    # The span from 0 holds 7 reads (59.4 < 59.7) and each of the 6 gaps is 9.9 < 9.95.
    faults = idle_faults([0, 9.9, 19.8, 29.7, 39.6, 49.5, 59.4])
    assert len(faults) == 7, faults

    # 4. Hides placed against an ask: nothing while hidden, the next read due or on show.
    assert hide_faults(0, 1, 4, [10.1]) == []
    assert hide_faults(0, 1, 4, [4.2]) != []
    assert hide_faults(0, 1, 26, [26.1]) == []
    assert hide_faults(0, 1, 26, [10.0]) != []
    assert hide_faults(0, 1, 4, [10.6]) != []

    # 5. Colours against the test colours, transparency, the browser's defaults and slice colours.
    defaults = {
        ("button", None, "background-color"): "rgb(239, 239, 239)",
        ("div", None, "color"): "rgb(0, 0, 0)",
        ("div", None, "background-color"): "rgba(0, 0, 0, 0)",
    }
    tc = {"rgb(10, 20, 30)"}
    sc = frozenset({"rgb(1, 2, 3)"})
    assert colour_fault("button", None, "background-color", "rgb(239, 239, 239)", tc, defaults) is False
    assert colour_fault("div", None, "color", "rgb(1, 2, 3)", tc, defaults) is True
    assert colour_fault("div", None, "color", "rgb(1, 2, 3)", tc, defaults, sc, True) is False
    assert colour_fault("div", None, "color", "rgb(1, 2, 3)", tc, defaults, sc, False) is True
    assert colour_fault("div", None, "color", "rgb(10, 20, 30)", tc, defaults) is False
    assert colour_fault("div", None, "background-color", "rgba(0, 0, 0, 0)", tc, defaults) is False
    assert colour_fault("div", None, "color", "rgb(0, 0, 0)", tc, defaults) is False

    # 6. Places matched by view and text, never by position.
    a = Place("spine", ("text", "x"), ("rgb(0, 0, 0)", "canvas"))
    b = Place("spine", ("text", "y"), ("rgb(9, 9, 9)", "canvas"))
    assert place_faults("light", [a, b], [b, a]) == []
    faults = place_faults("dark", [a], [Place("spine", ("text", "x"), ("rgb(1, 1, 1)", "canvas"))])
    assert len(faults) == 1, faults
    for part in ("dark", "x", "rgb(0, 0, 0)", "rgb(1, 1, 1)"):
        assert part in faults[0], (part, faults)
    faults = place_faults("light", [a, b], [a])
    assert len(faults) == 1 and "y" in faults[0], faults

    # 7. Declared colours: every key present, nothing extra, every value today's.
    slices = (*SLICE_VARS, NO_SLICE_VAR)
    today = {("fallback", t, s): f"rgb({i}, {j}, 0)" for i, t in enumerate(PALETTE) for j, s in enumerate(SIDES)}
    today |= {("slice", v, None): f"rgb(0, 0, {k})" for k, v in enumerate(slices)}
    assert len(today) == 37
    assert declared_faults(dict(today), [], today) == []
    short = dict(today)
    del short[("fallback", PALETTE[0], SIDES[0])]
    assert len(short) == 36
    assert declared_faults(short, [], today) != []
    assert declared_faults(dict(today), ["--slice-10: #000"], today) != []
    key = ("slice", NO_SLICE_VAR, None)
    changed = dict(today) | {key: "rgb(0, 0, 99)"}
    faults = declared_faults(changed, [], today)
    assert len(faults) == 1, faults
    for part in (NO_SLICE_VAR, today[key], "rgb(0, 0, 99)"):
        assert part in faults[0], (part, faults)

    # 8. The switch marker: the first new text, None when there is none.
    assert marker(["Spine · p", "B-88 (was B-01) opens", "x"], {"Spine · p"}) == "B-88 (was B-01) opens"
    assert marker(["a"], {"a"}) is None

    # 9. The constants are the counts they stand for.
    assert IDLE_SPAN_S == 60 - 6 * TIMESTAMP_TOLERANCE_S
    assert IDLE_MAX_READS == math.ceil(60 / ASK_INTERVAL_S) == 6
    assert DECLARED_FALLBACKS == 2 * len(PALETTE) == 26
    assert DECLARED_SLICES == len(SLICE_VARS) + 1 == 11
