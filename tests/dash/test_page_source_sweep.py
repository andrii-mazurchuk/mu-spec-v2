"""The page source sweep over dashboard/src and the template (T-155).

The sweep's constants are read from dashboard/src/render.ts itself (S-202) by
running bun over it, and render.ts alone is read less its SWEPT_DECLARATIONS
through dashcheck.browser.strip_declarations (S-284); every other file under
dashboard/src, and the template dashboard/index.html, is read whole.
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
COPY_IGNORE = shutil.ignore_patterns(".git", "node_modules", "out", "__pycache__", ".pytest_cache")


def read_constants() -> dict:
    render = REPO / RENDER
    assert render.is_file(), f"{RENDER} is absent"
    script = (
        f"const r = await import({json.dumps(render.as_uri())});"
        "console.log(JSON.stringify({"
        "MARKUP_SINKS: r.MARKUP_SINKS, ADDRESS_SINKS: r.ADDRESS_SINKS,"
        " ADDRESS_MODULE: r.ADDRESS_MODULE, SWEPT_DECLARATIONS: r.SWEPT_DECLARATIONS,"
        " COLOUR_LITERAL: r.COLOUR_LITERAL.source, PALETTE: r.PALETTE}));"
    )
    done = subprocess.run(
        ["bun", "-e", script], check=False, cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def copy_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    shutil.copytree(REPO, root, ignore=COPY_IGNORE)
    return root


def append(root: Path, rel: str, line: str) -> None:
    path = root / rel
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write("\n" + line + "\n")


def sweep(root: Path, consts: dict, strip: bool = True) -> list[tuple[str, str, str]]:
    """Every hit as (sweep, file, what matched), the file relative to root."""
    from dashcheck.browser import strip_declarations

    colour = re.compile(consts["COLOUR_LITERAL"])
    files = sorted(p for p in (root / "dashboard" / "src").rglob("*") if p.is_file())
    files.append(root / TEMPLATE)
    hits = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        if rel == RENDER and strip:
            text = strip_declarations(text, tuple(consts["SWEPT_DECLARATIONS"]))
        for sink in consts["MARKUP_SINKS"]:
            hits += [("markup", rel, sink)] * text.count(sink)
        if rel != consts["ADDRESS_MODULE"]:
            for sink in consts["ADDRESS_SINKS"]:
                hits += [("address", rel, sink)] * text.count(sink)
        hits += [("colour", rel, m.group(0)) for m in colour.finditer(text)]
    return hits


def test_sweep_exemptions(tmp_path):
    consts = read_constants()
    assert consts["ADDRESS_MODULE"] == ADDRESS
    assert (REPO / TEMPLATE).is_file(), f"{TEMPLATE} is absent"

    # 1. The repository as it stands: no hit, and a palette to colour from.
    assert sweep(copy_repo(tmp_path, "clean"), consts) == []
    assert len(consts["PALETTE"]) >= 1

    # 2. A markup sink inside el is seen: only the declarations are stripped.
    root = copy_repo(tmp_path, "markup")
    path = root / RENDER
    text = path.read_text(encoding="utf-8")
    head = re.search(r"export function el\([^)]*\)\s*:\s*HTMLElement\s*\{", text)
    assert head, "render.ts has no `export function el(...): HTMLElement {`"
    path.write_text(text[: head.end()] + "\n  x.innerHTML = s;" + text[head.end() :], encoding="utf-8", newline="")
    assert sweep(root, consts) == [("markup", RENDER, "innerHTML")]

    # 3. An address sink is a hit outside the address module, and none inside it.
    root = copy_repo(tmp_path, "address-views")
    append(root, VIEWS, "const k = localStorage;")
    assert sweep(root, consts) == [("address", VIEWS, "localStorage")]
    root = copy_repo(tmp_path, "address-module")
    append(root, ADDRESS, "const k = localStorage;")
    assert sweep(root, consts) == []

    # 4. Colour literals are hits; a palette token through colour() is not.
    root = copy_repo(tmp_path, "colour-hex")
    append(root, VIEWS, 'const c = "#1a2b3c";')
    assert sweep(root, consts) == [("colour", VIEWS, "#1a2b3c")]
    root = copy_repo(tmp_path, "colour-rgb")
    append(root, VIEWS, 'const c = "rgb(1, 2, 3)";')
    assert sweep(root, consts) == [("colour", VIEWS, "rgb(")]
    root = copy_repo(tmp_path, "colour-token")
    append(root, VIEWS, 'const c = colour("--fg");')
    assert sweep(root, consts) == []

    # 5. Unstripped, render.ts's own declarations name the sinks: the exemption is what step 1 relies on.
    hits = sweep(copy_repo(tmp_path, "unstripped"), consts, strip=False)
    assert {(kind, rel) for kind, rel, _ in hits} >= {("markup", RENDER), ("address", RENDER)}
    assert {rel for _, rel, _ in hits} == {RENDER}
