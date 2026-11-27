"""The page source sweep over dashboard/src and the template (T-447).

The sweep's constants are read from dashboard/src/render.ts itself (S-390) by
running bun over it. render.ts is read less its SWEPT_DECLARATIONS through
dashcheck.browser.strip_declarations; the template's colour text is read less
its palette block through dashcheck.browser.strip_palette_block, its markup
and address text whole; every other file under dashboard/src is read whole.
The palette block itself is read through dashcheck.browser.palette_values.
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
TEMPLATE = "dashboard/index.html"
DECLARED_FALLBACKS = 26
DECLARED_SLICES = 11
COPY_IGNORE = shutil.ignore_patterns("node_modules", "out", "__pycache__")


def read_constants() -> dict:
    render = REPO / RENDER
    assert render.is_file(), f"{RENDER} is absent"
    script = (
        f"const r = await import({json.dumps(render.as_uri())});"
        "console.log(JSON.stringify({"
        "PALETTE: r.PALETTE, SLICE_VARS: r.SLICE_VARS, NO_SLICE_VAR: r.NO_SLICE_VAR,"
        " MARKUP_SINKS: r.MARKUP_SINKS, ADDRESS_SINKS: r.ADDRESS_SINKS, ADDRESS_MODULE: r.ADDRESS_MODULE,"
        " SWEPT_DECLARATIONS: r.SWEPT_DECLARATIONS, PALETTE_BEGIN: r.PALETTE_BEGIN, PALETTE_END: r.PALETTE_END,"
        " FALLBACK_PREFIX: r.FALLBACK_PREFIX, COLOUR_LITERAL: r.COLOUR_LITERAL.source,"
        " COLOUR_VAR: r.COLOUR_VAR.source, FALLBACKS: r.PALETTE.map((t) => r.fallbackName(t))}));"
    )
    done = subprocess.run(
        ["bun", "-e", script], check=False, cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def copy_page(tmp_path: Path, name: str) -> Path:
    """A copy of the repository's page sources (dashboard/), which is all the sweep reads."""
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


def sweep(root: Path, consts: dict, strip_render: bool = True, strip_block: bool = True) -> list[tuple[str, str, str]]:
    """Every hit as (sweep, file, what matched), the file relative to root.

    Sweeps: markup, address, colour (a COLOUR_LITERAL match), var (a COLOUR_VAR
    match that is neither a token with its own fallback nor a bare slice colour)
    and palette (the template's begin and end markers not exactly one each, in order).
    """
    from dashcheck.browser import strip_declarations, strip_palette_block

    literal = re.compile(consts["COLOUR_LITERAL"])
    var = re.compile(consts["COLOUR_VAR"])
    allowed = {(t, f) for t, f in zip(consts["PALETTE"], consts["FALLBACKS"], strict=True)}
    allowed |= {(s, None) for s in [*consts["SLICE_VARS"], consts["NO_SLICE_VAR"]]}
    files = sorted(p for p in (root / "dashboard" / "src").rglob("*") if p.is_file())
    files.append(root / TEMPLATE)
    hits = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        if rel == RENDER and strip_render:
            text = strip_declarations(text, tuple(consts["SWEPT_DECLARATIONS"]))
        colour_text = text
        if rel == TEMPLATE:
            begin, end = consts["PALETTE_BEGIN"], consts["PALETTE_END"]
            if not (text.count(begin) == 1 and text.count(end) == 1 and text.index(begin) < text.index(end)):
                hits.append(("palette", rel, f"{text.count(begin)} begin, {text.count(end)} end"))
            if strip_block:
                colour_text = strip_palette_block(text)
        for sink in consts["MARKUP_SINKS"]:
            hits += [("markup", rel, sink)] * text.count(sink)
        if rel != consts["ADDRESS_MODULE"]:
            for sink in consts["ADDRESS_SINKS"]:
                hits += [("address", rel, sink)] * text.count(sink)
        hits += [("colour", rel, m.group(0)) for m in literal.finditer(colour_text)]
        hits += [("var", rel, m.group(0)) for m in var.finditer(colour_text) if (m.group(1), m.group(2)) not in allowed]
    return hits


def key_names(key) -> tuple[str, ...]:
    """The text parts of one palette_values key: the key itself, or each member of a tuple key."""
    return tuple(str(k) for k in key) if isinstance(key, tuple) else (str(key),)


def block(root: Path, consts: dict) -> tuple[list[str], int, int, object]:
    """(faults, fallback key count, slice key count, extra) of the template's palette block."""
    from dashcheck.browser import palette_values

    values, extra = palette_values((root / TEMPLATE).read_text(encoding="utf-8"))
    slices = {*consts["SLICE_VARS"], consts["NO_SLICE_VAR"]}
    keys = [key_names(k) for k in values]
    fallbacks = sum(1 for names in keys if any(consts["FALLBACK_PREFIX"] in n for n in names))
    slice_keys = sum(1 for names in keys if any(n in slices for n in names))
    faults = []
    if fallbacks != DECLARED_FALLBACKS:
        faults.append(f"{fallbacks} fallback keys")
    if slice_keys != DECLARED_SLICES:
        faults.append(f"{slice_keys} slice keys")
    if extra:
        faults.append(f"extra {extra!r}")
    return faults, fallbacks, slice_keys, extra


def in_block(consts: dict, find: str, change):
    """An edit of the template applying change(block text, offset of the first `find` in it) inside the palette block."""

    def apply(text: str) -> str:
        start = text.index(consts["PALETTE_BEGIN"])
        end = text.index(consts["PALETTE_END"], start)
        inner = text[start:end]
        at = inner.index(find)
        return text[:start] + change(inner, at) + text[end:]

    return apply


def test_sweep_exemptions(tmp_path):
    consts = read_constants()
    assert consts["ADDRESS_MODULE"] == ADDRESS
    assert (REPO / TEMPLATE).is_file(), f"{TEMPLATE} is absent"
    begin, end = consts["PALETTE_BEGIN"], consts["PALETTE_END"]

    # 1. The repository as it stands: no hit, no block fault, and the node's 13 distinct tokens.
    clean = copy_page(tmp_path, "clean")
    assert sweep(clean, consts) == []
    assert block(clean, consts)[0] == []
    assert len(consts["PALETTE"]) == 13
    assert len(set(consts["PALETTE"])) == 13

    # 2. A markup sink inside el is seen: only the declarations are stripped.
    root = copy_page(tmp_path, "markup")
    path = root / RENDER
    text = path.read_text(encoding="utf-8")
    head = re.search(r"export function el\b[^{]*\{", text)
    assert head, "render.ts has no `export function el(...) {`"
    path.write_text(text[: head.end()] + "\n  x.innerHTML = s;" + text[head.end() :], encoding="utf-8", newline="")
    assert sweep(root, consts) == [("markup", RENDER, "innerHTML")]

    # 3. An address sink is a hit outside the address module, and none inside it.
    root = copy_page(tmp_path, "address-views")
    append(root, VIEWS, "const k = localStorage;")
    assert sweep(root, consts) == [("address", VIEWS, "localStorage")]
    root = copy_page(tmp_path, "address-module")
    append(root, ADDRESS, "const k = localStorage;")
    assert sweep(root, consts) == []

    # 4. Colour literals are hits; a palette token through colour() is not.
    root = copy_page(tmp_path, "colour-hex")
    append(root, VIEWS, 'const c = "#1a2b3c";')
    assert sweep(root, consts) == [("colour", VIEWS, "#1a2b3c")]
    root = copy_page(tmp_path, "colour-rgb")
    append(root, VIEWS, 'const c = "rgb(1, 2, 3)";')
    assert sweep(root, consts) == [("colour", VIEWS, "rgb(")]
    root = copy_page(tmp_path, "colour-token")
    append(root, VIEWS, 'const c = colour("--ink");')
    assert sweep(root, consts) == []

    # 5. Both exemptions are what step 1 relies on: unstripped, render.ts's own declarations
    #    name the sinks, and the template's palette block holds colour literals.
    hits = sweep(copy_page(tmp_path, "unstripped-render"), consts, strip_render=False)
    assert {(kind, rel) for kind, rel, _ in hits} >= {("markup", RENDER), ("address", RENDER)}
    assert {rel for _, rel, _ in hits} == {RENDER}
    hits = sweep(copy_page(tmp_path, "unstripped-block"), consts, strip_block=False)
    assert hits, "the template's palette block gave no colour literal unstripped"
    assert {(kind, rel) for kind, rel, _ in hits} == {("colour", TEMPLATE)}

    # 6. A colour literal in the template's style: a hit outside the block; inside it, a block fault.
    root = copy_page(tmp_path, "template-outside")
    edit(root, TEMPLATE, lambda t: t.replace(end, end + "\ncolor: #fff;", 1))
    assert sweep(root, consts) == [("colour", TEMPLATE, "#fff")]
    root = copy_page(tmp_path, "template-inside")
    edit(root, TEMPLATE, in_block(consts, consts["FALLBACK_PREFIX"], lambda b, at: b[:at] + "color: #fff;\n" + b[at:]))
    assert sweep(root, consts) == []
    faults, _, _, extra = block(root, consts)
    assert faults
    assert extra and "color" in repr(extra)

    # 7. A var() is a token with its own fallback or a bare slice colour, nothing else.
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
        append(root, VIEWS, f'const c = "{ref}";')
        assert sweep(root, consts) == [("var", VIEWS, ref)] * expected, ref

    # 8. A block lacking one fallback declaration, or adding a twelfth slice colour, is a block fault.
    root = copy_page(tmp_path, "block-short")
    first = re.compile(re.escape(consts["FALLBACK_PREFIX"]) + r"[A-Za-z0-9-]+\s*:[^;]*;[ \t]*\n?")
    edit(root, TEMPLATE, in_block(consts, consts["FALLBACK_PREFIX"], lambda b, at: b[:at] + first.sub("", b[at:], count=1)))
    faults, fallbacks, _, _ = block(root, consts)
    assert fallbacks == DECLARED_FALLBACKS - 1
    assert faults
    root = copy_page(tmp_path, "block-twelfth")
    edit(root, TEMPLATE, in_block(consts, consts["SLICE_VARS"][0], lambda b, at: b[:at] + "--slice-10: #000000;\n" + b[at:]))
    faults, _, _, extra = block(root, consts)
    assert extra
    assert faults

    # 9. A second palette begin marker in the template is a hit.
    root = copy_page(tmp_path, "second-begin")
    edit(root, TEMPLATE, lambda t: t.replace(end, end + "\n" + begin, 1))
    assert [h for h in sweep(root, consts) if h[0] == "palette"] != []
