"""The page template's shape (T-473).

The S-390 constants are read from dashboard/src/render.ts by running bun over
it; S-13's TEMPLATE, ENTRY and MARKER are read by bun from the build module's
own `export const` initialisers. The build module is not imported: importing
it runs the build, which writes the page. The template is parsed by rules of
this file's own, with html.parser only on text that holds no style element.
"""

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PAGE = REPO / "dashboard"
RENDER = "dashboard/src/render.ts"
BUILD = "dashboard/build.ts"
DECLARATION = re.compile(r"([A-Za-z-][A-Za-z0-9-]*)\s*:\s*([^;{}]+?)\s*(?=[;}])")
SCHEME_OR_NETWORK = re.compile(r"^\s*([A-Za-z][A-Za-z0-9+.-]*:|//)")
COMMENT = re.compile(r"/\*.*?\*/", re.S)
WORD = re.compile(r"[A-Za-z0-9_$]")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def read_constants() -> dict:
    render = REPO / RENDER
    assert render.is_file(), f"{RENDER} is absent"
    assert (REPO / BUILD).is_file(), f"{BUILD} is absent"
    script = (
        f"const r = await import({json.dumps(render.as_uri())});"
        f"const src = await Bun.file({json.dumps(str(REPO / BUILD))}).text();"
        "const s13 = {};"
        "for (const name of ['TEMPLATE', 'ENTRY', 'MARKER']) {"
        "  const m = src.match(new RegExp('^export const ' + name + '\\\\s*=\\\\s*([^\\\\n;]+);', 'm'));"
        "  s13[name] = m ? (0, eval)(m[1]) : null;"
        "}"
        "console.log(JSON.stringify({...s13,"
        " PALETTE: r.PALETTE, SLICE_VARS: r.SLICE_VARS, NO_SLICE_VAR: r.NO_SLICE_VAR,"
        " PALETTE_BEGIN: r.PALETTE_BEGIN, PALETTE_END: r.PALETTE_END,"
        " MARKUP_SINKS: r.MARKUP_SINKS, ADDRESS_SINKS: r.ADDRESS_SINKS,"
        " FALLBACKS: r.PALETTE.map((t) => r.fallbackName(t))}));"
    )
    done = subprocess.run(
        ["bun", "-e", script], check=False, cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    consts = json.loads(done.stdout.strip().splitlines()[-1])
    for name in ("TEMPLATE", "ENTRY", "MARKER"):
        assert isinstance(consts[name], str), f"{BUILD} has no `export const {name} = <string>;`"
    return consts


def names(text: str, sink: str) -> bool:
    """True when sink occurs in text, not touching another word character at a word-character end."""
    at = text.find(sink)
    while at != -1:
        end = at + len(sink)
        before = WORD.match(sink[0]) and at > 0 and WORD.match(text[at - 1])
        after = WORD.match(sink[-1]) and end < len(text) and WORD.match(text[end])
        if not before and not after:
            return True
        at = text.find(sink, at + 1)
    return False


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
    assert not re.search(r"<style\b", s, re.I), "html.parser is only given text holding no style element"
    p = Tags()
    p.feed(s)
    p.close()
    return p


def inner(text: str, tag: str) -> tuple[int, int]:
    """(start, end) offsets of the text between the first <tag ...> tag's end and the next </tag>."""
    opening = re.search(rf"<{tag}\b[^>]*>", text, re.I)
    assert opening, f"no <{tag}> tag"
    closing = re.compile(rf"</{tag}\s*>", re.I).search(text, opening.end())
    assert closing, f"no </{tag}>"
    return opening.end(), closing.start()


def test_template_shape():
    consts = read_constants()
    path = PAGE / consts["TEMPLATE"]
    assert path.is_file(), f"{consts['TEMPLATE']} is absent from dashboard/"
    text = path.read_bytes().decode("utf-8")
    begin, end, marker = consts["PALETTE_BEGIN"], consts["PALETTE_END"], consts["MARKER"]

    # 1. One document: a doctype, one html, one head, one body.
    assert re.match(r"\s*<!doctype html", text, re.I), "the template does not open with <!doctype html"
    for tag in ("html", "head", "body"):
        assert len(re.findall(rf"<{tag}\b", text, re.I)) == 1, f"<{tag} not exactly once"
    head_at, head_end = inner(text, "head")
    body_at, body_end = inner(text, "body")
    head, body = text[head_at:head_end], text[body_at:body_end]

    # 2. Exactly one style element, in head; head otherwise holds charset, viewport and title only.
    styles = [
        (m.end(), re.compile(r"</style", re.I).search(text, m.end()))
        for m in re.finditer(r"<style\b[^>]*>", text, re.I)
    ]
    assert all(close for _, close in styles), "a <style> with no </style>"
    assert len(styles) == 1, f"{len(styles)} style elements"
    style_at, style_end = styles[0][0], styles[0][1].start()
    style = text[style_at:style_end]
    assert head_at <= style_at and style_end <= head_end, "the style element is not inside head"
    element = re.compile(r"<style\b.*?</style\s*>", re.I | re.S)
    head_tags = parse(element.sub("", head, count=1))
    metas = [a for t, a in head_tags.starts if t == "meta"]
    charsets = [a for a in metas if "charset" in a]
    assert len(charsets) == 1, f"{len(charsets)} metas with charset"
    assert (charsets[0]["charset"] or "").strip().lower() == "utf-8", charsets[0]
    assert len([a for a in metas if (a.get("name") or "").strip().lower() == "viewport"]) == 1, "no single viewport meta"
    assert [t for t, _ in head_tags.starts].count("title") == 1, "no single title"
    assert head_tags.title.strip(), "the title is empty"
    for tag in ("link", "base", "script", "style"):
        assert tag not in [t for t, _ in head_tags.starts], f"head holds a {tag} tag"
    assert not [a for a in metas if (a.get("http-equiv") or "").strip().lower() == "refresh"], "head holds a meta refresh"

    # 3. One palette block, opening the style element.
    assert text.count(begin) == 1 and text.count(end) == 1, (text.count(begin), text.count(end))
    b_at, e_at = text.index(begin), text.index(end)
    assert b_at < e_at, "PALETTE_END comes before PALETTE_BEGIN"
    assert style_at <= b_at and e_at + len(end) <= style_end, "the palette block is not inside the style element"
    assert style.lstrip().startswith(begin), "the style element does not open with the palette block"

    # 4. The block's declarations: each fallback twice, each slice colour once, 37 in all.
    block = COMMENT.sub("", text[b_at + len(begin) : e_at])

    def declared(name: str) -> int:
        return len(re.findall(r"(?<![A-Za-z0-9-])" + re.escape(name) + r"\s*:", block))

    for fallback in consts["FALLBACKS"]:
        assert declared(fallback) == 2, (fallback, declared(fallback))
    for slice_var in [*consts["SLICE_VARS"], consts["NO_SLICE_VAR"]]:
        assert declared(slice_var) == 1, (slice_var, declared(slice_var))
    expected = 2 * len(consts["PALETTE"]) + len(consts["SLICE_VARS"]) + 1
    assert expected == 37
    assert len(DECLARATION.findall(block)) == expected, DECLARATION.findall(block)

    # 5. The body: the root element only, then the marker once.
    assert text.count(marker) == 1, f"MARKER {text.count(marker)} times"
    assert body_at <= text.index(marker) < body_end, "MARKER is not inside body"
    body_tags = parse(body.replace(marker, ""))
    assert len(body_tags.starts) == 1, [t for t, _ in body_tags.starts]
    root, attrs = body_tags.starts[0]
    assert body_tags.ends == [root], (root, body_tags.ends)
    assert body_tags.data == [], body_tags.data
    root_end = re.compile(rf"</{re.escape(root)}\s*>", re.I).search(body)
    assert root_end, f"no </{root}> in body"
    assert body.index(marker) > root_end.start(), "MARKER is not after the root element's end tag"

    # 6. The entry looks the root up by its id.
    r = attrs.get("id")
    assert r, f"the root <{root}> has no id"
    entry = PAGE / consts["ENTRY"]
    assert entry.is_file(), f"ENTRY {consts['ENTRY']} is absent from dashboard/"
    entry_text = entry.read_text(encoding="utf-8")
    assert re.search(r"([\"'`])" + re.escape(r) + r"\1", entry_text), f"{consts['ENTRY']} holds no literal {r!r}"

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
