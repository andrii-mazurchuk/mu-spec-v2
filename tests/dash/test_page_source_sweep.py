"""The page source sweep over dashboard/src and the template (T-472).

The sweep's constants are read from dashboard/src/render.ts itself (S-390) by
running bun over it. Every helper is written here from S-390's sweep and
palette-block rules; nothing is imported from the browser check (S-434). render.ts
is read less its SWEPT_DECLARATIONS; the template's colour text is read less its
palette block, its markup and address text whole; every other file under
dashboard/src is read whole.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RENDER = "dashboard/src/render.ts"
VIEWS = "dashboard/src/views.ts"
ADDRESS = "dashboard/src/address.ts"
BUILD = "dashboard/build.ts"
COPY_IGNORE = shutil.ignore_patterns("node_modules", "out", "__pycache__")
IDENT = "A-Za-z0-9_$"
DECLARATION = re.compile(r"([A-Za-z-][A-Za-z0-9-]*)\s*:\s*([^;{}]+?)\s*(?=[;}])")
COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def read_constants() -> dict:
    render = REPO / RENDER
    assert render.is_file(), f"{RENDER} is absent"
    script = (
        f"const r = await import({json.dumps(render.as_uri())});"
        "console.log(JSON.stringify({"
        "PALETTE: r.PALETTE, SLICE_VARS: r.SLICE_VARS, NO_SLICE_VAR: r.NO_SLICE_VAR,"
        " FALLBACK_PREFIX: r.FALLBACK_PREFIX, MARKUP_SINKS: r.MARKUP_SINKS, ADDRESS_SINKS: r.ADDRESS_SINKS,"
        " ADDRESS_MODULE: r.ADDRESS_MODULE, SWEPT_DECLARATIONS: r.SWEPT_DECLARATIONS,"
        " PALETTE_BEGIN: r.PALETTE_BEGIN, PALETTE_END: r.PALETTE_END,"
        " COLOUR_LITERAL: r.COLOUR_LITERAL.source, COLOUR_VAR: r.COLOUR_VAR.source,"
        " FALLBACKS: r.PALETTE.map((t) => r.fallbackName(t))}));"
    )
    done = subprocess.run(
        ["bun", "-e", script], check=False, cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def template_path() -> str:
    """The template, root-relative: the file S-13 TEMPLATE names, relative to dashboard/."""
    found = re.search(r'^export const TEMPLATE = "([^"]+)"', (REPO / BUILD).read_text(encoding="utf-8"), re.MULTILINE)
    assert found, f"{BUILD} declares no TEMPLATE"
    return "dashboard/" + found.group(1)


def strip_declarations(source: str, names: tuple[str, ...]) -> str:
    """source less each top-level `export const <name>` statement for a name in names, and nothing else."""
    heads = [re.compile(r"export const " + re.escape(n) + rf"(?![{IDENT}])") for n in names]
    lines = source.splitlines(keepends=True)
    kept = []
    i = 0
    while i < len(lines):
        if any(h.match(lines[i]) for h in heads):
            i += 1
            while i < len(lines):
                line = lines[i]
                if line.strip("\r\n") == "" or line[0] in " \t":
                    i += 1
                elif line[0] in "]})":
                    i += 1
                    break
                else:
                    break
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


def sink_pattern(sink: str) -> re.Pattern:
    before = rf"(?<![{IDENT}])" if re.match(rf"[{IDENT}]", sink[0]) else ""
    after = rf"(?![{IDENT}])" if re.match(rf"[{IDENT}]", sink[-1]) else ""
    return re.compile(before + re.escape(sink) + after)


def names(text: str, sink: str) -> bool:
    """True when sink occurs in text, not run into an identifier on either identifier-character end."""
    return sink_pattern(sink).search(text) is not None


def sweep(root: Path, consts: dict, strip_render: bool = True, strip_block: bool = True) -> list[tuple[str, str, str]]:
    """Every hit as (sweep, file, what matched), the file relative to root.

    Sweeps: markup, address, colour (a COLOUR_LITERAL match), var (a COLOUR_VAR
    match that is neither a token with its own fallback nor a bare slice colour)
    and palette (the template's begin and end markers not exactly one each, in order).
    """
    template = template_path()
    begin, end = consts["PALETTE_BEGIN"], consts["PALETTE_END"]
    literal = re.compile(consts["COLOUR_LITERAL"])
    var = re.compile(consts["COLOUR_VAR"])
    allowed = {(t, f) for t, f in zip(consts["PALETTE"], consts["FALLBACKS"], strict=True)}
    allowed |= {(s, None) for s in [*consts["SLICE_VARS"], consts["NO_SLICE_VAR"]]}
    files = sorted(p for p in (root / "dashboard" / "src").rglob("*") if p.is_file())
    files.append(root / template)
    hits = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        if rel == RENDER and strip_render:
            text = strip_declarations(text, tuple(consts["SWEPT_DECLARATIONS"]))
        colour_text = text
        if rel == template:
            first = text.find(begin)
            if not (text.count(begin) == 1 and text.count(end) == 1 and text.find(end, first) > first):
                hits.append(("palette", rel, f"{text.count(begin)} begin, {text.count(end)} end"))
            if strip_block:
                colour_text = strip_palette_block(text, begin, end)
        for sink in consts["MARKUP_SINKS"]:
            hits += [("markup", rel, sink)] * len(sink_pattern(sink).findall(text))
        if rel != consts["ADDRESS_MODULE"]:
            for sink in consts["ADDRESS_SINKS"]:
                hits += [("address", rel, sink)] * len(sink_pattern(sink).findall(text))
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
    expected = {f: 2 for f in consts["FALLBACKS"]}
    expected |= {s: 1 for s in [*consts["SLICE_VARS"], consts["NO_SLICE_VAR"]]}
    faults: list[tuple] = [("count", n, counts.get(n, 0)) for n, k in expected.items() if counts.get(n, 0) != k]
    faults += [("other", n) for n in counts if n not in expected]
    return faults


def copy_page(tmp_path: Path, name: str) -> Path:
    """A fresh copy of the repository's dashboard/ folder, which holds the sources and the template."""
    root = tmp_path / name
    shutil.copytree(REPO / "dashboard", root / "dashboard", ignore=COPY_IGNORE)
    return root


def append(root: Path, rel: str, line: str) -> None:
    path = root / rel
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write("\n" + line + "\n")


def edit(root: Path, rel: str, change) -> None:
    path = root / rel
    path.write_text(change(path.read_text(encoding="utf-8")), encoding="utf-8", newline="")


def in_block(consts: dict, change):
    """An edit of the template applying change(block text) to the stretch inside the palette markers."""

    def apply(text: str) -> str:
        start = text.index(consts["PALETTE_BEGIN"]) + len(consts["PALETTE_BEGIN"])
        stop = text.index(consts["PALETTE_END"], start)
        return text[:start] + change(text[start:stop]) + text[stop:]

    return apply


def test_sweep_exemptions(tmp_path):
    consts = read_constants()
    assert consts["ADDRESS_MODULE"] == ADDRESS
    template = template_path()
    assert (REPO / template).is_file(), f"{template} is absent"
    begin, end = consts["PALETTE_BEGIN"], consts["PALETTE_END"]
    fallbacks = set(consts["FALLBACKS"])

    def block_of(root: Path) -> list[tuple]:
        return block((root / template).read_text(encoding="utf-8"), consts)

    # 1. The copy as committed: no hit, no block fault, and the node's 13 distinct tokens.
    clean = copy_page(tmp_path, "clean")
    assert sweep(clean, consts) == []
    assert block_of(clean) == []
    assert len(consts["PALETTE"]) == 13
    assert len(set(consts["PALETTE"])) == 13

    # 2. The helpers themselves.
    stripped = strip_declarations(
        'export const ADDRESS_SINKS = ["location",\n  "history."]\nconst x = location\n', ("ADDRESS_SINKS",)
    )
    assert stripped == "const x = location\n"
    assert names(stripped, "location") is True
    assert strip_palette_block("a /*palette*/ x; /*end palette*/ b", begin, end) == "a  b"
    assert strip_palette_block("a /*palette*/ x", begin, end) == "a /*palette*/ x"

    # 3. A markup sink inside el is seen: only the swept declarations are stripped.
    root = copy_page(tmp_path, "markup")
    path = root / RENDER
    text = path.read_text(encoding="utf-8")
    head = re.search(r"^export function el\b[^{]*\{[^\n]*\n", text, re.MULTILINE)
    assert head, "render.ts has no `export function el(...) {`"
    path.write_text(text[: head.end()] + "  x.innerHTML = s\n" + text[head.end() :], encoding="utf-8", newline="")
    assert sweep(root, consts) == [("markup", RENDER, "innerHTML")]

    # 4. An address sink is a hit outside the address module, and none inside it.
    root = copy_page(tmp_path, "address-views")
    append(root, VIEWS, "const k = localStorage")
    assert sweep(root, consts) == [("address", VIEWS, "localStorage")]
    root = copy_page(tmp_path, "address-module")
    append(root, ADDRESS, "const k = localStorage")
    assert sweep(root, consts) == []

    # 5. Colour literals are hits; a palette token through colour() is not.
    root = copy_page(tmp_path, "colour-hex")
    append(root, VIEWS, 'const c = "#1a2b3c"')
    assert sweep(root, consts) == [("colour", VIEWS, "#1a2b3c")]
    root = copy_page(tmp_path, "colour-rgb")
    append(root, VIEWS, 'const c = "rgb(1, 2, 3)"')
    assert sweep(root, consts) == [("colour", VIEWS, "rgb(")]
    root = copy_page(tmp_path, "colour-token")
    append(root, VIEWS, 'colour("--ink")')
    assert sweep(root, consts) == []

    # 6. Both exemptions are what step 1 relies on: unstripped, render.ts's own declarations
    #    name the sinks, and the template's palette block holds colour literals.
    hits = sweep(copy_page(tmp_path, "unstripped-render"), consts, strip_render=False)
    assert {(kind, rel) for kind, rel, _ in hits} >= {("markup", RENDER), ("address", RENDER)}
    hits = sweep(copy_page(tmp_path, "unstripped-block"), consts, strip_block=False)
    assert ("colour", template) in {(kind, rel) for kind, rel, _ in hits}

    # 7. A colour literal in the template's style: a hit outside the block; inside it, a block fault.
    root = copy_page(tmp_path, "template-outside")
    edit(root, template, lambda t: t.replace(end, end + "\ncolor: #fff;", 1))
    assert sweep(root, consts) == [("colour", template, "#fff")]
    root = copy_page(tmp_path, "template-inside")
    first_fallback = consts["FALLBACK_PREFIX"]
    edit(root, template, in_block(consts, lambda b: b.replace(first_fallback, "color: #fff;\n" + first_fallback, 1)))
    assert sweep(root, consts) == []
    assert ("other", "color") in block_of(root)

    # 8. A var() is a token with its own fallback or a bare slice colour, nothing else.
    cases = [
        ("var(--ink)", 1),
        ("var(--ink, var(--fallback-rule))", 1),
        ("var(--ink, var(--fallback-ink))", 0),
        ("var(--slice-3)", 0),
        ("var(--slice-3, var(--fallback-ink))", 1),
        ("var(--brandish, var(--fallback-brandish))", 1),
    ]
    for n, (ref, expected) in enumerate(cases):
        root = copy_page(tmp_path, f"var-{n}")
        append(root, VIEWS, f'"{ref}"')
        assert sweep(root, consts) == [("var", VIEWS, ref)] * expected, ref

    # 9. A block missing, adding or repeating a declaration is a block fault.
    root = copy_page(tmp_path, "block-short")
    original = (root / template).read_text(encoding="utf-8")
    removed = next(n for n, _ in declarations(original, begin, end) if n in fallbacks)
    one = re.compile(re.escape(removed) + r"\s*:[^;{}]*;?")
    edit(root, template, in_block(consts, lambda b: one.sub("", b, count=1)))
    assert ("count", removed, 1) in block_of(root)
    root = copy_page(tmp_path, "block-twelfth")
    edit(root, template, in_block(consts, lambda b: b.replace(first_fallback, "--slice-10: #000000;\n" + first_fallback, 1)))
    assert ("other", "--slice-10") in block_of(root)
    root = copy_page(tmp_path, "block-repeat")
    none_decl = re.compile(re.escape(consts["NO_SLICE_VAR"]) + r"\s*:[^;{}]*;?")
    twice = lambda m: m.group(0).rstrip(";") + ";\n" + m.group(0)
    edit(root, template, in_block(consts, lambda b: none_decl.sub(twice, b, count=1)))
    assert ("count", consts["NO_SLICE_VAR"], 2) in block_of(root)

    # 10. A second palette begin marker in the template is a hit.
    root = copy_page(tmp_path, "second-begin")
    edit(root, template, lambda t: t.replace(end, end + "\n" + begin, 1))
    assert [h for h in sweep(root, consts) if h[0] == "palette"] != []
