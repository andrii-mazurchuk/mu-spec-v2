"""The dashboard page build: its committed manifest, staging, line endings and build checks.

Modules under test are imported inside each test, so a module not yet written fails only the
tests that need it.
"""

import json
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


def test_committed_page_equals_fresh_build():
    from pagebuild.check import check_build

    verdict = check_build(REPO_ROOT)
    assert verdict.kind == "pass", verdict.message
    assert verdict.message == "committed page equals a fresh build"


FIXTURE_MANIFEST = b"build.ts\nbun-version\nmanifest.txt\n"
BUILD_TS_HEAD = 'import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "fs";\n'


def builder_version() -> str:
    """The trimmed output of `bun --version`; the builder must be installed."""
    import shutil
    import subprocess

    bun = shutil.which("bun")
    assert bun is not None, "page builder not installed: bun"
    return subprocess.run([bun, "--version"], check=True, capture_output=True).stdout.decode("utf-8").strip()


def append_log(log: Path) -> str:
    """The build.ts statement appending `<cwd> <Date.now()>` to log."""
    return f'appendFileSync({json.dumps(log.as_posix())}, process.cwd() + " " + Date.now() + "\\n");\n'


def write_page(name: str, text: str) -> str:
    """The build.ts statements writing text to out/<name>."""
    return f'mkdirSync("out", {{ recursive: true }});\nwriteFileSync("out/{name}", {json.dumps(text)});\n'


def fixture_repo(
    r: Path, build_ts: str, version: str, committed: bytes | None = None, manifest: bytes | None = FIXTURE_MANIFEST,
) -> Path:
    """A repository r with dashboard/ holding manifest, bun-version and build.ts, and the committed page."""
    (r / "dashboard").mkdir(parents=True)
    if manifest is not None:
        (r / "dashboard" / "manifest.txt").write_bytes(manifest)
    (r / "dashboard" / "bun-version").write_bytes(version.encode("utf-8") + b"\n")
    (r / "dashboard" / "build.ts").write_bytes((BUILD_TS_HEAD + build_ts).encode("utf-8"))
    (r / "mu_spec").mkdir()
    if committed is not None:
        (r / "mu_spec" / "dashboard.html").write_bytes(committed)
    return r


def log_lines(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_check_build_verdicts(tmp_path, monkeypatch):
    from pagebuild.check import check_build

    v = builder_version()

    def judge(case: str, build_ts, **repo) -> tuple[tuple[str, str], Path]:
        log = tmp_path / f"{case}.log"
        r = fixture_repo(tmp_path / case, build_ts(log), repo.pop("version", v), **repo)
        verdict = check_build(r)
        return (verdict.kind, verdict.message), log

    def one_page(log: Path) -> str:
        return write_page("dashboard.html", "x") + append_log(log)

    got, log = judge("pass", one_page, committed=b"x")
    assert got == ("pass", "committed page equals a fresh build")
    lines = log_lines(log)
    assert len(lines) == 2, lines
    cwds = [line.rpartition(" ")[0] for line in lines]
    assert cwds[0] != cwds[1], cwds
    for cwd in cwds:
        assert not Path(cwd).exists(), cwd

    differs = ("page_differs", "committed page mu_spec/dashboard.html differs from a fresh build")
    got, _ = judge("differs", one_page, committed=b"y")
    assert got == differs
    got, _ = judge("uncommitted", one_page)
    assert got == differs

    got, _ = judge(
        "two_files",
        lambda log: write_page("dashboard.html", "x") + write_page("extra.txt", "e") + append_log(log),
        committed=b"x",
    )
    assert got == ("not_one_page", "build produced 2 files under out/, expected exactly one page")

    got, _ = judge(
        "failed", lambda log: append_log(log) + 'console.error("boom");\nprocess.exit(3);\n', committed=b"x",
    )
    assert got == ("build_failed", "build failed: boom")

    got, log = judge("version", one_page, committed=b"x", version="0.0.0-pinned")
    assert got == ("builder_version_mismatch", f"page builder version {v} differs from pinned 0.0.0-pinned")
    assert log_lines(log) == []

    got, _ = judge("no_manifest", one_page, committed=b"x", manifest=None)
    assert got == ("manifest_fault", "manifest fault: manifest_absent -")
    got, _ = judge("gone", one_page, committed=b"x", manifest=FIXTURE_MANIFEST + b"src/gone.ts\n")
    assert got == ("manifest_fault", "manifest fault: listed_file_missing src/gone.ts")

    empty = tmp_path / "empty_path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    got, _ = judge("no_builder", one_page, committed=b"x")
    assert got == ("builder_missing", "page builder not installed: bun")


def test_check_build_gap_from_end(tmp_path):
    from pagebuild.check import check_build

    log = tmp_path / "gap.log"
    path = json.dumps(log.as_posix())
    build_ts = (
        f"const text = existsSync({path}) ? readFileSync({path}, \"utf-8\") : \"\";\n"
        "const lines = text.split(\"\\n\").filter((line) => line !== \"\");\n"
        "let page;\n"
        "if (lines.length === 0) {\n"
        "  await new Promise((resolve) => setTimeout(resolve, 3000));\n"
        "  page = \"x\";\n"
        "} else {\n"
        "  const last = lines[lines.length - 1];\n"
        "  const e = parseInt(last.slice(last.lastIndexOf(\" \") + 1), 10);\n"
        "  const d = Date.now() - e;\n"
        "  page = d < 1500 ? \"x\" : \"y\";\n"
        "}\n"
        "mkdirSync(\"out\", { recursive: true });\n"
        "writeFileSync(\"out/dashboard.html\", page);\n"
        + append_log(log)
        + "process.exit(0);\n"
    )
    r = fixture_repo(tmp_path / "gap", build_ts, builder_version(), committed=b"y")

    verdict = check_build(r)
    assert (verdict.kind, verdict.message) == ("not_reproducible", "build not reproducible: two fresh builds differ")
    lines = log_lines(log)
    assert len(lines) == 2, lines
    first, second = (int(line.rpartition(" ")[2]) for line in lines)
    assert second - first >= 2000, lines
