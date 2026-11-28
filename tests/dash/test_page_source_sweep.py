"""The page source sweep over render.ts and a fixture template (T-487).

The sweep's constants are read from dashboard/src/render.ts itself (S-509) by
running bun over it; render.ts is the only committed file this test reads.
Every helper is written here from S-509's naming, sweep and palette-block
rules; nothing is imported from dashcheck or any other check module. The swept
set is four scratch files under tmp_path: R, a byte copy of render.ts, read
less its SWEPT_DECLARATIONS; P, an empty probe source; Q, an empty file at
ADDRESS_MODULE; and F, a fixture template built from render.ts's constants,
whose palette block holds a light rule then a dark rule of 24 declarations
each, and whose colour text is read less that block and its markup and address
text whole. The committed template is judged by the template test and the
browser check.
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
RULE = re.compile(r"([^{}]+?)\s*\{([^{}]*)\}")
DECLARATION = re.compile(r"([A-Za-z-][A-Za-z0-9-]*)\s*:\s*([^;{}]+?)\s*(?=[;}])")
COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def read_constants() -> dict:
    render = REPO / RENDER
    assert render.is_file(), f"{RENDER} is absent"
    script = (
        f"const r = await import({json.dumps(render.as_uri())});"
        "console.log(JSON.stringify({"
        "PALETTE: r.PALETTE, SLICE_VARS: r.SLICE_VARS, NO_SLICE_VAR: r.NO_SLICE_VAR,"
        " SIDES: r.SIDES, SIDE_CONDITION: r.SIDE_CONDITION, DARK_MARK: r.DARK_MARK,"
        " PALETTE_DECLARATIONS: r.PALETTE_DECLARATIONS,"
        " MARKUP_SINKS: r.MARKUP_SINKS, ADDRESS_SINKS: r.ADDRESS_SINKS,"
        " ADDRESS_MODULE: r.ADDRESS_MODULE, SWEPT_DECLARATIONS: r.SWEPT_DECLARATIONS,"
        " PALETTE_BEGIN: r.PALETTE_BEGIN, PALETTE_END: r.PALETTE_END,"
        " COLOUR_LITERAL: r.COLOUR_LITERAL.source, COLOUR_VAR: r.COLOUR_VAR.source,"
        " IDENT_CHARS: r.IDENT_CHARS.source,"
        " FALLBACKS: Object.fromEntries(r.PALETTE.map((t) => [t, r.fallbackName(t)])),"
        " COLOURS: Object.fromEntries(r.PALETTE.map((t) => [t, r.colour(t)])),"
        " SLICE_0: r.sliceColour(0), SLICE_3: r.sliceColour(3), SLICE_NULL: r.sliceColour(null)}));"
    )
    done = subprocess.run(
        ["bun", "-e", script], check=False, cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def slice_names(consts: dict) -> list[str]:
    """N: SLICE_VARS followed by NO_SLICE_VAR."""
    return [*consts["SLICE_VARS"], consts["NO_SLICE_VAR"]]


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


def side_line(consts: dict, side: str, fallback_from: int, slice_from: int) -> str:
    """One side's rule: its 13 fallbacks then its 11 slice colours, each a distinct hex value."""
    decls = [f"{consts['FALLBACKS'][t]}: #{fallback_from + i:06x};" for i, t in enumerate(consts["PALETTE"])]
    decls += [f"{v}: #{slice_from + i:06x};" for i, v in enumerate(slice_names(consts))]
    return consts["SIDE_CONDITION"][side] + " { " + " ".join(decls) + " }"


def fixture_template(consts: dict) -> str:
    lines = [
        "<!doctype html><html><head><style>",
        consts["PALETTE_BEGIN"],
        side_line(consts, "light", 0x1, 0x101),
        side_line(consts, "dark", 0x201, 0x301),
        consts["PALETTE_END"],
        "body { color: " + consts["COLOURS"][consts["PALETTE"][1]] + "; }",
        ".s { border-color: " + consts["SLICE_0"] + "; }",
        '</style></head><body><div id="root"></div></body></html>',
    ]
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
    allowed |= {(s, None) for s in slice_names(consts)}
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


def between_markers(template: str, begin: str, end: str) -> str:
    """The text between the template's palette markers, other /* */ comments removed."""
    start = template.index(begin) + len(begin)
    return COMMENT.sub("", template[start : template.index(end, start)])


def rules(template: str, begin: str, end: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """The palette block read as (selector, its (name, value) declarations), rule by rule, in order."""
    inner = between_markers(template, begin, end)
    return [(m.group(1).strip(), DECLARATION.findall(m.group(2))) for m in RULE.finditer(inner)]


def block(template: str, consts: dict) -> list[tuple]:
    """The palette block's faults, read rule by rule; empty when it is the light rule then the dark rule of 24 each.

    Faults: ("rules", n) when not exactly two rules; ("selector", i, s) for a rule under
    the wrong side's condition; ("count", i, name, n) for a name declared other than once
    in rule i; ("other", i, name) for another name in rule i; ("text", rest) for anything
    but whitespace outside the rules.
    """
    begin, end = consts["PALETTE_BEGIN"], consts["PALETTE_END"]
    found = rules(template, begin, end)
    faults: list[tuple] = []
    if len(found) != 2:
        faults.append(("rules", len(found)))
    for i, side in enumerate(consts["SIDES"]):
        if i < len(found) and found[i][0] != consts["SIDE_CONDITION"][side]:
            faults.append(("selector", i, found[i][0]))
    expected = [consts["FALLBACKS"][t] for t in consts["PALETTE"]] + slice_names(consts)
    for i, (_, decls) in enumerate(found):
        counts: dict[str, int] = {}
        for name, _ in decls:
            counts[name] = counts.get(name, 0) + 1
        faults += [("count", i, n, counts.get(n, 0)) for n in expected if counts.get(n, 0) != 1]
        faults += [("other", i, n) for n in counts if n not in expected]
    rest = RULE.sub("", between_markers(template, begin, end)).strip()
    if rest:
        faults.append(("text", rest))
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
    n_names = slice_names(consts)

    def fresh(name: str) -> Path:
        return scratch(tmp_path, name, consts)

    def template_of(root: Path) -> str:
        return (root / TEMPLATE).read_text(encoding="utf-8")

    def block_of(root: Path) -> list[tuple]:
        return block(template_of(root), consts)

    # 1. The constants: today's palette, slice names, sides and count.
    palette = consts["PALETTE"]
    assert len(palette) == 13 == len(set(palette))
    assert consts["SLICE_VARS"] == [f"--slice-{i}" for i in range(1, 11)]
    assert consts["NO_SLICE_VAR"] == "--slice-none"
    assert len(n_names) == 11
    assert list(consts["SIDES"]) == ["light", "dark"]
    assert consts["PALETTE_DECLARATIONS"] == 48 == 2 * (len(palette) + len(n_names))
    assert consts["SLICE_0"] == "var(--slice-1)"
    assert consts["SLICE_3"] == "var(--slice-4)"
    assert consts["SLICE_NULL"] == "var(--slice-none)"
    for t in palette:
        assert consts["COLOURS"][t] == "var(" + t + ", var(" + consts["FALLBACKS"][t] + "))", t

    # 2. As committed and as built: no hit, no block fault, two rules of 24 declarations.
    clean = fresh("clean")
    assert sweep(clean, consts) == []
    assert block_of(clean) == []
    found = rules(template_of(clean), begin, end)
    assert [len(decls) for _, decls in found] == [24, 24]
    assert sum(len(decls) for _, decls in found) == 48

    # 3. The stripping helpers themselves.
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

    # 4. The bounded naming rule, S-509's worked cases.
    ident = consts["IDENT_CHARS"]
    for text, sink in [
        ("Array<any>", "<a"),
        ("<abbr>", "<a"),
        ("allocation", "location"),
        ("locationText", "location"),
        ("myhistory.x", "history."),
        ("_innerHTML", "innerHTML"),
        ("innerHTMLx", "innerHTML"),
    ]:
        assert names(text, sink, ident) is False, (text, sink)
    for text, sink in [
        ("<a href=", "<a"),
        ("<a>", "<a"),
        ("window.location", "location"),
        ("const location =", "location"),
        ("x.history.push", "history."),
        ("el.innerHTML =", "innerHTML"),
    ]:
        assert names(text, sink, ident) is True, (text, sink)
    root = fresh("naming")
    write(root, PROBE, "const a: Array<any> = []\nconst allocation = 1")
    assert sweep(root, consts) == []
    root = fresh("side-mark")
    write(
        root,
        PROBE,
        'win.self === win.top && win.matchMedia("(prefers-color-scheme: dark)").matches\n'
        "win.document.documentElement.classList.add(DARK_MARK)",
    )
    assert sweep(root, consts) == []

    # 5. A markup sink inside el is seen: only the swept declarations are stripped.
    root = fresh("markup")
    path = root / RENDER
    text = path.read_text(encoding="utf-8")
    head = re.search(r"^export function el\b[^{]*\{[^\n]*\n", text, re.MULTILINE)
    assert head, "render.ts has no `export function el(...) {`"
    path.write_text(text[: head.end()] + "  x.innerHTML = s\n" + text[head.end() :], encoding="utf-8", newline="")
    assert sweep(root, consts) == [("markup", RENDER, "innerHTML")]

    # 6. An address sink is a hit outside the address module, and none inside it.
    root = fresh("address-probe")
    write(root, PROBE, "const k = localStorage")
    assert sweep(root, consts) == [("address", PROBE, "localStorage")]
    root = fresh("address-module")
    write(root, address, "const k = localStorage")
    assert sweep(root, consts) == []

    # 7. Colour literals are hits; a palette token through colour() is not.
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

    # 8. Both exemptions are what step 2 relies on: unstripped, render.ts's own declarations
    #    name the sinks, and the fixture's palette block holds colour literals.
    hits = sweep(fresh("unstripped-render"), consts, strip_render=False)
    assert {(kind, rel) for kind, rel, _ in hits} >= {("markup", RENDER), ("address", RENDER)}
    hits = sweep(fresh("unstripped-block"), consts, strip_block=False)
    assert ("colour", TEMPLATE) in {(kind, rel) for kind, rel, _ in hits}

    # 9. Outside the block a colour literal is a hit; a token form is not, on either side.
    root = fresh("template-outside")
    edit(root, TEMPLATE, lambda t: t.replace(end, end + "\np { color: #fff; }", 1))
    assert sweep(root, consts) == [("colour", TEMPLATE, "#fff")]
    root = fresh("template-dark-token")
    edit(root, TEMPLATE, lambda t: t.replace(end, end + "\n.dark p { color: " + consts["COLOURS"][palette[1]] + "; }", 1))
    assert sweep(root, consts) == []

    # 10. A var() is a token with its own fallback or a bare slice colour of today's names, nothing else.
    cases = [
        ("var(--ink)", 1),
        ("var(--ink, var(--fallback-rule))", 1),
        ("var(--ink, var(--fallback-ink))", 0),
        ("var(--slice-3)", 0),
        ("var(--slice-none)", 0),
        ("var(--slice-0)", 1),
        ("var(--slice-3, var(--fallback-ink))", 1),
    ]
    for n, (ref, expected) in enumerate(cases):
        root = fresh(f"var-{n}")
        write(root, PROBE, f'"{ref}"')
        assert sweep(root, consts) == [("var", PROBE, ref)] * expected, ref

    # 11. The block is read rule by rule: each side's 24 names exactly once, light then dark.
    light = side_line(consts, "light", 0x1, 0x101)
    dark = side_line(consts, "dark", 0x201, 0x301)
    none = consts["NO_SLICE_VAR"]
    dark_none = f" {none}: #{0x301 + 10:06x};"
    assert dark_none in dark
    # (a) the dark rule's NO_SLICE_VAR removed: 47, a slice value under the light rule only
    root = fresh("block-a")
    edit(root, TEMPLATE, lambda t: t.replace(dark, dark.replace(dark_none, ""), 1))
    assert ("count", 1, none, 0) in block_of(root)
    # (b) that declaration moved into the light rule: each name twice in the block, but not per rule
    root = fresh("block-b")
    moved = light[: -len(" }")] + dark_none + " }"
    edit(root, TEMPLATE, lambda t: t.replace(dark, dark.replace(dark_none, ""), 1).replace(light, moved, 1))
    assert sum(len(decls) for _, decls in rules(template_of(root), begin, end)) == 48
    faults = block_of(root)
    assert ("count", 0, none, 2) in faults and ("count", 1, none, 0) in faults
    # (c) the light and dark lines swapped
    root = fresh("block-c")
    edit(root, TEMPLATE, lambda t: t.replace(light + "\n" + dark, dark + "\n" + light, 1))
    assert any(f[0] == "selector" for f in block_of(root))
    # (d) the dark rule under a media query instead of SIDE_CONDITION.dark
    root = fresh("block-d")
    media = "@media (prefers-color-scheme: dark) { :root" + dark[len(consts["SIDE_CONDITION"]["dark"]) :] + " }"
    edit(root, TEMPLATE, lambda t: t.replace(dark, media, 1))
    assert block_of(root) != []
    # (e) a twelfth slice colour in the light rule
    root = fresh("block-e")
    edit(root, TEMPLATE, lambda t: t.replace(light, light.replace(" { ", " { --slice-11: #000000; ", 1), 1))
    assert ("other", 0, "--slice-11") in block_of(root)
    # (f) another name inside the dark rule: a block fault, and no sweep hit
    root = fresh("block-f")
    edit(root, TEMPLATE, lambda t: t.replace(dark, dark.replace(" { ", " { color: #fff; ", 1), 1))
    assert ("other", 1, "color") in block_of(root)
    assert sweep(root, consts) == []

    # 12. A second palette begin marker in F is a hit.
    root = fresh("second-begin")
    edit(root, TEMPLATE, lambda t: t.replace(end, end + "\n" + begin, 1))
    assert sweep(root, consts) == [("palette", TEMPLATE, "2 begin, 1 end")]
