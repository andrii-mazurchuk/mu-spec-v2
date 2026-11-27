"""The page source sweep over render.ts and a fixture template (T-474).

The sweep's constants are read from dashboard/src/render.ts itself (S-487) by
running bun over it; render.ts is the only committed file this test reads.
Every helper is written here from S-487's naming, sweep and palette-block
rules; nothing is imported from the browser check (S-434). The swept set is
four scratch files under tmp_path: R, a byte copy of render.ts, read less its
SWEPT_DECLARATIONS; P, an empty probe source; Q, an empty file at
ADDRESS_MODULE; and F, a fixture template built from render.ts's constants,
whose colour text is read less its palette block and its markup and address
text whole. The sweep over every committed page file is the browser check's.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RENDER = "dashboard/src/render.ts"
PROBE = "dashboard/src/probe.ts"
# S-13 TEMPLATE is "index.html", named relative to the dashboard folder.
TEMPLATE = "dashboard/index.html"
BOUND = "[A-Za-z0-9_$]"
DECLARATION = re.compile(r"([A-Za-z-][A-Za-z0-9-]*)\s*:\s*([^;{}]+?)\s*(?=[;}])")
COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def read_constants() -> dict:
    render = REPO / RENDER
    assert render.is_file(), f"{RENDER} is absent"
    script = (
        f"const r = await import({json.dumps(render.as_uri())});"
        "console.log(JSON.stringify({"
        "PALETTE: r.PALETTE, SLICE_VARS: r.SLICE_VARS, NO_SLICE_VAR: r.NO_SLICE_VAR,"
        " SIDES: r.SIDES, SIDE_CONDITION: r.SIDE_CONDITION,"
        " MARKUP_SINKS: r.MARKUP_SINKS, ADDRESS_SINKS: r.ADDRESS_SINKS,"
        " ADDRESS_MODULE: r.ADDRESS_MODULE, SWEPT_DECLARATIONS: r.SWEPT_DECLARATIONS,"
        " PALETTE_BEGIN: r.PALETTE_BEGIN, PALETTE_END: r.PALETTE_END,"
        " COLOUR_LITERAL: r.COLOUR_LITERAL.source, COLOUR_VAR: r.COLOUR_VAR.source,"
        " IDENT_CHARS: r.IDENT_CHARS.source,"
        " FALLBACKS: Object.fromEntries(r.PALETTE.map((t) => [t, r.fallbackName(t)])),"
        " COLOURS: Object.fromEntries(r.PALETTE.map((t) => [t, r.colour(t)])),"
        " SLICE_0: r.sliceColour(0)}));"
    )
    done = subprocess.run(
        ["bun", "-e", script], check=False, cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def strip_declarations(source: str, names: tuple[str, ...], ident: str = BOUND) -> str:
    """source less each `export const <name>` declaration for a name in names, and nothing else.

    A declaration is its head line and every following line beginning with a space,
    a tab, ')', ']' or '}'; it ends before the first line that is empty or begins
    with anything else, or at the end of the text.
    """
    heads = [re.compile(r"export const " + re.escape(n) + rf"(?!{ident})") for n in names]
    lines = source.splitlines(keepends=True)
    kept = []
    i = 0
    while i < len(lines):
        if any(h.match(lines[i]) for h in heads):
            i += 1
            while i < len(lines) and lines[i].strip("\r\n") != "" and lines[i][0] in " \t)]}":
                i += 1
            continue
        kept.append(lines[i])
        i += 1
    return "".join(kept)


def strip_palette_block(text: str, begin: str, end: str) -> str:
    """text less the first begin marker through the first end marker after it; unchanged when either is absent."""
    start = text.find(begin)
    if start < 0:
        return text
    stop = text.find(end, start + len(begin))
    if stop < 0:
        return text
    return text[:start] + text[stop + len(end) :]


def sink_pattern(sink: str, ident: str = BOUND) -> re.Pattern:
    before = rf"(?<!{BOUND})" if re.fullmatch(ident, sink[0]) else ""
    after = rf"(?!{BOUND})" if re.fullmatch(ident, sink[-1]) else ""
    return re.compile(before + re.escape(sink) + after)


def names(text: str, sink: str, ident: str = BOUND) -> bool:
    """True when sink occurs in text, not run into an identifier on either identifier-character end."""
    return sink_pattern(sink, ident).search(text) is not None


def scratch(tmp_path: Path, name: str, consts: dict) -> Path:
    """A fresh scratch root holding R, P, Q and F as the contract lays them out."""
    root = tmp_path / name
    (root / "dashboard" / "src").mkdir(parents=True)
    shutil.copyfile(REPO / RENDER, root / RENDER)
    (root / PROBE).write_bytes(b"")
    (root / consts["ADDRESS_MODULE"]).parent.mkdir(parents=True, exist_ok=True)
    (root / consts["ADDRESS_MODULE"]).write_bytes(b"")
    (root / TEMPLATE).write_text(fixture_template(consts), encoding="utf-8", newline="")
    return root


def fixture_template(consts: dict) -> str:
    n = 0
    lines = ["<!doctype html><html><head><style>", consts["PALETTE_BEGIN"]]
    for side in consts["SIDES"]:
        decls = []
        for t in consts["PALETTE"]:
            n += 1
            decls.append(f"{consts['FALLBACKS'][t]}: #{n:06x};")
        lines.append(consts["SIDE_CONDITION"][side] + " { " + " ".join(decls) + " }")
    decls = [f"{v}: #{0x101 + i:06x};" for i, v in enumerate([*consts["SLICE_VARS"], consts["NO_SLICE_VAR"]])]
    lines.append(":root { " + " ".join(decls) + " }")
    lines.append(consts["PALETTE_END"])
    lines.append("body { color: " + consts["COLOURS"][consts["PALETTE"][1]] + "; }")
    lines.append(".s { border-color: " + consts["SLICE_0"] + "; }")
    lines.append('</style></head><body><div id="root"></div></body></html>')
    return "\n".join(lines) + "\n"


def sweep(root: Path, consts: dict, strip_render: bool = True, strip_block: bool = True) -> list[tuple[str, str, str]]:
    """Every hit over R, P, Q and F as (sweep, file, what matched), the file relative to root.

    Sweeps: markup, address, colour (a COLOUR_LITERAL match), var (a COLOUR_VAR
    match that is neither a token with its own fallback nor a bare slice colour)
    and palette (F's begin and end markers not exactly one each, in order).
    """
    ident = consts["IDENT_CHARS"]
    begin, end = consts["PALETTE_BEGIN"], consts["PALETTE_END"]
    literal = re.compile(consts["COLOUR_LITERAL"])
    var = re.compile(consts["COLOUR_VAR"])
    allowed = {(t, consts["FALLBACKS"][t]) for t in consts["PALETTE"]}
    allowed |= {(s, None) for s in [*consts["SLICE_VARS"], consts["NO_SLICE_VAR"]]}
    hits = []
    for rel in (RENDER, PROBE, consts["ADDRESS_MODULE"], TEMPLATE):
        text = (root / rel).read_text(encoding="utf-8")
        if rel == RENDER and strip_render:
            text = strip_declarations(text, tuple(consts["SWEPT_DECLARATIONS"]), ident)
        colour_text = text
        if rel == TEMPLATE:
            first = text.find(begin)
            if not (text.count(begin) == 1 and text.count(end) == 1 and text.find(end, first) > first):
                hits.append(("palette", rel, f"{text.count(begin)} begin, {text.count(end)} end"))
            if strip_block:
                colour_text = strip_palette_block(text, begin, end)
        for sink in consts["MARKUP_SINKS"]:
            hits += [("markup", rel, sink)] * len(sink_pattern(sink, ident).findall(text))
        if rel != consts["ADDRESS_MODULE"]:
            for sink in consts["ADDRESS_SINKS"]:
                hits += [("address", rel, sink)] * len(sink_pattern(sink, ident).findall(text))
        hits += [("colour", rel, m.group(0)) for m in literal.finditer(colour_text)]
        hits += [("var", rel, m.group(0)) for m in var.finditer(colour_text) if (m.group(1), m.group(2)) not in allowed]
    return hits


def declarations(text: str, begin: str, end: str) -> list[tuple[str, str]]:
    """The (name, value) declarations between the template's palette markers, other comments removed."""
    start = text.index(begin) + len(begin)
    inner = COMMENT.sub("", text[start : text.index(end, start)])
    return DECLARATION.findall(inner)


def block(template: str, consts: dict) -> list[tuple]:
    """The palette block's faults: ("count", name, n) for a declared name counted wrong, ("other", name) for another name."""
    counts: dict[str, int] = {}
    for name, _ in declarations(template, consts["PALETTE_BEGIN"], consts["PALETTE_END"]):
        counts[name] = counts.get(name, 0) + 1
    expected = {consts["FALLBACKS"][t]: 2 for t in consts["PALETTE"]}
    expected |= {s: 1 for s in [*consts["SLICE_VARS"], consts["NO_SLICE_VAR"]]}
    faults: list[tuple] = [("count", n, counts.get(n, 0)) for n, k in expected.items() if counts.get(n, 0) != k]
    faults += [("other", n) for n in counts if n not in expected]
    return faults


def write(root: Path, rel: str, text: str) -> None:
    (root / rel).write_text(text + "\n", encoding="utf-8", newline="")


def edit(root: Path, rel: str, change) -> None:
    path = root / rel
    path.write_text(change(path.read_text(encoding="utf-8")), encoding="utf-8", newline="")


def test_sweep_exemptions(tmp_path):
    consts = read_constants()
    begin, end = consts["PALETTE_BEGIN"], consts["PALETTE_END"]
    address = consts["ADDRESS_MODULE"]
    assert address not in (RENDER, PROBE, TEMPLATE), address

    def fresh(name: str) -> Path:
        return scratch(tmp_path, name, consts)

    def block_of(root: Path) -> list[tuple]:
        return block((root / TEMPLATE).read_text(encoding="utf-8"), consts)

    # 1. As committed and as built: no hit, no block fault, 37 declarations, 13 distinct tokens.
    clean = fresh("clean")
    assert sweep(clean, consts) == []
    assert block_of(clean) == []
    assert len(declarations((clean / TEMPLATE).read_text(encoding="utf-8"), begin, end)) == 37
    assert len(consts["PALETTE"]) == 13 == len(set(consts["PALETTE"]))

    # 2. The stripping helpers themselves.
    stripped = strip_declarations(
        'export const ADDRESS_SINKS = [\n  "location",\n  "history.",\n]\nconst x = location\n', ("ADDRESS_SINKS",)
    )
    assert stripped == "const x = location\n"
    assert names(stripped, "location") is True
    assert strip_declarations('export const ADDRESS_SINKS = ["location"]\n\nconst y = 1\n', ("ADDRESS_SINKS",)) == (
        "\nconst y = 1\n"
    )
    assert strip_palette_block("a /*palette*/ x; /*end palette*/ b", begin, end) == "a  b"
    assert strip_palette_block("a /*palette*/ x", begin, end) == "a /*palette*/ x"

    # 3. The bounded naming rule, S-487's worked cases.
    ident = consts["IDENT_CHARS"]
    for text, sink in [
        ("Array<any>", "<a"),
        ("<abbr>", "<a"),
        ("allocation", "location"),
        ("locationText", "location"),
        ("myhistory.x", "history."),
        ("_innerHTML", "innerHTML"),
        ("innerHTMLx", "innerHTML"),
        ("new ImageData(", "new Image"),
    ]:
        assert names(text, sink, ident) is False, (text, sink)
    for text, sink in [
        ("<a href=", "<a"),
        ("<a>", "<a"),
        ("window.location", "location"),
        ("x.history.push", "history."),
        ("el.innerHTML =", "innerHTML"),
        ("new Image()", "new Image"),
    ]:
        assert names(text, sink, ident) is True, (text, sink)
    root = fresh("naming")
    write(root, PROBE, "const a: Array<any> = []\nconst allocation = 1")
    assert sweep(root, consts) == []

    # 4. A markup sink inside el is seen: only the swept declarations are stripped.
    root = fresh("markup")
    path = root / RENDER
    text = path.read_text(encoding="utf-8")
    head = re.search(r"^export function el\b[^{]*\{[^\n]*\n", text, re.MULTILINE)
    assert head, "render.ts has no `export function el(...) {`"
    path.write_text(text[: head.end()] + "  x.innerHTML = s\n" + text[head.end() :], encoding="utf-8", newline="")
    assert sweep(root, consts) == [("markup", RENDER, "innerHTML")]

    # 5. An address sink is a hit outside the address module, and none inside it.
    root = fresh("address-probe")
    write(root, PROBE, "const k = localStorage")
    assert sweep(root, consts) == [("address", PROBE, "localStorage")]
    root = fresh("address-module")
    write(root, address, "const k = localStorage")
    assert sweep(root, consts) == []

    # 6. Colour literals are hits; a palette token through colour() is not.
    for n, (line, expected) in enumerate(
        [
            ('const c = "#1a2b3c"', [("colour", PROBE, "#1a2b3c")]),
            ('const c = "rgb(1, 2, 3)"', [("colour", PROBE, "rgb(")]),
            ('const c = "' + consts["COLOURS"]["--ink"] + '"', []),
        ]
    ):
        root = fresh(f"colour-{n}")
        write(root, PROBE, line)
        assert sweep(root, consts) == expected, line

    # 7. Both exemptions are what step 1 relies on: unstripped, render.ts's own declarations
    #    name the sinks, and the fixture's palette block holds colour literals.
    hits = sweep(fresh("unstripped-render"), consts, strip_render=False)
    assert {(kind, rel) for kind, rel, _ in hits} >= {("markup", RENDER), ("address", RENDER)}
    hits = sweep(fresh("unstripped-block"), consts, strip_block=False)
    assert ("colour", TEMPLATE) in {(kind, rel) for kind, rel, _ in hits}

    # 8. A colour literal in F's style: a hit outside the block; inside it, a block fault.
    root = fresh("template-outside")
    edit(root, TEMPLATE, lambda t: t.replace(end, end + "\np { color: #fff; }", 1))
    assert sweep(root, consts) == [("colour", TEMPLATE, "#fff")]
    root = fresh("template-inside")
    edit(root, TEMPLATE, lambda t: t.replace(":root { ", ":root { color: #fff; ", 1))
    assert sweep(root, consts) == []
    assert ("other", "color") in block_of(root)

    # 9. A var() is a token with its own fallback or a bare slice colour, nothing else.
    cases = [
        ("var(--ink)", 1),
        ("var(--ink, var(--fallback-rule))", 1),
        ("var(--ink, var(--fallback-ink))", 0),
        ("var(--slice-3)", 0),
        ("var(--slice-3, var(--fallback-ink))", 1),
        ("var(--brandish, var(--fallback-brandish))", 1),
    ]
    for n, (ref, expected) in enumerate(cases):
        root = fresh(f"var-{n}")
        write(root, PROBE, f'"{ref}"')
        assert sweep(root, consts) == [("var", PROBE, ref)] * expected, ref

    # 10. A block missing, adding or repeating a declaration is a block fault.
    first = consts["FALLBACKS"][consts["PALETTE"][0]]
    root = fresh("block-short")
    edit(root, TEMPLATE, lambda t: t.replace(f"{first}: #000001; ", "", 1))
    assert ("count", first, 1) in block_of(root)
    root = fresh("block-twelfth")
    edit(root, TEMPLATE, lambda t: t.replace(":root { ", ":root { --slice-10: #000000; ", 1))
    assert ("other", "--slice-10") in block_of(root)
    root = fresh("block-repeat")
    none = consts["NO_SLICE_VAR"]
    edit(root, TEMPLATE, lambda t: t.replace(f"{none}: ", f"{none}: #000000; {none}: ", 1))
    assert ("count", none, 2) in block_of(root)

    # 11. A second palette begin marker in F is a hit.
    root = fresh("second-begin")
    edit(root, TEMPLATE, lambda t: t.replace(end, end + "\n" + begin, 1))
    assert sweep(root, consts) == [("palette", TEMPLATE, "2 begin, 1 end")]
