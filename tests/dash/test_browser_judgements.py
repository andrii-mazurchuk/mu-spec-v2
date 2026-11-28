"""The browser check's pure judgements: dashcheck.browser's measuring functions (T-490).

No browser and no unit: each judgement the check's verdicts rest on is given hand-built inputs,
worked by hand from S-516's rules. The constants the keys and the palette block are built from are
the ones dashcheck.browser imports from S-509, never written here.
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
        PALETTE_BEGIN,
        PALETTE_DECLARATIONS,
        PALETTE_END,
        PLACEMENTS,
        SIDE_CONDITION,
        SIDES,
        SLICE_VARS,
        TIMESTAMP_TOLERANCE_S,
        TOKENLESS_RUNS,
        Place,
        colour_fault,
        declared_faults,
        fallbackName,
        hide_faults,
        idle_faults,
        marker,
        palette_values,
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

    # 6. Places matched by view and text, never by position, every fault naming its run.
    a = Place("spine", ("text", "x"), ("rgb(0, 0, 0)", "canvas"))
    b = Place("spine", ("text", "y"), ("rgb(9, 9, 9)", "canvas"))
    assert place_faults("framed/light", [a, b], [b, a]) == []
    faults = place_faults("top/dark", [a], [Place("spine", ("text", "x"), ("rgb(1, 1, 1)", "canvas"))])
    assert len(faults) == 1, faults
    for part in ("top/dark", "x", "rgb(0, 0, 0)", "rgb(1, 1, 1)"):
        assert part in faults[0], (part, faults)
    faults = place_faults("framed/dark", [a, b], [a])
    assert len(faults) == 1, faults
    for part in ("y", "framed/dark"):
        assert part in faults[0], (part, faults)

    # 7. The palette block read side by side: each name once per side, 48 keys.
    names = (*SLICE_VARS, NO_SLICE_VAR)
    fallbacks = [fallbackName(t) for t in PALETTE]
    every = fallbacks + list(names)
    assert len(every) == 24

    def decl(name, side, i):
        return f"{name}: rgb({i}, {0 if side == 'light' else 1}, 7);"

    def rule(side, more=()):
        return SIDE_CONDITION[side] + " { " + " ".join([*(decl(n, side, i) for i, n in enumerate(every)), *more]) + " }"

    def block(light_more=()):
        return PALETTE_BEGIN + "\n" + rule("light", light_more) + "\n" + rule("dark") + "\n" + PALETTE_END

    values, extra = palette_values(block())
    assert len(values) == 48 == PALETTE_DECLARATIONS, sorted(values)
    assert extra == [], extra
    for t in PALETTE:
        for s in SIDES:
            assert ("fallback", t, s) in values, (t, s, sorted(values))
    for v in names:
        for s in SIDES:
            assert ("slice", v, s) in values, (v, s, sorted(values))
    assert values[("slice", NO_SLICE_VAR, "light")] != values[("slice", NO_SLICE_VAR, "dark")]
    _, extra = palette_values(block([decl(NO_SLICE_VAR, "light", 23)]))
    assert extra != [], "a declaration repeated under the light rule is not reported"

    # 8. Declared colours: every key present on each side, nothing extra, every value today's.
    today = {("fallback", t, s): f"rgb({i}, {j}, 0)" for i, t in enumerate(PALETTE) for j, s in enumerate(SIDES)}
    today |= {("slice", v, s): f"rgb(0, {j}, {k})" for k, v in enumerate(names) for j, s in enumerate(SIDES)}
    assert len(today) == 48 == len(set(today.values()))
    assert declared_faults(dict(today), [], today) == []
    short = dict(today)
    del short[("fallback", PALETTE[0], SIDES[0])]
    assert len(short) == 47
    assert declared_faults(short, [], today) != []
    assert declared_faults(dict(today), ["--slice-11: #000"], today) != []
    key = ("slice", NO_SLICE_VAR, "dark")
    assert today[key] == "rgb(0, 1, 10)"
    faults = declared_faults(dict(today) | {key: "rgb(0, 1, 99)"}, [], today)
    assert len(faults) == 1, faults
    for part in (NO_SLICE_VAR, "dark", "rgb(0, 1, 10)", "rgb(0, 1, 99)"):
        assert part in faults[0], (part, faults)
    light_on_dark = dict(today) | {("slice", v, "dark"): today[("slice", v, "light")] for v in names}
    faults = declared_faults(light_on_dark, [], today)
    assert len(faults) == 11, faults

    # 9. The switch marker: the first new text, None when there is none.
    assert marker(["Spine · p", "B-88 (was B-01) opens", "x"], {"Spine · p"}) == "B-88 (was B-01) opens"
    assert marker(["a"], {"a"}) is None

    # 10. The constants are the counts they stand for.
    assert IDLE_SPAN_S == 60 - 6 * TIMESTAMP_TOLERANCE_S
    assert IDLE_MAX_READS == math.ceil(60 / ASK_INTERVAL_S) == 6
    assert DECLARED_FALLBACKS == 2 * len(PALETTE) == 26
    assert DECLARED_SLICES == 2 * len(names) == 22
    assert DECLARED_FALLBACKS + DECLARED_SLICES == PALETTE_DECLARATIONS
    assert TOKENLESS_RUNS == tuple((p, s) for p in PLACEMENTS for s in SIDES)
    assert len(TOKENLESS_RUNS) == 4
