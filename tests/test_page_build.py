"""The dashboard page build: its committed manifest, staging, line endings and build checks.

Modules under test are imported inside each test, so a module not yet written fails only the
tests that need it.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def snapshot(folder: Path) -> dict[str, bytes | None]:
    """Every path under folder, relative and POSIX, mapped to its bytes (None for a folder)."""
    return {
        p.relative_to(folder).as_posix(): (p.read_bytes() if p.is_file() else None)
        for p in sorted(folder.rglob("*"))
    }


def regular_files(folder: Path) -> dict[str, bytes]:
    """The regular files under folder, relative and POSIX, mapped to their bytes."""
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


SAFE_TAIL = b"\nbun-version\nmanifest.txt\n"

# (case folder, manifest bytes or None for no manifest.txt, reason, entry to check or ...)
MANIFEST_FAULTS = [
    ("absent", None, "manifest_absent", None),
    ("not_utf8", b"\xff\r\n", "not_utf8", ...),
    ("carriage_return", b"../x\r\nbun-version\r\nmanifest.txt\r\n", "carriage_return", None),
    ("no_final_newline", b"bun-version\nmanifest.txt", "no_final_newline", ...),
    ("blank_line", b"build.ts\n\nbun-version\nmanifest.txt\n", "blank_line", ...),
    ("unsafe_dotdot", b"../x" + SAFE_TAIL, "unsafe_path", "../x"),
    ("unsafe_dot", b"./x" + SAFE_TAIL, "unsafe_path", "./x"),
    ("unsafe_root", b"/x" + SAFE_TAIL, "unsafe_path", "/x"),
    ("unsafe_drive", b"C:/x" + SAFE_TAIL, "unsafe_path", "C:/x"),
    ("unsafe_backslash", b"a\\b" + SAFE_TAIL, "unsafe_path", "a\\b"),
    ("unsafe_empty_segment", b"a//b" + SAFE_TAIL, "unsafe_path", "a//b"),
    ("duplicate", b"bun-version\nbun-version\nmanifest.txt\n", "duplicate", "bun-version"),
    ("unordered", b"manifest.txt\nbun-version\n", "unordered", ...),
    ("self_unlisted", b"bun-version\n", "self_unlisted", ...),
    ("version_unlisted", b"manifest.txt\n", "version_unlisted", ...),
]


def test_read_manifest_faults(tmp_path):
    from pagebuild.manifest import ManifestError, read_manifest

    src = tmp_path / "valid"
    src.mkdir()
    (src / "manifest.txt").write_bytes(
        b"build.ts\nbun-version\nindex.html\nmanifest.txt\nsrc/B.ts\nsrc/a.ts\nsrc/main.ts\n"
    )
    before = snapshot(src)
    assert read_manifest(src) == (
        "build.ts", "bun-version", "index.html", "manifest.txt", "src/B.ts", "src/a.ts", "src/main.ts",
    )
    assert snapshot(src) == before

    for case, data, reason, entry in MANIFEST_FAULTS:
        src = tmp_path / case
        src.mkdir()
        if data is not None:
            (src / "manifest.txt").write_bytes(data)
        before = snapshot(src)
        with pytest.raises(ManifestError) as caught:
            read_manifest(src)
        assert caught.value.reason == reason, case
        if entry is not ...:
            assert caught.value.entry == entry, case
        assert snapshot(src) == before, case


def test_line_endings_held():
    from pagebuild.manifest import read_manifest

    root = REPO_ROOT
    lines = [line.removesuffix(b"\r") for line in (root / ".gitattributes").read_bytes().split(b"\n")]
    assert b"dashboard/** -text" in lines
    assert b"mu_spec/dashboard.html -text" in lines

    dashboard = root / "dashboard"
    held = [
        p for p in dashboard.rglob("*")
        if p.is_file() and p.relative_to(dashboard).parts[0] != "out"
    ]
    held.append(root / "mu_spec" / "dashboard.html")
    for path in held:
        assert b"\r" not in path.read_bytes(), path

    entries = read_manifest(dashboard)
    assert "manifest.txt" in entries
    assert "bun-version" in entries


def test_stage_copies_only_listed(tmp_path):
    from pagebuild.manifest import ManifestError, pinned_version, read_manifest, stage

    src = tmp_path / "dashboard"
    (src / "src").mkdir(parents=True)
    (src / "build.ts").write_bytes(b"x")
    (src / "bun-version").write_bytes(b"1.2.3\n")
    (src / "src" / "main.ts").write_bytes(b"a\nb\rc")
    (src / "manifest.txt").write_bytes(b"build.ts\nbun-version\nmanifest.txt\nsrc/main.ts\n")
    (src / "extra.ts").write_bytes(b"extra")
    (src / "src" / "x.ts").write_bytes(b"unlisted")
    source = regular_files(src)

    entries = read_manifest(src)
    scratch = tmp_path / "s1"
    scratch.mkdir()
    stage(src, entries, scratch)

    staged = regular_files(scratch)
    assert set(staged) == {"build.ts", "bun-version", "manifest.txt", "src/main.ts"}
    for name, data in staged.items():
        assert data == source[name], name
    assert (scratch / "src" / "main.ts").read_bytes() == b"a\nb\rc"
    assert pinned_version(scratch) == "1.2.3"
    assert regular_files(src) == source

    with pytest.raises(ValueError):
        stage(src, entries, scratch)

    fresh_empty = tmp_path / "s2"
    fresh_empty.mkdir()
    with pytest.raises(ManifestError) as caught:
        stage(src, ("build.ts", "src/gone.ts", "src/main.ts"), fresh_empty)
    assert caught.value.reason == "listed_file_missing"
    assert caught.value.entry == "src/gone.ts"

    fresh_empty2 = tmp_path / "s3"
    fresh_empty2.mkdir()
    with pytest.raises(ManifestError) as caught:
        stage(src, ("src",), fresh_empty2)
    assert caught.value.reason == "listed_file_missing"
    assert caught.value.entry == "src"

    assert regular_files(src) == source


def run_build(scratch: Path, build_ts: bytes, template: str, main_ts: str):
    """Lay out build.ts, index.html and src/main.ts in scratch and run the builder there."""
    import shutil
    import subprocess

    bun = shutil.which("bun")
    assert bun is not None, "page builder not installed: bun"
    (scratch / "src").mkdir(parents=True)
    (scratch / "build.ts").write_bytes(build_ts)
    (scratch / "index.html").write_bytes(template.encode("utf-8"))
    (scratch / "src" / "main.ts").write_bytes(main_ts.encode("utf-8"))
    done = subprocess.run([bun, "run", "build.ts"], check=False, cwd=scratch, capture_output=True)
    return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")


def test_build_script(tmp_path):
    build_ts = (REPO_ROOT / "dashboard" / "build.ts").read_bytes()
    main_ts = 'document.title = "x";\n'

    scratch = tmp_path / "one_page"
    code, _, stderr = run_build(
        scratch, build_ts, "<html><head><style>b{}</style></head><body>\n<!--bundle-->\n</body></html>\n", main_ts,
    )
    assert code == 0, stderr
    assert set(regular_files(scratch)) == {"build.ts", "index.html", "src/main.ts", "out/dashboard.html"}
    p = (scratch / "out" / "dashboard.html").read_bytes().decode("utf-8")
    assert p.startswith('<html><head><style>b{}</style></head><body>\n<script type="module">')
    assert p.endswith("</script>\n</body></html>\n")
    assert "<!--bundle-->" not in p
    assert p.count("<script") == 1
    assert str(scratch) not in p

    scratch = tmp_path / "marker_twice"
    code, _, stderr = run_build(
        scratch, build_ts, "<html><body>\n<!--bundle-->\n<!--bundle-->\n</body></html>\n", main_ts,
    )
    assert code != 0
    assert stderr.strip() == "template must hold the marker once, found 2"
    assert not (scratch / "out" / "dashboard.html").exists()

    scratch = tmp_path / "no_marker"
    code, _, stderr = run_build(scratch, build_ts, "<html><body>\n</body></html>\n", main_ts)
    assert code != 0
    assert stderr.strip() == "template must hold the marker once, found 0"

    scratch = tmp_path / "bad_bundle"
    code, _, stderr = run_build(
        scratch, build_ts, "<html><body>\n<!--bundle-->\n</body></html>\n", 'import "./missing";\n',
    )
    assert code != 0
    assert "\n" not in stderr.strip(), stderr
    assert stderr.strip().startswith("bundle failed: "), stderr

    text = build_ts.decode("utf-8")
    for line in (
        'export const TEMPLATE = "index.html";',
        'export const ENTRY = "src/main.ts";',
        'export const MARKER = "<!--bundle-->";',
        'export const OUT_PAGE = "out/dashboard.html";',
    ):
        assert line in text, line
