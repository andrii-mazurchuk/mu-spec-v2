"""The page template's shape (T-509).

The S-509 constants are read from dashboard/src/render.ts by running bun over
it; S-13's TEMPLATE and MARKER are read by bun from the build module's
own `export const` initialisers. The build module is not imported: importing
it runs the build, which writes the page. The template is parsed by rules of
this file's own, with html.parser only on text that holds no style element.
"""

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PAGE = REPO / "dashboard"
RENDER = "dashboard/src/render.ts"
BUILD = "dashboard/build.ts"
BUN = shutil.which("bun") or "bun"
RULE = re.compile(r"([^{}]+?)\s*\{([^{}]*)\}")
DECLARATION = re.compile(r"([A-Za-z-][A-Za-z0-9-]*)\s*:\s*([^;{}]+?)\s*(?=[;}])")
SCHEME_OR_NETWORK = re.compile(r"^\s*([A-Za-z][A-Za-z0-9+.-]*:|//)")
COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# S-509's bounded naming rule: an identifier character, as its IDENT_CHARS.
BOUND = "[A-Za-z0-9_$]"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def read_constants() -> dict:
    render = REPO / RENDER
    assert render.is_file(), f"{RENDER} is absent"
    assert (REPO / BUILD).is_file(), f"{BUILD} is absent"
    script = (
        f"const r = await import({json.dumps(render.as_uri())});"
        f"const src = await Bun.file({json.dumps(str(REPO / BUILD))}).text();"
        "const s13 = {};"
        "for (const name of ['TEMPLATE', 'MARKER']) {"
        "  const m = src.match(new RegExp('^export const ' + name + '\\\\s*=\\\\s*([^\\\\n;]+);', 'm'));"
        "  s13[name] = m ? (0, eval)(m[1]) : null;"
        "}"
        "console.log(JSON.stringify({...s13,"
        " PALETTE: r.PALETTE, SLICE_VARS: r.SLICE_VARS, NO_SLICE_VAR: r.NO_SLICE_VAR,"
        " SIDE_CONDITION: r.SIDE_CONDITION, PALETTE_DECLARATIONS: r.PALETTE_DECLARATIONS,"
        " PALETTE_BEGIN: r.PALETTE_BEGIN, PALETTE_END: r.PALETTE_END,"
        " MARKUP_SINKS: r.MARKUP_SINKS, ADDRESS_SINKS: r.ADDRESS_SINKS,"
        " COLOUR_LITERAL: r.COLOUR_LITERAL.source, COLOUR_VAR: r.COLOUR_VAR.source,"
        " FALLBACKS: Object.fromEntries(r.PALETTE.map((t) => [t, r.fallbackName(t)]))}));"
    )
    done = subprocess.run(
        [BUN, "-e", script], check=False, cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    consts = json.loads(done.stdout.strip().splitlines()[-1])
    for name in ("TEMPLATE", "MARKER"):
        assert isinstance(consts[name], str), f"{BUILD} has no `export const {name} = <string>;`"
    return consts


def names(text: str, sink: str) -> bool:
    """True when sink occurs in text, not run into an identifier on either identifier-character end."""
    before = rf"(?<!{BOUND})" if re.fullmatch(BOUND, sink[0]) else ""
    after = rf"(?!{BOUND})" if re.fullmatch(BOUND, sink[-1]) else ""
    return re.search(before + re.escape(sink) + after, text) is not None


def rules(s: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """(selector stripped, declarations) for each rule in s, /* */ comments removed first."""
    s = COMMENT.sub("", s)
    # The declarations are read through the rule's closing brace, so a last one with no ';' still counts.
    return [(m.group(1).strip(), DECLARATION.findall(s, m.start(2), m.end())) for m in RULE.finditer(s)]


class Tags(HTMLParser):
    """Start tags with their attributes, end tags, non-whitespace text outside tags and comments, title text."""

    def __init__(self) -> None:
        super().__init__()
        self.starts: list[tuple[str, dict]] = []
        self.ends: list[str] = []
        self.data: list[str] = []
        self.title = ""
        self._open: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.starts.append((tag, {k.lower(): v for k, v in attrs}))
        if tag not in VOID:
            self._open.append(tag)

    def handle_endtag(self, tag):
        self.ends.append(tag)
        if tag in self._open:
            del self._open[len(self._open) - 1 - self._open[::-1].index(tag) :]

    def handle_data(self, data):
        if self._open and self._open[-1] == "title":
            self.title += data
        if data.strip():
            self.data.append(data.strip())


def parse(s: str) -> Tags:
    assert not re.search(r"<style\b", s, re.IGNORECASE), "html.parser is only given text holding no style element"
    p = Tags()
    p.feed(s)
    p.close()
    return p


def inner(text: str, tag: str) -> tuple[int, int]:
    """(start, end) offsets of the text between the first <tag ...> tag's end and the next </tag>."""
    opening = re.search(rf"<{tag}\b[^>]*>", text, re.IGNORECASE)
    assert opening, f"no <{tag}> tag"
    closing = re.compile(rf"</{tag}\s*>", re.IGNORECASE).search(text, opening.end())
    assert closing, f"no </{tag}>"
    return opening.end(), closing.start()


def test_template_shape():
    consts = read_constants()
    path = PAGE / consts["TEMPLATE"]
    assert path.is_file(), f"{consts['TEMPLATE']} is absent from dashboard/"
    text = path.read_bytes().decode("utf-8")
    begin, end, marker = consts["PALETTE_BEGIN"], consts["PALETTE_END"], consts["MARKER"]

    # 1. One document: a doctype, one html, one head, one body.
    assert re.match(r"\s*<!doctype html", text, re.IGNORECASE), "the template does not open with <!doctype html"
    for tag in ("html", "head", "body"):
        assert len(re.findall(rf"<{tag}\b", text, re.IGNORECASE)) == 1, f"<{tag} not exactly once"
    head_at, head_end = inner(text, "head")
    body_at, body_end = inner(text, "body")
    head, body = text[head_at:head_end], text[body_at:body_end]

    # 2. Exactly one style element, in head; head otherwise holds charset, viewport and title only.
    styles = [
        (m.end(), re.compile(r"</style", re.IGNORECASE).search(text, m.end()))
        for m in re.finditer(r"<style\b[^>]*>", text, re.IGNORECASE)
    ]
    assert all(close for _, close in styles), "a <style> with no </style>"
    assert len(styles) == 1, f"{len(styles)} style elements"
    style_at, style_end = styles[0][0], styles[0][1].start()
    style = text[style_at:style_end]
    assert head_at <= style_at and style_end <= head_end, "the style element is not inside head"
    element = re.compile(r"<style\b.*?</style\s*>", re.IGNORECASE | re.DOTALL)
    head_tags = parse(element.sub("", head, count=1))
    metas = [a for t, a in head_tags.starts if t == "meta"]
    charsets = [a for a in metas if "charset" in a]
    assert len(charsets) == 1, f"{len(charsets)} metas with charset"
    assert (charsets[0]["charset"] or "").strip().lower() == "utf-8", charsets[0]
    assert len([a for a in metas if (a.get("name") or "").strip().lower() == "viewport"]) == 1, "no single viewport meta"
    assert [t for t, _ in head_tags.starts].count("title") == 1, "no single title"
    assert head_tags.title.strip(), "the title is empty"
    for tag in ("link", "base", "style"):
        assert tag not in [t for t, _ in head_tags.starts], f"head holds a {tag} tag"
    assert not [a for a in metas if (a.get("http-equiv") or "").strip().lower() == "refresh"], "head holds a meta refresh"
    assert "<script" not in text.lower(), "the template holds a script of its own"

    # 3. One palette block, opening the style element.
    assert text.count(begin) == 1 and text.count(end) == 1, (text.count(begin), text.count(end))
    b_at, e_at = text.index(begin), text.index(end)
    assert b_at < e_at, "PALETTE_END comes before PALETTE_BEGIN"
    assert style_at <= b_at and e_at + len(end) <= style_end, "the palette block is not inside the style element"
    assert style.lstrip().startswith(begin), "the style element does not open with the palette block"

    # 4. The block: a light rule then a dark rule, each declaring every fallback and slice colour once.
    slices = [*consts["SLICE_VARS"], consts["NO_SLICE_VAR"]]
    expected = sorted([*consts["FALLBACKS"].values(), *slices])
    assert len(expected) == len(set(expected)) == 24, expected
    block = text[b_at + len(begin) : e_at]
    block_rules = rules(block)
    sides = consts["SIDE_CONDITION"]
    assert [s for s, _ in block_rules] == [sides["light"], sides["dark"]], [s for s, _ in block_rules]
    for selector, decls in block_rules:
        assert sorted(n for n, _ in decls) == expected, (selector, [n for n, _ in decls])
    assert sum(len(d) for _, d in block_rules) == consts["PALETTE_DECLARATIONS"] == 48
    assert not RULE.sub("", COMMENT.sub("", block)).strip(), "the block holds text outside its two rules"

    # 5. The page's own rules: no colour literal, only token-with-fallback or bare slice vars, no dark side of their own.
    own = style[: b_at - style_at] + style[e_at + len(end) - style_at :]
    literal = re.compile(consts["COLOUR_LITERAL"])
    assert not literal.search(own), literal.findall(own)
    allowed = {(t, f) for t, f in consts["FALLBACKS"].items()} | {(s, None) for s in slices}
    for m in re.finditer(consts["COLOUR_VAR"], own):
        assert (m.group(1), m.group(2)) in allowed, m.group(0)
    for selector, _ in rules(own):
        assert sides["dark"] not in selector, f"a page rule sits under {selector!r}"
    assert "prefers-color-scheme" not in own.lower(), "the page's own rules hold prefers-color-scheme"

    # 6. The body is the root: it holds the marker once and nothing else, and carries no id.
    assert text.count(marker) == 1, f"MARKER {text.count(marker)} times"
    assert body_at <= text.index(marker) < body_end, "MARKER is not inside body"
    assert body.strip() == marker, f"body holds more than MARKER: {body.strip()!r}"
    body_tags = parse(body.replace(marker, ""))
    assert body_tags.starts == [], [t for t, _ in body_tags.starts]
    assert body_tags.data == [], body_tags.data
    body_tag = parse(re.search(r"<body\b[^>]*>", text, re.IGNORECASE).group(0))
    assert "id" not in body_tag.starts[0][1], f"the body tag carries an id: {body_tag.starts[0][1]}"

    # 7. No attribute names a URL with a scheme or a leading //; no url( and no @import.
    outside = parse(element.sub("", text, count=1))
    for tag, a in outside.starts:
        for key, value in a.items():
            assert not (value and SCHEME_OR_NETWORK.match(value)), (tag, key, value)
    assert "url(" not in text.lower(), "the template holds url("
    assert "@import" not in text.lower(), "the template holds @import"

    # 8. No markup or address sink is named anywhere in the template.
    for sink in [*consts["MARKUP_SINKS"], *consts["ADDRESS_SINKS"]]:
        assert not names(text, sink), f"the template names {sink!r}"
